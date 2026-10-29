"""Installation discovery and read-only status reporting."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import manifest, paths, payload, platforms, steam
from .constants import GAME_CONFIG, GAME_JAR
from .errors import DetectionError, ManifestError, UnsafePathError, ValidationError

APPID = payload.APPID
WORKSHOP_ID = payload.WORKSHOP_ID
MOD_NAME = payload.MOD_NAME


@dataclass(frozen=True)
class Installation:
    steam_library: Path | None
    wrapper: Path
    runtime: Path
    workshop_item: Path | None
    mod_dir: Path | None
    payload: Path
    data_dir: Path
    state_dir: Path
    layout: str

    def as_paths(self) -> dict[str, str | None]:
        return {
            "steam_library": str(self.steam_library) if self.steam_library else None,
            "wrapper": str(self.wrapper),
            "runtime": str(self.runtime),
            "workshop_item": str(self.workshop_item) if self.workshop_item else None,
            "mod_dir": str(self.mod_dir) if self.mod_dir else None,
            "payload": str(self.payload),
            "data_dir": str(self.data_dir),
            "state_dir": str(self.state_dir),
        }


def _derive_library_from_wrapper(wrapper: Path) -> Path | None:
    try:
        if wrapper.parent.name == "common" and wrapper.parent.parent.name == "steamapps":
            return wrapper.parent.parent.parent
    except IndexError:
        return None
    return None


def _derive_library_from_item(item: Path) -> Path | None:
    try:
        if item.parents[3].name == "steamapps":
            return item.parents[4]
    except IndexError:
        return None
    return None


def _derive_item_from_payload(payload_root: Path) -> Path | None:
    mod = payload_root.parent
    try:
        if mod.parent.name == "mods":
            return mod.parent.parent
    except IndexError:
        return None
    return None


def _default_state_dir() -> Path:
    return platforms.default_state_dir()


def _runtime_for_wrapper(wrapper: Path) -> tuple[Path, str]:
    checked: list[str] = []
    for candidate, layout in platforms.runtime_candidates(wrapper):
        checked.append(str(candidate / GAME_JAR))
        if (candidate / GAME_JAR).is_file() and (candidate / GAME_CONFIG).is_file():
            return paths.realpath(candidate), layout
    raise DetectionError(
        "the native runtime directory was not found",
        details={"wrapper": str(wrapper), "checked": checked},
    )


def resolve_installation(opts: Any) -> Installation:
    explicit_library = getattr(opts, "steam_library", None)
    explicit_workshop = getattr(opts, "workshop_item", None)
    explicit_wrapper = getattr(opts, "wrapper_root", None)
    explicit_runtime = getattr(opts, "runtime", None)
    explicit_payload = getattr(opts, "payload", None)
    explicit_data = getattr(opts, "data_dir", None)

    any_explicit = any(
        value is not None
        for value in (
            explicit_library,
            explicit_workshop,
            explicit_wrapper,
            explicit_runtime,
            explicit_payload,
            explicit_data,
        )
    )

    library: Path | None = None
    wrapper: Path | None = None
    item: Path | None = None
    runtime: Path | None = None
    payload_root: Path | None = None
    data_dir: Path | None = None

    if explicit_library:
        library = paths.resolve_dir(explicit_library, "Steam library")
    if explicit_workshop:
        item = paths.resolve_dir(explicit_workshop, "Workshop item")
    if explicit_wrapper:
        wrapper = paths.resolve_dir(explicit_wrapper, "game wrapper root")
    if explicit_runtime:
        runtime = paths.resolve_dir(explicit_runtime, "native runtime directory")
    if explicit_payload:
        payload_root = paths.resolve_dir(explicit_payload, "optimization payload")
    if explicit_data:
        data_dir = paths.resolve_dir(explicit_data, "preconfigured Zomboid data directory")

    if not any_explicit:
        libraries = steam.discover_libraries()
        candidates: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []
        for lib in libraries:
            candidate_wrapper = lib / "steamapps" / "common" / "ProjectZomboid"
            candidate_item = lib / "steamapps" / "workshop" / "content" / APPID / WORKSHOP_ID
            mod = candidate_item / "mods" / MOD_NAME
            candidate_payload = mod / "ProjectZomboid"
            candidate_data = mod / "Zomboid"
            present = {
                "wrapper": candidate_wrapper.is_dir(),
                "workshop": candidate_item.is_dir(),
                "payload": candidate_payload.is_dir(),
                "data": candidate_data.is_dir(),
            }
            if all(present.values()):
                candidates.append(
                    {
                        "library": lib,
                        "wrapper": candidate_wrapper,
                        "item": candidate_item,
                        "payload": candidate_payload,
                        "data": candidate_data,
                    }
                )
            else:
                diagnostics.append({"library": str(lib), **present})
        if not candidates:
            raise DetectionError(
                "no complete 13thPandemic installation was found",
                details={"libraries": [str(item) for item in libraries], "diagnostics": diagnostics},
            )
        if len(candidates) > 1:
            raise DetectionError(
                "multiple 13thPandemic installations were found; choose one with --steam-library or --wrapper-root",
                details={
                    "candidates": [
                        {
                            "library": str(candidate["library"]),
                            "wrapper": str(candidate["wrapper"]),
                            "workshop_item": str(candidate["item"]),
                        }
                        for candidate in candidates
                    ]
                },
            )
        chosen = candidates[0]
        library = chosen["library"]
        wrapper = chosen["wrapper"]
        item = chosen["item"]
        payload_root = chosen["payload"]
        data_dir = chosen["data"]

    # Fill in derived paths from whichever explicit piece was supplied.
    if item is not None:
        library = library or _derive_library_from_item(item)
    if payload_root is not None:
        item = item or _derive_item_from_payload(payload_root)
        library = library or (_derive_library_from_item(item) if item else None)
    if wrapper is not None:
        library = library or _derive_library_from_wrapper(wrapper)

    if library is not None:
        wrapper = wrapper or (library / "steamapps" / "common" / "ProjectZomboid")
        item = item or (library / "steamapps" / "workshop" / "content" / APPID / WORKSHOP_ID)

    if item is not None:
        mod = item / "mods" / MOD_NAME
        payload_root = payload_root or (mod / "ProjectZomboid")
        data_dir = data_dir or (mod / "Zomboid")

    if payload_root is not None:
        mod = payload_root.parent
        data_dir = data_dir or (mod / "Zomboid")

    if runtime is not None and wrapper is None:
        wrapper = runtime.parent if runtime.name == "projectzomboid" else runtime

    if wrapper is None:
        raise DetectionError("the game wrapper root was not provided and could not be discovered")
    wrapper = paths.resolve_dir(wrapper, "game wrapper root")

    if runtime is None:
        runtime, layout = _runtime_for_wrapper(wrapper)
    else:
        runtime = paths.resolve_dir(runtime, "native runtime directory")
        layout = platforms.layout_for_runtime(wrapper, runtime)

    if not paths.is_within(runtime, wrapper):
        raise DetectionError(
            "runtime directory is outside the game wrapper root",
            details={"runtime": str(runtime), "wrapper": str(wrapper)},
        )

    if payload_root is None:
        if item is None:
            raise DetectionError("optimization payload path was not provided and could not be discovered")
        payload_root = item / "mods" / MOD_NAME / "ProjectZomboid"
    payload_root = paths.resolve_dir(payload_root, "optimization payload")

    if data_dir is None:
        if item is None:
            raise DetectionError("preconfigured data directory was not provided and could not be discovered")
        data_dir = item / "mods" / MOD_NAME / "Zomboid"
    data_dir = paths.resolve_dir(data_dir, "preconfigured Zomboid data directory")

    mod_dir = payload_root.parent
    if item is None:
        derived_item = _derive_item_from_payload(payload_root)
        if derived_item is not None and derived_item.is_dir():
            item = derived_item

    workshop_item: Path | None = None
    if item is not None:
        try:
            workshop_item = paths.resolve_dir(item, "Workshop item")
        except ValidationError:
            workshop_item = None

    state_value = getattr(opts, "state_dir", None)
    state_dir = Path(state_value).expanduser() if state_value else _default_state_dir()
    if state_dir.exists():
        state_dir = paths.realpath(state_dir)

    # Validate the runtime files without following symlinks at the jar level.
    jar = runtime / GAME_JAR
    config = runtime / GAME_CONFIG
    for required, label in ((jar, GAME_JAR), (config, GAME_CONFIG)):
        try:
            st = os.lstat(required)
        except FileNotFoundError as exc:
            raise DetectionError(
                f"native runtime is missing {label}",
                details={"path": str(required)},
            ) from exc
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            raise DetectionError(
                f"native runtime {label} is not a regular file",
                details={"path": str(required)},
            )

    return Installation(
        steam_library=library,
        wrapper=wrapper,
        runtime=runtime,
        workshop_item=workshop_item,
        mod_dir=mod_dir,
        payload=payload_root,
        data_dir=data_dir,
        state_dir=state_dir,
        layout=layout,
    )


def _windows_launcher_state(inst: Installation) -> dict[str, Any]:
    if inst.mod_dir is None:
        return {"present": False}
    launcher = inst.mod_dir / "Launcher"
    manifest_path = launcher / "optimizations.json"
    owner_path = launcher / "optimizations.owner"
    lock_path = launcher / "maintenance.lock"
    if not launcher.is_dir():
        return {"present": False}
    result: dict[str, Any] = {
        "present": True,
        "path": str(launcher),
        "manifest": str(manifest_path) if manifest_path.is_file() else None,
        "owner": str(owner_path) if owner_path.is_file() else None,
        "maintenance_lock": str(lock_path) if lock_path.exists() else None,
        "entry_count": None,
        "destination_root": None,
        "states": {},
        "backups": 0,
        "owner_hash_matches": None,
    }
    if not manifest_path.is_file():
        return result
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        result["error"] = str(exc)
        return result
    entries = data.get("entries") if isinstance(data, dict) else None
    if isinstance(entries, list):
        result["entry_count"] = len(entries)
        states: dict[str, int] = {}
        backups = 0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            state_name = str(entry.get("state", "unknown"))
            states[state_name] = states.get(state_name, 0) + 1
            if entry.get("backup_relative_path"):
                backups += 1
        result["states"] = states
        result["backups"] = backups
    if isinstance(data, dict):
        result["destination_root"] = data.get("destination_root")
    if owner_path.is_file():
        try:
            owner = json.loads(owner_path.read_text(encoding="utf-8"))
            result["owner_hash_matches"] = (
                isinstance(owner, dict)
                and owner.get("manifest_sha256") == paths.sha256_file(manifest_path)
            )
        except (OSError, ValueError):
            result["owner_hash_matches"] = False
    return result


def _legacy_wrapper_report(inst: Installation) -> dict[str, Any]:
    markers = [
        "zombie",
        "pzopt",
        "jre64.xml",
        "ProjectZomboid64.json",
        "pzopt-files.txt",
        "media",
    ]
    present = {name: (inst.wrapper / name).exists() for name in markers}
    return {
        "present": any(present.values()),
        "markers": present,
        "note": "The legacy wrapper-root payload is reported only and is never modified.",
    }


def launch_options_for(inst: Installation, opts: Any) -> dict[str, Any]:
    roots: list[Path] = []
    if inst.steam_library is not None:
        roots.append(inst.steam_library)
    roots.extend(steam.default_steam_roots())
    seen: set[str] = set()
    unique: list[Path] = []
    for root in roots:
        real = paths.realpath(root)
        if str(real) in seen:
            continue
        seen.add(str(real))
        unique.append(real)
    return steam.find_launch_options(unique, APPID, getattr(opts, "steam_user", None))


def _unexpected_overlay_files(inst: Installation, inventory: dict[str, Any]) -> list[str]:
    expected = {item["relative_path"] for item in inventory["included"]}
    unexpected: list[str] = []
    for top in ("zombie", "pzopt", "fmod", "org", "se"):
        base = inst.runtime / top
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            for filename in filenames:
                full = Path(dirpath) / filename
                rel = os.path.relpath(full, inst.runtime).replace(os.sep, "/")
                if rel not in expected:
                    unexpected.append(rel)
    return unexpected


def build_detection(inst: Installation, opts: Any, inventory: dict[str, Any] | None = None) -> dict[str, Any]:
    info = payload.build_info(inst.payload)
    jar = payload.validate_runtime_jar(inst.runtime, info)
    inventory = inventory or payload.inventory(inst.payload)
    targets = payload.classify_targets(inventory, inst.runtime)
    launch_options = launch_options_for(inst, opts)
    windows_state = _windows_launcher_state(inst)
    legacy = _legacy_wrapper_report(inst)
    unexpected = _unexpected_overlay_files(inst, inventory)

    warnings: list[str] = []
    if inst.layout not in platforms.NATIVE_LAYOUTS:
        warnings.append(
            "native runtime layout is not supported for optimization apply"
        )
    if not jar["match"]:
        warnings.append(jar.get("reason") or "runtime jar does not match the payload build info")
    if inventory["unsafe"]:
        warnings.append(f"payload contains {len(inventory['unsafe'])} unsafe source entries")
    if targets["unsafe"]:
        warnings.append(f"runtime contains {len(targets['unsafe'])} unsafe target paths")
    if legacy["present"]:
        warnings.append(
            "legacy wrapper-root optimization files are present; they are reported only and will not be modified"
        )
    if windows_state.get("present"):
        warnings.append(
            "Windows launcher state is present; it is reported only and has no verified backups for this installation"
        )
    if unexpected:
        warnings.append(f"runtime contains {len(unexpected)} untracked overlay files")
    warnings.extend(launch_options.get("warnings", []))

    return {
        "ok": True,
        "appid": APPID,
        "workshop_id": WORKSHOP_ID,
        "mod_name": MOD_NAME,
        "paths": inst.as_paths(),
        "layout": inst.layout,
        "jar": jar,
        "build_info": {
            "path": info["path"],
            "sha256": info["sha256"],
            "revision": info["revision"],
            "release": info["release"],
            "jar_sha256": info["jar_sha256"],
            "jar_size": info["jar_size"],
        },
        "payload": {
            "root": inventory["root"],
            "total_count": inventory["total_count"],
            "included_count": inventory["included_count"],
            "excluded_count": inventory["excluded_count"],
            "custom_video_count": inventory["custom_video_count"],
            "inventory_sha256": inventory["inventory_sha256"],
            "excluded": [
                {
                    "relative_path": item["relative_path"],
                    "reason": item["exclusion_reason"],
                    "size": item["source_size"],
                }
                for item in inventory["excluded"]
            ],
            "unsafe": inventory["unsafe"],
        },
        "targets": {
            "counts": targets["counts"],
            "conflicts": targets["conflicts"],
            "unsafe": targets["unsafe"],
            "videos": targets["videos"],
            "unexpected": unexpected,
        },
        "launch_options": launch_options,
        "windows_launcher": windows_state,
        "legacy_wrapper": legacy,
        "warnings": warnings,
    }


def applied_status(inst: Installation, inventory: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        current_manifest = manifest.load_manifest(inst.state_dir)
    except ManifestError as exc:
        return {"state": "invalid", "error": exc.as_dict()}
    if current_manifest is None:
        return {"state": "not_applied"}

    try:
        manifest.validate_manifest(current_manifest)
        owner = manifest.verify_owner(inst.state_dir, current_manifest)
    except ManifestError as exc:
        return {"state": "invalid", "error": exc.as_dict()}

    game = current_manifest.get("game", {})
    runtime_matches = str(paths.realpath(inst.runtime)) == game.get("runtime")
    jar = payload.validate_runtime_jar(inst.runtime, {"jar_sha256": game.get("jar_sha256"), "jar_size": game.get("jar_size")})
    missing: list[str] = []
    modified: list[str] = []
    checked = 0
    for entry in current_manifest.get("entries", []):
        if not isinstance(entry, dict):
            continue
        rel = entry.get("relative_path")
        if not isinstance(rel, str):
            continue
        checked += 1
        try:
            target = paths.safe_target(inst.runtime, rel)
        except UnsafePathError:
            modified.append(rel)
            continue
        try:
            st = os.lstat(target)
        except FileNotFoundError:
            missing.append(rel)
            continue
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            modified.append(rel)
            continue
        actual = paths.sha256_file(target)
        if actual != entry.get("applied_sha256"):
            modified.append(rel)

    payload_hash = current_manifest.get("payload", {}).get("inventory_sha256")
    source_changed = bool(inventory and payload_hash and inventory.get("inventory_sha256") != payload_hash)
    untracked: list[str] = []
    tracked = {entry.get("relative_path") for entry in current_manifest.get("entries", []) if isinstance(entry, dict)}
    for top in ("zombie", "pzopt", "fmod", "org", "se"):
        base = inst.runtime / top
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            for filename in filenames:
                full = Path(dirpath) / filename
                rel = os.path.relpath(full, inst.runtime).replace(os.sep, "/")
                if rel not in tracked:
                    untracked.append(rel)

    state = "applied"
    if not runtime_matches or not jar.get("match") or missing or modified:
        state = "needs_attention"
    if source_changed:
        state = "source_changed"

    return {
        "state": state,
        "state_id": current_manifest.get("state_id"),
        "created_at": current_manifest.get("created_at"),
        "runtime_matches": runtime_matches,
        "jar_matches": jar.get("match"),
        "entry_count": checked,
        "missing": missing,
        "modified": modified,
        "untracked_overlay": untracked[:200],
        "untracked_overlay_count": len(untracked),
        "source_changed": source_changed,
        "manifest_sha256": owner.get("manifest_sha256"),
        "backup_directory": str(inst.state_dir / "backups" / str(current_manifest.get("state_id"))),
    }
