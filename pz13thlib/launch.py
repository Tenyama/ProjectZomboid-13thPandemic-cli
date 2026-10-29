"""Steam launch argument construction and execution."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import urllib.parse
from pathlib import Path
from typing import Any

from . import manifest, paths, platforms
from .constants import ACTIVE_MODS, APP_ID, DEFAULT_SERVER_HOST, GAME_CONFIG, ZOMBIE_BUDDY_JAR
from .errors import LaunchError, ValidationError

APPID = APP_ID
DEFAULT_HOST = DEFAULT_SERVER_HOST
METHODS = ("applaunch", "run", "rungameid")


def validate_port(value: Any) -> int:
    if value is None or value == "":
        raise LaunchError("server port is not configured; pass --port or set server.port in config.toml")
    if isinstance(value, bool):
        raise LaunchError("invalid server port")
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise LaunchError(f"invalid server port: {value!r}") from exc
    if port < 1 or port > 65535:
        raise LaunchError(f"server port must be between 1 and 65535, got {port}")
    return port


def validate_host(value: Any) -> str:
    host = str(value or "").strip()
    if not host:
        raise LaunchError("server host is empty")
    if any(char.isspace() for char in host):
        raise LaunchError("server host must not contain whitespace")
    return host


def _split_options(option: str) -> list[str]:
    try:
        return shlex.split(option, posix=True)
    except ValueError:
        return option.split()


def _has_duplicate(tokens: list[str], name: str) -> bool:
    for index, token in enumerate(tokens):
        if token == name or token.startswith(name + "="):
            return True
        if token == name and index + 1 < len(tokens):
            return True
    return False


def _launch_option_conflicts(entries: list[dict[str, Any]]) -> list[str]:
    conflicts: list[str] = []
    for entry in entries:
        tokens = _split_options(str(entry.get("options", "")))
        for name in ("-cachedir", "-connect", "-port"):
            if _has_duplicate(tokens, name):
                conflicts.append(f"{name} already appears in Steam LaunchOptions ({entry.get('path')})")
    return conflicts


def build_args(data_dir: Path, host: str, port: int, pzexeconfig: Path | None = None) -> list[str]:
    args: list[str] = []
    if pzexeconfig is not None:
        args.extend(["-pzexeconfig", str(pzexeconfig), "--"])
    args.extend(
        [
            f"-cachedir={paths.realpath(data_dir)}",
            f"-connect={host}",
            f"-port={port}",
        ]
    )
    return args


def _existing_agent_option(entries: list[dict[str, Any]]) -> bool:
    for entry in entries:
        for token in _split_options(str(entry.get("options", ""))):
            if token.startswith(f"-javaagent:{ZOMBIE_BUDDY_JAR}"):
                return True
    return False


def _zombie_buddy_plan(
    *,
    runtime: Path | None,
    state_dir: Path | None,
    mode: str,
    launch_options: dict[str, Any],
) -> dict[str, Any]:
    if mode not in ("auto", "on", "off"):
        raise LaunchError(f"invalid ZombieBuddy mode {mode!r}; expected auto, on, or off")
    entries = launch_options.get("entries", []) if isinstance(launch_options, dict) else []
    existing_option = _existing_agent_option(entries)
    jar = runtime / ZOMBIE_BUDDY_JAR if runtime is not None else None
    if mode == "off":
        return {
            "enabled": False,
            "mode": mode,
            "reason": "disabled by configuration",
            "jar": str(jar) if jar else None,
            "config_path": None,
            "config": None,
            "existing_steam_option": existing_option,
        }
    if runtime is None or state_dir is None:
        if mode == "on":
            raise LaunchError("ZombieBuddy mode 'on' requires a detected runtime and state directory")
        return {
            "enabled": False,
            "mode": mode,
            "reason": "runtime/state path unavailable",
            "jar": None,
            "config_path": None,
            "config": None,
            "existing_steam_option": existing_option,
        }
    if jar is None or not jar.is_file():
        if mode == "on" or existing_option:
            raise LaunchError(
                "ZombieBuddy is enabled but ZombieBuddy.jar is missing from the native runtime",
                details={"jar": str(jar)},
            )
        return {
            "enabled": False,
            "mode": mode,
            "reason": "ZombieBuddy.jar not found",
            "jar": str(jar),
            "config_path": None,
            "config": None,
            "existing_steam_option": existing_option,
        }

    source_config = runtime / GAME_CONFIG
    if platforms.is_macos():
        return {
            "enabled": True,
            "mode": mode,
            "reason": "ZombieBuddy.jar found; macOS uses JavaAppLauncher arguments",
            "jar": str(jar),
            "config_path": None,
            "config": None,
            "existing_steam_option": existing_option,
            "method": "javaagent_args",
        }
    try:
        base = json.loads(source_config.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LaunchError(
            f"cannot read Linux launcher config {source_config}: {exc}",
            details={"path": str(source_config)},
        ) from exc
    if not isinstance(base, dict) or not isinstance(base.get("vmArgs"), list):
        raise LaunchError(
            "Linux launcher config has no vmArgs array",
            details={"path": str(source_config)},
        )
    vm_args = [str(item) for item in base["vmArgs"]]
    agent = f"-javaagent:{ZOMBIE_BUDDY_JAR}"
    if not any(item.startswith(agent) for item in vm_args):
        vm_args.insert(0, agent)
    merged = dict(base)
    merged["vmArgs"] = vm_args
    config_path = Path(state_dir) / "launch" / "ProjectZomboid64.zombiebuddy.json"
    return {
        "enabled": True,
        "mode": mode,
        "reason": "ZombieBuddy.jar found",
        "jar": str(jar),
        "config_path": str(config_path),
        "config": merged,
        "existing_steam_option": existing_option,
        "method": "pzexeconfig",
    }


def prepare(plan: dict[str, Any]) -> Path | None:
    """Write the launch-only pzexe config if ZombieBuddy is enabled."""

    zombie = plan.get("zombie_buddy", {})
    if not zombie.get("enabled"):
        return None
    if zombie.get("method") == "javaagent_args":
        return None
    config = zombie.get("config")
    config_path = zombie.get("config_path")
    if not isinstance(config, dict) or not config_path:
        raise LaunchError("ZombieBuddy launch config is incomplete")
    path = Path(str(config_path))
    manifest.atomic_write_json(path, config)
    return path


def inspect_mods_alias(data_dir: Path) -> dict[str, Any]:
    """Inspect the Windows-uppercase Mods vs Linux-lowercase mods mismatch."""

    upper = data_dir / "Mods"
    lower = data_dir / "mods"
    result: dict[str, Any] = {
        "upper": str(upper),
        "lower": str(lower),
        "upper_present": upper.is_dir(),
        "lower_kind": "missing",
        "entry_count": None,
        "status": "unknown",
        "action": "none",
    }
    if not upper.is_dir():
        result["status"] = "no_uppercase_mods"
        return result
    try:
        same = os.path.samefile(upper, lower)
    except OSError:
        same = False
    if same:
        result["lower_kind"] = "case_insensitive"
        result["status"] = "case_insensitive"
        result["action"] = "none"
        return result
    try:
        st = os.lstat(lower)
    except FileNotFoundError:
        result["status"] = "missing_lowercase"
        result["action"] = "create_symlink"
        return result
    if os.path.islink(lower):
        result["lower_kind"] = "symlink"
        if os.path.realpath(lower) == os.path.realpath(upper):
            result["status"] = "already_linked"
        else:
            result["status"] = "wrong_symlink"
            result["action"] = "refuse"
        return result
    if not os.path.isdir(lower):
        result["lower_kind"] = "file"
        result["status"] = "unexpected_file"
        result["action"] = "refuse"
        return result
    entries = list(lower.iterdir())
    result["lower_kind"] = "directory"
    result["entry_count"] = len(entries)
    if not entries:
        result["status"] = "empty_directory"
        result["action"] = "backup_and_link"
        return result
    if all(entry.is_file() and not entry.is_symlink() for entry in entries):
        result["status"] = "files_only"
        result["action"] = "backup_and_link"
        return result
    result["status"] = "directory_with_entries"
    result["action"] = "refuse"
    return result


def prepare_mods_alias(data_dir: Path, state_dir: Path) -> dict[str, Any]:
    """Make lowercase `mods` point at the modpack's uppercase `Mods`.

    PZ on Linux reads the lowercase path.  The upstream modpack was built on a
    case-insensitive filesystem.  Existing lowercase files are copied to the
    CLI state directory before the directory is replaced with a relative
    symlink.  Non-file contents are never merged or deleted.
    """

    info = inspect_mods_alias(data_dir)
    action = info.get("action")
    upper = data_dir / "Mods"
    lower = data_dir / "mods"

    if info.get("status") in ("already_linked", "no_uppercase_mods"):
        return info
    if action == "refuse":
        raise LaunchError(
            "cannot create the Linux lowercase mods alias safely",
            details=info,
        )
    if action == "none":
        return info

    if info.get("lower_kind") == "directory":
        entries = list(lower.iterdir())
        if entries:
            state = manifest.ensure_state_dir(state_dir)
            backup = state / "backups" / "cachedir-mods" / manifest.new_state_id()
            backup.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(lower, backup, symlinks=True)
            shutil.rmtree(lower)
            try:
                os.symlink("Mods", lower)
            except OSError:
                # Put the original lowercase files back if the symlink fails.
                shutil.copytree(backup, lower, symlinks=True)
                raise
        else:
            lower.rmdir()
            os.symlink("Mods", lower)
        info["backup"] = str(backup) if entries else None
    else:
        os.symlink("Mods", lower)
        info["backup"] = None

    if os.path.realpath(lower) != os.path.realpath(upper):
        raise LaunchError("lowercase mods symlink verification failed", details=info)
    info["status"] = "linked"
    info["action"] = "created_symlink"
    return info


def inspect_mod_info_case(data_dir: Path) -> list[str]:
    """Find Windows-style Mod.info files that Linux PZ will not read."""

    upper = data_dir / "Mods"
    if not upper.is_dir():
        return []
    result: list[str] = []
    for dirpath, dirnames, filenames in os.walk(upper, followlinks=False):
        if "Mod.info" not in filenames:
            continue
        parent = Path(dirpath)
        lower = parent / "mod.info"
        if os.path.lexists(lower):
            continue
        result.append(os.path.relpath(parent / "Mod.info", upper).replace(os.sep, "/"))
    return sorted(result)


def prepare_mod_info_case(data_dir: Path) -> list[dict[str, str]]:
    """Create lowercase mod.info symlinks for case-mismatched mod metadata."""

    upper = data_dir / "Mods"
    changed: list[dict[str, str]] = []
    for rel in inspect_mod_info_case(data_dir):
        parent = upper / Path(rel).parent
        source = parent / "Mod.info"
        target = parent / "mod.info"
        if os.path.lexists(target):
            continue
        os.symlink("Mod.info", target)
        changed.append({"source": str(source), "target": str(target)})
    return changed


def inspect_active_mods(data_dir: Path) -> list[dict[str, str]]:
    """Check the required 13thPandemic mod IDs in Mods/default.txt."""

    default = data_dir / "Mods" / "default.txt"
    if not default.is_file():
        return []
    data = default.read_bytes()
    missing: list[dict[str, str]] = []
    for mod_id, after_id in ACTIVE_MODS:
        pattern = re.compile(
            rb"(?m)^[ \t]*mod[ \t]*=[ \t]*" + re.escape(mod_id.encode()) + rb"[ \t]*,?[ \t]*\r?$"
        )
        if pattern.search(data):
            continue
        missing.append({"mod_id": mod_id, "after": after_id})
    return missing


def prepare_active_mods(data_dir: Path, state_dir: Path) -> list[dict[str, str]]:
    """Add known-required 13thPandemic mods to the active default.txt list."""

    default = data_dir / "Mods" / "default.txt"
    if not default.is_file():
        return []
    data = default.read_bytes()
    sep = b"\r\n" if b"\r\n" in data else b"\n"
    changed: list[dict[str, str]] = []

    for item in inspect_active_mods(data_dir):
        mod_id = item["mod_id"]
        after_id = item["after"]
        pattern = re.compile(
            rb"(?m)^[ \t]*mod[ \t]*=[ \t]*" + re.escape(after_id.encode()) + rb"[ \t]*,?[ \t]*\r?\n"
        )
        match = pattern.search(data)
        if not match:
            raise LaunchError(
                f"cannot activate {mod_id}: anchor mod {after_id} is not in default.txt",
                details={"default": str(default), "mod_id": mod_id, "after": after_id},
            )
        backup = (
            manifest.ensure_state_dir(state_dir)
            / "backups"
            / "modlist"
            / manifest.new_state_id()
            / "default.txt"
        )
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(default, backup)
        insert = b"    mod = " + mod_id.encode() + b"," + sep
        data = data[: match.end()] + insert + data[match.end():]
        fd, tmp_name = tempfile.mkstemp(prefix=".default.txt.", suffix=".tmp", dir=str(default.parent))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, os.stat(default).st_mode & 0o777)
            os.replace(tmp, default)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        changed.append({"mod_id": mod_id, "backup": str(backup)})
    return changed


def build_plan(
    *,
    data_dir: Path,
    host: str,
    port: int,
    method: str,
    steam_binary: str,
    launch_options: dict[str, Any],
    runtime: Path | None = None,
    state_dir: Path | None = None,
    zombie_buddy: str = "off",
) -> dict[str, Any]:
    if method not in METHODS:
        raise LaunchError(f"unknown launch method {method!r}; expected one of {', '.join(METHODS)}")
    if not data_dir.is_dir():
        raise ValidationError("preconfigured data directory does not exist", details={"path": str(data_dir)})

    host = validate_host(host)
    port = validate_port(port)
    entries = launch_options.get("entries", []) if isinstance(launch_options, dict) else []
    zombie = _zombie_buddy_plan(
        runtime=runtime,
        state_dir=state_dir,
        mode=zombie_buddy,
        launch_options=launch_options,
    )
    mods_alias = inspect_mods_alias(data_dir)
    mod_info_case = inspect_mod_info_case(data_dir)
    active_mods = inspect_active_mods(data_dir)
    if mods_alias.get("action") == "refuse":
        # Dry-run must still be able to report the conflict, but a real launch
        # will call prepare_mods_alias() and refuse before starting Steam.
        warnings_alias = True
    else:
        warnings_alias = False
    if zombie.get("enabled") and method == "rungameid":
        raise LaunchError(
            "ZombieBuddy requires -pzexeconfig, which rungameid cannot pass; use applaunch or run"
        )
    config_path = Path(str(zombie["config_path"])) if zombie.get("config_path") else None
    if zombie.get("method") == "javaagent_args":
        args = [f"-javaagent:{ZOMBIE_BUDDY_JAR}", "--"] + build_args(data_dir, host, port)
    else:
        args = build_args(data_dir, host, port, config_path)
    conflicts = _launch_option_conflicts(entries)
    if conflicts:
        raise LaunchError(
            "Steam launch options already contain one of the arguments this launcher needs; "
            "refusing to duplicate them. Resolve the Steam launch options manually.",
            details={"conflicts": conflicts, "launch_options": entries},
        )

    binary = shutil.which(steam_binary) if "/" not in steam_binary else steam_binary
    warnings: list[str] = []
    if not binary:
        warnings.append(f"Steam binary not found: {steam_binary}")
    if entries:
        warnings.append(
            "Steam will also apply its stored LaunchOptions; the exact merge order with -applaunch arguments is not verified"
        )
        for entry in entries:
            if "--" in _split_options(str(entry.get("options", ""))):
                warnings.append(
                    "the stored Steam LaunchOptions contain '--'; extra arguments may be treated as program arguments rather than JVM arguments"
                )
    if zombie.get("enabled"):
        warnings.append(
            "ZombieBuddy will be injected through the launch arguments, not through Steam's stored -javaagent option"
        )
        if zombie.get("method") == "javaagent_args":
            warnings.append(
                "macOS JavaAppLauncher agent argument handling is not verified; inspect the running process and ZombieBuddy log"
            )
        if zombie.get("existing_steam_option"):
            warnings.append(
                "Steam LaunchOptions still contain -javaagent:ZombieBuddy.jar; the agent is injected by the launcher instead. "
                "Remove the Steam option manually if desired"
            )
    if warnings_alias:
        warnings.append(
            "lowercase mods exists but contains directories; the launcher will refuse to replace it"
        )
    elif mods_alias.get("action") == "backup_and_link":
        warnings.append(
            "lowercase mods will be backed up and replaced with a symlink to the uppercase Mods directory"
        )
    if mod_info_case:
        warnings.append(
            f"{len(mod_info_case)} Windows-style Mod.info file(s) will get a lowercase mod.info symlink"
        )
    if active_mods:
        warnings.append(
            f"{len(active_mods)} required mod(s) will be added to the active default.txt list"
        )

    if method == "applaunch":
        argv = [steam_binary, "-applaunch", APPID, *args]
        url = None
        warnings.append(
            "applaunch argument forwarding is not verified; inspect the running game command line (/proc on Linux, ps on macOS)"
        )
    elif method == "run":
        encoded = "%20".join(urllib.parse.quote(arg, safe="") for arg in args)
        url = f"steam://run/{APPID}//{encoded}"
        argv = [steam_binary, url]
        warnings.append(
            "steam://run argument encoding is not verified; inspect the running game command line (/proc on Linux, ps on macOS)"
        )
    else:
        argv = [steam_binary, f"steam://rungameid/{APPID}"]
        url = argv[-1]
        warnings.append(
            "rungameid cannot pass -cachedir, -connect, or -port; the preconfigured data directory and server join will not be used"
        )

    return {
        "method": method,
        "appid": APPID,
        "host": host,
        "port": port,
        "cachedir": str(paths.realpath(data_dir)),
        "args": args,
        "argv": argv,
        "url": url,
        "steam_binary": steam_binary,
        "steam_binary_resolved": binary,
        "launch_options": entries,
        "zombie_buddy": zombie,
        "mods_alias": mods_alias,
        "mod_info_case": mod_info_case,
        "active_mods": active_mods,
        "runtime": str(runtime) if runtime is not None else None,
        "state_dir": str(state_dir) if state_dir is not None else None,
        "warnings": warnings,
    }


def run(plan: dict[str, Any]) -> dict[str, Any]:
    binary = plan.get("steam_binary_resolved")
    if not binary:
        raise LaunchError("Steam binary was not found; refusing to launch")
    runtime_value = plan.get("runtime")
    if runtime_value and paths.game_running(Path(str(runtime_value))):
        raise LaunchError("the game is already running from the native runtime; quit it before launching again")
    state_value = plan.get("state_dir")
    if state_value is None:
        raise LaunchError("state directory is unavailable; cannot prepare launch state")
    mods_result = prepare_mods_alias(Path(str(plan.get("cachedir"))), Path(str(state_value)))
    mod_info_result = prepare_mod_info_case(Path(str(plan.get("cachedir"))))
    active_mod_result = prepare_active_mods(Path(str(plan.get("cachedir"))), Path(str(state_value)))
    config_path = prepare(plan)
    argv = [str(item) for item in plan["argv"]]
    try:
        completed = subprocess.run(argv, check=False)
    except OSError as exc:
        raise LaunchError(f"failed to run Steam: {exc}", details={"argv": argv}) from exc
    return {
        "started": True,
        "returncode": completed.returncode,
        "argv": argv,
        "zombie_buddy_config": str(config_path) if config_path else None,
        "mods_alias": mods_result,
        "mod_info_case": mod_info_result,
        "active_mods": active_mod_result,
        "note": "Steam may return before the game process starts; verify the game process separately.",
    }
