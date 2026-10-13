"""Run native commands, with the safety rules applied around them.

- Arch: refuse any pacman command that refreshes (-y) without upgrading (-u)
  while installing packages. A partial upgrade breaks Arch systems.
- snapper: when enabled and installed, take a pre/post snapshot around the
  whole transaction. If the pre-snapshot fails, the transaction does not run.

Two runners share these rules: `execute` for plain terminals (native output and
prompts go straight to the terminal) and `arun` for the TUI (output streams into
the log view; helpers that must prompt get the terminal handed over).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from collections.abc import Callable
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


def snapper_enabled(cfg: dict) -> bool:
    return bool(cfg["safety"]["snapper"]) and shutil.which("snapper") is not None


def _as_root(argv: list[str], root: bool, sudo: str) -> list[str]:
    return ([sudo] if root and os.geteuid() != 0 else []) + argv


def snapshot_argv(kind: str, desc: str, pre: str | None = None) -> list[str]:
    args = ["snapper", "create", "--type", kind, "--description", desc]
    if kind == "pre":
        return args + ["--print-number"]
    return args + ["--pre-number", pre or "<pre>"]


def execute(steps: list[Step], cfg: dict, dry_run: bool, label: str, echo=print) -> int:
    """Plain runner: run (or with dry_run, print) the steps. Returns the first failing exit code."""
    for step in steps:
        check_safe(step)

    sudo = cfg["general"]["sudo"]
    use_snapper = snapper_enabled(cfg)
    desc = f"uni: {label}"

    if dry_run:
        if use_snapper:
            echo("$ " + " ".join(_as_root(snapshot_argv("pre", desc), True, sudo)))
        for step in steps:
            echo("$ " + " ".join(_as_root(step.argv, step.root, sudo)))
        if use_snapper:
            echo("$ " + " ".join(_as_root(snapshot_argv("post", desc, "<pre>"), True, sudo)))
        return 0

    pre_num: str | None = None
    if use_snapper:
        proc = subprocess.run(_as_root(snapshot_argv("pre", desc), True, sudo),
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
        subprocess.run(_as_root(snapshot_argv("post", desc, pre_num), True, sudo), check=False)
    return rc


async def _capture(argv: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace")


async def _stream(argv: list[str], emit: Callable[[str], None]) -> int:
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None
    async for raw in proc.stdout:
        for piece in raw.decode(errors="replace").replace("\r", "\n").split("\n"):
            if piece.strip():
                emit(piece.rstrip())
    return await proc.wait()


async def arun(steps: list[Step], cfg: dict, label: str, emit: Callable[[str], None],
               suspend: Callable[[list[str]], int]) -> int:
    """TUI runner. `suspend(argv)` runs a command with the terminal handed over and returns its code."""
    for step in steps:
        check_safe(step)
    sudo = cfg["general"]["sudo"]
    desc = f"uni: {label}"

    # The snapshot runs under sudo too, so prime whenever either needs root.
    needs_root = (any(s.root for s in steps) or snapper_enabled(cfg)) and os.geteuid() != 0
    if needs_root:
        # sudo reads its password from the terminal, so cache credentials before the log view takes over.
        emit(f"$ {sudo} -v   (authenticate once for this transaction)")
        if suspend([sudo, "-v"]) != 0:
            emit("error: sudo authentication failed; nothing was changed")
            return 1

    pre_num: str | None = None
    if snapper_enabled(cfg):
        rc, out = await _capture(_as_root(snapshot_argv("pre", desc), True, sudo))
        if rc or not out.strip().isdigit():
            emit("error: snapper pre-snapshot failed; not running the transaction")
            return 1
        pre_num = out.strip()
        emit(f"snapshot {pre_num} taken before the transaction")

    rc = 0
    for step in steps:
        argv = _as_root(step.argv, step.root, sudo)
        emit("$ " + " ".join(argv) + ("   (interactive)" if step.interactive else ""))
        rc = suspend(argv) if step.interactive else await _stream(argv, emit)
        if rc:
            emit(f"command exited with status {rc}")
            break

    if pre_num is not None:
        post_rc, _ = await _capture(_as_root(snapshot_argv("post", desc, pre_num), True, sudo))
        if not post_rc:
            emit(f"snapshot {pre_num} closed")
    return rc
