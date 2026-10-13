"""Entry point: wires config, detection, resolution and transactions together."""

from __future__ import annotations

import sys
from dataclasses import asdict
from pathlib import Path

from . import config, detect, frontends, resolver
from .backends import Step, make_backends, run
from .frontends import ParseCtx
from .model import Candidate, Intent, Passthrough
from .render import emit_json, record, table, warn
from .repology import Repology
from .transaction import UnsafeCommand, execute

EXIT_OK, EXIT_ERROR, EXIT_AMBIGUOUS, EXIT_NOT_FOUND, EXIT_REFUSED = 0, 1, 2, 3, 4


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


def _flatpak_remotes() -> set[str]:
    rc, out = run(["flatpak", "remotes", "--columns=name"])
    if rc:
        return set()
    return {line.strip() for line in out.splitlines() if line.strip() and line.strip() != "Name"}


def _user_packages_path() -> Path:
    return config.config_dir() / "packages.toml"


def build_context(cfg: dict) -> tuple[resolver.Context, detect.Host]:
    backends = make_backends(cfg)
    host = detect.probe(set(backends))
    db = resolver.PackageDB.load([resolver.DATA_PACKAGES, _user_packages_path()])
    repology = Repology(
        enabled=cfg["resolver"]["repology"],
        cache=config.cache_dir() / "repology",
        ttl_hours=cfg["resolver"]["cache_hours"],
    )
    return resolver.Context(cfg, backends, host.native, db, repology), host


def _command_line(ctx: resolver.Context, cand: Candidate, remove: bool, yes: bool) -> str:
    backend = ctx.backends[cand.source]
    steps = backend.remove([cand], yes) if remove else backend.install([cand], yes)
    return " ; ".join(" ".join(s.argv) for s in steps)


def _ask(prompt: str) -> bool:
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _notice(ctx: resolver.Context, intent: Intent, term: str, cand: Candidate) -> None:
    """The 'Understood as' line: explains a translated command that resolved elsewhere."""
    implied = frontends.FRONTENDS[intent.syntax].backend
    ranking = ", ".join(f"{s} {ctx.priority(s)}" for s in ctx.enabled_ids())
    print(
        f"Understood as: {intent.op} {term} -> resolved to {cand.source} "
        f"(your priority: {ranking}; {intent.syntax} implies {implied}). "
        "Change priorities in the config, or set [syntax] notice = false to hide this."
    )


def _detect(ctx: resolver.Context, cfg: dict, host: detect.Host, json_out: bool) -> int:
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
        rc = execute([step], run_cfg, dry_run=False, label=" ".join(p.argv))
    except UnsafeCommand as exc:
        warn(str(exc))
        return EXIT_REFUSED
    if cfg["general"]["log"]:
        record({"syntax": p.syntax, "argv": p.argv, "passthrough": True, "rc": rc})
    return rc


def _run_search(intent: Intent, ctx: resolver.Context, source: str | None) -> int:
    results: list[Candidate] = []
    for term in intent.terms:
        results.extend(resolver.search(term, ctx, source))
    if intent.json_out:
        emit_json([asdict(c) | {"priority": ctx.priority(c.source)} for c in results])
        return EXIT_OK
    if not results:
        print("no matches")
        return EXIT_OK
    rows = [
        [str(ctx.priority(c.source) if source is None else "-"), c.source, c.ref, c.version, c.description]
        for c in results
    ]
    print(table(["Prio", "Source", "Name", "Version", "Description"], rows))
    return EXIT_OK


def _resolve_all(intent: Intent, ctx: resolver.Context, source: str | None, interactive: bool):
    """Resolve every term. Returns (chosen candidates, failures as (code, message))."""
    from .tui import pick

    remove = intent.op == "remove"
    chosen: list[Candidate] = []
    failures: list[tuple[int, str]] = []
    for term in intent.terms:
        res = resolver.resolve(term, ctx, source, installed_only=remove)
        for note in res.notes:
            warn(note)
        if res.error:
            failures.append((res.code or EXIT_ERROR, res.error))
            continue
        if res.replacement and not intent.accept_replacement:
            question = f"'{res.replacement}' is a different project replacing '{term}'. Use it instead? [y/N] "
            if not (interactive and _ask(question)):
                failures.append((EXIT_REFUSED,
                                 f"'{term}' resolves to '{res.replacement}', a different project; "
                                 "rerun with --accept-replacement"))
                continue
        if res.tie:
            if not interactive:
                options = ", ".join(c.describe() for c in res.ranked)
                failures.append((EXIT_AMBIGUOUS, f"'{term}' is ambiguous ({options}); run interactively to choose"))
                continue
            picked = pick(term, [(c, _command_line(ctx, c, remove, intent.yes)) for c in res.ranked])
            if picked is None:
                failures.append((EXIT_ERROR, f"'{term}': cancelled"))
                continue
            chosen.append(picked)
        else:
            chosen.append(res.best)
    return chosen, failures


