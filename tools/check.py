"""The one command that answers "is this change ready?".

    python tools/check.py            everything CI checks
    python tools/check.py test       the test suite
    python tools/check.py lint       ruff
    python tools/check.py types      mypy
    python tools/check.py fixtures   no raw recordings in the repository

CI calls this script rather than repeating the commands, so a green run here
and a green run there mean the same thing. Standard library only: it has to work
before anything is installed, and it says so plainly when a tool is missing.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: (passed, one-line detail, full output to show on failure)
Outcome = tuple[bool, str, str]


def _run(*args: str) -> tuple[int, str]:
    done = subprocess.run(
        args,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return done.returncode, done.stdout


def _module(name: str, *args: str) -> tuple[int, str]:
    """Run a tool from the interpreter running this script, so a venv is honoured."""
    code, output = _run(sys.executable, "-m", name, *args)
    if f"No module named {name}" in output:
        output += '\nInstall the development tools first: python -m pip install -e ".[dev]"\n'
    return code, output


def lint() -> Outcome:
    code, output = _module("ruff", "check", "src", "tests", "tools")
    return code == 0, "", output


def types() -> Outcome:
    code, output = _module("mypy")
    return code == 0, "", output


def test() -> Outcome:
    code, output = _module("pytest")
    summary = re.search(r"\d+ passed[^\n]*", output)
    return code == 0, summary.group(0) if summary else "", output


def fixtures() -> Outcome:
    """Refuse a raw recording anywhere in the repository.

    Raw ``.ytx`` files are private by definition and may contain a previous
    owner's credentials. The schema already refuses to load one as a fixture;
    this stops one being committed at all, because a secret in git history
    cannot be taken back.
    """
    code, output = _run("git", "ls-files", "*.ytx")
    if code != 0:
        return False, "", output
    raw = [line for line in output.splitlines() if line.strip()]
    if raw:
        listing = "\n".join(raw)
        return (
            False,
            "",
            f"{listing}\nraw .ytx transcripts must be scrubbed before committing\n",
        )
    return True, "no raw transcripts committed", ""


CHECKS: dict[str, tuple[str, Callable[[], Outcome]]] = {
    "lint": ("ruff", lint),
    "types": ("mypy", types),
    "test": ("pytest", test),
    "fixtures": ("fixture safety", fixtures),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Run the checks CI runs. With no argument, all of them ("check").'
    )
    parser.add_argument(
        "what",
        nargs="*",
        metavar="{" + ",".join([*CHECKS, "check"]) + "}",
        help="which checks to run (default: check, meaning all)",
    )
    selected: list[str] = parser.parse_args(argv).what
    unknown = [name for name in selected if name not in (*CHECKS, "check")]
    if unknown:
        parser.error(f"unknown check: {', '.join(unknown)}")
    names = list(CHECKS) if not selected or "check" in selected else selected

    failed = False
    for name in names:
        label, check = CHECKS[name]
        passed, detail, output = check()
        suffix = f" ({detail})" if detail else ""
        # ASCII marks: this runs on a legacy Windows console too.
        print(f"{'ok  ' if passed else 'FAIL'} {label}{suffix}", flush=True)
        if not passed:
            failed = True
            print(output.rstrip(), flush=True)

    if not failed and len(names) == len(CHECKS):
        print("\nReady: this is what CI checks.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
