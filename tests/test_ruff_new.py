"""tools/ruff_new.py — the ruff NEW-findings ratchet must never read "could not check" as a pass.

Each case is a real run of the tool against a throwaway git repo (global/system git config
isolated), except the two failure-injection cases, which patch one call in-process. The first
four cases each PASSED (rc 0, a false "no new findings") before the #182 review fixes:
untracked file hidden by status.showUntrackedFiles=no, RUFF_OUTPUT_FILE diverting the JSON,
a swallowed `git status` failure, and an empty ruff stdout read as zero findings.

Run: PYTHONPATH=src python3 tests/test_ruff_new.py   (skips if ruff is not installed)
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

TOOL = Path(__file__).resolve().parent.parent / "tools" / "ruff_new.py"
BIN_PATH = os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("PATH", "")])


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("RUFF_")}
    env.update({"PATH": BIN_PATH, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
    env.update(extra)
    return env


def _repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('[tool.ruff.lint]\nselect = ["F401"]\n')
    (repo / "clean.py").write_text("x = 1\n")
    for args in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"]):
        subprocess.run(["git", *args], cwd=repo, env=_env(), check=True, capture_output=True)
    return repo


def _tool(repo: Path, **env_extra: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(TOOL), "HEAD"], cwd=repo, env=_env(**env_extra),
                          capture_output=True, text=True, timeout=120, check=False)  # rc IS the result


def _load_module() -> Any:
    # Any, not ModuleType: the failure-injection cases reassign module functions (git/run)
    spec = importlib.util.spec_from_file_location("ruff_new", TOOL)
    assert spec is not None and spec.loader is not None, f"cannot load {TOOL}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _expect_die(fn) -> None:
    try:
        fn()
    except SystemExit as exc:
        assert exc.code == 2, f"expected die() rc 2, got {exc.code}"
        return
    raise AssertionError("expected die() (SystemExit 2), the call returned normally")


def main() -> int:
    if shutil.which("ruff", path=BIN_PATH) is None:
        print("SKIP test_ruff_new: ruff not installed (pip install -e '.[dev]')")
        return 0
    with tempfile.TemporaryDirectory() as td:
        repo = _repo(Path(td))

        (repo / "new_clean.py").write_text("y = 2\n")
        r = _tool(repo)
        assert r.returncode == 0, f"control: a clean untracked file must pass, got {r.returncode}: {r.stderr}"
        print("PASS control: clean untracked file -> rc 0")
        (repo / "new_clean.py").unlink()

        (repo / "new_bad.py").write_text("import os\n")
        r = _tool(repo)
        assert r.returncode == 1 and "F401" in r.stdout, f"untracked new finding: rc {r.returncode}"
        print("PASS untracked file with a new finding -> rc 1")

        subprocess.run(["git", "config", "status.showUntrackedFiles", "no"], cwd=repo, env=_env(), check=True)
        r = _tool(repo)
        assert r.returncode == 1, f"showUntrackedFiles=no hid the new file (rc {r.returncode})"
        print("PASS status.showUntrackedFiles=no does not hide untracked files")
        subprocess.run(["git", "config", "--unset", "status.showUntrackedFiles"], cwd=repo, env=_env(), check=True)

        r = _tool(repo, RUFF_OUTPUT_FILE=str(Path(td) / "diverted.json"))
        assert r.returncode == 1, f"RUFF_OUTPUT_FILE produced a false pass (rc {r.returncode})"
        print("PASS RUFF_OUTPUT_FILE in the env cannot divert the findings")
        (repo / "new_bad.py").unlink()

        (repo / "-dash.py").write_text("import os\n")
        r = _tool(repo)
        assert r.returncode == 1, f"a '-'-named file must be linted as a path (rc {r.returncode}): {r.stderr}"
        print("PASS a file named like an option is linted as a path")
        (repo / "-dash.py").unlink()

        mod = _load_module()
        real_git = mod.git
        mod.git = lambda root, *args: (subprocess.CompletedProcess(args, 128, "", "fatal: injected")
                                       if args and args[0] == "status" else real_git(root, *args))
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, env=_env(),
                              capture_output=True, text=True, check=True).stdout.strip()
        _expect_die(lambda: mod.changed_py_files(repo, head))
        print("PASS a failed git status dies (rc 2), never an empty file list")
        mod.git = real_git

        mod.run = lambda cmd, cwd, env=None: subprocess.CompletedProcess(cmd, 0, "", "")
        _expect_die(lambda: mod.ruff_json("ruff", None, repo, ["clean.py"]))
        print("PASS an empty ruff stdout dies (rc 2), never zero findings")
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
