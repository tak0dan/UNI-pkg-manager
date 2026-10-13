# uni

A unified wrapper around native package managers and extra sources.

```
uni install discord           # resolves to the right package on this distro
uni search swww               # one table across every enabled source
uni --as apt install discord  # apt syntax, resolved through your hierarchy
uni --as flatpak install com.discordapp.Discord --dry-run
```

Design notes: [UNI_DESIGN.md](UNI_DESIGN.md).

## Status

Implemented (MVP from the design):

- Backends: pacman, AUR (via paru/yay, disabled by default), apt, dnf, zypper, flatpak, snap.
- uni's own syntax: `install`, `remove`, `search`, `detect`.
- Foreign syntaxes: apt, dnf, pacman, zypper, flatpak. Unified ops translate; anything
  else passes straight through to the native manager.
- Name resolution: bundled and user alias tables, then Repology (cached), then the literal name.
  Every name is verified live against each source.
- Source hierarchy: priorities, per-term pins, `--from SOURCE` (exclusive, no fallback).
- Renames vs forks: `renamed` resolves automatically; `fork` asks first (`--accept-replacement`).
- Safety: refuses `pacman -S…` forms that refresh without upgrading (`-Sy <pkg>`); optional
  snapper pre/post snapshots around each transaction.
- Route and translate modes; the "Understood as" notice.
- Textual picker shown only on real ties; `--yes`, non-TTY, and `--json` never prompt.

Not implemented yet: Pacstall, COPR, OBS, PPA/extrepo sources; the pkgmap data source;
fleet dispatch.

## Usage

```
uni install <name>...   [--from SOURCE] [--dry-run] [--yes] [--accept-replacement]
uni remove  <name>...   [--from SOURCE] [--dry-run] [--yes]
uni search  <term>...   [--from SOURCE] [--json]
uni detect              [--json]
```

- `--as SYNTAX` picks the input syntax (`uni`, `apt`, `dnf`, `pacman`, `zypper`, `flatpak`).
  A symlink named `uni-<syntax>` does the same.
- `--dry-run` prints the native commands without running them.
- `--json` works with `search`, `detect`, and `install/remove --dry-run`.
- With `--from`, a configured source name or a backend id is accepted (`native` means
  the detected native manager).

Exit codes: `0` ok, `1` error, `2` ambiguous (non-interactive tie), `3` not found,
`4` refused (fork not confirmed, unsafe command).

## Configuration

`~/.config/uni/config.toml` (or `$UNI_CONFIG`, or `--config PATH`). Defaults are shown.

```toml
[sources]
native  = { enabled = true,  priority = 1 }
flatpak = { enabled = true,  priority = 2 }
snap    = { enabled = false, priority = 3 }
aur     = { enabled = false, priority = 4, helper = "paru" }

[pins]
discord = "flatpak"

[syntax]
mode   = "translate"           # or "route"
route  = []                    # e.g. ["flatpak"]: flatpak commands go to flatpak only
notice = true                  # show "Understood as: ..." for translated commands

[safety]
snapper = true                 # used only when the snapper binary exists

[resolver]
repology   = true
cache_hours = 24

[general]
sudo = "sudo"
log  = true                    # transaction log: ~/.local/state/uni/uni.log
```

Lower priority number = preferred. Equal priority across candidates is a tie: uni asks
(interactively) or fails with exit code 2.

### Names and renames

Name mappings live in `~/.config/uni/packages.toml` and override the bundled table:

```toml
[packages.discord]
pacman  = ["discord"]
aur     = ["discord-git"]
dnf     = ["discord"]
flatpak = ["com.discordapp.Discord"]

[relations.swww]
to   = "awww"
kind = "fork"   # or "renamed"
```

## Modes

- **Translate** (default): any syntax becomes an intent and goes through the hierarchy.
  `flatpak install discord` on Arch installs the pacman package, and prints the notice.
- **Route**: a syntax in `route = [...]` only searches its own source, still with uni's
  resolver. `uni --as flatpak install x` in route mode only looks at flatpak.

## Development

```
python3 -m unittest discover -s tests -v
```

Tests cover the frontends, resolver ranking and ties, the safety guard, backend parsers
(with fixtures), the Repology cache, config validation, the live pacman queries, and the
Textual picker. apt, dnf, zypper, and snap parsers are tested against fixtures only: those
managers are not installed on the development machine.
