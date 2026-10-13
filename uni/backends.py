"""Backends: one per native package manager or extra source.

A backend answers questions (query, search, installed) and turns resolved
candidates into native commands (install, remove). It never runs a transaction
itself; that is transaction.py's job. Whether a native flag can be expressed by
a backend is decided by capabilities.py, which the planner consults.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from .model import Candidate

AUR_RPC = "https://aur.archlinux.org/rpc/v5"


class BackendError(Exception):
    """A backend could not do what was asked. Always names the backend."""

    def __init__(self, backend: str, message: str):
        super().__init__(f"{backend}: {message}")
        self.backend = backend
        self.message = message


@dataclass
class Step:
    """One native command to run.

    root: needs privilege escalation.
    interactive: the helper prompts or reviews on its own, so it runs with the
    terminal handed over rather than inside uni's log view.
    """

    argv: list[str]
    root: bool = False
    label: str = ""
    interactive: bool = False


# Set by the CLI so every query is traced (argv, exit code, duration).
TRACE: Callable[[list[str], int, float], None] | None = None


def run(argv: list[str], timeout: int = 60) -> tuple[int, str]:
    """Run a query command with C locale so parsing is predictable."""
    env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, env=env, check=False
        )
        rc, out = proc.returncode, proc.stdout
    except (OSError, subprocess.TimeoutExpired):
        rc, out = 127, ""
    if TRACE is not None:
        TRACE(argv, rc, time.monotonic() - started)
    return rc, out


def _fields(text: str) -> dict[str, str]:
    """Parse 'Key : value' blocks (pacman, apt, zypper info)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line and not line[:1].isspace():
            key, _, value = line.partition(":")
            out.setdefault(key.strip(), value.strip())
    return out


def _yes(flag: str, enabled: bool) -> list[str]:
    return [flag] if enabled else []


class Backend:
    id = ""
    binary = ""
    native = False  # True for the distro's own manager
    needs_root = False
    # Native remove flags this backend can express (canonical names, see capabilities.py).
    remove_flags: frozenset[str] = frozenset()

    def __init__(self, options: dict | None = None):
        self.options = options or {}

    def available(self) -> bool:
        return shutil.which(self.binary) is not None

    def query(self, name: str) -> list[Candidate]:
        """Exact-name lookup. Used for live verification of resolved names."""
        raise NotImplementedError

    def search(self, term: str) -> list[Candidate]:
        raise NotImplementedError

    def installed(self, ref: str) -> bool:
        raise NotImplementedError

    def install(self, cands: list[Candidate], yes: bool) -> list[Step]:
        raise NotImplementedError

    def remove(self, cands: list[Candidate], yes: bool, flags: frozenset[str] = frozenset()) -> list[Step]:
        raise NotImplementedError


class PacmanBackend(Backend):
    id = "pacman"
    binary = "pacman"
    native = True
    needs_root = True
    remove_flags = frozenset({"cascade", "nosave", "nodeps", "nocheck"})

    def query(self, name):
        rc, out = run(["pacman", "-Si", name])
        if rc:
            return []
        info = _fields(out)
        if info.get("Name") != name:
            return []
        return [Candidate(self.id, name, info.get("Version", ""), info.get("Description", ""))]

    def search(self, term):
        _, out = run(["pacman", "-Ss", term])
        lines = out.splitlines()
        results = []
        for i, line in enumerate(lines):
            if not line or line[0].isspace() or "/" not in line:
                continue
            head = line.split()
            name = head[0].split("/", 1)[1]
            version = head[1] if len(head) > 1 else ""
            desc = lines[i + 1].strip() if i + 1 < len(lines) and lines[i + 1][:1].isspace() else ""
            results.append(Candidate(self.id, name, version, desc))
        return results

    def installed(self, ref):
        return run(["pacman", "-Q", ref])[0] == 0

    def install(self, cands, yes):
        # No --needed: an explicit install reinstalls, as the user asked.
        refs = [c.ref for c in cands]
        return [Step(["pacman", "-S", *_yes("--noconfirm", yes), *refs], True)]

    def remove(self, cands, yes, flags=frozenset()):
        refs = [c.ref for c in cands]
        letters = ("n" if "nosave" in flags else "") + ("s" if "cascade" in flags else "")
        if "nocheck" in flags:
            letters += "dd"
        elif "nodeps" in flags:
            letters += "d"
        op = "-R" + letters
        return [Step(["pacman", op, *_yes("--noconfirm", yes), *refs], True)]


