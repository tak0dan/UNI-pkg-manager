"""Run native commands, with the safety rules applied around them.

- Arch: refuse any pacman command that refreshes (-y) without upgrading (-u)
  while installing packages. A partial upgrade breaks Arch systems.
- snapper: when enabled and installed, take a pre/post snapshot around the
  whole transaction. If the pre-snapshot fails, the transaction does not run.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .backends import Step


class UnsafeCommand(Exception):
    pass


def check_safe(step: Step) -> None:
    argv = step.argv
    if Path(argv[0]).name != "pacman":
        return
    short = "".join(a[1:] for a in argv[1:] if a.startswith("-") and not a.startswith("--"))
    positional = [a for a in argv[1:] if not a.startswith("-")]
    if "S" in short and "y" in short and "u" not in short and positional:
        raise UnsafeCommand(
            "refusing 'pacman -Sy <package>': refreshing without a full upgrade causes "
            "partial upgrades. Use -Syu, or -S for installs."
        )


def _as_root(argv: list[str], root: bool, sudo: str) -> list[str]:
    return ([sudo] if root and os.geteuid() != 0 else []) + argv


def _snapper_args(kind: str, desc: str, pre: str | None = None) -> list[str]:
    args = ["snapper", "create", "--type", kind, "--description", desc]
    if kind == "pre":
        return args + ["--print-number"]
    return args + ["--pre-number", pre or "<pre>"]


def execute(steps: list[Step], cfg: dict, dry_run: bool, label: str, echo=print) -> int:
    """Run (or with dry_run, print) the steps. Returns the first failing exit code."""
    for step in steps:
        check_safe(step)

    sudo = cfg["general"]["sudo"]
    use_snapper = cfg["safety"]["snapper"] and shutil.which("snapper") is not None
    desc = f"uni: {label}"

    if dry_run:
        if use_snapper:
            echo("$ " + " ".join(_as_root(_snapper_args("pre", desc), True, sudo)))
        for step in steps:
            echo("$ " + " ".join(_as_root(step.argv, step.root, sudo)))
        if use_snapper:
            echo("$ " + " ".join(_as_root(_snapper_args("post", desc, "<pre>"), True, sudo)))
        return 0

    pre_num: str | None = None
    if use_snapper:
        proc = subprocess.run(_as_root(_snapper_args("pre", desc), True, sudo),
                              capture_output=True, text=True, check=False)
        if proc.returncode != 0 or not proc.stdout.strip().isdigit():
            echo(f"error: snapper pre-snapshot failed; not running the transaction: {proc.stderr.strip()}")
            return 1
        pre_num = proc.stdout.strip()

    rc = 0
    for step in steps:
        rc = subprocess.run(_as_root(step.argv, step.root, sudo), check=False).returncode
        if rc:
            break

    if pre_num is not None:
        subprocess.run(_as_root(_snapper_args("post", desc, pre_num), True, sudo), check=False)
    return rc
