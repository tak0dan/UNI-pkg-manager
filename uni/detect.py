"""Host detection: distro family, native package manager, available backends."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

OS_RELEASE = Path("/etc/os-release")

# Distro family ids (ID / ID_LIKE) -> native backend. Checked in this order.
FAMILY = {
    "pacman": ("arch",),
    "apt": ("debian", "ubuntu"),
    "dnf": ("fedora", "rhel", "centos"),
    "zypper": ("suse", "sles", "opensuse"),
}


def read_os_release(path: Path = OS_RELEASE) -> dict[str, str]:
    data: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return data
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def native_backend(os_release: dict[str, str], available: set[str]) -> str | None:
    idents = [os_release.get("ID", "")] + os_release.get("ID_LIKE", "").split()
    for ident in idents:
        for backend, families in FAMILY.items():
            matches = ident in families or (backend == "zypper" and ident.startswith("opensuse"))
            if matches and backend in available:
                return backend
    # Unknown distro: fall back to whichever native manager is installed.
    for backend in ("pacman", "apt", "dnf", "zypper"):
        if backend in available:
            return backend
    return None


@dataclass
class Host:
    os_id: str
    os_like: str
    pretty: str
    native: str | None
    available: list[str] = field(default_factory=list)


def probe(available: set[str], os_release: dict[str, str] | None = None) -> Host:
    rel = read_os_release() if os_release is None else os_release
    return Host(
        os_id=rel.get("ID", "unknown"),
        os_like=rel.get("ID_LIKE", ""),
        pretty=rel.get("PRETTY_NAME", rel.get("NAME", "unknown")),
        native=native_backend(rel, available),
        available=sorted(available),
    )
