#!/usr/bin/env python3
"""Reject NON-ADDITIVE migration DDL — the precondition for safe auto-revert (design §2.8).

serve re-applies EVERY migrations/*.sql on every boot (no applied-migrations table — see
postgres_store.migrate), so a contraction in ANY shipped migration actually executes. The deploy is
auto-revertible: on a health failure reconcile resets CODE to the last-good SHA. That is only safe if
every migration is ADDITIVE (expand-contract) — the schema only ever GROWS, so old code always runs
against a compatible superset. A destructive change (drop/rename/retype/tighten a column or
constraint) breaks the old code against the new schema and can't be undone by a code-revert.

TWO layers:

1. A hand-written character SCANNER (`_normalize`) neutralizes comments, string/dollar-quoted bodies, and
   double-quoted identifiers by modeling Postgres's lexer exactly (NOT a regex — a regex can't count
   nested block-comment depth, and kept diverging on E'' escapes, Unicode identifiers, and CR line-endings
   across review rounds). So a keyword split across a newline is caught, a delimiter hidden inside a
   string/comment/quoted-ident is never mis-acted-upon, and a quoted name can't smuggle a keyword.

2. An ALLOWLIST (`_is_additive`) is the gate: a statement passes ONLY if it matches a provably-additive
   shape (CREATE a brand-new object; ALTER TABLE whose every clause is ADD COLUMN / INHERIT — plus ATTACH
   PARTITION only when both parent and attached child are new this deploy; ALTER TYPE ADD VALUE|ATTRIBUTE;
   COMMENT; INSERT). EVERYTHING ELSE is rejected fail-closed. The
   earlier DENYLIST ("reject known-bad DDL") was fail-OPEN by default and could not converge — Postgres's
   non-additive surface is large and grows every release, so each review round found one more un-rejected
   contraction. For a gate protecting an autonomous factory a false-NEGATIVE (a hidden contraction) is far
   worse than a false-POSITIVE (a human sign-off), so the default is "not proven additive => reject".

A blessed contraction is a deliberate, human-gated two-release change via RECONCILE_ALLOW_CONTRACTION=1; a
blessed routine/behavioral object (a trigger/util function) has a NARROWER escape RECONCILE_ALLOW_ROUTINE=1
(`--allow-routine`) that only blesses CREATE/DROP of a function/procedure/rule/trigger — a DROP TABLE
alongside is a separate statement and still fails.

This is a conservative gate, not a proof. Its bias is fail-closed — an unrecognized construct is rejected
(a false positive costs a human sign-off), never silently passed. A table counts as "born this migration"
(so ops on it are additive) ONLY if created UNCONDITIONALLY (not `IF NOT EXISTS`, which is a no-op on an
existing table) and only for LATER statements (order-aware) — else a create could launder a DROP onto an
existing table (cross-family r10 CRITICAL). Accepted fail-closed cost: an idempotent migration that uses
`CREATE TABLE IF NOT EXISTS t` then adds a UNIQUE INDEX / CREATE OR REPLACE VIEW to `t` can't be proven
additive from its text alone. PR-1d closes most of this: reconcile passes `--existing` = the live pg_catalog
relation set (which at lint time still reflects prev_good, since the new migrations haven't applied), so an
op on a relation BORN this deploy (a new table's UNIQUE INDEX, a NEW view) is additive without an escape,
while a DROP/tighten on a PRE-EXISTING relation stays rejected. A CREATE OR REPLACE VIEW that RE-DEFINES a
pre-existing view still needs RECONCILE_ALLOW_CONTRACTION (its column set could narrow un-revertibly — the
lint can't prove otherwise). `--existing` never WEAKENS the gate; absent it (DB unreadable), the lint runs
the conservative same-migration fallback.
Other residuals: plain INSERT / a LOCKING SELECT ... FOR UPDATE / UPDATE are treated additive (schema-
neutral).

FUNDAMENTAL CEILING (by design, not an unclosed hole): a static text lint CANNOT evaluate SQL
EXPRESSIONS — it can't tell that a `DEFAULT coalesce(NULL,NULL)` evaluates to null, or that a
pre-existing function called from an allowed INSERT/UPDATE runs DDL. This gate catches the STRUCTURAL
contractions (drop/rename/retype/tighten), the KNOWN dynamic-execution mechanisms (DO/EXECUTE/CALL,
CREATE FUNCTION/RULE/TRIGGER, dblink), and the OBVIOUS accidental forms (NOT NULL DEFAULT NULL, SET
DEFAULT CAST(NULL...)). A semantically-null default expression or a DDL-capable PRE-EXISTING function
invoked from DML is beyond static analysis — out of scope, backstopped by RECONCILE_ALLOW_CONTRACTION and,
for a *malicious* author (who could also just push bad code), by the AUTHOR_ALLOWLIST + code review, which
are the real security boundary. The lint's job is to stop ACCIDENTAL and KNOWN-MECHANISM non-additive
migrations from auto-deploying, fail-closed — not to be a complete additivity proof, which is impossible
from text alone.

reconcile lints only the migrations ADDED-OR-MODIFIED in a deploy (the prev_good..HEAD delta,
--diff-filter=AMR), NOT the full history — historical migrations that already ran may contain
legitimate one-time contractions (0006 uses DROP-in-a-string, 0007/0010 drop old tables/views).

Usage:  migration_lint.py <file.sql|dir> ...
"""
from __future__ import annotations

import re
import string
import sys
from pathlib import Path

_ADD_COL_RE = re.compile(r"\bADD\s+COLUMN\b", re.IGNORECASE)
_NOT_NULL_RE = re.compile(r"\bNOT\s+NULL\b", re.IGNORECASE)
_DEFAULT_RE = re.compile(r"\bDEFAULT\b", re.IGNORECASE)
# A GENERATED column (stored expr, or GENERATED ... AS IDENTITY) supplies its own value on old INSERTs,
# so `ADD COLUMN c ... NOT NULL GENERATED ALWAYS ...` is additive even without a DEFAULT keyword (r5 FP-6).
# Anchor to the actual GENERATED { ALWAYS | BY DEFAULT } clause — the bare word also matches a column
# literally NAMED `generated` (an unreserved keyword), which wrongly exempted a real contraction (r13).
_GENERATED_RE = re.compile(r"\bGENERATED\s+(?:ALWAYS|BY\s+DEFAULT)\b", re.IGNORECASE)
_ALTER_TABLE_RE = re.compile(r"\bALTER\s+TABLE\b", re.IGNORECASE)
# ADD [CONSTRAINT <name>] (CHECK|UNIQUE|PRIMARY KEY|FOREIGN KEY|EXCLUDE) — named OR unnamed (r2 #5d).
_ADD_TIGHTENING_RE = re.compile(
    r"\bADD\s+(?:CONSTRAINT\s+\S+\s+)?(?:CHECK|UNIQUE|PRIMARY\s+KEY|FOREIGN\s+KEY|EXCLUDE)\b", re.IGNORECASE)
