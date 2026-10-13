"""Name resolution: common name -> live candidates per source -> ranked choice.

Order of lookup for a name: local alias table, Repology, then the literal name,
each checked live against every source that is searched. Candidates are ranked
by pin first, then source priority. Equal rank means a real tie, which the
caller must resolve by asking.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .backends import Backend, BackendError
from .model import Candidate
from .repology import Repology

DATA_PACKAGES = Path(__file__).parent / "data" / "packages.toml"

EXIT_NOT_FOUND = 3


@dataclass(frozen=True)
class Relation:
    to: str
    kind: str  # "renamed" (same project) or "fork" (different project, must confirm)


@dataclass
class PackageDB:
    aliases: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    relations: dict[str, Relation] = field(default_factory=dict)

    @classmethod
    def load(cls, paths: list[Path]) -> "PackageDB":
        db = cls()
        for path in paths:
            if not path.exists():
                continue
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            for term, per_backend in data.get("packages", {}).items():
                db.aliases.setdefault(term, {}).update(per_backend)
            for term, rel in data.get("relations", {}).items():
                if rel.get("kind") not in ("renamed", "fork"):
                    raise ValueError(f"{path}: relation '{term}' has unknown kind")
                db.relations[term] = Relation(rel["to"], rel["kind"])
        return db


@dataclass
class Resolution:
    term: str
    ranked: list[Candidate] = field(default_factory=list)
    tie: bool = False
    replacement: str | None = None  # set when the request is a fork of another project
    notes: list[str] = field(default_factory=list)
    hints: list[Candidate] = field(default_factory=list)  # matches in disabled sources
    error: str | None = None
    code: int = 0

    @property
    def best(self) -> Candidate | None:
        return self.ranked[0] if self.ranked else None


class Context:
    """Everything resolution needs: config, live backends, alias DB, Repology."""

    def __init__(self, cfg: dict, backends: dict[str, Backend], native: str | None,
                 db: PackageDB, repology: Repology):
        self.cfg = cfg
        self.backends = backends
        self.native = native
        self.db = db
        self.repology = repology
        self.warnings: list[str] = []  # backend failures seen during resolution, shown as notes

    def concrete(self, name: str) -> str | None:
        """Turn the config's logical 'native' into the detected manager's id."""
        return self.native if name == "native" else name

    def _source_cfg(self, sid: str) -> dict:
        key = "native" if sid == self.native else sid
        return self.cfg["sources"].get(key, {})

    def priority(self, sid: str) -> int:
        return self._source_cfg(sid).get("priority", 99)

    def enabled(self, sid: str) -> bool:
        return sid in self.backends and bool(self._source_cfg(sid).get("enabled"))

    def enabled_ids(self) -> list[str]:
        return sorted((s for s in self.backends if self.enabled(s)), key=self.priority)

    def pin_for(self, term: str) -> str | None:
        pin = self.cfg["pins"].get(term)
        return self.concrete(pin) if pin else None

    def available_ids(self) -> list[str]:
        return sorted(self.backends, key=lambda s: (self.enabled(s) is False, self.priority(s)))

    def names_for(self, lookup: str, sources: list[str]) -> dict[str, list[str]]:
        names: dict[str, list[str]] = {sid: [] for sid in sources}

        def add(sid: str, name: str) -> None:
            if sid in names and name not in names[sid]:
                names[sid].append(name)

        for sid, aliases in self.db.aliases.get(lookup, {}).items():
            for name in aliases:
                add(sid, name)
        for sid, name in self.repology.names(lookup):
            add(sid, name)
        for sid in sources:
            add(sid, lookup)
        return names


def _lookup(lookup: str, ctx: Context, sources: list[str]) -> list[Candidate]:
    names = ctx.names_for(lookup, sources)
    cands: list[Candidate] = []
    seen: set[tuple[str, str]] = set()
    for sid in sources:
        for name in names.get(sid, []):
            try:
                found = ctx.backends[sid].query(name)
            except BackendError as exc:
                ctx.warnings.append(f"{exc} (while looking up '{name}')")
                continue
            for cand in found:
                if (cand.source, cand.ref) not in seen:
                    seen.add((cand.source, cand.ref))
                    cands.append(cand)
    return cands


def resolve(term: str, ctx: Context, source: str | None = None, installed_only: bool = False) -> Resolution:
    res = Resolution(term)
    lookup = term

    rel = ctx.db.relations.get(term)
    if rel and rel.kind == "renamed":
        res.notes.append(f"'{term}' was renamed to '{rel.to}'; using '{rel.to}'")
        lookup = rel.to
    elif rel:
        res.replacement = rel.to
        lookup = rel.to
        res.notes.append(f"'{rel.to}' replaces '{term}' upstream; it is a different project")

    if source is not None:
        if source not in ctx.backends:
            res.error = f"'{source}' is not available on this system"
            res.code = 1
            return res
        sources = [source]  # explicit: exclusive, no fallback, priorities ignored
    else:
        sources = ctx.enabled_ids()
        if not sources:
            res.error = "no enabled sources; check [sources] in the config"
            res.code = 1
            return res

    cands = _lookup(lookup, ctx, sources)
    res.notes.extend(ctx.repology.warnings + ctx.warnings)
    ctx.repology.warnings.clear()
    ctx.warnings.clear()

    if installed_only:
        cands = [c for c in cands if ctx.backends[c.source].installed(c.ref)]

    if source is None and not installed_only:
        # Show where else the name exists, so a disabled source (AUR, snap) is discoverable.
        off = [sid for sid in ctx.available_ids() if not ctx.enabled(sid)]
        res.hints = _lookup(lookup, ctx, off) if off else []

    if not cands:
        where = source or ", ".join(sources)
        verb = "installed" if installed_only else "found"
        res.error = f"'{term}' not {verb} in: {where}"
        res.code = EXIT_NOT_FOUND
        return res

    pin = ctx.pin_for(term)

    def rank(c: Candidate) -> tuple[int, int, int]:
        pinned_first = 0 if (pin and c.source == pin) else 1
        # The exact name you typed wins over same-project siblings (steam over steam-devices).
        exact = 0 if c.ref == lookup else 1
        return (pinned_first, ctx.priority(c.source), exact)

    res.ranked = sorted(cands, key=rank)
    top = rank(res.ranked[0])
    res.tie = sum(1 for c in res.ranked if rank(c) == top) > 1
    if pin and not any(c.source == pin for c in res.ranked):
        res.notes.append(f"pin for '{term}' is '{pin}' but nothing matched there; using the next source")
    return res


def search(term: str, ctx: Context, source: str | None = None) -> list[Candidate]:
    """Raw search across every available source, enabled or not.

    Enabled sources sort by priority; disabled ones come after, so they are
    visible without ever being chosen automatically.
    """
    from rapidfuzz import fuzz

    sources = [source] if source else ctx.available_ids()
    found: list[Candidate] = []
    for sid in sources:
        try:
            found.extend(ctx.backends[sid].search(term))
        except BackendError as exc:
            ctx.warnings.append(str(exc))
    ctx.warnings.clear()  # search reports its own warnings through the caller

    def key(c: Candidate):
        on = ctx.enabled(c.source)
        return (0 if on else 1, ctx.priority(c.source), -fuzz.WRatio(term, c.ref), c.ref)

    return sorted(found, key=key)
