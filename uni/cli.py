"""Entry point: wires config, detection, resolution, the TUI and transactions together."""

from __future__ import annotations

import re
import sys
from dataclasses import asdict
from pathlib import Path

from . import backends, config, detect, frontends, resolver
from .backends import Step
from .frontends import ParseCtx
from .model import Candidate, Intent, Invalid, Passthrough
from .render import (emit_json, format_entry, make_trace, read_log, record, search_table, warn,
                     write_transaction)
from .repology import Repology
from .resolver import Resolution
from .transaction import UnsafeCommand, execute

EXIT_OK, EXIT_ERROR, EXIT_AMBIGUOUS, EXIT_NOT_FOUND, EXIT_REFUSED = 0, 1, 2, 3, 4

# `uni -S steam`, `uni -Syu`, `uni -Rns foo`: pacman-style first word selects pacman syntax.
PACMAN_FLAGS = re.compile(r"^-[SRQUFTDV][A-Za-z]*$")


def _take_flag(argv: list[str], *flags: str) -> tuple[bool, list[str]]:
    kept = [a for a in argv if a not in flags]
    return len(kept) != len(argv), kept


def _take_option(argv: list[str], flag: str) -> tuple[str | None, list[str]]:
    """Remove `flag VALUE` or `flag=VALUE` from anywhere in argv."""
    rest: list[str] = []
    value = None
    i = 0
    while i < len(argv):
        if argv[i] == flag and i + 1 < len(argv):
            value = argv[i + 1]
            i += 2
            continue
        if argv[i].startswith(flag + "="):
            value = argv[i].partition("=")[2]
        else:
            rest.append(argv[i])
        i += 1
    return value, rest


def _guess_syntax(argv: list[str]) -> tuple[str | None, list[str]]:
    """`uni apt install x` and `uni -S x` select a syntax without --as."""
    if argv and argv[0] in frontends.FRONTENDS and argv[0] != "uni":
        return argv[0], argv[1:]
    if argv and PACMAN_FLAGS.match(argv[0]):
        return "pacman", argv
    return None, argv


def _flatpak_remotes() -> set[str]:
    rc, out = backends.run(["flatpak", "remotes", "--columns=name"])
    if rc:
        return set()
    return {line.strip() for line in out.splitlines() if line.strip() and line.strip() != "Name"}


def build_context(cfg: dict) -> tuple[resolver.Context, detect.Host]:
    available = backends.make_backends(cfg)
    host = detect.probe(set(available))
    db = resolver.PackageDB.load([resolver.DATA_PACKAGES, config.config_dir() / "packages.toml"])
    repology = Repology(
        enabled=cfg["resolver"]["repology"],
        cache=config.cache_dir() / "repology",
        ttl_hours=cfg["resolver"]["cache_hours"],
    )
    return resolver.Context(cfg, available, host.native, db, repology), host


def _build_steps(ctx: resolver.Context, chosen: list[Candidate], remove: bool, yes: bool) -> list[Step]:
    by_backend: dict[str, list[Candidate]] = {}
    for cand in chosen:
        by_backend.setdefault(cand.source, []).append(cand)
    steps: list[Step] = []
    for sid, cands in by_backend.items():
        backend = ctx.backends[sid]
        steps += backend.remove(cands, yes) if remove else backend.install(cands, yes)
    return steps


def _dedupe(chosen: list[Candidate]) -> list[Candidate]:
    unique: dict[tuple[str, str], Candidate] = {}
    for cand in chosen:
        unique.setdefault((cand.source, cand.ref), cand)
    return list(unique.values())


def _notice(ctx: resolver.Context, intent: Intent, term: str, cand: Candidate) -> str:
    """The 'Understood as' line: explains a translated command that resolved elsewhere."""
    implied = frontends.FRONTENDS[intent.syntax].backend
    ranking = ", ".join(f"{s} {ctx.priority(s)}" for s in ctx.enabled_ids())
    return (
        f"Understood as: {intent.op} {term} -> resolved to {cand.source} "
        f"(your priority: {ranking}; {intent.syntax} implies {implied}). "
        "Change priorities in the config, or set [syntax] notice = false to hide this."
    )


