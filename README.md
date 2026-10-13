# uni

A unified wrapper around native package managers and extra sources.

```
uni install discord           # resolves to the right package on this distro
uni search swww               # one table across every enabled source
uni --as apt install discord  # apt syntax, resolved through your hierarchy
uni --as flatpak install com.discordapp.Discord --dry-run
```

Design notes: [UNI_DESIGN.md](UNI_DESIGN.md).

## Install

**Arch Linux** (and derivatives such as Garuda, EndeavourOS, Manjaro): one line, builds and installs a pacman package.

```sh
git clone https://github.com/tak0dan/UNI-pkg-manager && cd UNI-pkg-manager && makepkg -si
```

**Other distributions**: installs into an isolated environment with `pipx`.

```sh
pipx install git+https://github.com/tak0dan/UNI-pkg-manager.git
```

Check it works:

```sh
uni detect
```

Uninstall: `sudo pacman -R uni-pkg-git` on Arch, or `pipx uninstall uni-pkg` otherwise.

## Status

Implemented:

- Backends: pacman, AUR (via paru/yay), apt, dnf, zypper, flatpak, snap.
- uni's own syntax: `install`, `remove`, `search`, `detect`, `log`. Foreign syntaxes: apt,
  dnf, pacman, zypper, flatpak. Unified ops translate; anything else passes straight through.
- Name resolution: bundled and user alias tables, then Repology (cached), then the literal name.
  Every name is verified live against each source.
- Source hierarchy: priorities, per-term pins, `--from SOURCE` or `--<source>` (exclusive, no fallback).
- Sources that are off (AUR, snap by default) are never chosen automatically, but matches are
  shown as hints and in `search`.
- Renames vs forks: `renamed` resolves automatically; `fork` asks first (`--accept-replacement`).
- Safety: refuses `pacman -Sy <pkg>`; optional snapper pre/post snapshots around each transaction.
- Interface: a Textual flow for install and remove (pick on ties, plan review, sudo once, live output).
- Logs: every native query, every resolution and every transaction; each transaction keeps its
  full output in a file.

Not implemented yet: Pacstall, COPR, OBS, PPA/extrepo sources; the pkgmap data source;
fleet dispatch.

## Usage

```
uni install <name>...   [--from SOURCE | --<source>] [--dry-run] [--yes] [--accept-replacement]
uni remove  <name>...   [--from SOURCE | --<source>] [--dry-run] [--yes]
uni search  <term>...   [--from SOURCE | --<source>] [--json]
uni detect              [--json]
uni log                 [-n COUNT]
```

- Syntax: `uni -S steam` and `uni apt install steam` pick the syntax from the first word.
  `--as SYNTAX` forces one; a symlink named `uni-<syntax>` does the same.
- Sources: `--flatpak`, `--aur`, `--snap`, `--pacman`, `--apt`, `--dnf`, `--zypper`, `--native`
  are shorthand for `--from <source>`. `--from` is exclusive: nothing falls back.
- Install and remove open the interface in a terminal. Enter confirms, Esc cancels, `q` closes
  when the run has finished. Native commands run non-interactively after you confirm, except
  the AUR helper, which runs in the terminal so you can review PKGBUILDs. sudo asks for the
  password once, before the run starts.
- Install no longer passes `--needed`, so an explicit install reinstalls.
- `--yes`, pipes, and `--dry-run` skip the interface and use plain output. Native managers
  then ask their own questions.
- `--dry-run` prints the native commands without running them.
- `--json` works with `search`, `detect`, and `install/remove --dry-run`.
- `-v` prints each native query to stderr as it runs.

Logs: `~/.local/state/uni/uni.log` (summary, one JSON object per line) and
`~/.local/state/uni/transactions/<time>-<op>.log` (commands plus full output). `uni log` prints
the summary.

Exit codes: `0` ok, `1` error or cancelled, `2` ambiguous (non-interactive tie), `3` not found,
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
