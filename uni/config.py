"""Configuration: built-in defaults, deep-merged with the user's config.toml."""

from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path

SYNTAXES = ("uni", "apt", "dnf", "pacman", "zypper", "flatpak")
ROUTABLE = tuple(s for s in SYNTAXES if s != "uni")
MODES = ("translate", "route")

DEFAULTS: dict = {
    "sources": {
        "native": {"enabled": True, "priority": 1},
        "flatpak": {"enabled": True, "priority": 2},
        "snap": {"enabled": False, "priority": 3},
        "nix": {"enabled": False, "priority": 5},
        "aur": {"enabled": False, "priority": 4},
    },
    "pins": {},
    "syntax": {"mode": "translate", "route": [], "notice": True},
    "safety": {"snapper": True},
    "resolver": {"repology": True, "cache_hours": 24},
    "general": {"sudo": "sudo", "log": True},
}


class ConfigError(Exception):
    pass


def _xdg(var: str, fallback: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / fallback)


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "uni"


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / "uni"


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache") / "uni"


def default_config_path() -> Path:
    return Path(os.environ.get("UNI_CONFIG") or config_dir() / "config.toml")


TEMPLATE = Path(__file__).parent / "data" / "config.toml"


def ensure_user_config() -> Path | None:
    """Write the commented default config on first run. Returns the path if it was created.

    Skipped when UNI_CONFIG points elsewhere, and never overwrites an existing file.
    """
    if os.environ.get("UNI_CONFIG"):
        return None
    path = config_dir() / "config.toml"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "x", encoding="utf-8") as fh:
            fh.write(TEMPLATE.read_text(encoding="utf-8"))
    except FileExistsError:
        return None
    except OSError:
        return None  # read-only home etc.: uni still runs on built-in defaults
    return path


def _deep_merge(base: dict, over: dict) -> None:
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def validate(cfg: dict) -> None:
    for name, src in cfg["sources"].items():
        if not isinstance(src, dict):
            raise ConfigError(f"[sources.{name}] must be a table")
        if not isinstance(src.get("priority"), int):
            raise ConfigError(f"[sources.{name}] needs an integer priority")
        if not isinstance(src.get("enabled"), bool):
            raise ConfigError(f"[sources.{name}] needs enabled = true/false")
    for term, source in cfg["pins"].items():
        if not isinstance(source, str):
            raise ConfigError(f"[pins] {term} must name a source")
    syntax = cfg["syntax"]
    if syntax["mode"] not in MODES:
        raise ConfigError(f"[syntax] mode must be one of {', '.join(MODES)}")
    for name in syntax["route"]:
        if name not in ROUTABLE:
            raise ConfigError(f"[syntax] route: unknown syntax '{name}'")
    if not isinstance(syntax["notice"], bool):
        raise ConfigError("[syntax] notice must be true or false")


def load(path: Path | None = None) -> dict:
    path = path or default_config_path()
    cfg = copy.deepcopy(DEFAULTS)
    if path.exists():
        try:
            user = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc
        _deep_merge(cfg, user)
    validate(cfg)
    return cfg
