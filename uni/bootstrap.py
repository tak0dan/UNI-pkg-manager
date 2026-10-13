"""Bootstrap: install a missing native package manager.

Order of preference for each manager:

1. Through another manager that is already on the system (for example
   `choco install winget`). Only offered when that manager is present.
2. From the manager's official source installer, a fixed URL shown to the
   user before anything runs. This is the fallback when step 1 fails.

Bootstrapping downloads and runs installers, so it always asks first (or needs
--yes), and the plan shows exactly what will run.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from .backends import Step

POWERSHELL = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command"]

WINGET_SOURCE_URL = "https://aka.ms/getwinget"
CHOCO_SOURCE_URL = "https://community.chocolatey.org/install.ps1"
SCOOP_SOURCE_URL = "https://get.scoop.sh"

# Windows managers that can be installed from a source installer.
WINDOWS_TARGETS = ("winget", "choco", "scoop")
# Linux native managers. Missing ones can only be restored by the distribution, so they are manual.
LINUX_TARGETS = ("pacman", "apt", "dnf", "zypper")
TARGETS = WINDOWS_TARGETS + LINUX_TARGETS

LINUX_MANUAL = {
    "pacman": ("Arch-based system without pacman. pacman is part of the base system: restore it from an "
               "Arch Linux live ISO (pacstrap / arch-chroot) or your distribution's recovery media."),
    "apt": ("Debian-based system without apt. apt is part of the base system: restore it from the "
            "distribution's installer or recovery media."),
    "dnf": ("Fedora-based system without dnf. dnf is part of the base system: reinstall it from the "
            "distribution's recovery media."),
    "zypper": ("openSUSE system without zypper. zypper is part of the base system: restore it from the "
               "openSUSE installer or rescue system."),
}


@dataclass
class Recipe:
    target: str
    steps: list[Step]  # the first step, with its fallbacks; run with execute()
    description: list[str]  # what will happen, for the user to read before confirming
    manual: bool = False  # True: uni cannot do this unattended; the description is the instruction


def _powershell(script: str, label: str) -> Step:
    return Step([*POWERSHELL, script], root=False, label=label)


def recipe(target: str, present: set[str]) -> Recipe | None:
    """How to install `target`, given the managers already present (backend ids)."""
    if target in LINUX_TARGETS:
        return Recipe(target, [], [f"{target} is missing.", LINUX_MANUAL[target]], manual=True)
    if target == "winget":
        source = _powershell(
            "Invoke-WebRequest -Uri https://aka.ms/getwinget -OutFile $env:TEMP\\winget.msixbundle; "
            "Add-AppxPackage $env:TEMP\\winget.msixbundle",
            f"from source: {WINGET_SOURCE_URL}",
        )
        via = ("choco", ["choco", "install", "winget", "-y"], "choco install winget")
    elif target == "choco":
        source = _powershell(
            "Set-ExecutionPolicy Bypass -Scope Process -Force; "
            "[System.Net.ServicePointManager]::SecurityProtocol = 3072; "
            f"iex ((New-Object System.Net.WebClient).DownloadString('{CHOCO_SOURCE_URL}'))",
            f"from source: {CHOCO_SOURCE_URL}",
        )
        via = ("winget", ["winget", "install", "--id", "Chocolatey.Chocolatey", "--exact", "--silent",
                          "--accept-package-agreements", "--accept-source-agreements"],
               "winget install Chocolatey.Chocolatey")
    elif target == "scoop":
        source = _powershell(
            f"Set-ExecutionPolicy RemoteSigned -Scope CurrentUser -Force; irm {SCOOP_SOURCE_URL} | iex",
            f"from source: {SCOOP_SOURCE_URL}",
        )
        via = ("choco", ["choco", "install", "scoop", "-y"], "choco install scoop")
    else:
        return None

    via_backend, via_argv, via_text = via
    description = [f"{target} is not installed."]
    if via_backend in present:
        first = Step(via_argv, root=False, label=via_text, fallback=[source])
        description.append(f"Try: {via_text} (through {via_backend}).")
        description.append(f"If that fails: {source.label}.")
        return Recipe(target, [first], description)
    description.append(f"Install it: {source.label}.")
    return Recipe(target, [source], description)


def describe_steps(steps: list[Step]) -> list[str]:
    lines = []
    for step in steps:
        lines.append("  $ " + " ".join(step.argv[:6]) + (" ..." if len(step.argv) > 6 else ""))
        for fb in step.fallback:
            lines.append("  or $ " + " ".join(fb.argv[:6]) + (" ..." if len(fb.argv) > 6 else ""))
    return lines


def default_target(family: str | None) -> str | None:
    """The manager to restore when none is present: winget on Windows, the distro's own elsewhere."""
    if sys.platform == "win32":
        return "winget"
    return family if family in LINUX_TARGETS else None
