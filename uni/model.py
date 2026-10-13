"""Data types shared by syntax frontends, the resolver and the backends."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Intent:
    """One unified operation, produced by a syntax frontend."""

    op: str  # install | remove | search | detect | log
    terms: list[str] = field(default_factory=list)
    syntax: str = "uni"
    # Source to search exclusively: set by --from, or by the syntax in route mode.
    source: str | None = None
    # Native flags with a meaning of their own (canonical names, see capabilities.py).
    flags: frozenset[str] = frozenset()
    # --needed: skip a name when one of its candidates is already installed.
    needed: bool = False
    accept_replacement: bool = False
    yes: bool = False
    dry_run: bool = False
    json_out: bool = False
    count: int = 30  # for `log`


@dataclass
class Passthrough:
    """A manager-specific command that goes to its native manager unchanged."""

    backend: str
    argv: list[str]
    syntax: str
    read_only: bool = False
    dry_run: bool = False


@dataclass
class Invalid:
    """The command line cannot be understood; nothing should run."""

    message: str


@dataclass(frozen=True)
class Candidate:
    """A package that a backend says exists, under the name that backend understands."""

    source: str
    ref: str
    version: str = ""
    description: str = ""
    remote: str | None = None  # flatpak remote the ref lives in

    def describe(self) -> str:
        return f"{self.source}: {self.ref}" + (f" {self.version}" if self.version else "")
