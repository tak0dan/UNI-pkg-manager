"""Syntax frontends: parse apt/dnf/pacman/zypper/flatpak/uni argv into an Intent.

Anything a frontend cannot map exactly (unknown flags, manager-only commands
such as `flatpak remote-add` or `pacman -Qdt`) becomes a Passthrough. uni never
guesses at a flag's meaning.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass

from .model import Intent, Passthrough

ParseResult = Intent | Passthrough

# Every source that can be named with --<source> as shorthand for --from <source>.
SOURCE_FLAGS = ("native", "pacman", "apt", "dnf", "zypper", "flatpak", "snap", "aur")


@dataclass
class ParseCtx:
    known_sources: frozenset[str]  # valid values for --from
    remotes: Callable[[], set[str]]  # flatpak remote names, fetched lazily


@dataclass(frozen=True)
class Frontend:
    name: str
    backend: str | None  # concrete source this syntax implies; None for uni's own
    parse: Callable[[list[str], ParseCtx], ParseResult]


def _is_uni_flag_value(args: list[str], i: int, ctx: ParseCtx) -> bool:
    return i + 1 < len(args) and args[i + 1] in ctx.known_sources


def _scan(args, ctx, yes_flags, dry_flags, ignored=()):
    """Split args into positionals and uni-level options.

    Returns (positionals, options, unknown_flags). `--from` only counts as uni's
    option when its value is a known source, so `flatpak install --from x.flatpakref`
    still passes through.
    """
    pos: list[str] = []
    opts = {"source": None, "yes": False, "dry_run": False, "json": False, "accept": False}
    unknown: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--from" and _is_uni_flag_value(args, i, ctx):
            opts["source"] = args[i + 1]
            i += 2
            continue
        if a.startswith("--from=") and a.partition("=")[2] in ctx.known_sources:
            opts["source"] = a.partition("=")[2]
        elif a.startswith("--") and a[2:] in SOURCE_FLAGS and a[2:] in ctx.known_sources:
            opts["source"] = a[2:]  # --flatpak is shorthand for --from flatpak
        elif a in yes_flags:
            opts["yes"] = True
        elif a in dry_flags or a == "--dry-run":
            opts["dry_run"] = True
        elif a == "--json":
            opts["json"] = True
        elif a == "--accept-replacement":
            opts["accept"] = True
        elif a in ignored:
            pass
        elif a.startswith("-") and a != "-":
            unknown.append(a)
        else:
            pos.append(a)
        i += 1
    return pos, opts, unknown


def _generic(syntax, ops, yes_flags, dry_flags, read_only, ignored=()):
    """Frontend factory for apt/dnf/zypper/flatpak: `[flags] <op> [flags] <terms>`."""

    def parse(args: list[str], ctx: ParseCtx) -> ParseResult:
        pos, opts, unknown = _scan(args, ctx, yes_flags, dry_flags, ignored)
        passthrough = Passthrough(backend=syntax, argv=[syntax, *args], syntax=syntax,
                                  read_only=bool(pos) and pos[0] in read_only)
        if unknown or not pos or pos[0] not in ops:
            return passthrough
        op = ops[pos[0]]
        terms = pos[1:]
        if syntax == "flatpak" and op == "install" and len(terms) > 1 and terms[0] in ctx.remotes():
            terms = terms[1:]  # `flatpak install flathub app`: the remote is implied by uni
        if not terms:
            return passthrough
        return Intent(op=op, terms=terms, syntax=syntax, source=opts["source"],
                      accept_replacement=opts["accept"], yes=opts["yes"],
                      dry_run=opts["dry_run"], json_out=opts["json"])

    return parse


def _parse_pacman(args: list[str], ctx: ParseCtx) -> ParseResult:
    """pacman's ops are single letters with modifiers: -S, -Ss, -R, -Syu, -Qdt ..."""
    passthrough = Passthrough(backend="pacman", argv=["pacman", *args], syntax="pacman")
    if not args or not args[0].startswith("-") or args[0].startswith("--") or len(args[0]) < 2:
        return passthrough
    letter, mods = args[0][1], set(args[0][2:])
    rest, opts, unknown = _scan(args[1:], ctx, {"--noconfirm"}, set(), ignored={"--needed"})
    if unknown:
        return passthrough
    read_only = letter in "QTVH" or (letter == "S" and mods and mods <= {"s", "i"})
    passthrough.read_only = read_only
    if mods - {"s"}:
        return passthrough  # -Sy, -Syu, -Si, -Sc, -Rs, ... are manager-specific
    if letter == "S" and "s" in mods and rest:
        op = "search"
    elif letter == "S" and not mods and rest:
        op = "install"
    elif letter == "R" and not mods and rest:
        op = "remove"
    else:
        return passthrough
    return Intent(op=op, terms=rest, syntax="pacman", source=opts["source"],
                  accept_replacement=opts["accept"], yes=opts["yes"] or "--noconfirm" in args,
                  dry_run=opts["dry_run"], json_out=opts["json"])