# Code the linter cannot inspect is fail-closed. A statement that BEGINS with DO/EXECUTE/CALL runs a body
# `_normalize` strips (r3 #1). But that anchor alone was bypassable: a `CREATE FUNCTION f() AS $$ DROP
# TABLE x $$` (body stripped) invoked by a later `SELECT f()` starts with CREATE/SELECT, not DO/CALL
# (r4 CRIT-2). So ALSO reject CREATE FUNCTION/PROCEDURE outright — its body is opaque and can run DDL
# when invoked — and treat DROP FUNCTION/PROCEDURE as a contraction (old code may depend on it).
_DYNAMIC_DDL_RE = re.compile(r"^(?:DO|EXECUTE|CALL)\b", re.IGNORECASE)
_CREATE_ROUTINE_RE = re.compile(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:FUNCTION|PROCEDURE)\b", re.IGNORECASE)


def _top_level_clauses(stmt: str) -> list[str]:
    """Split on TOP-LEVEL commas only (paren-depth 0) so a comma inside a type/args — numeric(10,2),
    CHECK(a,b) — does not break a clause (cross-family r2 #5c). Strings are already stripped upstream."""
    out: list[str] = []
    depth = 0
    cur: list[str] = []
    for ch in stmt:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def _add_column_not_null_no_default(stmt: str) -> bool:
    # ADD COLUMN ... NOT NULL without a DEFAULT is not additive. PER top-level clause so a DEFAULT on a
    # DIFFERENT added column can't spoof it (r2 #4), paren-aware so numeric(10,2) doesn't mis-split (r2 #5c).
    # A GENERATED column is exempt — Postgres computes its value for old INSERTs, so it's additive (r5 FP-6).
    if not _ADD_COL_RE.search(stmt):
        return False
    return any(_ADD_COL_RE.search(c) and _NOT_NULL_RE.search(c)
               and not _DEFAULT_RE.search(c) and not _GENERATED_RE.search(c)
               for c in _top_level_clauses(stmt))


def _drop_column(stmt: str) -> bool:
    # ALTER TABLE column drop (old code reads a column that's gone). Two cases:
    #   (a) the COLUMN keyword is present -> unambiguously a column drop, WHATEVER the column is named —
    #       incl. a column named `constraint`/`default`/`type`; the old spelling-based exclusions were a
    #       bypass (r2 #5b, r3 #8). `DROP COLUMN [IF EXISTS] <ident>`.
    #   (b) no COLUMN keyword: `DROP [IF EXISTS] <ident>` is still a column drop. Only DROP NOT NULL is
    #       excluded — it is the one genuinely-additive property drop (relaxes nullability). Everything
    #       else is caught: DROP CONSTRAINT / DROP DEFAULT are contractions ruled elsewhere too (a double
    #       flag is harmless), and a column literally NAMED constraint/default/expression/identity is a
    #       real drop that the old per-keyword exclusions silently let through (r3 #8, r4 HIGH-3). The
    #       rare genuinely-additive DROP EXPRESSION/IDENTITY now flags — a deliberate fail-closed tradeoff
    #       (use RECONCILE_ALLOW_CONTRACTION for a blessed generated-column cleanup).
    # Quoted identifiers are already neutralized to `qi` by _normalize, so a digit-leading or space-
    # containing quoted column name can't slip past the anchor (r3 #6/#7).
    if not _ALTER_TABLE_RE.search(stmt):
        return False
    if re.search(r"\bDROP\s+COLUMN\b", stmt, re.IGNORECASE):
        return True
    # `\S` (any non-whitespace token start), NOT `[A-Za-z_]`: an UNQUOTED Unicode column name (`DROP é`)
    # is a valid destructive drop that an ASCII-only anchor missed (cross-family r7 CRIT). Quoted names are
    # already neutralized to `qi` upstream; the NOT NULL lookahead still exempts the additive relaxation.
    return bool(re.search(
        r"\bDROP\s+(?:IF\s+EXISTS\s+)?(?!NOT\s+NULL\b)\S", stmt, re.IGNORECASE))


def _drop_default(stmt: str) -> bool:
    # ALTER TABLE ... ALTER COLUMN ... DROP DEFAULT looks additive but a NOT NULL column with no default
    # breaks old INSERTs that omitted it (relying on the default) after a revert (r3 #5). The linter can't
    # know the column's nullability, so fail closed — a blessed drop uses RECONCILE_ALLOW_CONTRACTION.
    return bool(_ALTER_TABLE_RE.search(stmt) and re.search(r"\bDROP\s+DEFAULT\b", stmt, re.IGNORECASE))


def _add_constraint_tightening(stmt: str) -> bool:
    # ALTER TABLE ... ADD [CONSTRAINT] CHECK/UNIQUE/PK/FK/EXCLUDE tightens a guarantee old code doesn't
    # honor after a revert. NOT VALID is NOT an additive escape (r3 #2): Postgres skips validating
    # EXISTING rows but still enforces the constraint on every new/updated row immediately, so reverted
    # old writes can fail. PER top-level clause (paren-aware) so a later clause can't exempt an earlier one.
    if not _ALTER_TABLE_RE.search(stmt):
        return False
    return any(_ADD_TIGHTENING_RE.search(c) for c in _top_level_clauses(stmt))


# Opaque BEHAVIORAL objects. A RULE rewrites DML — a live-Postgres check proved `CREATE RULE ... DO
# INSTEAD NOTHING` silently turns every DELETE into a no-op, and a code-revert CAN'T drop a pg_rewrite
# rule (cross-family r8 attack-fleet CRITICAL, verified on PG 16). A TRIGGER changes write behavior via
# an opaque function. Treat CREATE/DROP of RULE/TRIGGER like routines: reject, but escapable via the
# narrow RECONCILE_ALLOW_ROUTINE for a blessed additive one (e.g. an updated_at trigger).
_CREATE_BEHAVIORAL_RE = re.compile(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:CONSTRAINT\s+)?(?:RULE|TRIGGER)\b", re.I)
# DROP FUNCTION|PROCEDURE|ROUTINE|RULE|TRIGGER (PG's ROUTINE drops a function/procedure — r5 HIGH-4).
_DROP_ROUTINE_RE = re.compile(r"\bDROP\s+(FUNCTION|PROCEDURE|ROUTINE|RULE|TRIGGER)\b", re.I)
# The routine/behavioral rules the narrow RECONCILE_ALLOW_ROUTINE escape filters (every OTHER check stays).
_ROUTINE_CHECKERS = (_CREATE_ROUTINE_RE, _CREATE_BEHAVIORAL_RE, _DROP_ROUTINE_RE)

