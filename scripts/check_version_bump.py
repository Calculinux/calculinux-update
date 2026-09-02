#!/usr/bin/env python3
"""Fail if packaged source changed without bumping pyproject.toml version."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"


def project_version(toml: str) -> str:
    for line in toml.splitlines():
        if line.startswith("version = "):
            return line.split("=", 1)[1].strip().strip("\"'")
    raise SystemExit("pyproject.toml has no 'version =' field")


def src_changed(paths: list[str]) -> bool:
    return any(p == "src" or p.startswith("src/") for p in paths)


def version_tuple(ver: str) -> tuple[int, ...]:
    parts = []
    for piece in ver.split("."):
        digits = ""
        for ch in piece:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits or 0))
    return tuple(parts)


def check(base_sha: str) -> None:
    names = subprocess.check_output(
        ["git", "diff", "--name-only", base_sha, "HEAD", "--", "src/"],
        text=True,
        cwd=ROOT,
    ).splitlines()
    if not src_changed(names):
        print("No src/ changes; version bump not required")
        return
    old = project_version(
        subprocess.check_output(
            ["git", "show", f"{base_sha}:pyproject.toml"],
            text=True,
            cwd=ROOT,
        )
    )
    new = project_version(PYPROJECT.read_text())
    print(f"src/ changed ({len(names)} files); version {old} -> {new}")
    if version_tuple(new) <= version_tuple(old):
        print(
            f"error: src/ files changed but version did not increase ({old} -> {new})",
            file=sys.stderr,
        )
        sys.exit(1)


def self_check() -> None:
    assert project_version('name = "x"\nversion = "0.6.0"\n') == "0.6.0"
    assert src_changed(["src/calculinux_update/cli.py"])
    assert not src_changed(["tests/test_cli.py", ".github/workflows/ci.yaml"])
    assert version_tuple("0.6.1") > version_tuple("0.6.0")
    assert version_tuple("0.6.0") <= version_tuple("0.6.0")
    print("ok")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", help="git SHA of the PR base")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        self_check()
        return
    if not args.base:
        parser.error("provide --base or --self-check")
    check(args.base)


if __name__ == "__main__":
    main()
