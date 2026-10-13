"""Output and logging: plain tables, JSON, colour search results, and the activity log.

Log file: $XDG_STATE_HOME/uni/uni.log, one JSON object per line. Every native
query, every resolution decision and every transaction is recorded there, so
`uni log` can show what happened and why.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Sequence
from datetime import datetime

from .config import state_dir

LOG_FILE = "uni.log"


def table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    cols = list(zip(*([headers] + [list(r) for r in rows]))) if rows else [[h] for h in headers]
    widths = [max(len(str(v)) for v in col) for col in cols]
    fmt = "  ".join("{:<" + str(w) + "}" for w in widths)
    lines = [fmt.format(*headers).rstrip(), fmt.format(*["-" * w for w in widths]).rstrip()]
    lines += [fmt.format(*[str(v) for v in r]).rstrip() for r in rows]
    return "\n".join(lines)


def emit_json(obj) -> None:
    json.dump(obj, sys.stdout, indent=2)
    sys.stdout.write("\n")


def warn(msg: str) -> None:
    print(f"uni: {msg}", file=sys.stderr)


SOURCE_STYLE = {
    "pacman": "bold cyan", "apt": "bold green", "dnf": "bold blue", "zypper": "bold green",
    "flatpak": "bold magenta", "snap": "bold yellow", "aur": "bold red",
}


def _clip(text: str, width: int = 70) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def search_table(rows: list[tuple[str, str, str, str, str, bool]]) -> None:
    """rows: (priority, source, name, version, description, enabled). Rich when on a terminal."""
    if not sys.stdout.isatty():
        print(table(["Prio", "Source", "Name", "Version", "Description"],
                    [[p, s, n, v, _clip(d)] for p, s, n, v, d, _ in rows]))
        return
    from rich.console import Console
    from rich.table import Table

    t = Table(show_lines=False, header_style="bold", expand=True)
    for col in ("Prio", "Source", "Name", "Version", "Description"):
        t.add_column(col, overflow="fold")
    for prio, source, name, version, desc, enabled in rows:
        style = SOURCE_STYLE.get(source, "bold")
        label = f"[{style}]{source}[/]" + ("" if enabled else " [dim](off)[/]")
        t.add_row(prio, label, f"[bold]{name}[/]", version, desc if enabled else f"[dim]{desc}[/]")
    Console().print(t)


def record(event: dict) -> None:
    """Append one JSON line to the activity log. Logging never breaks a command."""
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        with open(state_dir() / LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), **event}) + "\n")
    except OSError:
        pass


def make_trace(verbose: bool):
    """Hook for backends.TRACE: records every native query, prints it when verbose."""

    def trace(argv: list[str], rc: int, seconds: float) -> None:
        record({"type": "query", "argv": argv, "rc": rc, "ms": round(seconds * 1000)})
        if verbose:
            warn(f"$ {' '.join(argv)}  -> {rc} ({seconds * 1000:.0f} ms)")

    return trace


def transactions_dir():
    return state_dir() / "transactions"


def write_transaction(op: str, label: str, commands: list[str], output: list[str], rc: int) -> str:
    """Write one transaction's full record and return its path (empty string if it could not be written)."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = transactions_dir() / f"{stamp}-{op}.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = [f"uni {op}: {label}", f"exit status: {rc}", "", "commands:"]
        body += [f"  $ {c}" for c in commands]
        body += ["", "output:"] + output
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
        return str(path)
    except OSError:
        return ""


def read_log(count: int) -> list[dict]:
    path = state_dir() / LOG_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries = []
    for line in lines[-count:]:
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


def format_entry(e: dict) -> str:
    stamp = datetime.fromtimestamp(e.get("ts", 0)).strftime("%Y-%m-%d %H:%M:%S")
    kind = e.get("type", "event")
    if kind == "query":
        body = f"query  rc={e.get('rc')} {e.get('ms')}ms  {' '.join(e.get('argv', []))}"
    elif kind == "resolve":
        body = f"resolve {e.get('term')} -> {', '.join(e.get('chosen') or []) or 'nothing'}"
        if e.get("tie"):
            body += "  (tie)"
        if e.get("error"):
            body += f"  error: {e['error']}"
    elif kind == "transaction":
        body = f"{e.get('op')} {' '.join(e.get('resolved', []))} rc={e.get('rc')}"
        if e.get("dry_run"):
            body += " (dry run)"
        if e.get("log"):
            body += f"\n{' ' * 20}full output: {e['log']}"
    else:
        body = json.dumps({k: v for k, v in e.items() if k != "ts"})
    return f"{stamp}  {body}"
