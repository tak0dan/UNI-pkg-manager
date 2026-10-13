"""Syntax frontends: parse apt/dnf/pacman/zypper/flatpak/uni argv into an Intent.

Anything a frontend cannot map exactly (unknown flags, manager-only commands
such as `flatpak remote-add` or `pacman -Qdt`) becomes a Passthrough. uni never
guesses at a flag's meaning.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass

from .capabilities import PACMAN_REMOVE_LETTERS
from .model import Intent, Invalid, Passthrough

ParseResult = Intent | Passthrough | Invalid

# Every source that can be named with --<source> as shorthand for --from <source>.
SOURCE_FLAGS = ("native", "pacman", "apt", "dnf", "zypper", "flatpak", "snap", "aur", "paru", "yay", "nix",
                "winget", "choco", "scoop")


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


def _scan(args, ctx, yes_flags, dry_flags, ignored=(), native_flags=None):
    """Split args into positionals and uni-level options.

    Returns (positionals, options, unknown_flags). `--from` only counts as uni's
    option when its value is a known source, so `flatpak install --from x.flatpakref`
    still passes through.
    """
    pos: list[str] = []
    native_flags = native_flags or {}
    opts = {"source": None, "yes": False, "dry_run": False, "json": False, "accept": False, "error": None,
            "flags": set()}
    unknown: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--from" and not _is_uni_flag_value(args, i, ctx):
            unknown.append(a)  # flatpak's own --from FILE: the native manager keeps it
            i += 1
            continue
        if a in ("--from", "--with"):
            if _is_uni_flag_value(args, i, ctx):
                opts["source"] = args[i + 1]
                i += 2
                continue
            known = ", ".join(sorted(ctx.known_sources))
            if i + 1 < len(args):
                opts["error"] = f"unknown source '{args[i + 1]}'; one of {known}"
                i += 2
            else:
                opts["error"] = f"{a} needs a source; one of {known}"
                i += 1
            continue
        if a.startswith(("--from=", "--with=")):
            value = a.partition("=")[2]
            if value in ctx.known_sources:
                opts["source"] = value
            else:
                opts["error"] = f"unknown source '{value}'; one of {', '.join(sorted(ctx.known_sources))}"
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
        elif a in native_flags:
            opts["flags"].add(native_flags[a])
        elif a in ignored:
            pass
        elif a.startswith("-") and a != "-":
            unknown.append(a)
        else:
            pos.append(a)
        i += 1
    return pos, opts, unknown


def _generic(syntax, ops, yes_flags, dry_flags, read_only, ignored=(), native_flags=None):
    """Frontend factory for apt/dnf/zypper/flatpak: `[flags] <op> [flags] <terms>`.

    `ops` maps a native verb to (unified op, flags the verb implies); `apt purge` is
    `remove` with the `purge` flag, so it keeps its meaning.
    """

    def parse(args: list[str], ctx: ParseCtx) -> ParseResult:
        pos, opts, unknown = _scan(args, ctx, yes_flags, dry_flags, ignored, native_flags)
        if opts["error"]:
            return Invalid(opts["error"])
        passthrough = _passthrough(syntax, args, read_only=bool(pos) and pos[0] in read_only)
        if unknown or not pos or pos[0] not in ops:
            return passthrough
        op, verb_flags = ops[pos[0]]
        terms = pos[1:]
        if syntax == "flatpak" and op == "install" and len(terms) > 1 and terms[0] in ctx.remotes():
            terms = terms[1:]  # `flatpak install flathub app`: the remote is implied by uni
        if not terms:
            return passthrough
        return Intent(op=op, terms=terms, syntax=syntax, source=opts["source"],
                      accept_replacement=opts["accept"], yes=opts["yes"],
                      dry_run=opts["dry_run"], json_out=opts["json"],
                      flags=frozenset(verb_flags | (opts["flags"] - {"needed"})),
                      needed="needed" in opts["flags"])

    return parse


# uni-only options that a native command cannot understand. Forwarding them would fail in
# the native manager, or worse, be silently taken as something else.
UNI_ONLY = {"--with", "--accept-replacement", "--json"} | {
    f"--{name}" for name in SOURCE_FLAGS
}


def _passthrough(syntax: str, args: list[str], read_only: bool = False) -> Passthrough | Invalid:
    """Hand the command to the native manager. --dry-run is honoured here, not forwarded."""
    clash = [a for a in args if a.partition("=")[0] in UNI_ONLY]
    if clash:
        return Invalid(
            f"{clash[0]} is a uni option and cannot be used with this native command; "
            "drop it, or use the source's own syntax"
        )
    dry = "--dry-run" in args
    kept = [a for a in args if a != "--dry-run"]
    return Passthrough(backend=syntax, argv=[syntax, *kept], syntax=syntax, read_only=read_only, dry_run=dry)


def _parse_pacman(args: list[str], ctx: ParseCtx) -> ParseResult:
    """pacman's ops are single letters with modifiers: -S, -Ss, -R, -Syu, -Qdt ..."""
    if not args or not args[0].startswith("-") or args[0].startswith("--") or len(args[0]) < 2:
        return _passthrough("pacman", args)
    letter, mods = args[0][1], set(args[0][2:])
    rest, opts, unknown = _scan(args[1:], ctx, {"--noconfirm"}, set(), ignored=set(),
                                native_flags={"--needed": "needed"})
    if opts["error"]:
        return Invalid(opts["error"])
    read_only = letter in "QTVH" or (letter == "S" and mods and mods <= {"s", "i"})

    if unknown:
        return _passthrough("pacman", args, read_only=read_only)
    if letter == "R":
        # -R, or -R with -s/-n/-d/-dd: a remove whose native flags are carried along.
        if mods and (not mods <= set("sndd") or args[0][2:].count("d") > 2):
            return _passthrough("pacman", args, read_only=read_only)
        op, flags = "remove", _pacman_remove_flags(args[0][2:])
    elif letter == "S" and mods <= {"s"}:
        if not mods and not rest:
            return _passthrough("pacman", args, read_only=read_only)
        op, flags = ("search" if mods else "install"), set()
    else:
        # -Sy, -Syu, -Si, -Sc, -Qdt, ...: manager-specific, pass through unchanged.
        return _passthrough("pacman", args, read_only=read_only)
    if not rest:
        return _passthrough("pacman", args, read_only=read_only)
    return Intent(op=op, terms=rest, syntax="pacman", source=opts["source"],
                  accept_replacement=opts["accept"], yes=opts["yes"] or "--noconfirm" in args,
                  dry_run=opts["dry_run"], json_out=opts["json"], flags=frozenset(flags - {"needed"}),
                  needed="needed" in opts["flags"])