# (checker, why). A checker is a compiled regex (matched with .search) or a callable(stmt)->bool.
_RULES = [
    (_DYNAMIC_DDL_RE,                                                "DO/EXECUTE/CALL — dynamic DDL the linter can't inspect (a contraction can hide in the body); fail-closed"),
    (_CREATE_ROUTINE_RE,                                             "CREATE FUNCTION/PROCEDURE — opaque body can run DDL when invoked (a stripped body defeats inspection); fail-closed"),
    (_CREATE_BEHAVIORAL_RE,                                          "CREATE RULE/TRIGGER — rewrites/changes DML behavior opaquely and survives a code-revert; fail-closed"),
    (_DROP_ROUTINE_RE,                                               "DROP FUNCTION/PROCEDURE/ROUTINE/RULE/TRIGGER — old code may depend on it"),
    (re.compile(r"\bTRUNCATE\b", re.I),                              "TRUNCATE — destroys rows a code-revert cannot restore (old code is stranded with an empty table)"),
    (re.compile(r"\bDROP\s+TABLE\b", re.I),                          "DROP TABLE — old code reads a table that's gone"),
    (re.compile(r"\bDROP\s+(MATERIALIZED\s+VIEW|VIEW)\b", re.I),     "DROP VIEW — old code reads a relation that's gone"),
    (re.compile(r"\bDROP\s+(SEQUENCE|SCHEMA|TYPE)\b", re.I),         "DROP SEQUENCE/SCHEMA/TYPE — old code depends on it"),
    (re.compile(r"\bDROP\s+CONSTRAINT\b", re.I),                     "DROP CONSTRAINT — relaxes/changes a guarantee old code assumes"),
    (re.compile(r"\bDROP\s+ATTRIBUTE\b", re.I),                      "ALTER TYPE DROP ATTRIBUTE — old code reads a composite-type field that's gone (r8, live-PG confirmed)"),
    (_drop_column,                                                   "DROP COLUMN (optional COLUMN kw) — old code reads a column that's gone"),
    (_drop_default,                                                  "DROP DEFAULT — a NOT NULL column with no default breaks old INSERTs that omit it after a revert"),
    (re.compile(r"\bDETACH\s+PARTITION\b", re.I),                    "DETACH PARTITION — removes rows from the parent table old code queries"),
    (re.compile(r"\bNO\s+INHERIT\b", re.I),                          "NO INHERIT — removes a child's rows from the parent old code queries"),
    (re.compile(r"\bALTER\s+SEQUENCE\b.*\bRESTART\b", re.I),         "ALTER SEQUENCE RESTART — reissued IDs can collide (PK violation) with rows old code inserted (kilabz HIGH; oracle deems acceptable — kept per gate)"),
    (re.compile(r"\bCREATE\s+UNIQUE\s+INDEX\b", re.I),               "CREATE UNIQUE INDEX — tightens uniqueness; reverted old code can insert a duplicate and violate it"),
    (re.compile(r"\bSET\s+DEFAULT\s+\(?\s*NULL\b", re.I),            "SET DEFAULT NULL — removes a column's effective default (like DROP DEFAULT); breaks old INSERTs on a NOT NULL column after a revert"),
    (re.compile(r"\bSET\s+GENERATED\s+ALWAYS\b", re.I),             "SET GENERATED ALWAYS — rejects the explicit-value INSERTs old code issues for the column"),
    (re.compile(r"\bRENAME\s+VALUE\b", re.I),                        "ALTER TYPE RENAME VALUE — an enum label old code still writes/compares is gone (r8/fleet, live-PG confirmed)"),
    (re.compile(r"\bRENAME\s+(?:COLUMN\s+|CONSTRAINT\s+|ATTRIBUTE\s+)?\S+\s+TO\b", re.I), "RENAME COLUMN/CONSTRAINT/ATTRIBUTE — old code reads the old name"),
    (re.compile(r"\bRENAME\s+TO\b", re.I),                           "RENAME TABLE — old code reads the old name"),
    (re.compile(r"\bALTER\s+(?:COLUMN\s+|ATTRIBUTE\s+)?\S+\s+(SET\s+DATA\s+)?TYPE\b", re.I), "column/attribute TYPE change — old code mis-reads the type"),
    (re.compile(r"\bSET\s+NOT\s+NULL\b", re.I),                      "SET NOT NULL — breaks old writes that omit the column"),
    (_add_constraint_tightening,                                    "ADD CONSTRAINT (not NOT VALID) — tightens a guarantee old code doesn't honor"),
    (_add_column_not_null_no_default,                               "ADD COLUMN NOT NULL without DEFAULT — breaks old INSERTs / fails on a populated table"),
]


# A hand-written character scanner, NOT a regex (cross-family r4-r6). A regex cannot model Postgres's
# scanner: block comments NEST (not a regular language — a regex can't count depth, r6 CRIT), E'' strings
# take backslash escapes while plain '' strings don't (r6 CRIT), identifiers include Unicode chars (r6
# CRIT), and `--` ends at CR or LF (r6 HIGH). Three review rounds of "one more regex edge" is the tell.
# This scanner consumes each lexical token exactly as Postgres does, so a delimiter INSIDE an open token
# is never re-interpreted and the linter's token boundaries match the engine that actually runs the SQL.
#
# Fail-closed direction: for WELL-FORMED SQL the scanner matches Postgres exactly. The only divergence is
# on malformed input (an unclosed token) — which Postgres also rejects, so nothing executes — and the
# safe direction on any doubt is to UNDER-consume (expose text to the rules → at worst a false positive),
# never OVER-consume (which could swallow a real DROP). A tag/prefix the scanner doesn't recognize is left
# as ordinary text, so its contents stay visible to the rules.
# $tag$ open; tag = a PG dollar-quote tag or empty. PG's scan.l dolq tag is [A-Za-z\200-\377_][...0-9]*
# — the start is a letter/underscore OR ANY high-bit byte; the rest adds digits. Python `\w`/`[^\W\d]`
# alone narrows the high-bit range (a single-char high-bit tag like `$§$` or an NFD `$cafe◌́$` was
# rejected → a false positive on an additive migration, cross-family r7/r8). Add `[^\x00-\x7f]` (any
# non-ASCII char) to both classes to mirror PG. Under-consume direction only: a mis-tokenized dollar
# exposes its body to the rules (a false positive), it never hides a DROP.
_DOLLAR_OPEN = re.compile(r"\$((?:[^\W\d]|[^\x00-\x7f])(?:\w|[^\x00-\x7f])*|)\$")


