"""Small platform abstraction for Linux and macOS.

No external dependencies.  The Linux and macOS paths are kept here so the rest
of the package does not grow platform branches.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable

NATIVE_LAYOUTS = {"native-subdir", "macos-app-java"}


def is_macos() -> bool:
    return sys.platform == "darwin"


def default_state_dir() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg).expanduser() / "13thpandemic"
    if is_macos():
        return Path.home() / "Library" / "Application Support" / "13thpandemic"
    return Path.home() / ".local" / "state" / "13thpandemic"


def default_config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "13thpandemic" / "config.toml"
    if is_macos():
        return Path.home() / "Library" / "Application Support" / "13thpandemic" / "config.toml"
    return Path.home() / ".config" / "13thpandemic" / "config.toml"


def steam_roots() -> list[Path]:
    home = Path.home()
    candidates: list[str | None] = [
        os.environ.get("STEAM_ROOT"),
        str(home / ".local" / "share" / "Steam"),
        str(home / ".steam" / "steam"),
        str(home / ".steam" / "root"),
        str(home / ".steam" / "debian-installation"),
        str(home / ".var" / "app" / "com.valvesoftware.Steam" / ".local" / "share" / "Steam"),
        str(home / ".var" / "app" / "com.valvesoftware.Steam" / "data" / "Steam"),
        str(home / "Library" / "Application Support" / "Steam"),
    ]
    result: list[Path] = []
    seen: set[str] = set()
    for item in candidates:
        if not item:
            continue
        path = Path(item).expanduser()
        if not path.is_dir():
            continue
        real = Path(os.path.realpath(path))
        if str(real) in seen:
            continue
        seen.add(str(real))
        result.append(real)
    return result


def runtime_candidates(wrapper: Path) -> list[tuple[Path, str]]:
    """Return possible native runtime directories and layout labels."""

    return [
        (wrapper / "projectzomboid", "native-subdir"),
        (wrapper / "Project Zomboid.app" / "Contents" / "Java", "macos-app-java"),
        (wrapper, "direct"),
    ]


def layout_for_runtime(wrapper: Path, runtime: Path) -> str:
    runtime_real = Path(os.path.realpath(runtime))
    for candidate, label in runtime_candidates(wrapper):
        if Path(os.path.realpath(candidate)) == runtime_real:
            return label
    return "nonstandard"


def default_steam_binary() -> str:
    if is_macos():
        candidates = [
            Path("/Applications/Steam.app/Contents/MacOS/steam_osx"),
            Path.home() / "Applications" / "Steam.app" / "Contents" / "MacOS" / "steam_osx",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
    found = shutil.which("steam")
    return found or "steam"


def process_cwd(pid: int) -> Path | None:
    proc_cwd = Path("/proc") / str(pid) / "cwd"
    try:
        if proc_cwd.exists():
            return Path(os.path.realpath(os.readlink(proc_cwd)))
    except OSError:
        pass
    try:
        completed = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    for line in completed.stdout.splitlines():
        if line.startswith("n") and len(line) > 1:
            try:
                return Path(os.path.realpath(line[1:]))
            except OSError:
                return None
    return None


def processes() -> Iterable[tuple[int, str]]:
    proc = Path("/proc")
    if proc.is_dir():
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes().replace(b"\x00", b" ")
            except OSError:
                continue
            yield int(entry.name), cmdline.decode("utf-8", "replace")
        return
    try:
        completed = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return
    for line in completed.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, command = stripped.partition(" ")
        if pid_text.isdigit():
            yield int(pid_text), command
