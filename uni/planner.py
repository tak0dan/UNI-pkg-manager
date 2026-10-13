"""Planning: from an intent to a plan you can inspect, before anything executes.

Two stages, with no system changes in either:

- draft():    resolve every requested name to candidates (queries only). The draft
              may still need choices: ties to pick, forks to confirm.
- finalize(): take the chosen candidates, check that every requested native flag
              has an equivalent in the chosen backend, and build the native steps.
              A flag without an equivalent refuses the whole plan, with the reason.

A Plan is data: its commands, the packages, the notices, and whether a snapshot is
taken. Executing it is a separate step (transaction.py).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from . import capabilities, frontends, resolver
from .backends import Step
from .model import Candidate, Intent
from .resolver import Resolution
from .transaction import elevate, snapper_enabled

EXIT_REFUSED = 4


class PlanError(Exception):
    """The plan cannot be built. `code` is the exit status uni should return."""

    def __init__(self, message: str, code: int = EXIT_REFUSED):
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass
class Draft:
    intent: Intent
    resolutions: list[Resolution]
    failures: list[tuple[int, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def ties(self) -> list[int]:
        return [i for i, r in enumerate(self.resolutions) if r.tie and r.skipped is None]

    def forks(self) -> list[int]:
        """Replacements (fork relations) the user has not accepted yet."""
        if self.intent.accept_replacement:
            return []
        return [i for i, r in enumerate(self.resolutions) if r.replacement and r.skipped is None]


@dataclass
class Plan:
    op: str
    syntax: str
    items: list[tuple[str, Candidate]]  # requested term -> chosen candidate
    steps: list[Step]
    flags: frozenset[str]
    notices: list[str]
    snapshot: bool
    skipped: list[tuple[str, Candidate]] = field(default_factory=list)  # --needed: already installed

    @property
    def label(self) -> str:
        return f"{self.op} " + " ".join(c.ref for _, c in self.items)

    def commands(self) -> list[list[str]]:
        return [s.argv for s in self.steps]

    def render(self, sudo: str = "sudo") -> list[str]:
        """Human-readable plan, as the interface and plain mode show it."""
        lines = ["Plan"]
        lines += [f"  {c.source:<8} {c.ref} {c.version}".rstrip() for _, c in self.items]
        for term, cand in self.skipped:
            lines.append(f"  already installed, skipped: {cand.source} {cand.ref} ({term})")
        if self.flags:
            names = ", ".join(sorted(self.flags))
            lines.append(f"  native flags: {names}")
        if self.steps:
            lines += ["", "Commands"]
            if self.snapshot:
                lines.append("  snapshot before and after (snapper)")
            for step in self.steps:
                lines.append("  $ " + " ".join(elevate(step, sudo)))
        if self.notices:
            lines += [""] + self.notices
        return lines

    def to_dict(self) -> dict:
        return {
            "op": self.op,
            "syntax": self.syntax,
            "items": [{"term": t, **asdict(c)} for t, c in self.items],
            "flags": sorted(self.flags),
            "commands": self.commands(),
            "snapshot": self.snapshot,
            "skipped": [{"term": t, **asdict(c)} for t, c in self.skipped],
        }


def draft(intent: Intent, ctx: resolver.Context) -> Draft:
    """Resolve every term. Queries only; nothing is changed."""
    remove = intent.op == "remove"
    # With an explicit source, the flags can be checked before anything is looked up.
    if remove and intent.flags and intent.source in ctx.backends:
        problems = capabilities.unsupported(ctx.backends[intent.source], intent.flags)
        if problems:
            message = "cannot build this plan:\n  " + "\n  ".join(problems)
            return Draft(intent, [], [(EXIT_REFUSED, message)])
    resolutions: list[Resolution] = []
    failures: list[tuple[int, str]] = []
    for term in intent.terms:
        res = resolver.resolve(term, ctx, intent.source, installed_only=remove)
        if res.error is None and intent.op == "install" and intent.needed:
            # --needed: a term whose candidate is already installed needs no action.
            for cand in res.ranked:
                if ctx.backends[cand.source].installed(cand.ref):
                    res.skipped = cand
                    break
        resolutions.append(res)
        if res.error:
            failures.append((res.code or 1, res.error))
    return Draft(intent, resolutions, failures)


def default_choice(res: Resolution) -> Candidate:
    """What a term resolves to without asking: the installed match with --needed, else the best."""
    return res.skipped if res.skipped is not None else res.best


def _understood_as(ctx: resolver.Context, intent: Intent, term: str, cand: Candidate) -> str:
    implied = frontends.FRONTENDS[intent.syntax].backend
    ranking = ", ".join(f"{s} {ctx.priority(s)}" for s in ctx.enabled_ids())
    return (
        f"Understood as: {intent.op} {term} -> {cand.source} "
        f"({intent.syntax} commands normally use {implied}; your priorities: {ranking}). "
        "Change priorities in the config, or set [syntax] notice = false to hide this."
    )


def finalize(d: Draft, chosen: list[Candidate], ctx: resolver.Context, cfg: dict,
             assume_yes: bool = False) -> Plan:
    """Build the plan for the chosen candidates. Raises PlanError if it cannot be valid."""
    intent = d.intent
    remove = intent.op == "remove"
    if len(chosen) != len(intent.terms):
        raise PlanError("internal: one candidate is needed per requested name", code=1)

    skipped = [(res.term, cand) for res, cand in zip(d.resolutions, chosen) if res.skipped is not None]
    items = [(term, cand) for term, cand, res in zip(intent.terms, chosen, d.resolutions) if res.skipped is None]

    unique: dict[tuple[str, str], Candidate] = {}
    for _, cand in items:
        unique.setdefault((cand.source, cand.ref), cand)
    cands = list(unique.values())

    # Every native flag must have an equivalent in every backend that receives a package.
    if remove and intent.flags:
        problems: list[str] = []
        for cand in cands:
            problems += capabilities.unsupported(ctx.backends[cand.source], intent.flags)
        if problems:
            raise PlanError("cannot build this plan:\n  " + "\n  ".join(problems)
                            + "\nRemove the flag, or name a source that supports it.")

    by_backend: dict[str, list[Candidate]] = {}
    for cand in cands:
        by_backend.setdefault(cand.source, []).append(cand)
    steps: list[Step] = []
    for sid, group in by_backend.items():
        backend = ctx.backends[sid]
        if remove:
            steps += backend.remove(group, assume_yes or intent.yes, intent.flags)
        else:
            steps += backend.install(group, assume_yes or intent.yes)

    # The notice explains a translated command. An explicit or routed source is not a translation.
    notices: list[str] = []
    implied = frontends.FRONTENDS[intent.syntax].backend
    if intent.syntax != "uni" and intent.source is None and cfg["syntax"]["notice"]:
        for term, cand in items:
            if cand.source != implied:
                notices.append(_understood_as(ctx, intent, term, cand))

    return Plan(
        op=intent.op,
        syntax=intent.syntax,
        items=items,
        steps=steps,
        flags=intent.flags,
        notices=notices,
        snapshot=snapper_enabled(cfg) and bool(steps),
        skipped=skipped,
    )


def preview(d: Draft, cand: Candidate, ctx: resolver.Context) -> str:
    """The command one candidate would run, for the picker."""
    remove = d.intent.op == "remove"
    backend = ctx.backends[cand.source]
    if remove:
        steps = backend.remove([cand], True, d.intent.flags)
    else:
        steps = backend.install([cand], True)
    return " ; ".join(" ".join(s.argv) for s in steps)