def _ident_char(c: str) -> bool:
    # Postgres ident_cont is [A-Za-z\200-\377_0-9$] (scan.l) — ASCII alnum/_/$ OR ANY non-ASCII byte
    # (every high-bit char: Unicode letters AND combining marks). str.isalnum() is NARROWER — it is False
    # for a combining mark like U+0301 — which let a fake dollar-quote open mid-identifier and hide a DROP
    # (cross-family r7 CRIT). Mirror PG's byte rule directly: any non-ASCII char is an identifier char.
    return (not c.isascii()) or c.isalnum() or c == "_" or c == "$"


def _scan_quoted(sql: str, j: int, n: int, escape: bool) -> int:
    # From the OPENING quote at j, return the index just past the CLOSING quote. `''` always escapes a
    # quote; a backslash escapes the next char only in an E'' string (escape=True).
    k = j + 1
    while k < n:
        ch = sql[k]
        if escape and ch == "\\":
            k += 2
        elif ch == "'":
            if k + 1 < n and sql[k + 1] == "'":
                k += 2
            else:
                return k + 1
        else:
            k += 1
    return n   # unterminated (Postgres would reject too)


def _decode_uescape(body: str) -> str:
    # Decode a Postgres U&"..." identifier body's default (backslash) escapes: \\ -> \, \XXXX -> char,
    # \+XXXXXX -> char. Used only to see a name obfuscated as U&"\0064blink" (= dblink) for the dblink
    # guard (r13). Best-effort: an invalid escape is dropped (it wouldn't be a valid identifier anyway).
    def repl(m: "re.Match[str]") -> str:
        if m.group(1):
            return "\\"
        try:
            return chr(int(m.group(2) or m.group(3), 16))
        except ValueError:
            return ""
    return re.sub(r"\\(?:(\\)|\+([0-9A-Fa-f]{6})|([0-9A-Fa-f]{4}))", repl, body)