_READ_ONLY = {
    "apt": {"search", "show", "list", "policy", "madison"},
    "dnf": {"search", "list", "info", "repoquery", "provides"},
    "zypper": {"se", "search", "info", "if", "packages", "pa", "repos", "lr", "list-updates", "lu"},
    "flatpak": {"search", "list", "info", "remotes", "remote-ls"},
}

_UNI_OPS = ("install", "remove", "search", "detect")


def _uni_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--from", dest="source", metavar="SOURCE",
                        help="search only this source, ignoring priorities (no fallback)")
    for name in SOURCE_FLAGS:
        common.add_argument(f"--{name}", dest="source", action="store_const", const=name,
                            help=f"same as --from {name}")
    common.add_argument("-y", "--yes", action="store_true",
                        help="never prompt; ties and replacements fail instead of asking")
    common.add_argument("--accept-replacement", action="store_true",
                        help="allow a fork/replacement package (e.g. swww -> awww)")
    common.add_argument("--dry-run", action="store_true", help="print the native commands only")
    common.add_argument("--json", dest="json_out", action="store_true",
                        help="JSON output (search, detect, --dry-run)")

    parser = argparse.ArgumentParser(prog="uni", description="Unified package manager wrapper.")
    parser.add_argument("--as", dest="as_syntax", metavar="SYNTAX", help="read commands in this syntax")
    parser.add_argument("--config", metavar="PATH", help="config file (default ~/.config/uni/config.toml)")
    parser.add_argument("-v", "--verbose", action="store_true", help="print every native query to stderr")
    sub = parser.add_subparsers(dest="op", required=True)
    for op in ("install", "remove", "search"):
        sp = sub.add_parser(op, parents=[common])
        sp.add_argument("terms", nargs="+", metavar="PACKAGE")
    sub.add_parser("detect", parents=[common], help="show the detected host and sources")
    log = sub.add_parser("log", help="show recent uni activity")
    log.add_argument("-n", type=int, default=30, help="number of entries (default 30)")
    return parser


def _parse_uni(args: list[str], ctx: ParseCtx) -> ParseResult:
    ns = _uni_parser().parse_args(args)
    # `log` and `detect` have fewer options; getattr keeps the Intent defaults for those.
    return Intent(op=ns.op, terms=getattr(ns, "terms", []), syntax="uni",
                  source=getattr(ns, "source", None),
                  accept_replacement=getattr(ns, "accept_replacement", False),
                  yes=getattr(ns, "yes", False), dry_run=getattr(ns, "dry_run", False),
                  json_out=getattr(ns, "json_out", False), count=getattr(ns, "n", 30))


FRONTENDS: dict[str, Frontend] = {
    "uni": Frontend("uni", None, _parse_uni),
    "apt": Frontend("apt", "apt", _generic(
        "apt", {"install": "install", "remove": "remove", "purge": "remove", "search": "search"},
        yes_flags={"-y", "--yes", "--assume-yes"}, dry_flags={"-s", "--simulate", "--just-print"},
        read_only=_READ_ONLY["apt"], ignored={"-q", "--quiet"})),
    "dnf": Frontend("dnf", "dnf", _generic(
        "dnf", {"install": "install", "remove": "remove", "erase": "remove", "search": "search"},
        yes_flags={"-y", "--assumeyes"}, dry_flags=set(), read_only=_READ_ONLY["dnf"],
        ignored={"-q", "--quiet"})),
    "pacman": Frontend("pacman", "pacman", _parse_pacman),
    "zypper": Frontend("zypper", "zypper", _generic(
        "zypper", {"install": "install", "in": "install", "remove": "remove", "rm": "remove",
                   "search": "search", "se": "search"},
        yes_flags={"-n", "--non-interactive", "--no-confirm"}, dry_flags=set(),
        read_only=_READ_ONLY["zypper"], ignored={"-q", "--quiet"})),
    "flatpak": Frontend("flatpak", "flatpak", _generic(
        "flatpak", {"install": "install", "uninstall": "remove", "search": "search"},
        yes_flags={"-y", "--assumeyes"}, dry_flags=set(), read_only=_READ_ONLY["flatpak"])),
}


def parse(syntax: str, args: list[str], ctx: ParseCtx) -> ParseResult:
    return FRONTENDS[syntax].parse(args, ctx)