def _run_transaction(intent: Intent, ctx: resolver.Context, cfg: dict, explicit: bool, routed: bool) -> int:
    remove = intent.op == "remove"
    interactive = not (intent.yes or intent.json_out) and sys.stdin.isatty() and sys.stdout.isatty()
    chosen, failures = _resolve_all(intent, ctx, intent.source, interactive)
    if failures:
        for _, msg in failures:
            warn(msg)
        return failures[0][0]

    unique: dict[tuple[str, str], Candidate] = {}
    for cand in chosen:
        unique.setdefault((cand.source, cand.ref), cand)
    chosen = list(unique.values())

    by_backend: dict[str, list[Candidate]] = {}
    for cand in chosen:
        by_backend.setdefault(cand.source, []).append(cand)
    steps: list[Step] = []
    for sid, cands in by_backend.items():
        backend = ctx.backends[sid]
        steps += backend.remove(cands, intent.yes) if remove else backend.install(cands, intent.yes)

    if intent.json_out:
        emit_json({
            "op": intent.op,
            "resolved": [asdict(c) for c in chosen],
            "commands": [s.argv for s in steps],
        })
        return EXIT_OK

    if not explicit and not routed and intent.syntax != "uni" and cfg["syntax"]["notice"]:
        implied = frontends.FRONTENDS[intent.syntax].backend
        for term, cand in zip(intent.terms, chosen):
            if cand.source != implied:
                _notice(ctx, intent, term, cand)

    for term, cand in zip(intent.terms, chosen):
        print(f"{term} -> {cand.source} {cand.ref} {cand.version}".rstrip())
    label = f"{intent.op} " + " ".join(c.ref for c in chosen)
    try:
        rc = execute(steps, cfg, dry_run=intent.dry_run, label=label)
    except UnsafeCommand as exc:
        warn(str(exc))
        return EXIT_REFUSED
    if cfg["general"]["log"] and not intent.dry_run:
        record({"op": intent.op, "syntax": intent.syntax, "resolved": [c.describe() for c in chosen], "rc": rc})
    return rc


def _run_intent(intent: Intent, ctx: resolver.Context, cfg: dict, host: detect.Host, explicit: bool, routed: bool) -> int:
    if intent.op == "detect":
        return _detect(ctx, cfg, host, intent.json_out)
    if intent.json_out and intent.op in ("install", "remove") and not intent.dry_run:
        warn("--json works with search, detect and --dry-run")
        return EXIT_ERROR
    source = None
    if intent.source is not None:
        source = ctx.concrete(intent.source)
        if source not in ctx.backends:
            warn(f"'{intent.source}' is not available on this system")
            return EXIT_ERROR
    intent.source = source
    if intent.op == "search":
        return _run_search(intent, ctx, source)
    return _run_transaction(intent, ctx, cfg, explicit, routed)


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
        prog = prog or Path(sys.argv[0]).name
    as_syntax, argv = _take_option(list(argv), "--as")
    config_path, argv = _take_option(argv, "--config")
    prog = prog or "uni"
    syntax = as_syntax or (prog[4:] if prog.startswith("uni-") else "uni")
    if syntax not in frontends.FRONTENDS:
        warn(f"unknown syntax '{syntax}'; choose one of {', '.join(frontends.FRONTENDS)}")
        return EXIT_ERROR

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

    known = frozenset(cfg["sources"]) | frozenset(ctx.backends)
    parsed = frontends.parse(syntax, argv, ParseCtx(known_sources=known, remotes=_flatpak_remotes))

    if isinstance(parsed, Passthrough):
        return _run_passthrough(parsed, ctx, cfg)

    explicit = parsed.source is not None
    routed = False
    if syntax in cfg["syntax"]["route"] and not explicit:
        parsed.source = frontends.FRONTENDS[syntax].backend
        routed = True
    return _run_intent(parsed, ctx, cfg, host, explicit, routed)