def _normalize(sql: str, keep_quoted: bool = False) -> str:
    """Neutralize comments, string/dollar-quoted bodies, and double-quoted identifiers by scanning the SQL
    exactly as Postgres's lexer would, then collapse whitespace. Comments -> ' ', strings/dollar bodies ->
    "''", double-quoted identifiers -> 'qi' (a safe bareword: a "..." token is always a NAME, never a
    keyword, so it can't smuggle a keyword or hide a space/digit past a rule).

    keep_quoted=True instead emits a quoted identifier's DE-QUOTED CONTENT (comments/strings still stripped)
    — used only to detect a dangerous NAME (dblink) that quoting would otherwise hide behind `qi` (r12)."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        two = sql[i:i + 2]
        if two == "--":                                   # line comment -> end at CR or LF (r6 HIGH)
            j = i + 2
            while j < n and sql[j] not in "\r\n":
                j += 1
            out.append(" "); i = j
        elif two == "/*":                                 # block comment -> NESTED depth count (r6 CRIT)
            depth, j = 1, i + 2
            while j < n and depth > 0:
                pair = sql[j:j + 2]
                if pair == "/*":
                    depth += 1; j += 2
                elif pair == "*/":
                    depth -= 1; j += 2
                else:
                    j += 1
            out.append(" "); i = j
        elif c == "$" and not (i > 0 and _ident_char(sql[i - 1])):   # $tag$ dollar body (not mid-ident)
            m = _DOLLAR_OPEN.match(sql, i)
            close = ("$" + m.group(1) + "$") if m else ""
            k = sql.find(close, m.end()) if m else -1
            if m and k != -1:
                out.append(" '' "); i = k + len(close)
            else:
                out.append(c); i += 1                     # not a real dollar-quote -> literal $, keep scanning
        elif c in "Ee" and i + 1 < n and sql[i + 1] == "'" and not (i > 0 and _ident_char(sql[i - 1])):
            out.append(" '' "); i = _scan_quoted(sql, i + 1, n, escape=True)    # E'' string (\ escapes)
        elif c == "'":
            out.append(" '' "); i = _scan_quoted(sql, i, n, escape=False)       # plain '' string
        elif c in "Uu" and sql[i + 1:i + 3] == '&"' and not (i > 0 and _ident_char(sql[i - 1])):
            # U&"..." unicode-escape identifier (Postgres): the 3rd identifier syntax. Body uses "" escapes;
            # for keep_quoted DECODE its \XXXX escapes so an obfuscated name (U&"\0064blink" = dblink) is
            # seen by the dblink guard (r13). Normal path -> qi like any identifier.
            j = i + 3
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        j += 2
                    else:
                        j += 1; break
                else:
                    j += 1
            if keep_quoted:
                out.append(" " + _decode_uescape(sql[i + 3:j - 1].replace('""', '"')) + " ")
            else:
                out.append(" qi ")
            i = j
        elif c == '"':                                    # "..." identifier ("" escapes) -> safe bareword
            j = i + 1
            while j < n:
                if sql[j] == '"':
                    if j + 1 < n and sql[j + 1] == '"':
                        j += 2
                    else:
                        j += 1; break
                else:
                    j += 1
            if keep_quoted:
                out.append(" " + sql[i + 1:j - 1].replace('""', '"') + " ")  # de-quoted content (dblink scan)
            else:
                out.append(" qi ")
            i = j
        else:
            out.append(c); i += 1
    # Collapse whitespace, THEN collapse whitespace AROUND DOTS so a schema-qualified name whose quoted
    # part became ` qi ` reconnects: `public. qi ` -> `public.qi` (cross-family r14). Without this the name
    # extractors capture only the bare `public.` prefix — laundering a DROP onto an existing table and
    # false-rejecting a legit ADD COLUMN. A `qi` component still marks the whole name unresolvable.
    return re.sub(r"\s*\.\s*", ".", re.sub(r"\s+", " ", "".join(out)))


# =====================================================================================================
# ALLOWLIST — the actual gate (design §2.8; cross-family review r9). A DENYLIST ("reject known-bad DDL")
# is fail-OPEN by default and cannot converge: Postgres's non-additive DDL surface is large and grows
# every release, so each review round found "one more" un-rejected contraction (SET SCHEMA, RLS, DROP
# EXTENSION/DOMAIN/POLICY/OWNED, REPLICA IDENTITY, ADD GENERATED ALWAYS, inline column constraints, …).
# For a gate protecting an autonomous factory a false-NEGATIVE (a hidden contraction slips through) is far
# worse than a false-POSITIVE (an extra human sign-off), so INVERT it: a statement passes ONLY if it
# matches a provably-ADDITIVE shape; everything else is rejected fail-closed. New/obscure non-additive DDL
# now needs NO new rule — it simply isn't additive. The _RULES denylist below is kept ONLY to annotate a
# rejection with a specific reason (else a generic one).

# CREATE of a brand-NEW standalone object is additive: old code never referenced it. Excludes `OR REPLACE`
# (mutates an existing object — a VIEW replace can drop a column), `UNIQUE INDEX` (tightens an existing
# table), and every opaque/behavioral CREATE (FUNCTION/PROCEDURE/RULE/TRIGGER/POLICY) by listing only the
# safe new-object kinds. `CREATE TABLE ... PARTITION OF` / `... AS SELECT` are still new tables (additive).
_ADDITIVE_CREATE_RE = re.compile(
    r"^CREATE\s+(?:GLOBAL\s+|LOCAL\s+|TEMP(?:ORARY)?\s+|UNLOGGED\s+)*"
    r"(?:TABLE|SEQUENCE|SCHEMA|VIEW|MATERIALIZED\s+VIEW|EXTENSION|DOMAIN|TYPE|COLLATION|STATISTICS|INDEX)\b",
    re.IGNORECASE)
# A DEFAULT that evaluates to NULL doesn't satisfy a NOT NULL column for an old omitted-column INSERT — the
# obvious/accidental forms `DEFAULT NULL`, `DEFAULT (NULL)`, `DEFAULT CAST(NULL AS t)` (cross-family r11).
# A semantically-null expression (`DEFAULT coalesce(null,null)`) is beyond a static lint — see the ceiling
# note in the module docstring.
_NULL_DEFAULT_RE = re.compile(r"\bDEFAULT\s*(?:\(\s*|CAST\s*\(\s*)*NULL\b", re.IGNORECASE)
# dblink is Postgres's built-in mechanism to run DDL from a DML/SELECT expression (dblink_exec('…',
# 'ALTER TABLE … DROP COLUMN …')). Reject ANY reference to it outright — it defeats the allowlist by
# smuggling a contraction into an otherwise-additive INSERT/UPDATE/RETURNING/locked-SELECT (r11).
_DBLINK_RE = re.compile(r"\bdblink", re.IGNORECASE)


def _additive_alter_table_clause(c: str) -> bool:
    # An ALTER TABLE is additive only if EVERY top-level clause is: ADD COLUMN (nullable-or-defaulted, no
    # inline tightening constraint), INHERIT, or SET DEFAULT <non-null>. Anything else (DROP/ALTER/RENAME/SET
    # NOT NULL/ENABLE/DISABLE/OWNER/DETACH/NO INHERIT/VALIDATE/…) makes the statement non-additive. ATTACH
    # PARTITION is NOT here — it re-routes/bounds a possibly-PRE-EXISTING relation and is gated separately at
    # the statement level in _is_additive (both parent and child must be new this deploy — r6).
    c = c.strip()
    if re.match(r"^ADD\s+COLUMN\b", c, re.IGNORECASE):
        # inline PRIMARY KEY/UNIQUE/CHECK/REFERENCES on the new column tightens writes old code still does
        # (a PK/UNIQUE the old omitted-column INSERT violates; a CHECK it can't satisfy) — cross-family r9 HIGH-2.
        if re.search(r"\b(PRIMARY\s+KEY|UNIQUE|CHECK|REFERENCES)\b", c, re.IGNORECASE):
            return False
        # NOT NULL needs a NON-NULL default (else an old omitted-column INSERT fails). Reject both no-default
        # and a DEFAULT that is (obviously) NULL (r11). A GENERATED column supplies its own value (r5 FP-6).
        if _NOT_NULL_RE.search(c) and not _GENERATED_RE.search(c) \
           and (not _DEFAULT_RE.search(c) or _NULL_DEFAULT_RE.search(c)):
            return False
        return True
    if re.match(r"^INHERIT\b", c, re.IGNORECASE):   # NOT "NO INHERIT" (that clause starts with NO)
        # Old-style inheritance does NOT route the parent's inserts to the child and does NOT impose a bound
        # on the child (PG requires the child to already satisfy the parent's constraints), so — unlike ATTACH
        # PARTITION — the INHERIT clause itself never tightens either relation. A later recursing op on the
        # parent is still caught by the file/deploy `linked` flag (r4).
        return True
    # SET DEFAULT <non-null value> is additive: old INSERTs that omit the column now get the value instead
    # of erroring; a code-revert keeps the default (harmless). SET DEFAULT NULL / CAST(NULL) is the
    # contraction (a NOT NULL column loses its effective default) and is NOT allowed here.
    if re.search(r"\bSET\s+DEFAULT\b", c, re.IGNORECASE) and not _NULL_DEFAULT_RE.search(c):
        return True
    return False


# A table is provably born THIS migration ONLY if created UNCONDITIONALLY — an `IF NOT EXISTS` create is a
# runtime NO-OP against an existing table, so it can't prove newness (cross-family r10 CRITICAL: it let a
# DROP COLUMN / UNIQUE INDEX launder onto an EXISTING table). The negative lookahead rejects IF NOT EXISTS.
# `qi` (a quoted name) is also excluded: neutralization collapses every quoted name to `qi`, so a `qi`
# match can't identify a specific table. The set is built INCREMENTALLY in lint_file (a CREATE only blesses
# LATER statements), so a create can't retroactively bless an EARLIER op (r10 order-reversal shape).
_CREATE_TABLE_NAME_RE = re.compile(
    r"^CREATE\s+(?:GLOBAL\s+|LOCAL\s+|TEMP(?:ORARY)?\s+|UNLOGGED\s+)*TABLE\s+(?!IF\s+NOT\s+EXISTS\b)([^\s(*]+)",
    re.IGNORECASE)


def _target_after_on(s: str) -> str | None:
    m = re.search(r"\bON\s+(?:ONLY\s+)?([^\s(*]+)", s, re.IGNORECASE)
    return m.group(1) if m else None


def _resolvable_table(name: str) -> bool:
    # A table name is a reliable "born this migration" key ONLY if every dot-component is a plain
    # identifier — NOT `qi` (a quoted, neutralized component, so the real name is unknowable) and not a
    # bare `qi`. `public.orders` is resolvable; `public.qi` (was public."orders") and `qi` are not (r14).
    return name != "qi" and "qi" not in name.split(".")


_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


def _pg_fold(ident: str) -> str:
    # Fold an UNQUOTED identifier the way Postgres stores it, so a migration's bare name compares equal to
    # the pg_catalog relname reconcile passed in. Postgres downcases ASCII A-Z ONLY (NOT Python's Unicode
    # str.lower(), which diverges on e.g. İ -> i̇, changing byte length), then truncates to NAMEDATALEN-1
    # (63) BYTES. Applied IDENTICALLY to both sides (prev_objects load + the target name) — cross-family PR-1d.
    return ident.translate(_ASCII_LOWER).encode("utf-8")[:63].decode("utf-8", "ignore")


def _new_this_deploy(name: str, created: frozenset[str], prev_objects: "frozenset[str] | None") -> bool:
    # Is <name> a relation BORN in this deploy (so any op on it — a UNIQUE INDEX, a CREATE OR REPLACE VIEW,
    # even a DROP — is additive, since old code has never referenced it)? PR-1d:
    #   - AUTHORITATIVE (prev_objects given = reconcile read the LIVE pg_catalog, which at lint time reflects
    #     prev_good's schema since the new migrations haven't applied): new IFF the bare name is NOT already
    #     a relation in the DB. This closes the idempotent-migration false positive (a CREATE TABLE IF NOT
    #     EXISTS of a genuinely-new table + its UNIQUE INDEX / OR REPLACE VIEW) WITHOUT reopening the r10
    #     launder (a DROP on a pre-existing table: its bare name IS in prev_objects -> not new -> rejected).
    #   - FALLBACK (prev_objects is None: DB unreadable / first converge): the conservative same-migration
    #     rule — new only if UNCONDITIONALLY created in a preceding statement THIS file (r10).
    # An unresolvable (quoted) name is never trusted as new (fail-closed).
    if not _resolvable_table(name):
        return False
    if prev_objects is not None:
        # Compare the bare last component, folded EXACTLY as Postgres stores it (ASCII downcase + 63-byte
        # truncate — _pg_fold) so a 64-byte spelling of a pre-existing 63-byte relation, or a non-ASCII
        # name, still matches the pg_catalog set (cross-family PR-1d r1/r2).
        return _pg_fold(name.split(".")[-1]) not in prev_objects
    return name in created


def _is_additive(stmt: str, allow_routine: bool = False, created: frozenset[str] = frozenset(),
                 prev_objects: "frozenset[str] | None" = None, linked: bool = False) -> bool:
    """True iff `stmt` (already _normalize'd) is a provably-additive migration statement — the allowlist.
    linked: the DEPLOY contains an INHERIT / ATTACH PARTITION / PARTITION OF (any delta file), so a "new"
    table may share rows/routing with a PRE-EXISTING relation — a recursing ALTER TABLE or a UNIQUE INDEX on
    it could then hit that pre-existing relation, so the new-this-deploy shortcuts are disabled and everything
    clause-checks (cross-family PR-1d r4/r5)."""
    s = stmt.strip()
    if not s:
        return True
    # dblink can run DDL from a DML/SELECT expression — a hard reject anywhere, even under --allow-routine
    # (cross-family r11: it smuggled `dblink_exec('…ALTER TABLE…DROP COLUMN…')` through an INSERT RETURNING
    # and a locked SELECT).
    if _DBLINK_RE.search(s):
        return False
    # Operator-blessed routine/behavioral object (RECONCILE_ALLOW_ROUTINE): a CREATE/DROP of a
    # function/procedure/rule/trigger the operator vouched for. Still NARROW — a DROP TABLE alongside is a
    # separate statement and is judged on its own (not additive), so the file still fails.
    if allow_routine and (_CREATE_ROUTINE_RE.search(s) or _CREATE_BEHAVIORAL_RE.search(s)
                          or _DROP_ROUTINE_RE.search(s)):
        return True
    if re.match(r"^CREATE\b", s, re.IGNORECASE):
        if re.match(r"^CREATE\s+OR\s+REPLACE\b", s, re.IGNORECASE):
            # A CREATE OR REPLACE VIEW that changes an EXISTING view's column set is NOT safely revertible —
            # Postgres forbids narrowing/retyping a view via OR REPLACE, so re-applying prev_good's older
            # definition ERRORS and serve bricks (cross-family r10, live-PG confirmed). Additive ONLY when
            # the view is NEW this deploy (PR-1d: not already a relation in prev_good). CREATE OR REPLACE
            # FUNCTION etc. stays rejected (opaque). Absent prev_objects -> reject (a view is never in the
            # table-only `created` fallback set).
            vm = re.match(r"^CREATE\s+OR\s+REPLACE\s+(?:MATERIALIZED\s+)?VIEW\s+(?:IF\s+NOT\s+EXISTS\s+)?([^\s(*]+)",
                          s, re.IGNORECASE)
            return bool(vm) and _new_this_deploy(vm.group(1), created, prev_objects)
        if re.search(r"^CREATE\s+(?:\w+\s+)*?UNIQUE\s+INDEX\b", s, re.IGNORECASE):
            # additive ONLY on a table born this deploy AND when the migration created no descendant/partition
            # link (a unique index on a partitioned parent propagates to its partitions — a pre-existing one
            # would be tightened; conservative file-level guard, r4).
            tgt = _target_after_on(s)
            return tgt is not None and not linked and _new_this_deploy(tgt, created, prev_objects)
        # A CREATE TABLE ... PARTITION OF <parent> is additive ONLY when <parent> is born this deploy. Adding
        # ANY partition to a PRE-EXISTING partitioned parent tightens a pre-existing relation — even a BARE
        # partition with no extra constraints (cross-family PR-1d r5+r7):
        #   (a) a partition-local CHECK / NOT NULL / UNIQUE rejects rows the parent routes to it (r5); AND
        #   (b) even bare, the new partition claims values that a pre-existing DEFAULT partition used to hold,
        #       so an old DIRECT insert into that default partition for the newly-claimed values now fails —
        #       the same child-side tightening class r6 closed for ATTACH, via CREATE TABLE this time (r7).
        # The linter can't see whether the pre-existing parent has a DEFAULT partition, so fail-closed: reject
        # any PARTITION OF a not-provably-new parent. A partition of a table BORN this deploy passes (no old
        # code touches it). (INHERITS is NOT here — old-style inheritance neither routes the parent's inserts
        # nor bounds the child.)
        pm = re.search(r"\bPARTITION\s+OF\s+([^\s(*]+)", s, re.IGNORECASE)
        if pm and not _new_this_deploy(pm.group(1), created, prev_objects):
            return False
        return bool(_ADDITIVE_CREATE_RE.match(s))          # the safe brand-new object kinds
    if re.match(r"^ALTER\s+TABLE\b", s, re.IGNORECASE):
        # `([^\s(*]+)\*?` — exclude the inheritance wildcard from the NAME and consume it separately:
        # `ALTER TABLE job* DROP COLUMN` targets the pre-existing `job` (+ its children), so capturing `job*`
        # would read as new-this-deploy and skip the clause checks (cross-family PR-1d r3).
        m = re.match(r"^ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(?:ONLY\s+)?([^\s(*]+)\s*\*?\s+", s, re.IGNORECASE)
        if not m:
            return False
        rest = s[m.end():]
        # ATTACH PARTITION tightens on BOTH sides unless BOTH relations are born this deploy (cross-family
        # PR-1d r6): a PRE-EXISTING PARENT re-routes its inserts into the attached partition, whose own
        # CHECK / NOT NULL / UNIQUE can reject rows the parent used to accept (kilabz r6); and a PRE-EXISTING
        # CHILD gains the implicit partition-bound CHECK, so its own direct inserts that used to succeed now
        # fail. Additive ONLY when the parent AND the attached child are both new this deploy (no old code
        # touches either). Handled at statement level — ATTACH is never comma-combined with another action —
        # so it never reaches the clause loop (which no longer blesses ATTACH). Fail-closed: an unresolvable
        # (quoted) name is not provably new, so it rejects.
        am = re.match(r"^ATTACH\s+PARTITION\s+([^\s(*]+)", rest, re.IGNORECASE)
        if am:
            return (_new_this_deploy(m.group(1), created, prev_objects)
                    and _new_this_deploy(am.group(1), created, prev_objects))
        # Any op on a table born this deploy is additive — old code has never referenced it. BUT an ALTER
        # TABLE without ONLY RECURSES to descendants, so if the migration linked a PRE-EXISTING descendant
        # (INHERIT / ATTACH, `linked`), an op on a "new" parent could hit that pre-existing table (r4). When
        # linked, DON'T short-circuit — clause-check so ADD COLUMN (additive even recursed) passes while
        # DROP/RENAME/ADD CONSTRAINT/tighten reject.
        if not linked and _new_this_deploy(m.group(1), created, prev_objects):
            return True
        return all(_additive_alter_table_clause(c) for c in _top_level_clauses(rest))
    if re.match(r"^ALTER\s+TYPE\b", s, re.IGNORECASE):     # enum ADD VALUE / composite ADD ATTRIBUTE only
        m = re.match(r"^ALTER\s+TYPE\s+(?:IF\s+EXISTS\s+)?\S+\s+", s, re.IGNORECASE)
        if not m:
            return False
        # PER top-level clause, exactly like ALTER TABLE: a composite ALTER TYPE takes comma-separated
        # actions, so `ADD ATTRIBUTE x, DROP ATTRIBUTE y` must NOT pass on its first clause alone (r12 HIGH).
        return all(re.match(r"^ADD\s+(?:VALUE|ATTRIBUTE)\b", c.strip(), re.IGNORECASE)
                   for c in _top_level_clauses(s[m.end():]))
    if re.match(r"^COMMENT\s+ON\b", s, re.IGNORECASE):     # metadata only
        return True
    # Schema-NEUTRAL data DML is out of scope for a SCHEMA-additivity gate (the schema is unchanged, so old
    # code still runs). Real idempotent backfills use INSERT ... ON CONFLICT, UPDATE, and SELECT ... FOR
    # UPDATE. TRUNCATE and DELETE (bulk row destruction that strands old code) are NOT allowed.
    if re.match(r"^(?:INSERT\s+INTO|UPDATE)\b", s, re.IGNORECASE):
        # a RETURNING clause is an executable-expression injection point (RETURNING <func-that-runs-DDL>);
        # migrations don't need it, so reject fail-closed (cross-family r11).
        return not re.search(r"\bRETURNING\b", s, re.IGNORECASE)
    if re.match(r"^SELECT\b", s, re.IGNORECASE):
        # Only a LOCKING select (FOR UPDATE/SHARE) is an accepted migration use — the lock-ordering guard
        # real idempotent backfills need. Additionally the select-list (before FROM) must be plain column
        # refs / * : a `(` signals a function call that could run DDL even under a row lock (cross-family
        # r11: `SELECT dblink_exec(...) FROM t FOR UPDATE`). dblink itself is already hard-rejected above.
        if not re.search(r"\bFOR\s+(?:UPDATE|SHARE|NO\s+KEY\s+UPDATE|KEY\s+SHARE)\b", s, re.IGNORECASE):
            return False
        return "(" not in re.split(r"\bFROM\b", s, maxsplit=1, flags=re.IGNORECASE)[0]
    return False                                           # fail-closed: unrecognized => not provably additive


def _reject_reason(stmt: str, allow_routine: bool = False) -> str:
    # A rejected statement gets a SPECIFIC message if the denylist recognizes the contraction, else generic.
    for checker, why in _RULES:
        if allow_routine and checker in _ROUTINE_CHECKERS:
            continue
        hit = checker.search(stmt) if isinstance(checker, re.Pattern) else checker(stmt)
        if hit:
            return why
    return ("not a provably-additive statement (allowlist gate, fail-closed): only CREATE-of-a-new-object, "
            "CREATE OR REPLACE VIEW, ALTER TABLE ADD COLUMN / INHERIT (ATTACH PARTITION only when both "
            "parent and child are new this deploy), ALTER TYPE ADD VALUE|ATTRIBUTE, COMMENT, and "
            "INSERT/UPDATE/SELECT data-DML auto-pass; else needs "
            "RECONCILE_ALLOW_CONTRACTION (or --allow-routine for a blessed function/trigger)")


# A descendant/partition LINK ties a "new" table to a PRE-EXISTING relation, so an op on the "new" side can
# recurse/route to the pre-existing side and the new-this-deploy shortcuts become unsafe:
#   ATTACH PARTITION / INHERIT (ALTER) — the new PARENT gains a pre-existing partition/child; a recursing
#     ALTER TABLE or a UNIQUE INDEX on the "new" parent then hits the pre-existing relation (r4).
#   PARTITION OF (CREATE) — the new partition catches the pre-existing parent's routed inserts; a later
#     ALTER/UNIQUE INDEX on that partition tightens what the parent accepts (r5 BLOCKER 1 Path B).
# NOT `\bINHERITS\b`: `CREATE TABLE c INHERITS (parent)` makes the NEW table a child; PostgreSQL inheritance
# recurses DOWNWARD only, so the child's constraints never affect the pre-existing parent's writes (r5, both
# reviewers agree). `\bINHERIT\b`'s word boundary already excludes the trailing-S `INHERITS`.
_LINK_RE = re.compile(r"\bATTACH\s+PARTITION\b|\bINHERIT\b|\bPARTITION\s+OF\b", re.IGNORECASE)


def lint_file(path: Path, allow_routine: bool = False,
              prev_objects: "frozenset[str] | None" = None, deploy_linked: bool = False) -> list[str]:
    # ALLOWLIST gate: reject any statement that is not provably additive (fail-closed). allow_routine
    # (operator-gated via RECONCILE_ALLOW_ROUTINE) additionally blesses a routine/behavioral CREATE/DROP —
    # narrow: a DROP TABLE alongside is its own statement and still fails (r5 FP-7). prev_objects (PR-1d,
    # reconcile's --existing = the live pg_catalog relation set = prev_good's schema) lets an op on a
    # relation BORN this deploy pass without the escape; None keeps the conservative same-file behavior.
    raw = path.read_text()
    # FILE-LEVEL dblink guard on the DE-IDENTIFIED text (quoted-ident content preserved, comments/strings
    # stripped): the per-statement check runs on _normalize output where `"dblink_exec"` became `qi`, so a
    # quoted/schema-qualified dblink would slip past it (cross-family r12 HIGH). dblink has no legitimate use
    # in an additive migration — any reference disqualifies the whole file (a comment/string mention doesn't
    # count; it was stripped). Deeper obfuscation via a pre-existing wrapper function is ceiling-class.
    if _DBLINK_RE.search(_normalize(raw, keep_quoted=True)):
        return [f"{path.name}: NON-ADDITIVE — references dblink (runs DDL from a DML/SELECT expression); "
                "not additive — set RECONCILE_ALLOW_CONTRACTION=1 for a blessed one"]
    norm = _normalize(raw)
    # Does the migration create a descendant/partition LINK (INHERIT / ATTACH PARTITION / PARTITION OF)? If so
    # a "new" table shares rows/routing with a PRE-EXISTING relation, so a recursing ALTER TABLE or a UNIQUE
    # INDEX on it can't be blessed as new-only (r4/r5). `deploy_linked` carries this DEPLOY-wide: the linkage
    # and a later tightening can live in DIFFERENT files, and prev_objects is deploy-global, so a per-file
    # `linked` alone would miss the cross-file split (r5 BLOCKER 2). Conservative — any such link disables the
    # new-this-deploy shortcuts for the whole deploy.
    linked = deploy_linked or bool(_LINK_RE.search(norm))
    out: list[str] = []
    created: set[str] = set()   # tables UNCONDITIONALLY created in a PRECEDING statement (order-aware, r10)
    for stmt in (s.strip() for s in norm.split(";")):
        if not stmt:
            continue
        if not _is_additive(stmt, allow_routine, frozenset(created), prev_objects, linked):
            out.append(f"{path.name}: NON-ADDITIVE — {_reject_reason(stmt, allow_routine)}\n    {stmt[:140]}")
        # Register an unconditional CREATE TABLE AFTER checking this statement, so it can only bless LATER
        # ops on the genuinely-new table — never retroactively an earlier op (r10 order-reversal).
        m = _CREATE_TABLE_NAME_RE.match(stmt)
        if m and _resolvable_table(m.group(1)):
            created.add(m.group(1))
    return out


def main(argv: list[str]) -> int:
    args = argv[1:]
    allow_routine = "--allow-routine" in args
    args = [a for a in args if a != "--allow-routine"]
    # --existing <file>: newline-separated relation names that EXIST before this deploy (reconcile passes the
    # live pg_catalog set). PR-1d: an op on a relation NOT in this set is on an object born this deploy ->
    # additive. Names are compared bare + case-folded (unquoted PG identifiers fold to lowercase).
    prev_objects: "frozenset[str] | None" = None
    if "--existing" in args:
        idx = args.index("--existing")
        if idx + 1 >= len(args):
            sys.stderr.write("migration_lint: --existing needs a file argument\n")
            return 2
        expath = args[idx + 1]
        del args[idx:idx + 2]
        try:
            prev_objects = frozenset(_pg_fold(n) for n in Path(expath).read_text().split() if n.strip())
        except OSError as e:
            sys.stderr.write(f"migration_lint: cannot read --existing {expath}: {e}\n")
            return 2
    if not args:
        sys.stderr.write("usage: migration_lint.py [--allow-routine] [--existing <file>] <file.sql|dir> ...\n")
        return 2
    files: list[Path] = []
    for a in args:
        p = Path(a)
        if p.is_dir():
            files.extend(sorted(p.glob("*.sql")))
        elif p.suffix == ".sql":
            files.append(p)
    # Cross-file linkage pre-pass (PR-1d r5 BLOCKER 2): the `linked` guard must be DEPLOY-global. A migration
    # can create+link a parent in one file and tighten it in another; prev_objects is deploy-global but the
    # linkage keyword lives only in the first file, so a per-file `linked` would read the second file as clean
    # and let a recursing DROP / UNIQUE INDEX through. If ANY delta file links, every file clause-checks.
    # (No current runtime migration partitions/inherits, so this only bites future ones — fail-closed,
    # escapable via RECONCILE_ALLOW_CONTRACTION.)
    deploy_linked = False
    for sql in files:
        try:
            if _LINK_RE.search(_normalize(sql.read_text())):
                deploy_linked = True
                break
        except OSError:
            pass   # the lint loop below reports the read error and exits 2
    violations: list[str] = []
    for sql in files:
        try:
            violations.extend(lint_file(sql, allow_routine=allow_routine,
                                        prev_objects=prev_objects, deploy_linked=deploy_linked))
        except OSError as e:
            sys.stderr.write(f"migration_lint: cannot read {sql}: {e}\n")
            return 2
    if violations:
        sys.stderr.write("migration_lint: NON-ADDITIVE migration DDL rejected (design §2.8):\n")
        for v in violations:
            sys.stderr.write("  " + v + "\n")
        sys.stderr.write("A contraction (drop/rename/retype/tighten) must be a deliberate, human-gated\n"
                         "two-release change — set RECONCILE_ALLOW_CONTRACTION=1 to override a blessed one.\n")
        return 1
    print(f"migration_lint: {len(files)} migration(s) additive-only — OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