class AurBackend(Backend):
    """AUR: queried over the RPC JSON API, installed through a helper (paru or yay).

    `aur` uses whichever helper is configured or installed; `paru` and `yay` pin one.
    All three are pacman-based once installed, so removal goes through pacman.
    """

    id = "aur"
    needs_root = False
    remove_flags = PacmanBackend.remove_flags

    def __init__(self, options=None, source_id: str = "aur", helper: str | None = None):
        super().__init__(options)
        self.id = source_id
        self.binary = helper or self.options.get("helper", "paru")

    def _rpc(self, endpoint: str, params: list[tuple[str, str]]) -> list[dict]:
        url = f"{AUR_RPC}/{endpoint}?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                return json.load(resp).get("results", [])
        except (OSError, ValueError):
            return []

    def _to_cand(self, r: dict) -> Candidate:
        return Candidate(self.id, r["Name"], r.get("Version", ""), r.get("Description") or "")

    def query(self, name):
        rows = self._rpc("info", [("arg[]", name)])
        return [self._to_cand(r) for r in rows if r.get("Name") == name]

    def search(self, term):
        rows = self._rpc(f"search/{urllib.parse.quote(term)}", [("by", "name-desc")])
        return [self._to_cand(r) for r in rows]

    def installed(self, ref):
        return run(["pacman", "-Q", ref])[0] == 0

    def install(self, cands, yes):
        # The helper reviews PKGBUILDs and asks questions, so it always runs interactively.
        return [Step([self.binary, "-S", *[c.ref for c in cands]], interactive=True)]

    def remove(self, cands, yes, flags=frozenset()):
        return PacmanBackend().remove(cands, yes, flags)


class ParuBackend(AurBackend):
    id = "paru"  # the registry is keyed by class id, so each helper declares its own
    binary = "paru"

    def __init__(self, options=None):
        super().__init__(options, source_id="paru", helper="paru")


class YayBackend(AurBackend):
    id = "yay"
    binary = "yay"

    def __init__(self, options=None):
        super().__init__(options, source_id="yay", helper="yay")


class AptBackend(Backend):
    id = "apt"
    binary = "apt"
    native = True
    needs_root = True
    remove_flags = frozenset({"purge"})

    def query(self, name):
        rc, out = run(["apt-cache", "show", "--no-all-versions", name])
        if rc:
            return []
        info = _fields(out)
        if info.get("Package") != name:
            return []
        return [Candidate(self.id, name, info.get("Version", ""), info.get("Description", ""))]

    def search(self, term):
        _, out = run(["apt-cache", "search", term])
        results = []
        for line in out.splitlines():
            name, sep, desc = line.partition(" - ")
            if sep:
                results.append(Candidate(self.id, name.strip(), "", desc.strip()))
        return results

    def installed(self, ref):
        rc, out = run(["dpkg-query", "-W", "-f=${Status}", ref])
        return rc == 0 and "install ok installed" in out

    def install(self, cands, yes):
        return [Step(["apt", "install", *_yes("-y", yes), *[c.ref for c in cands]], True)]

    def remove(self, cands, yes, flags=frozenset()):
        verb = "purge" if "purge" in flags else "remove"
        return [Step(["apt", verb, *_yes("-y", yes), *[c.ref for c in cands]], True)]


class DnfBackend(Backend):
    id = "dnf"
    binary = "dnf"
    native = True
    needs_root = True

    def query(self, name):
        rc, out = run(["dnf", "-q", "repoquery", "--qf", "%{name}\t%{version}-%{release}\t%{summary}", name])
        if rc:
            return []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) == 3 and parts[0] == name:
                return [Candidate(self.id, name, parts[1], parts[2])]
        return []

    def search(self, term):
        _, out = run(["dnf", "-q", "search", term])
        results = []
        for line in out.splitlines():
            left, sep, desc = line.partition(" : ")
            if sep and "." in left:
                results.append(Candidate(self.id, left.rsplit(".", 1)[0], "", desc.strip()))
        return results

    def installed(self, ref):
        return run(["rpm", "-q", ref])[0] == 0

    def install(self, cands, yes):
        return [Step(["dnf", "install", *_yes("-y", yes), *[c.ref for c in cands]], True)]

    def remove(self, cands, yes, flags=frozenset()):
        return [Step(["dnf", "remove", *_yes("-y", yes), *[c.ref for c in cands]], True)]


class ZypperBackend(Backend):
    id = "zypper"
    binary = "zypper"
    native = True
    needs_root = True

    def query(self, name):
        rc, out = run(["zypper", "--non-interactive", "info", name])
        if rc:
            return []
        info = _fields(out)
        if info.get("Name") != name:
            return []
        return [Candidate(self.id, name, info.get("Version", ""), info.get("Summary", ""))]

    def search(self, term):
        _, out = run(["zypper", "--non-interactive", "search", term])
        results = []
        for line in out.splitlines():
            parts = [p.strip() for p in line.split("|")]
            # Columns: S | Name | Summary | Type
            if len(parts) >= 4 and parts[1] not in ("", "Name") and parts[3] == "package":
                results.append(Candidate(self.id, parts[1], "", parts[2]))
        return results

    def installed(self, ref):
        return run(["rpm", "-q", ref])[0] == 0

    def install(self, cands, yes):
        flags = ["--non-interactive"] if yes else []
        return [Step(["zypper", *flags, "install", *[c.ref for c in cands]], True)]

    def remove(self, cands, yes, flags=frozenset()):
        flags_ = ["--non-interactive"] if yes else []
        return [Step(["zypper", *flags_, "remove", *[c.ref for c in cands]], True)]


