"""Entry point: wires config, detection, resolution, the TUI and transactions together."""

from __future__ import annotations

import dataclasses
import re
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

from . import backends, config, detect, frontends, planner, resolver
from .backends import Step
from .frontends import ParseCtx
from .model import Candidate, Intent, Invalid, Passthrough
from .planner import Plan, PlanError
from .render import (emit_json, format_entry, make_trace, read_log, record, search_table, warn,
                     write_transaction)
from .repology import Repology
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


# uni-only options that can come before the native command word.
_LEADING_VALUE_OPTIONS = {"--with", "--from"}
_LEADING_FLAGS = {"--yes", "-y", "--dry-run", "--json", "--accept-replacement"}


def _guess_syntax(argv: list[str]) -> tuple[str | None, list[str]]:
    """`uni apt install x`, `uni -S x` and `uni --with flatpak -S x` select a syntax without --as."""
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in _LEADING_VALUE_OPTIONS and i + 1 < len(argv):
            i += 2
        elif token in _LEADING_FLAGS or (token.startswith("--") and token[2:] in frontends.SOURCE_FLAGS):
            i += 1
        else:
            break
    head = argv[i] if i < len(argv) else ""
    if head in frontends.FRONTENDS and head != "uni":
        return head, argv[:i] + argv[i + 1:]
    if head and PACMAN_FLAGS.match(head):
        # pacman's parser needs its own command word first; uni's options follow it.
        return "pacman", argv[i:] + argv[:i]
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


def _ask(prompt: str) -> bool:
    """A yes/no question. Anything but y or yes is a no, and end-of-input is a no."""
    try:
        return input(prompt).strip().lower() in ("y", "yes")
    except EOFError:
        return False


def _run_bootstrap(target: str, cfg: dict, yes: bool, present: set[str], dry_run: bool = False) -> int:
    """Install a missing package manager. Every command is shown and confirmed before it runs."""
    from . import bootstrap

    if target not in bootstrap.TARGETS:
        warn(f"cannot bootstrap '{target}'; choose one of {', '.join(bootstrap.TARGETS)}")
        return EXIT_ERROR
    if target in present:
        print(f"{target} is already installed")
        return EXIT_OK
    recipe = bootstrap.recipe(target, present)
    if recipe is None:
        warn(f"no way to install {target} on this system")
        return EXIT_ERROR
    for line in recipe.description:
        print(line)
    if recipe.manual:
        warn(f"uni cannot restore {target} unattended; follow the steps above and run uni again")
        return EXIT_ERROR
    for line in bootstrap.describe_steps(recipe.steps):
        print(line)
    if dry_run:
        return EXIT_OK
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not yes and not interactive:
        warn(f"bootstrapping {target} runs installers; each command needs a confirmation, "
             "so rerun in a terminal or pass --yes to approve them all")
        return EXIT_REFUSED

    # Each command is its own decision: a declined one stops here, and a failed one may be
    # followed by its fallback, which is asked about separately.
    quiet_cfg = {**cfg, "safety": {**cfg["safety"], "snapper": False}}
    for step in recipe.steps:
        for attempt in [step, *step.fallback]:
            command = " ".join(attempt.argv)
            if not yes and not _ask(f"Run this command? [y/N]\n  $ {command}\n> "):
                print(f"not run: {target} was not installed")
                record({"type": "transaction", "op": "bootstrap", "syntax": "uni", "resolved": [target],
                        "rc": EXIT_REFUSED})
                return EXIT_REFUSED
            # The attempt runs alone: its own fallback would otherwise run without a confirmation.
            alone = dataclasses.replace(attempt, fallback=[])
            rc = execute([alone], quiet_cfg, dry_run=False, label=f"bootstrap {target}")
            if rc == 0:
                break
            warn(f"command failed (exit {rc})")
        else:
            record({"type": "transaction", "op": "bootstrap", "syntax": "uni", "resolved": [target], "rc": 1})
            warn(f"could not install {target}")
            return EXIT_ERROR
    record({"type": "transaction", "op": "bootstrap", "syntax": "uni", "resolved": [target], "rc": 0})
    if not shutil.which(target):
        warn(f"{target} is still not found on PATH; open a new terminal and run uni again")
        return EXIT_ERROR
    print(f"{target} installed")
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


def _report_resolution(d) -> None:
    for res in d.resolutions:
        for note in res.notes:
            warn(note)
        record({"type": "resolve", "term": res.term, "chosen": [c.describe() for c in res.ranked[:1]],
                "tie": res.tie, "hints": [c.describe() for c in res.hints], "error": res.error})


