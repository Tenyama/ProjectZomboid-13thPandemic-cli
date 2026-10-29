# pz13th — native Linux/macOS launcher for 13thPandemic

A Python 3 standard-library-only CLI that replaces the Windows launcher for the
13thPandemic Project Zomboid modpack on Linux and macOS.

## Installation

From a clone of the repository:

```bash
cd pz13th-linux
python3 -m pip install --user .
```

Or install directly from GitHub:

```bash
python3 -m pip install --user "git+https://github.com/Tenyama/ProjectZomboid-13thPandemic-cli"
```

On an externally managed Python installation, use `pipx install .`, a virtual
environment, or the distribution package manager’s supported pip mechanism.

After installation, the `pz13th` command works from any directory:

```bash
pz13th detect
pz13th status
```

`pipx install .` is also supported if `pipx` is available.

Python 3.11 or newer is required.

## Safety model

- Optimization files are applied only inside the verified native runtime
  directory (Linux: `<wrapper-root>/projectzomboid/`; macOS:
  `<wrapper-root>/Project Zomboid.app/Contents/Java/`).
- The Steam wrapper root, the Workshop item, the Windows launcher state, the
  Windows JRE archive, and the platform `ProjectZomboid64.json` are never modified.
- `projectzomboid.jar` is never a target.
- State, manifests, locks, and backups live outside Steam. Linux defaults to
  `$XDG_STATE_HOME/13thpandemic/` or `~/.local/state/13thpandemic/`; macOS uses
  `~/Library/Application Support/13thpandemic/` unless XDG variables are set.
- Replaced files are backed up and hashed. Restores require verified backups.
- `apply --dry-run` creates nothing: no state directory, lock, manifest, backup,
  or temporary file.
- No direct game-executable launch and no automatic Steam launch-option edits.

## Commands

```text
pz13th detect [--json]
pz13th status [--json]
pz13th launch --port N [--dry-run] [--method applaunch|run|rungameid]
pz13th apply --dry-run [--json]
pz13th apply [--json]
pz13th restore [--dry-run] [--json]
```

Tests:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -t . -v
```

Global overrides:

```text
--steam-library PATH
--workshop-item PATH
--wrapper-root PATH
--runtime PATH
--payload PATH
--data-dir PATH
--state-dir PATH
--config PATH
--steam-user USERDATA_ID
--steam-binary NAME_OR_PATH
--host HOST
--port PORT
--method applaunch|run|rungameid
--verbose
```

## Configuration

Default config path:

```text
Linux:  ~/.config/13thpandemic/config.toml
macOS:  ~/Library/Application Support/13thpandemic/config.toml
```

`XDG_CONFIG_HOME` overrides both when set. The file is created automatically on
the first CLI invocation with `port = 16262`; an existing file is never
overwritten. `apply --dry-run` and `launch --dry-run` do not create it.

Example:

```toml
[server]
host = "pz.13thpandemic.net"
port = 16262

[launch]
method = "applaunch"
# steam_binary = "steam"   # omitted by default for platform auto-detection
zombie_buddy = "auto"      # auto | on | off

[paths]
# All optional; detection uses Steam metadata when empty.
# state_dir = ""
```

The port is intentionally not guessed. `launch` refuses until `--port` or
`server.port` provides a value between 1 and 65535.

## Launch argument forwarding

Steam currently stores per-app launch options. This CLI reads them, reports them,
and refuses to duplicate `-cachedir`, `-connect`, or `-port` if they are already
present. It never edits Steam's configuration.

Three methods are implemented:

- `applaunch`: `steam -applaunch 108600 -cachedir=... -connect=... -port=...`
- `run`: `steam steam://run/108600//<encoded arguments>`
- `rungameid`: `steam steam://rungameid/108600` (cannot pass custom arguments)

The default is `applaunch`, but argument forwarding is **not verified** until a
manual launch inspects the real process command line. `launch --dry-run` prints
the exact command and all warnings.

Manual verification after approval:

1. `pz13th launch --dry-run --port N`
2. launch once
3. inspect the running `ProjectZomboid64` command line (`/proc/<pid>/cmdline` on Linux, `ps` on macOS)
4. confirm `-cachedir=`, `-connect=`, and `-port=`
5. if forwarding is wrong, try `--method run`

## Optimization payload

Included:

```text
zombie/**
pzopt/**                (excluding pzopt/uninstall/**)
media/**                (all custom .bik videos included)
fmod/**
org/**
se/**
```

Excluded:

```text
jre64.xml
ProjectZomboid64*.json
pzopt-files.txt
pzopt/uninstall/**
*.exe, *.dll, *.cmd, *.bat, *.ps1
```

The exact classification is recomputed from the payload on every run.
`media/videos/title_screen_background.bik` is the known replacement conflict; it
is backed up before being replaced. Other videos are classified dynamically.

## Transaction state

```text
Linux:  $XDG_STATE_HOME/13thpandemic/ or ~/.local/state/13thpandemic/
macOS:  ~/Library/Application Support/13thpandemic/
├── lock
├── manifest.json
├── manifest.owner
├── manifest.pending.json
└── backups/<state_id>/
```

The pending manifest is updated atomically after each entry. If an apply is
interrupted, the next `apply` or `restore` recovers it by checking actual target
and backup hashes. Restore refuses if a target was changed unexpectedly.

## ZombieBuddy

`launch` defaults to `zombie_buddy = "auto"`. When `ZombieBuddy.jar` is present
in the native runtime, the CLI injects it automatically.

On Linux, it creates a launch-only config copy under:

```text
$XDG_STATE_HOME/13thpandemic/launch/ProjectZomboid64.zombiebuddy.json
```

It adds `-javaagent:ZombieBuddy.jar` to that copy and launches with:

```text
-pzexeconfig <that file> -- -cachedir=... -connect=... -port=...
```

The native Linux `ProjectZomboid64.json` is never modified. Steam’s stored
`-javaagent:ZombieBuddy.jar` launch option is not sufficient: `pzexe` treats it
as a game argument, so the agent is injected through the custom config instead.
The old Steam option is redundant and may be removed manually; the CLI does not
edit Steam launch options.

On macOS, PZ uses `JavaAppLauncher`, not `pzexe`; the CLI passes
`-javaagent:ZombieBuddy.jar --` as launch arguments. This macOS path is
implemented but not yet verified on real hardware, so `launch --dry-run` and the
ZombieBuddy log should be checked on the first macOS run.

## Mods directory case

The 13thPandemic cachedir ships the Windows-style uppercase `Mods` directory,
while native Linux PZ reads lowercase `mods`. Before launching, the CLI checks
this. On case-sensitive filesystems, if `mods` is missing, it creates a relative
symlink to `Mods`. If `mods` contains only regular files, those files are copied
to `$XDG_STATE_HOME/13thpandemic/backups/cachedir-mods/<id>/` first, then the
directory is replaced with the symlink. If `mods` contains directories, the CLI
refuses rather than merging or deleting unknown content. On macOS’s default
case-insensitive filesystem, the check detects that both names are the same
directory and makes no change.

It also repairs Windows-only filename casing (`Mod.info` → `mod.info` symlink)
and ensures `ProjectVarietyChapterTwo` is active in `default.txt` immediately
after `ProjectVarietyChapterOne`. The pre-modification `default.txt` is backed
up under `$XDG_STATE_HOME/13thpandemic/backups/modlist/<id>/`.

## Tests

Tests are written with `unittest` and use temporary directories only.

```text
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -t . -v
```

They do not launch the game, modify the real Steam installation, or edit Steam
launch options.