def _pacman_remove_flags(letters: str) -> set[str]:
    """-s, -n, -d, -dd -> canonical flags. The second d means 'skip all checks'."""
    flags: set[str] = set()
    for letter in letters:
        if letter == "d":
            flags.add("nocheck" if "nodeps" in flags else "nodeps")
        else:
            flags.add(PACMAN_REMOVE_LETTERS[letter])
    return flags


_READ_ONLY = {
    "apt": {"search", "show", "list", "policy", "madison"},
    "dnf": {"search", "list", "info", "repoquery", "provides"},
    "zypper": {"se", "search", "info", "if", "packages", "pa", "repos", "lr", "list-updates", "lu"},
    "flatpak": {"search", "list", "info", "remotes", "remote-ls", "remote-list", "history", "documentation"},
}



def _add_common(parser: argparse.ArgumentParser) -> None:
    """Options accepted both before and after the verb. SUPPRESS keeps the other parser's value."""
    sup = argparse.SUPPRESS
    parser.add_argument("--from", dest="source", metavar="SOURCE", default=sup,
                        help="search only this source, ignoring priorities (no fallback)")
    for name in SOURCE_FLAGS:
        parser.add_argument(f"--{name}", dest="source", action="store_const", const=name, default=sup,
                            help=f"same as --from {name}")
    parser.add_argument("--with", dest="source", metavar="SOURCE", default=sup,
                        help="same as --from: translate the command to this source")
    parser.add_argument("-y", "--yes", action="store_true", default=sup,
                        help="never prompt; ties and replacements fail instead of asking")
    parser.add_argument("--needed", action="store_true", default=sup,
                        help="install: skip packages already installed")
    parser.add_argument("--accept-replacement", action="store_true", default=sup,
                        help="allow a fork/replacement package (e.g. swww -> awww)")
    parser.add_argument("--dry-run", action="store_true", default=sup,
                        help="print the native commands only")
    parser.add_argument("--json", dest="json_out", action="store_true", default=sup,
                        help="JSON output (search, detect, --dry-run)")