def _detect(ctx: resolver.Context, host: detect.Host, json_out: bool) -> int:
    enabled = [{"source": s, "priority": ctx.priority(s)} for s in ctx.enabled_ids()]
    info = {
        "os": host.pretty,
        "id": host.os_id,
        "id_like": host.os_like,
        "native": host.native,
        "available": host.available,
        "enabled_sources": enabled,
        "config": str(config.default_config_path()),
    }
    if json_out:
        emit_json(info)
        return EXIT_OK
    print(f"OS:               {host.pretty} (ID={host.os_id} ID_LIKE={host.os_like or '-'})")
    print(f"Native manager:   {host.native or 'none detected'}")
    print(f"Available:        {', '.join(host.available) or 'none'}")
    print("Enabled sources:  " + (", ".join(f"{e['source']}({e['priority']})" for e in enabled) or "none"))
    print(f"Config:           {info['config']}")
    return EXIT_OK


def _run_log(count: int) -> int:
    entries = read_log(count)
    if not entries:
        print("no activity yet")
        return EXIT_OK
    for entry in entries:
        print(format_entry(entry))
    return EXIT_OK


def _run_passthrough(p: Passthrough, ctx: resolver.Context, cfg: dict) -> int:
    backend = ctx.backends.get(p.backend)
    if backend is None:
        warn(f"'{p.backend}' is not available on this system")
        return EXIT_ERROR
    step = Step(p.argv, root=backend.needs_root and not p.read_only, label=p.syntax)
    run_cfg = cfg
    if p.read_only:  # queries change nothing, so no snapshot is taken
        run_cfg = {**cfg, "safety": {**cfg["safety"], "snapper": False}}
    try:
        rc = execute([step], run_cfg, dry_run=p.dry_run, label=" ".join(p.argv))
    except UnsafeCommand as exc:
        warn(str(exc))
        return EXIT_REFUSED
    record({"type": "transaction", "op": "passthrough", "syntax": p.syntax, "argv": p.argv, "rc": rc})
    return rc


def _run_search(intent: Intent, ctx: resolver.Context, source: str | None) -> int:
    results: list[Candidate] = []
    for term in intent.terms:
        results.extend(resolver.search(term, ctx, source))
    if intent.json_out:
        emit_json([asdict(c) | {"priority": ctx.priority(c.source), "enabled": ctx.enabled(c.source)}
                   for c in results])
        return EXIT_OK
    if not results:
        print("no matches")
        return EXIT_OK
    rows = [
        (str(ctx.priority(c.source)) if ctx.enabled(c.source) else "-", c.source, c.ref, c.version,
         c.description, ctx.enabled(c.source) or source is not None)
        for c in results
    ]
    search_table(rows)
    return EXIT_OK


def _resolve_terms(intent: Intent, ctx: resolver.Context, source: str | None):
    """Resolve each term. Returns (resolutions, failures)."""
    remove = intent.op == "remove"
    resolutions: list[Resolution] = []
    failures: list[tuple[int, str]] = []
    for term in intent.terms:
        res = resolver.resolve(term, ctx, source, installed_only=remove)
        resolutions.append(res)
        for note in res.notes:
            warn(note)
        record({"type": "resolve", "term": term, "chosen": [c.describe() for c in res.ranked[:1]],
                "tie": res.tie, "hints": [c.describe() for c in res.hints], "error": res.error})
        if res.error:
            failures.append((res.code or EXIT_ERROR, res.error))
    return resolutions, failures


