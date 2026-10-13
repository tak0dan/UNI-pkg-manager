"""Native flag capabilities: which backend can express which flag, and why it matters.

A request that carries a native flag (pacman's -s, apt's purge, ...) is only valid
on a backend that has an equivalent. The planner checks this before anything runs,
and refuses with the reason, instead of dropping the flag or guessing.

Canonical names are shared across syntaxes; each backend declares its own
`remove_flags` (see backends.py).
"""

from __future__ import annotations

from .backends import Backend

# Canonical flag -> what the user asked for, in plain words.
MEANINGS = {
    "cascade": "-s: also remove dependencies nothing else needs",
    "nosave": "-n: remove configuration files as well",
    "nodeps": "-d: skip dependency checks",
    "nocheck": "-dd: skip all checks",
    "purge": "purge: remove configuration files as well",
}

# Letters a pacman remove may carry. Anything else is a manager-specific operation.
PACMAN_REMOVE_LETTERS = {"s": "cascade", "n": "nosave", "d": "nodeps"}


def unsupported(backend: Backend, flags: frozenset[str]) -> list[str]:
    """Flags in `flags` that `backend` cannot express, as explanations."""
    return [
        f"'{MEANINGS.get(flag, flag)}' has no equivalent in {backend.id}"
        for flag in sorted(flags)
        if flag not in backend.remove_flags
    ]