def _uni_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    _add_common(common)

    parser = argparse.ArgumentParser(prog="uni", description="Unified package manager wrapper.", parents=[common])
    parser.add_argument("--as", dest="as_syntax", metavar="SYNTAX", help="read commands in this syntax")
    parser.add_argument("--config", metavar="PATH", help="config file (default ~/.config/uni/config.toml)")
    parser.add_argument("-v", "--verbose", action="store_true", help="print every native query to stderr")
    sub = parser.add_subparsers(dest="op", required=True)
    for op in ("install", "remove", "search"):
        sp = sub.add_parser(op, parents=[common], aliases=["uninstall"] if op == "remove" else [])
        sp.add_argument("terms", nargs="+", metavar="PACKAGE")
    sub.add_parser("detect", parents=[common], help="show the detected host and sources")
    boot = sub.add_parser("bootstrap", parents=[common],
                          help="install a missing package manager (winget, choco or scoop)")
    boot.add_argument("target", nargs="?", default=None, metavar="MANAGER")
    log = sub.add_parser("log", help="show recent uni activity")
    log.add_argument("-n", type=int, default=30, help="number of entries (default 30)")
    return parser


def _parse_uni(args: list[str], ctx: ParseCtx) -> ParseResult:
    ns = _uni_parser().parse_args(args)
    # `log` and `detect` have fewer options; getattr keeps the Intent defaults for those.
    op = "remove" if ns.op == "uninstall" else ns.op
    terms = ([ns.target] if ns.target else []) if ns.op == "bootstrap" else getattr(ns, "terms", [])
    return Intent(op=op, terms=terms, syntax="uni",
                  source=getattr(ns, "source", None),
                  accept_replacement=getattr(ns, "accept_replacement", False),
                  yes=getattr(ns, "yes", False), dry_run=getattr(ns, "dry_run", False),
                  json_out=getattr(ns, "json_out", False), count=getattr(ns, "n", 30),
                  needed=getattr(ns, "needed", False))


FRONTENDS: dict[str, Frontend] = {
    "uni": Frontend("uni", None, _parse_uni),
    "apt": Frontend("apt", "apt", _generic(
        "apt",
        {"install": ("install", frozenset()), "remove": ("remove", frozenset()),
         "purge": ("remove", frozenset({"purge"})), "search": ("search", frozenset())},
        yes_flags={"-y", "--yes", "--assume-yes"}, dry_flags={"-s", "--simulate", "--just-print"},
        read_only=_READ_ONLY["apt"], ignored={"-q", "--quiet"},
        native_flags={"--purge": "purge"})),
    "dnf": Frontend("dnf", "dnf", _generic(
        "dnf",
        {"install": ("install", frozenset()), "remove": ("remove", frozenset()),
         "erase": ("remove", frozenset()), "search": ("search", frozenset())},
        yes_flags={"-y", "--assumeyes"}, dry_flags=set(), read_only=_READ_ONLY["dnf"],
        ignored={"-q", "--quiet"})),
    "pacman": Frontend("pacman", "pacman", _parse_pacman),
    "zypper": Frontend("zypper", "zypper", _generic(
        "zypper",
        {"install": ("install", frozenset()), "in": ("install", frozenset()),
         "remove": ("remove", frozenset()), "rm": ("remove", frozenset()),
         "search": ("search", frozenset()), "se": ("search", frozenset())},
        yes_flags={"-n", "--non-interactive", "--no-confirm"}, dry_flags=set(),
        read_only=_READ_ONLY["zypper"], ignored={"-q", "--quiet"})),
    "winget": Frontend("winget", "winget", _generic(
        "winget",
        {"install": ("install", frozenset()), "uninstall": ("remove", frozenset()),
         "remove": ("remove", frozenset()), "search": ("search", frozenset())},
        yes_flags={"--silent", "--disable-interactivity", "-h"}, dry_flags=set(),
        read_only={"search", "list", "show"},
        ignored={"--exact", "--accept-package-agreements", "--accept-source-agreements"})),
    "choco": Frontend("choco", "choco", _generic(
        "choco",
        {"install": ("install", frozenset()), "uninstall": ("remove", frozenset()),
         "search": ("search", frozenset())},
        yes_flags={"-y", "--yes"}, dry_flags=set(), read_only={"search", "list"},
        ignored={"--limit-output"})),
    "scoop": Frontend("scoop", "scoop", _generic(
        "scoop",
        {"install": ("install", frozenset()), "uninstall": ("remove", frozenset()),
         "remove": ("remove", frozenset()), "search": ("search", frozenset())},
        yes_flags=set(), dry_flags=set(), read_only={"search", "list", "info", "status"})),
    "flatpak": Frontend("flatpak", "flatpak", _generic(
        "flatpak",
        {"install": ("install", frozenset()), "uninstall": ("remove", frozenset()),
         "search": ("search", frozenset())},
        yes_flags={"-y", "--assumeyes"}, dry_flags=set(), read_only=_READ_ONLY["flatpak"])),
}


def parse(syntax: str, args: list[str], ctx: ParseCtx) -> ParseResult:
    return FRONTENDS[syntax].parse(args, ctx)