class FlatpakBackend(Backend):
    id = "flatpak"
    binary = "flatpak"
    native = False
    needs_root = False

    def _rows(self, term: str) -> list[Candidate]:
        _, out = run(["flatpak", "search", "--columns=application,version,description,remotes", term])
        results = []
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 4 or parts[0].strip() in ("", "Application ID"):
                continue
            remote = parts[3].split(",")[0].strip() or None
            results.append(Candidate(self.id, parts[0].strip(), parts[1].strip(), parts[2].strip(), remote))
        return results

    def query(self, name):
        return [c for c in self._rows(name) if c.ref == name]

    def search(self, term):
        return self._rows(term)

    def installed(self, ref):
        return run(["flatpak", "info", ref])[0] == 0

    def install(self, cands, yes):
        # One command per remote, because `flatpak install <remote> <ref>` takes one remote.
        by_remote: dict[str | None, list[str]] = {}
        for c in cands:
            by_remote.setdefault(c.remote, []).append(c.ref)
        steps = []
        for remote, refs in by_remote.items():
            remote_arg = [remote] if remote else []
            steps.append(Step(["flatpak", "install", *_yes("-y", yes), *remote_arg, *refs]))
        return steps

    def remove(self, cands, yes, flags=frozenset()):
        return [Step(["flatpak", "uninstall", *_yes("-y", yes), *[c.ref for c in cands]])]


class SnapBackend(Backend):
    id = "snap"
    binary = "snap"
    native = False
    needs_root = True

    def query(self, name):
        rc, out = run(["snap", "info", name])
        if rc:
            return []
        info = _fields(out)
        version = ""
        for line in out.splitlines():
            if line.strip().startswith("latest/stable:"):
                parts = line.split()
                version = parts[1] if len(parts) > 1 else ""
                break
        return [Candidate(self.id, name, version, info.get("summary", ""))]

    def search(self, term):
        _, out = run(["snap", "find", term])
        results = []
        for line in out.splitlines()[1:]:
            parts = line.split(None, 4)
            if len(parts) >= 2:
                desc = parts[4] if len(parts) > 4 else ""
                results.append(Candidate(self.id, parts[0], parts[1], desc))
        return results

    def installed(self, ref):
        return run(["snap", "list", ref])[0] == 0

    def install(self, cands, yes):
        return [Step(["snap", "install", *[c.ref for c in cands]], True)]

    def remove(self, cands, yes, flags=frozenset()):
        return [Step(["snap", "remove", *[c.ref for c in cands]], True)]


NIX_FLAGS = ["--extra-experimental-features", "nix-command flakes"]


class NixBackend(Backend):
    """Nix: imperative profile operations through `nix profile` and `nix search`.

    Packages are addressed as nixpkgs#<attribute>. Needs flakes enabled, which the
    commands request explicitly. Not yet tested against a live Nix installation.
    """

    id = "nix"
    binary = "nix"
    native = False
    needs_root = False

    def _rows(self, term: str) -> list[Candidate]:
        rc, out = run(["nix", *NIX_FLAGS, "search", "--json", "nixpkgs", term])
        if rc or not out.strip():
            return []
        try:
            data = json.loads(out)
        except ValueError:
            raise BackendError(self.id, "could not read `nix search` output")
        results = []
        for key, info in data.items():
            # keys look like legacyPackages.x86_64-linux.<attribute>
            attr = key.split(".", 2)[2] if key.count(".") >= 2 else key
            results.append(Candidate(self.id, attr, info.get("version", ""), info.get("description", "")))
        return results

    def query(self, name):
        return [c for c in self._rows(name) if c.ref == name]

    def search(self, term):
        return self._rows(term)

    def installed(self, ref):
        rc, out = run(["nix", *NIX_FLAGS, "profile", "list"])
        return rc == 0 and any(ref in line.split() for line in out.splitlines())

    def install(self, cands, yes):
        return [Step(["nix", *NIX_FLAGS, "profile", "install", f"nixpkgs#{c.ref}"]) for c in cands]

    def remove(self, cands, yes, flags=frozenset()):
        return [Step(["nix", *NIX_FLAGS, "profile", "remove", c.ref]) for c in cands]


REGISTRY: dict[str, type[Backend]] = {
    cls.id: cls
    for cls in (PacmanBackend, AptBackend, DnfBackend, ZypperBackend, FlatpakBackend, SnapBackend,
                NixBackend, AurBackend, ParuBackend, YayBackend)
}


def make_backends(cfg: dict) -> dict[str, Backend]:
    """Instantiate every backend whose binary exists on this host.

    `aur` runs through the helper named in its config, falling back to the first helper found.
    """
    found: dict[str, Backend] = {}
    for bid, cls in REGISTRY.items():
        options = cfg["sources"].get(bid, {})
        if bid == "aur":
            helper = options.get("helper") or next(
                (h for h in ("paru", "yay") if shutil.which(h)), None)
            if helper is None:
                continue  # no AUR helper: the aur source is unavailable
            backend = AurBackend(options, source_id="aur", helper=helper)
        else:
            backend = cls(options)
        if backend.available():
            found[bid] = backend
    return found
