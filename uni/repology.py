"""Repology: maps an upstream project to its per-repository package names.

Used as a data backbone for name resolution. Results are cached on disk so a
search does not hit the network every time. Any network failure degrades to
"no extra names" and records a warning; resolution then falls back to the
local alias table and the literal name.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://repology.org/api/v1/project/"
USER_AGENT = "uni-pkg-manager/0.1 (+https://github.com/tak0dan/UNI-pkg-manager)"


def repo_to_backend(repo: str) -> str | None:
    """Map a Repology repository name to a uni backend id."""
    if repo == "arch":
        return "pacman"
    if repo == "aur":
        return "aur"
    if repo.startswith(("debian_", "ubuntu_")):
        return "apt"
    if repo.startswith("fedora"):
        return "dnf"
    if repo.startswith("opensuse"):
        return "zypper"
    if repo == "flathub":
        return "flatpak"
    if repo == "snap":
        return "snap"
    return None


def _entries_from_api(payload: list[dict]) -> list[list[str]]:
    """Flatten Repology packages to (backend, binary name) pairs, one per binary."""
    seen: set[tuple[str, str]] = set()
    out: list[list[str]] = []
    for pkg in payload:
        backend = repo_to_backend(pkg.get("repo", ""))
        names = pkg.get("binnames") or [pkg.get("visiblename") or pkg.get("srcname")]
        for name in names:
            if backend and name and (backend, name) not in seen:
                seen.add((backend, name))
                out.append([backend, name])
    return out


class Repology:
    def __init__(self, enabled: bool, cache: Path, ttl_hours: float, fetch=None):
        self.enabled = enabled
        self.cache = cache
        self.ttl = ttl_hours * 3600
        self.warnings: list[str] = []
        self._fetch = fetch or self._http_fetch

    def _http_fetch(self, project: str) -> list[dict]:
        req = urllib.request.Request(API + urllib.parse.quote(project), headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp)

    def names(self, project: str) -> list[tuple[str, str]]:
        """Return (backend, package name) pairs for an upstream project."""
        if not self.enabled:
            return []
        path = self.cache / f"{project}.json"
        cached = None
        if path.exists():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                cached = None
        if cached and time.time() - cached.get("fetched", 0) < self.ttl:
            return [tuple(e) for e in cached["entries"]]
        try:
            entries = _entries_from_api(self._fetch(project))
        except (OSError, ValueError) as exc:
            self.warnings.append(f"repology lookup for '{project}' failed: {exc}")
            return [tuple(e) for e in cached["entries"]] if cached else []
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"fetched": time.time(), "entries": entries}), encoding="utf-8")
        except OSError:
            pass  # caching is an optimisation only
        return [tuple(e) for e in entries]