def _run_transaction(intent: Intent, ctx: resolver.Context, cfg: dict) -> int:
    """draft -> choices -> plan -> run. Nothing changes before the plan is complete."""
    d = planner.draft(intent, ctx)
    _report_resolution(d)
    if not d.ok:
        for _, msg in d.failures:
            warn(msg)
        return d.failures[0][0]

    interactive = (sys.stdin.isatty() and sys.stdout.isatty()
                   and not (intent.yes or intent.json_out or intent.dry_run))
    if interactive:
        from .tui import Job, run_job

        outcome: dict = {}

        def on_done(plan: Plan, rc: int, output: list[str]) -> None:
            path = write_transaction(intent.op, plan.label, [" ".join(c) for c in plan.commands()], output, rc)
            outcome.update(plan=plan, rc=rc, log=path)
            record({"type": "transaction", "op": intent.op, "syntax": intent.syntax,
                    "resolved": [c.describe() for _, c in plan.items], "rc": rc, "log": path})

        rc = run_job(Job(draft=d, ctx=ctx, cfg=cfg, on_done=on_done))
        # The interface draws on its own screen, which disappears on exit. Say the outcome here,
        # where it stays in the terminal.
        if outcome:
            names = " ".join(c.ref for _, c in outcome["plan"].items)
            if not outcome["plan"].steps:
                print("nothing to do: every requested package is already installed")
            elif outcome["rc"] == 0:
                print(f"done: {intent.op} {names}")
            else:
                print(f"failed: {intent.op} {names} (exit {outcome['rc']})")
            if outcome["log"]:
                print(f"full output: {outcome['log']}")
        else:
            print("not run: cancelled or refused; nothing was changed")
        return rc

    # Plain mode: no choices are made silently.
    for res in d.resolutions:
        if res.replacement and d.forks():
            warn(f"'{res.term}' resolves to '{res.replacement}', a different project; "
                 "rerun with --accept-replacement")
            return EXIT_REFUSED
        if res.tie and res.skipped is None:
            options = ", ".join(c.describe() for c in res.ranked)
            warn(f"'{res.term}' is ambiguous ({options}); run in a terminal to choose")
            return EXIT_AMBIGUOUS
    try:
        plan = planner.finalize(d, [planner.default_choice(res) for res in d.resolutions], ctx, cfg,
                                assume_yes=intent.yes)
    except PlanError as exc:
        warn(exc.message)
        return exc.code

    if intent.json_out:
        emit_json(plan.to_dict())
        return EXIT_OK
    if not plan.steps:
        for line in plan.render(cfg["general"]["sudo"]):
            print(line)
        print("nothing to do: every requested package is already installed")
        return EXIT_OK
    for line in plan.render(cfg["general"]["sudo"]):
        print(line)
    try:
        rc = execute(plan.steps, cfg, dry_run=intent.dry_run, label=plan.label)
    except UnsafeCommand as exc:
        warn(str(exc))
        return EXIT_REFUSED
    record({"type": "transaction", "op": intent.op, "syntax": intent.syntax, "dry_run": intent.dry_run,
            "resolved": [c.describe() for _, c in plan.items], "rc": rc})
    return rc


def _run_intent(intent: Intent, ctx: resolver.Context, cfg: dict, host: detect.Host) -> int:
    if intent.op == "detect":
        return _detect(ctx, host, intent.json_out)
    if intent.op == "log":
        return _run_log(intent.count)
    if intent.op == "bootstrap":
        from . import bootstrap as _bootstrap

        target = intent.terms[0] if intent.terms else _bootstrap.default_target(
            detect.family_of(detect.read_os_release()))
        if target is None:
            warn("nothing to bootstrap: this system has a native manager and no target was named")
            return EXIT_ERROR
        return _run_bootstrap(target, cfg, intent.yes, set(ctx.backends), intent.dry_run)
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
    return _run_transaction(intent, ctx, cfg)


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

    # On Windows with no package manager at all, install one first (asks, unless --yes), then go on.
    if isinstance(parsed, Intent) and host.native is None and parsed.op in ("install", "remove", "search"):
        from . import bootstrap as _bootstrap

        family = detect.family_of(detect.read_os_release())
        target = syntax if syntax in _bootstrap.TARGETS else _bootstrap.default_target(family)
        if target is None:
            warn("no native package manager found, and uni does not know how to restore one here")
            return EXIT_ERROR
        rc = _run_bootstrap(target, cfg, parsed.yes, set(ctx.backends), parsed.dry_run)
        if rc != EXIT_OK:
            return rc
        ctx, host = build_context(cfg)

    # Route mode: the syntax names its own source, unless the command already named one.
    if syntax in cfg["syntax"]["route"] and parsed.source is None:
        parsed.source = frontends.FRONTENDS[syntax].backend
    return _run_intent(parsed, ctx, cfg, host)