def _run_transaction(intent: Intent, ctx: resolver.Context, cfg: dict, explicit: bool, routed: bool) -> int:
    remove = intent.op == "remove"
    source = intent.source
    interactive = (sys.stdin.isatty() and sys.stdout.isatty()
                   and not (intent.yes or intent.json_out or intent.dry_run))

    resolutions, failures = _resolve_terms(intent, ctx, source)
    if failures:
        for _, msg in failures:
            warn(msg)
        return failures[0][0]

    notices: list[str] = []
    implied = frontends.FRONTENDS[intent.syntax].backend
    translated = intent.syntax != "uni" and not explicit and not routed and cfg["syntax"]["notice"]
    for term, res in zip(intent.terms, resolutions):
        if translated and res.best and res.best.source != implied:
            notices.append(_notice(ctx, intent, term, res.best))

    if not interactive:
        chosen: list[Candidate] = []
        for term, res in zip(intent.terms, resolutions):
            if res.replacement and not intent.accept_replacement:
                warn(f"'{term}' resolves to '{res.replacement}', a different project; "
                     "rerun with --accept-replacement")
                return EXIT_REFUSED
            if res.tie:
                options = ", ".join(c.describe() for c in res.ranked)
                warn(f"'{term}' is ambiguous ({options}); run in a terminal to choose")
                return EXIT_AMBIGUOUS
            chosen.append(res.best)
        chosen = _dedupe(chosen)
        steps = _build_steps(ctx, chosen, remove, intent.yes)
        if intent.json_out:
            emit_json({"op": intent.op, "resolved": [asdict(c) for c in chosen],
                       "commands": [s.argv for s in steps]})
            return EXIT_OK
        for note in notices:
            print(note)
        for term, cand in zip(intent.terms, chosen):
            print(f"{term} -> {cand.source} {cand.ref} {cand.version}".rstrip())
        try:
            rc = execute(steps, cfg, dry_run=intent.dry_run, label=f"{intent.op} " + " ".join(c.ref for c in chosen))
        except UnsafeCommand as exc:
            warn(str(exc))
            return EXIT_REFUSED
        record({"type": "transaction", "op": intent.op, "syntax": intent.syntax, "dry_run": intent.dry_run,
                "resolved": [c.describe() for c in chosen], "rc": rc})
        return rc

    from .tui import Plan, run_plan

    def build(chosen: list[Candidate]) -> list[Step]:
        # The TUI is the confirmation, so native commands run non-interactively.
        return _build_steps(ctx, _dedupe(chosen), remove, yes=True)

    def on_done(chosen: list[Candidate], steps: list[Step], rc: int, output: list[str]) -> None:
        commands = [" ".join(s.argv) for s in steps]
        path = write_transaction(intent.op, " ".join(c.ref for c in chosen), commands, output, rc)
        record({"type": "transaction", "op": intent.op, "syntax": intent.syntax,
                "resolved": [c.describe() for c in chosen], "rc": rc, "log": path})

    plan = Plan(
        title=f"{intent.op} {' '.join(intent.terms)}",
        cfg=cfg,
        label=f"{intent.op} " + " ".join(intent.terms),
        resolutions=resolutions,
        accept=[intent.accept_replacement] * len(resolutions),
        build=build,
        notices=notices,
        on_done=on_done,
    )
    return run_plan(plan)


def _run_intent(intent: Intent, ctx: resolver.Context, cfg: dict, host: detect.Host,
                explicit: bool, routed: bool) -> int:
    if intent.op == "detect":
        return _detect(ctx, host, intent.json_out)
    if intent.op == "log":
        return _run_log(intent.count)
    if intent.json_out and intent.op in ("install", "remove") and not intent.dry_run:
        warn("--json works with search, detect and --dry-run")
        return EXIT_ERROR
    if intent.source is not None:
        intent.source = ctx.concrete(intent.source)
        if intent.source not in ctx.backends:
            warn(f"'{intent.source}' is not available on this system")
            return EXIT_ERROR
    if intent.op == "search":
        return _run_search(intent, ctx, intent.source)
    return _run_transaction(intent, ctx, cfg, explicit, routed)


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
        prog = prog or Path(sys.argv[0]).name
    as_syntax, argv = _take_option(list(argv), "--as")
    config_path, argv = _take_option(argv, "--config")
    prog = prog or "uni"

    if as_syntax is not None:
        syntax = as_syntax
    elif prog.startswith("uni-"):
        syntax = prog[4:]
    else:
        guessed, argv = _guess_syntax(argv)
        syntax = guessed or "uni"
    if syntax not in frontends.FRONTENDS:
        warn(f"unknown syntax '{syntax}'; choose one of {', '.join(frontends.FRONTENDS)}")
        return EXIT_ERROR
    # -v belongs to uni itself; foreign syntaxes keep their own -v.
    verbose, argv = _take_flag(argv, "--verbose") if syntax != "uni" else _take_flag(argv, "-v", "--verbose")

    if config_path is None:
        created = config.ensure_user_config()
        if created:
            warn(f"wrote default config to {created}")
    try:
        cfg = config.load(Path(config_path) if config_path else None)
    except config.ConfigError as exc:
        warn(str(exc))
        return EXIT_ERROR
    try:
        ctx, host = build_context(cfg)
    except (ValueError, OSError) as exc:
        warn(f"cannot load package data: {exc}")
        return EXIT_ERROR

    backends.TRACE = make_trace(verbose) if cfg["general"]["log"] or verbose else None

    known = frozenset(cfg["sources"]) | frozenset(ctx.backends) | frozenset(frontends.SOURCE_FLAGS)
    parsed = frontends.parse(syntax, argv, ParseCtx(known_sources=known, remotes=_flatpak_remotes))

    if isinstance(parsed, Invalid):
        warn(parsed.message)
        return EXIT_ERROR
    if isinstance(parsed, Passthrough):
        return _run_passthrough(parsed, ctx, cfg)

    explicit = parsed.source is not None
    routed = False
    if syntax in cfg["syntax"]["route"] and not explicit:
        parsed.source = frontends.FRONTENDS[syntax].backend
        routed = True
    return _run_intent(parsed, ctx, cfg, host, explicit, routed)
