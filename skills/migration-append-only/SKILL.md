---
name: migration-append-only
description: A shipped migration's schema changes via ALTER, not CREATE
path_trigger: src/runtime/ledger/migrations/*.sql
---

migrate() re-runs every migrations/*.sql on each boot via `CREATE TABLE IF NOT EXISTS`, with no
version table — so editing a SHIPPED migration's CREATE is INVISIBLE to any DB that already ran it
(the change silently never applies, and code that assumes the new shape then errors at runtime).

To change an existing table's schema, add an APPEND-ONLY follow-up migration with a GUARDED,
idempotent ALTER: only alter when the OLD shape is detected (e.g. a DO-block that checks
pg_constraint/information_schema first), because a bare DROP/ADD would churn or fail on every boot.

Flag any in-place edit to a shipped migration's CREATE that isn't paired with such a heal, and any
ALTER that isn't idempotent under migrate()'s re-run-every-boot model.

(Learned 2026-06: the skill-table PK was changed in-place from `name` to `(repo_scope, name)`; a
cross-family review caught that existing DBs would keep the old PK and fail-open — fixed by an
append-only 0006 with a guarded ALTER.)
