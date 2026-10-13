"""Plain-text and JSON output, plus the local transaction log."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Sequence

from .config import state_dir


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


def record(event: dict) -> None:
    """Append one JSON line to the transaction log. Logging never breaks a command."""
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        with open(state_dir() / "uni.log", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.time(), **event}) + "\n")
    except OSError:
        pass
