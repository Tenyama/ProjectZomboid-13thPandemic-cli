"""Optimization payload inventory and classification."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from . import paths
from .constants import APP_ID, GAME_JAR, MOD_DIR_NAME, WORKSHOP_ITEM_ID
from .errors import UnsafePathError, ValidationError

APPID = APP_ID
WORKSHOP_ID = WORKSHOP_ITEM_ID
MOD_NAME = MOD_DIR_NAME

WINDOWS_CONFIGS = {
    "ProjectZomboid64.json",
    "ProjectZomboid64 (8GB).json",
    "ProjectZomboid64 (12GB).json",
    "ProjectZomboid64 (24GB).json",
    "ProjectZomboid64 (Vanilla).json",
}

WINDOWS_SUFFIXES = {".exe", ".dll", ".cmd", ".bat", ".ps1"}


def exclusion_reason(relative_path: str) -> str | None:
    pure = PurePosixPath(relative_path)
    parts = pure.parts
    suffix = pure.suffix.lower()

    if relative_path == "jre64.xml":
        return "windows_jre"
    if len(parts) == 1 and parts[0] in WINDOWS_CONFIGS:
        return "windows_jvm_config"
    if relative_path == "pzopt-files.txt":
        return "payload_metadata"
    if len(parts) >= 2 and parts[0] == "pzopt" and parts[1] == "uninstall":
        return "installer_script"
    if suffix in WINDOWS_SUFFIXES:
        return "windows_binary"
    if parts and parts[0] == "jre64":
        return "windows_jre"
    return None


def read_properties(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise ValidationError(f"cannot read {path}: {exc}", details={"path": str(path)}) from exc
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def build_info(payload_root: Path) -> dict[str, Any]:
    info_path = payload_root / "pzopt" / "build-info.properties"
    if not info_path.is_file():
        raise ValidationError(
            "payload is missing pzopt/build-info.properties",
            details={"path": str(info_path)},
        )
    properties = read_properties(info_path)
    try:
        expected_size = int(properties["jar.size"])
    except (KeyError, ValueError):
        expected_size = None
    return {
        "path": str(info_path),
        "sha256": paths.sha256_file(info_path),
        "properties": properties,
        "revision": properties.get("revision"),
        "release": properties.get("release"),
        "jar_sha256": properties.get("jar.sha256"),
        "jar_size": expected_size,
    }


def validate_runtime_jar(runtime: Path, info: dict[str, Any]) -> dict[str, Any]:
    jar = runtime / GAME_JAR
    result: dict[str, Any] = {
        "path": str(jar),
        "exists": False,
        "sha256": None,
        "size": None,
        "expected_sha256": info.get("jar_sha256"),
        "expected_size": info.get("jar_size"),
        "match": False,
        "reason": None,
    }
    try:
        st = os.lstat(jar)
    except FileNotFoundError:
        result["reason"] = "projectzomboid.jar is missing"
        return result
    if stat.S_ISLNK(st.st_mode):
        result["reason"] = "projectzomboid.jar is a symlink"
        return result
    if not stat.S_ISREG(st.st_mode):
        result["reason"] = "projectzomboid.jar is not a regular file"
        return result
    result["exists"] = True
    result["size"] = st.st_size
    result["sha256"] = paths.sha256_file(jar)
    result["match"] = (
        result["expected_sha256"] is not None
        and result["expected_size"] is not None
        and result["sha256"] == result["expected_sha256"]
        and result["size"] == result["expected_size"]
    )
    if not result["match"]:
        result["reason"] = "runtime jar does not match pzopt/build-info.properties"
    return result


def inventory(payload_root: Path) -> dict[str, Any]:
    try:
        root_stat = os.lstat(payload_root)
    except FileNotFoundError as exc:
        raise ValidationError("optimization payload is missing", details={"path": str(payload_root)}) from exc
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise ValidationError("optimization payload is not a regular directory", details={"path": str(payload_root)})

    entries: list[dict[str, Any]] = []
    unsafe: list[dict[str, str]] = []

    for dirpath, dirnames, filenames in os.walk(payload_root, followlinks=False):
        # Do not descend into symlinked directories.
        for dirname in list(dirnames):
            full_dir = Path(dirpath) / dirname
            try:
                st = os.lstat(full_dir)
            except OSError as exc:
                unsafe.append({"path": str(full_dir), "reason": str(exc)})
                dirnames.remove(dirname)
                continue
            if stat.S_ISLNK(st.st_mode):
                unsafe.append({"path": str(full_dir), "reason": "symlink directory"})
                dirnames.remove(dirname)

        for filename in filenames:
            full = Path(dirpath) / filename
            rel = os.path.relpath(full, payload_root).replace(os.sep, "/")
            try:
                st = os.lstat(full)
            except OSError as exc:
                unsafe.append({"path": str(full), "reason": str(exc)})
                continue
            if stat.S_ISLNK(st.st_mode):
                unsafe.append({"path": str(full), "reason": "symlink file"})
                continue
            if not stat.S_ISREG(st.st_mode):
                unsafe.append({"path": str(full), "reason": "not a regular file"})
                continue
            try:
                digest = paths.sha256_file(full)
            except OSError as exc:
                unsafe.append({"path": str(full), "reason": str(exc)})
                continue

            pure = PurePosixPath(rel)
            custom_video = (
                len(pure.parts) >= 3
                and pure.parts[0] == "media"
                and pure.parts[1] == "videos"
                and pure.suffix.lower() == ".bik"
            )
            reason = exclusion_reason(rel)
            entries.append(
                {
                    "relative_path": rel,
                    "source_path": str(full),
                    "source_size": st.st_size,
                    "source_sha256": digest,
                    "source_mode": stat.S_IMODE(st.st_mode),
                    "excluded": reason is not None,
                    "exclusion_reason": reason,
                    "custom_video": custom_video,
                }
            )

    entries.sort(key=lambda item: item["relative_path"])
    included = [item for item in entries if not item["excluded"]]
    excluded = [item for item in entries if item["excluded"]]

    digest = hashlib.sha256()
    for item in included:
        digest.update(
            f"{item['relative_path']}\0{item['source_size']}\0{item['source_sha256']}\n".encode("utf-8")
        )

    return {
        "root": str(payload_root),
        "entries": entries,
        "included": included,
        "excluded": excluded,
        "unsafe": unsafe,
        "total_count": len(entries),
        "included_count": len(included),
        "excluded_count": len(excluded),
        "custom_video_count": sum(1 for item in included if item["custom_video"]),
        "inventory_sha256": digest.hexdigest(),
    }


def classify_targets(payload_inventory: dict[str, Any], runtime: Path) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    unsafe: list[dict[str, str]] = []

    for item in payload_inventory["included"]:
        rel = item["relative_path"]
        try:
            target = paths.safe_target(runtime, rel)
        except UnsafePathError as exc:
            unsafe.append({"relative_path": rel, "reason": exc.message})
            continue

        try:
            st = os.lstat(target)
        except FileNotFoundError:
            action = "new"
            target_sha = None
            target_size = None
            target_mode = None
        else:
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                unsafe.append({"relative_path": rel, "reason": "target is not a regular file"})
                continue
            target_sha = paths.sha256_file(target)
            target_size = st.st_size
            target_mode = stat.S_IMODE(st.st_mode)
            action = "identical" if target_sha == item["source_sha256"] else "replaced"

        action_entry = {
            "relative_path": rel,
            "action": action,
            "custom_video": item["custom_video"],
            "source_size": item["source_size"],
            "source_sha256": item["source_sha256"],
            "source_mode": item["source_mode"],
            "target_path": str(target),
            "target_exists": action != "new",
            "target_size": target_size,
            "target_sha256": target_sha,
            "target_mode": target_mode,
        }
        actions.append(action_entry)
        if action == "replaced":
            conflicts.append(
                {
                    "relative_path": rel,
                    "custom_video": item["custom_video"],
                    "target_path": str(target),
                    "target_sha256": target_sha,
                    "source_sha256": item["source_sha256"],
                    "backup_required": True,
                }
            )

    videos = [entry for entry in actions if entry["custom_video"]]
    counts = {
        "new": sum(1 for entry in actions if entry["action"] == "new"),
        "identical": sum(1 for entry in actions if entry["action"] == "identical"),
        "replaced": sum(1 for entry in actions if entry["action"] == "replaced"),
        "unsafe": len(unsafe),
    }
    return {
        "actions": actions,
        "conflicts": conflicts,
        "unsafe": unsafe,
        "counts": counts,
        "videos": videos,
    }
