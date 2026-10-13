# uni — Unified Package Manager Wrapper

Design notes for `uni`: a cross-distro wrapper that unifies package management
across imperative Linux systems (Debian/Ubuntu, Fedora, Arch, openSUSE, …) and
extra sources (Flatpak, Snap, AUR, Pacstall, COPR, OBS).

Status: design phase. May live as a package inside nixorcist-py, sharing its
search/resolver engine, with Nix as one more backend.

## Goals

- **Same command, same output everywhere.** `uni install discord` behaves and
  looks the same on Fedora, Arch, Debian, openSUSE.
- **Name resolution.** Common names map to the correct per-distro package
  (`discord` → `discord` / `discord-bin` / `com.discordapp.Discord` / …).
- **Source hierarchy.** User decides where packages come from (native, Flatpak,
  Snap, user repos) via priorities, pins, and per-command overrides.
- **Two ways to use it:** a unified syntax for newcomers and mixed fleets, or
  native syntaxes (pacman, apt, dnf, zypper, flatpak…) routed through uni for
  experienced users.
- **Fleet-friendly.** Works across clusters of hosts with different bases.

## Prior art

- **rhino-pkg (rpk)**: Rhino Linux's wrapper for APT, Pacstall, Flatpak, Snap.
  Lists all matches with a number and the source, user picks by number.
  Reference for the result view. https://github.com/rhino-linux/rhino-pkg
- **pacapt / pacaptr**: pacman-style syntax mapped onto other managers.
- **upt** (sigoden/upt): accepts several managers' syntaxes; closest to the
  translation-layer idea.
- **Repology**: cross-repo project → package-name mapping. Data backbone.
- **pkgmap** (github.com/Levizor/pkgmap): cross-distro package name mapper.

uni's difference: syntax translation + name resolution + source hierarchy +
fleet use, combined.

## Architecture

```
argv ──► syntax frontend ──► Intent ──► resolver ──► backend(s) ──► native exec
         (uni/apt/dnf/        {op, pkgs,  (aliases,     (apt, dnf,
          pacman/zypper/       flags,      hierarchy,    pacman, zypper,
          flatpak/snap)        source?}    live check)   flatpak, snap,
                                                         aur, pacstall,
                                                         copr, obs, nix)
```

### 1. Detection

- Read `ID` and `ID_LIKE` from `/etc/os-release`.
- Probe available managers and sources: native manager, AUR helper
  (paru/yay), pacstall, flatpak (+ configured remotes), snap, nix.
- Cache the result; refresh on demand.

### 2. Syntax frontends → Intent

Each supported syntax has a parser producing one internal object:

```python
Intent(op="install", pkgs=["discord"], flags={...}, source=None)
```

- Flags with no equivalent in the target backend → **warn**, never guess.
- Select syntax with `uni --as zypper in foo` or symlinks (`uni-pacman`,
  `uni-zypper`, …) via argv[0].
- **Manager-specific commands** with no unified meaning (`flatpak remote-add`,
  `flatpak override`, `pacman -Qdt`, …) always pass straight through to that
  manager, whatever the mode.

### 3. Resolver

1. Look up the common name in: local alias table → pkgmap → Repology.
2. Confirm candidates with live queries per source:
   `apt-cache policy`, `dnf repoquery`, `pacman -Si` / AUR RPC,
   `zypper info`, `flatpak search`, `snap info`, pacstall search.
3. Rank by source priority (see config), then pins.
4. Equal priority → ask the user, never pick silently.

**Renames and forks** (e.g. swww → awww) need explicit semantics in the alias
table:
- `renamed_to`: same project, new name → may install automatically.
- `replaced_by` / `fork_of`: different project → always confirm.

**Discord case:** not in Debian repos at all; Fedora via RPM Fusion or Flathub;
Arch via repo/AUR. Resolution ranks *sources*, not just names.

### 4. Backends

- Prefer bindings / machine-readable queries for search and info, so output can
  be rendered uniformly: `python-apt`, `libdnf5`, `pyalpm`, AUR RPC JSON.
- Let the actual transaction run natively (sudo, progress, hooks).
- **Arch safety:** never emit `-Sy <pkg>` without `-u` (no partial upgrades).

## Source hierarchy & config

```toml
[sources]
native   = { enabled = true,  priority = 1 }
flatpak  = { enabled = true,  priority = 2 }
snap     = { enabled = false, priority = 3 }
aur      = { enabled = true,  priority = 4, helper = "paru" }
pacstall = { enabled = false, priority = 5 }
copr     = { enabled = false, priority = 6 }

[pins]
discord = "flatpak"

[syntax]
mode   = "translate"           # default mode: "translate" or "route"
route  = ["flatpak", "pacman"] # syntaxes that route to their own manager
notice = true                  # show the "understood as…" notice

[safety]
snapper = true                 # pre/post btrfs snapshots around transactions
```

- **Priority:** lower number = preferred.
- **Pins:** per-package source override (prefer native in general, but this app
  from Flatpak).
- **User repos** (AUR, Pacstall, COPR, OBS home repos, PPAs/extrepo) are
  disabled by default: highest trust risk.
- **Snapper integration** (optional): pre/post snapshots around each uni
  transaction on btrfs systems.

### Per-command override

```
uni install discord --from snap
```

Searches only that source and ignores priority. If nothing matches, **fail**,
don't fall back: the user asked for that source explicitly.

## Syntax modes

### Translate (unify the commands)

Syntax is just syntax. Any supported syntax becomes an Intent and goes through
the hierarchy.

Example: `flatpak install discord` with native at priority 1 → resolves to the
native package. First time, uni shows a notice:

> Understood as: install discord → resolved to **native** (your priority:
> native 1, flatpak 2). Change priorities in the config. [don't show again]

For newcomers and mixed fleets: same commands, same output, everywhere.

### Route (unify the execution)

The syntax picks the source: flatpak commands → Flatpak, pacman commands →
pacman, still using uni's name resolver, TUI, and logging.

For experienced users who manage 3–5 package managers with their native
commands through one tool.

Modes can be mixed per syntax via `route = [...]`.

## TUI

Inspired by rhino-pkg: a dynamic terminal UI rather than line-by-line output.

- **Textual** (Rich team) for panes, live-filtering lists, keybindings.
- **rapidfuzz** for fzf-style fuzzy ranking.
- Result row: name · source · priority rank · version · description.
- Preview pane shows the **exact native command(s)** that will run.
- Pre-select per hierarchy; prompt only on real ties
  (vs. rpk, which asks every time).
- **Non-interactive path:** when stdout isn't a TTY, or with `--yes` /
  `--json`, skip the TUI and print plain or JSON output. Required for scripts
  and fleets.

## Fleet use

- `--dry-run` prints the resolved native commands per host before acting.
- Same Intent can be dispatched to hosts with different bases; each host
  resolves locally.

## MVP

1. Backends: apt, dnf, pacman, zypper (+ flatpak).
2. uni's own syntax only.
3. Ops: install, remove, search.
4. Resolver: alias table + Repology + live verification.
5. Config: sources, priorities, pins, `--from`.

Later: cross-syntax frontends (translate/route), Snap/AUR/Pacstall/COPR/OBS,
Textual TUI, Snapper hooks, fleet dispatch, Nix backend via nixorcist.
