"""Transactional apply and restore of the optimization overlay."""

from __future__ import annotations

import fcntl
import os
import secrets
import stat
from pathlib import Path
from typing import Any

from . import __version__, manifest, paths, payload, platforms
from .constants import GAME_CONFIG
from .detection import Installation
from .errors import ConflictError, LockError, ManifestError, TransactionError, UnsafePathError


class FileLock:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self._handle = None

    def __enter__(self) -> "FileLock":
        state_dir = manifest.ensure_state_dir(self.state_dir)
        lock_path = state_dir / "lock"
        handle = open(lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise LockError(
                "another apply/restore operation is already running",
                details={"lock": str(lock_path)},
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\n")
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._handle = None


def _hash_if_regular(path: Path, label: str = "file") -> tuple[str, int, int] | None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode):
        raise UnsafePathError(f"{label} is a symlink", details={"path": str(path)})
    if not stat.S_ISREG(st.st_mode):
        raise UnsafePathError(f"{label} is not a regular file", details={"path": str(path)})
    return paths.sha256_file(path), st.st_size, stat.S_IMODE(st.st_mode)


def _copy_to_temp(source: Path, target: Path, expected_sha: str, mode: int) -> Path:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(source, flags)
    except OSError as exc:
        raise TransactionError(f"cannot open source {source}: {exc}", details={"path": str(source)}) from exc
    temp = target.parent / f".{target.name}.pz13th-{os.getpid()}-{secrets.token_hex(4)}.tmp"
    temp_fd = -1
    try:
        st = os.fstat(source_fd)
        if not stat.S_ISREG(st.st_mode):
            raise TransactionError(f"source is not a regular file: {source}", details={"path": str(source)})
        temp_fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(temp_fd, view)
                view = view[written:]
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = -1
        os.chmod(temp, mode)
        actual = paths.sha256_file(temp)
        if actual != expected_sha:
            raise TransactionError(
                "source content changed while it was being copied",
                details={"path": str(source), "expected": expected_sha, "actual": actual},
            )
        return temp
    except BaseException:
        if temp_fd >= 0:
            try:
                os.close(temp_fd)
            except OSError:
                pass
        try:
            temp.unlink()
        except OSError:
            pass
        raise
    finally:
        try:
            os.close(source_fd)
        except OSError:
            pass


def _install_temp(temp: Path, target: Path, *, create_new: bool, expected_sha: str) -> None:
    if create_new:
        if os.path.lexists(target):
            try:
                temp.unlink()
            except OSError:
                pass
            raise ConflictError("target appeared while applying", details={"path": str(target)})
        try:
            os.link(temp, target)
        except FileExistsError as exc:
            try:
                temp.unlink()
            except OSError:
                pass
            raise ConflictError("target appeared while applying", details={"path": str(target)}) from exc
        temp.unlink()
    else:
        os.replace(temp, target)
    actual = paths.sha256_file(target)
    if actual != expected_sha:
        raise TransactionError(
            "target hash verification failed after writing",
            details={"path": str(target), "expected": expected_sha, "actual": actual},
        )


def _copy_file(
    source: Path,
    target: Path,
    *,
    expected_sha: str,
    mode: int,
    create_new: bool,
) -> None:
    temp = _copy_to_temp(source, target, expected_sha, mode)
    try:
        _install_temp(temp, target, create_new=create_new, expected_sha=expected_sha)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise


def _backup_file(source: Path, backup: Path, original_sha: str, backup_root: Path) -> None:
    backup_root = Path(backup_root)
    paths.require_within(backup, backup_root, "backup")
    backup_root.mkdir(parents=True, exist_ok=True)
    paths.ensure_parent_chain(backup_root, backup.parent)
    if os.path.lexists(backup):
        existing = _hash_if_regular(backup, "existing backup")
        if existing is None or existing[0] != original_sha:
            raise TransactionError(
                "an existing backup does not match the original file",
                details={"backup": str(backup), "expected": original_sha},
            )
        return
    _copy_file(source, backup, expected_sha=original_sha, mode=0o600, create_new=True)


def make_plan(
    inst: Installation,
    inventory: dict[str, Any],
    *,
    state_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    state_id = state_id or ("dry-run" if dry_run else manifest.new_state_id())
    if inventory.get("unsafe"):
        raise UnsafePathError(
            "the payload contains unsafe source entries",
            details={"unsafe": inventory["unsafe"]},
        )
    classified = payload.classify_targets(inventory, inst.runtime)
    if classified["unsafe"]:
        raise UnsafePathError(
            "the runtime contains unsafe target paths",
            details={"unsafe": classified["unsafe"]},
        )

    info = payload.build_info(inst.payload)
    jar = payload.validate_runtime_jar(inst.runtime, info)
    config_path = inst.runtime / GAME_CONFIG
    config_sha = paths.sha256_file(config_path)

    entries: list[dict[str, Any]] = []
    for action in classified["actions"]:
        rel = action["relative_path"]
        manifest_action = "created" if action["action"] == "new" else action["action"]
        original_exists = action["target_exists"]
        backup_rel = None
        backup_path = None
        if action["action"] == "replaced":
            backup_rel = rel
            backup_path = manifest.backup_path(inst.state_dir, state_id, rel)
        entries.append(
            {
                "relative_path": rel,
                "source_path": str(inst.payload / rel),
                "target_path": action["target_path"],
                "action": manifest_action,
                "custom_video": action["custom_video"],
                "source_size": action["source_size"],
                "source_sha256": action["source_sha256"],
                "source_mode": action["source_mode"],
                "original": {
                    "exists": original_exists,
                    "sha256": action["target_sha256"],
                    "size": action["target_size"],
                    "mode": action["target_mode"],
                },
                "backup": {
                    "relative_path": backup_rel,
                    "path": str(backup_path) if backup_path else None,
                    "sha256": None,
                    "size": None,
                },
                "applied_sha256": action["source_sha256"],
                "applied_size": action["source_size"],
                "status": "unchanged" if action["action"] == "identical" else "pending",
            }
        )

    plan: dict[str, Any] = {
        "schema_version": manifest.SCHEMA_VERSION,
        "state_id": state_id,
        "created_at": manifest.utc_now(),
        "tool_version": __version__,
        "operation": "apply",
        "game": {
            "wrapper": str(inst.wrapper),
            "runtime": str(paths.realpath(inst.runtime)),
            "jar_sha256": jar.get("sha256"),
            "jar_size": jar.get("size"),
            "expected_jar_sha256": jar.get("expected_sha256"),
            "expected_jar_size": jar.get("expected_size"),
            "jar_match": jar.get("match"),
            "runtime_config_sha256": config_sha,
            "linux_config_sha256": config_sha,
        },
        "payload": {
            "root": str(inst.payload),
            "inventory_sha256": inventory["inventory_sha256"],
            "build_info_sha256": info["sha256"],
        },
        "entries": entries,
        "excluded": [
            {
                "relative_path": item["relative_path"],
                "reason": item["exclusion_reason"],
                "size": item["source_size"],
            }
            for item in inventory["excluded"]
        ],
        "created_dirs": [],
    }
    return plan


def summarize_plan(plan: dict[str, Any]) -> dict[str, Any]:
    counts = {"new": 0, "replaced": 0, "identical": 0, "applied": 0}
    for entry in plan["entries"]:
        action = entry["action"]
        if action in ("created", "new"):
            counts["new"] += 1
        elif action == "replaced":
            counts["replaced"] += 1
        elif action == "identical":
            counts["identical"] += 1
        if entry.get("status") == "applied":
            counts["applied"] += 1
    return {
        "state_id": plan.get("state_id"),
        "counts": counts,
        "conflicts": [
            entry
            for entry in plan["entries"]
            if entry["action"] == "replaced"
        ],
        "videos": [
            entry
            for entry in plan["entries"]
            if entry.get("custom_video")
        ],
        "excluded": plan.get("excluded", []),
        "created_dirs": plan.get("created_dirs", []),
    }


def _manifest_targets_match(inst: Installation, current: dict[str, Any]) -> bool:
    for entry in current.get("entries", []):
        rel = entry.get("relative_path")
        if not isinstance(rel, str):
            return False
        try:
            target = paths.safe_target(inst.runtime, rel)
        except UnsafePathError:
            return False
        if entry.get("action") in ("created", "new"):
            if not target.is_file():
                return False
            try:
                if paths.sha256_file(target) != entry.get("applied_sha256"):
                    return False
            except OSError:
                return False
        elif entry.get("action") == "replaced":
            if not target.is_file():
                return False
            if paths.sha256_file(target) != entry.get("applied_sha256"):
                return False
        elif entry.get("action") == "identical":
            if not target.is_file():
                return False
            if paths.sha256_file(target) != entry.get("applied_sha256"):
                return False
    return True


def _restore_backup(entry: dict[str, Any], backup: Path, target: Path) -> None:
    backup_info = entry.get("backup", {})
    expected_sha = backup_info.get("sha256")
    if not expected_sha:
        raise ManifestError("manifest entry has no backup hash", details={"entry": entry})
    actual = _hash_if_regular(backup, "backup")
    if actual is None:
        raise ManifestError("backup file is missing", details={"path": str(backup)})
    if actual[0] != expected_sha:
        raise ManifestError(
            "backup hash does not match the manifest",
            details={"path": str(backup), "expected": expected_sha, "actual": actual[0]},
        )
    original = entry.get("original", {})
    mode = int(original.get("mode") or 0o644)
    _copy_file(backup, target, expected_sha=expected_sha, mode=mode, create_new=False)


def _cleanup_temp_files(runtime: Path, entries: list[dict[str, Any]]) -> None:
    """Remove only our own abandoned temp files next to planned targets."""

    for entry in entries:
        rel = entry.get("relative_path")
        if not isinstance(rel, str):
            continue
        try:
            target = paths.safe_target(runtime, rel)
        except UnsafePathError:
            continue
        parent = target.parent
        if not parent.is_dir():
            continue
        for candidate in parent.glob(f".{target.name}.pz13th-*.tmp"):
            try:
                st = os.lstat(candidate)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                try:
                    candidate.unlink()
                except OSError:
                    pass


def recover_pending(state_dir: Path) -> dict[str, Any] | None:
    pending = manifest.load_pending(state_dir)
    if pending is None:
        return None

    layout = manifest.layouts(state_dir)
    if layout["manifest"].is_file():
        current = manifest.load_manifest(state_dir)
        if current is not None:
            try:
                manifest.verify_owner(state_dir, current)
            except ManifestError:
                # The final manifest exists but its owner has not been written yet.
                game = current.get("game", {})
                if not layout["owner"].is_file() and current.get("state_id") == pending.get("state_id"):
                    manifest.write_owner(
                        state_dir,
                        layout["manifest"],
                        str(current.get("state_id")),
                        Path(game.get("runtime", "/")),
                        str(game.get("jar_sha256", "")),
                    )
                else:
                    raise
            manifest.remove_pending(state_dir)
            return {"recovered": "committed"}

    game = pending.get("game", {})
    runtime_value = game.get("runtime")
    if not runtime_value:
        raise ManifestError("pending manifest has no runtime path")
    runtime = Path(runtime_value)
    if paths.game_running(runtime):
        raise ConflictError("the game is running; recovery would modify runtime files")

    errors: list[str] = []
    for entry in reversed(pending.get("entries", [])):
        if not isinstance(entry, dict):
            continue
        action = entry.get("action")
        rel = entry.get("relative_path")
        if action == "identical":
            continue
        try:
            target = paths.safe_target(runtime, str(rel))
        except UnsafePathError as exc:
            errors.append(f"{rel}: {exc.message}")
            continue
        source_sha = entry.get("source_sha256")
        original = entry.get("original", {})
        existing = None
        try:
            existing = _hash_if_regular(target, "target")
        except UnsafePathError as exc:
            errors.append(f"{rel}: {exc.message}")
            continue
        if action in ("created", "new"):
            if existing is None:
                continue
            if existing[0] == source_sha:
                try:
                    target.unlink()
                except OSError as exc:
                    errors.append(f"{rel}: cannot remove created file: {exc}")
            else:
                errors.append(f"{rel}: created target was modified unexpectedly")
        elif action == "replaced":
            backup_rel = entry.get("backup", {}).get("relative_path")
            if not backup_rel:
                errors.append(f"{rel}: replaced entry has no backup path")
                continue
            try:
                backup = manifest.backup_path(state_dir, str(pending.get("state_id")), str(backup_rel))
            except (ManifestError, UnsafePathError) as exc:
                errors.append(f"{rel}: invalid backup path: {exc}")
                continue
            if existing is None:
                errors.append(f"{rel}: replaced target is missing")
                continue
            if existing[0] == source_sha:
                try:
                    _restore_backup(entry, backup, target)
                except Exception as exc:
                    errors.append(f"{rel}: cannot restore backup: {exc}")
            elif original.get("exists") and existing[0] == original.get("sha256"):
                continue
            else:
                errors.append(f"{rel}: replaced target was modified unexpectedly")

    if errors:
        raise TransactionError(
            "cannot recover the interrupted transaction safely; no further files were changed",
            details={"errors": errors, "pending": str(layout["pending"])},
        )

    created_dirs = [Path(item) for item in pending.get("created_dirs", [])]
    _cleanup_temp_files(runtime, pending.get("entries", []))
    paths.remove_empty_dirs(runtime, created_dirs)
    manifest.remove_backup_set(state_dir, str(pending.get("state_id")))
    manifest.remove_pending(state_dir)
    return {"recovered": "rolled_back", "state_id": pending.get("state_id")}


def apply(
    inst: Installation,
    inventory: dict[str, Any],
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    if dry_run:
        plan = make_plan(inst, inventory, dry_run=True)
        return {
            "ok": True,
            "dry_run": True,
            "would_change": True,
            "plan": plan,
            "summary": summarize_plan(plan),
            "state_written": False,
        }

    if inst.layout not in platforms.NATIVE_LAYOUTS:
        raise ConflictError(
            "optimization may only be applied inside a verified native runtime directory",
            details={"layout": inst.layout, "wrapper": str(inst.wrapper), "runtime": str(inst.runtime)},
        )
    info = payload.build_info(inst.payload)
    jar = payload.validate_runtime_jar(inst.runtime, info)
    if not jar.get("match"):
        raise ConflictError(
            "runtime jar does not match the payload build info; refusing to apply",
            details={"jar": jar},
        )
    if paths.game_running(inst.runtime):
        raise ConflictError("the game is running from the runtime directory; quit it before applying")

    with FileLock(inst.state_dir):
        recover_pending(inst.state_dir)
        existing = manifest.load_manifest(inst.state_dir)
        if existing is not None:
            manifest.validate_manifest(existing)
            manifest.verify_owner(inst.state_dir, existing)
            if existing.get("payload", {}).get("inventory_sha256") == inventory["inventory_sha256"]:
                if _manifest_targets_match(inst, existing):
                    return {
                        "ok": True,
                        "already_applied": True,
                        "state_id": existing.get("state_id"),
                        "summary": {
                            "entry_count": len(existing.get("entries", [])),
                            "action": "no changes",
                        },
                        "state_written": False,
                    }
            raise ConflictError(
                "a different optimization manifest already exists; run restore before applying again",
                details={"state_id": existing.get("state_id")},
            )

        plan = make_plan(inst, inventory)
        plan["state_id"] = manifest.new_state_id()
        plan["created_at"] = manifest.utc_now()
        # Recompute backup paths because the state id changed.
        for entry in plan["entries"]:
            if entry["action"] == "replaced":
                rel = entry["relative_path"]
                entry["backup"]["relative_path"] = rel
                entry["backup"]["path"] = str(manifest.backup_path(inst.state_dir, plan["state_id"], rel))
        manifest.write_pending(inst.state_dir, plan)

        try:
            for entry in plan["entries"]:
                action = entry["action"]
                rel = entry["relative_path"]
                target = paths.safe_target(inst.runtime, rel)
                if action == "identical":
                    entry["status"] = "unchanged"
                    manifest.write_pending(inst.state_dir, plan)
                    continue

                if action == "created" or action == "new":
                    created = paths.ensure_parent_chain(inst.runtime, target.parent)
                    for directory in created:
                        value = str(directory)
                        if value not in plan["created_dirs"]:
                            plan["created_dirs"].append(value)
                    if created:
                        manifest.write_pending(inst.state_dir, plan)
                    existing_target = _hash_if_regular(target, "target")
                    if existing_target is not None:
                        raise ConflictError(
                            "target appeared after preflight",
                            details={"relative_path": rel, "target": str(target)},
                        )
                    _copy_file(
                        Path(entry["source_path"]),
                        target,
                        expected_sha=entry["source_sha256"],
                        mode=int(entry["source_mode"]),
                        create_new=True,
                    )
                elif action == "replaced":
                    existing_target = _hash_if_regular(target, "target")
                    if existing_target is None or existing_target[0] != entry["original"]["sha256"]:
                        raise ConflictError(
                            "target changed after preflight; refusing to overwrite",
                            details={"relative_path": rel, "target": str(target)},
                        )
                    backup = Path(entry["backup"]["path"])
                    _backup_file(target, backup, existing_target[0], inst.state_dir / "backups")
                    backup_st = _hash_if_regular(backup, "backup")
                    if backup_st is None:
                        raise TransactionError("backup was not created", details={"path": str(backup)})
                    entry["backup"]["sha256"] = backup_st[0]
                    entry["backup"]["size"] = backup_st[1]
                    # Recheck immediately before replacing.
                    again = _hash_if_regular(target, "target")
                    if again is None or again[0] != existing_target[0]:
                        raise ConflictError(
                            "target changed while preparing the backup; refusing to overwrite",
                            details={"relative_path": rel, "target": str(target)},
                        )
                    _copy_file(
                        Path(entry["source_path"]),
                        target,
                        expected_sha=entry["source_sha256"],
                        mode=int(entry["source_mode"]),
                        create_new=False,
                    )
                else:
                    raise TransactionError(f"unknown action {action!r} for {rel}")

                entry["status"] = "applied"
                manifest.write_pending(inst.state_dir, plan)

            plan["committed_at"] = manifest.utc_now()
            final_path = manifest.write_final_manifest(inst.state_dir, plan)
            manifest.write_owner(
                inst.state_dir,
                final_path,
                plan["state_id"],
                inst.runtime,
                plan["game"]["jar_sha256"],
            )
            manifest.remove_pending(inst.state_dir)
        except BaseException as exc:
            recovery_error = None
            try:
                recover_pending(inst.state_dir)
            except Exception as rec_exc:  # noqa: BLE001
                recovery_error = str(rec_exc)
            if recovery_error:
                raise TransactionError(
                    f"apply failed: {exc}; recovery also failed: {recovery_error}",
                    details={"pending": str(inst.state_dir / "manifest.pending.json")},
                ) from exc
            raise

    return {
        "ok": True,
        "dry_run": False,
        "state_id": plan["state_id"],
        "summary": summarize_plan(plan),
        "manifest": str(inst.state_dir / "manifest.json"),
        "backup_directory": str(inst.state_dir / "backups" / plan["state_id"]),
        "state_written": True,
    }


def _restore_preflight(inst: Installation, current: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    actions: list[dict[str, Any]] = []
    errors: list[str] = []
    for entry in current.get("entries", []):
        rel = entry.get("relative_path")
        if not isinstance(rel, str):
            errors.append("manifest entry has no relative path")
            continue
        try:
            target = paths.safe_target(inst.runtime, rel)
        except UnsafePathError as exc:
            errors.append(f"{rel}: {exc.message}")
            continue
        try:
            existing = _hash_if_regular(target, "target")
        except UnsafePathError as exc:
            errors.append(f"{rel}: {exc.message}")
            continue
        action = entry.get("action")
        if action in ("created", "new"):
            if existing is None:
                actions.append({"relative_path": rel, "action": "already_absent"})
            elif existing[0] == entry.get("applied_sha256"):
                actions.append({"relative_path": rel, "action": "delete"})
            else:
                errors.append(f"{rel}: created target was modified unexpectedly")
        elif action == "replaced":
            if existing is None:
                errors.append(f"{rel}: replaced target is missing")
                continue
            backup_info = entry.get("backup", {})
            backup_rel = backup_info.get("relative_path")
            if not backup_rel:
                errors.append(f"{rel}: no backup path")
                continue
            try:
                backup = manifest.backup_path(inst.state_dir, str(current.get("state_id")), str(backup_rel))
            except (ManifestError, UnsafePathError) as exc:
                errors.append(f"{rel}: invalid backup path: {exc}")
                continue
            try:
                backup_st = _hash_if_regular(backup, "backup")
            except UnsafePathError as exc:
                errors.append(f"{rel}: {exc.message}")
                continue
            if backup_st is None:
                errors.append(f"{rel}: backup is missing")
                continue
            expected_backup = backup_info.get("sha256")
            if not expected_backup or backup_st[0] != expected_backup:
                errors.append(f"{rel}: backup hash does not match the manifest")
                continue
            if existing[0] == entry.get("applied_sha256"):
                actions.append({"relative_path": rel, "action": "restore", "backup": str(backup)})
            elif existing[0] == entry.get("original", {}).get("sha256"):
                actions.append({"relative_path": rel, "action": "already_restored"})
            else:
                errors.append(f"{rel}: target was modified unexpectedly")
        elif action == "identical":
            if existing is None:
                errors.append(f"{rel}: unchanged target is missing")
            elif existing[0] != entry.get("applied_sha256"):
                errors.append(f"{rel}: unchanged target was modified unexpectedly")
            else:
                actions.append({"relative_path": rel, "action": "unchanged"})
        else:
            errors.append(f"{rel}: unknown action {action!r}")
    return actions, errors


def _restore_apply(inst: Installation, current: dict[str, Any], actions: list[dict[str, Any]]) -> int:
    changed = 0
    for action in actions:
        rel = action["relative_path"]
        entry = next(
            (item for item in current.get("entries", []) if item.get("relative_path") == rel),
            None,
        )
        if entry is None:
            raise ManifestError("manifest entry disappeared during restore", details={"relative_path": rel})
        target = paths.safe_target(inst.runtime, rel)
        if action["action"] == "delete":
            existing = _hash_if_regular(target, "target")
            if existing is None:
                continue
            if existing[0] != entry.get("applied_sha256"):
                raise ConflictError(
                    "target changed after restore preflight; refusing to delete it",
                    details={"relative_path": rel, "target": str(target)},
                )
            target.unlink()
            changed += 1
        elif action["action"] == "restore":
            backup = Path(action["backup"])
            existing = _hash_if_regular(target, "target")
            if existing is None:
                raise ConflictError(
                    "target disappeared after restore preflight",
                    details={"relative_path": rel, "target": str(target)},
                )
            if existing[0] == entry.get("original", {}).get("sha256"):
                continue
            if existing[0] != entry.get("applied_sha256"):
                raise ConflictError(
                    "target changed after restore preflight; refusing to overwrite it",
                    details={"relative_path": rel, "target": str(target)},
                )
            _restore_backup(entry, backup, target)
            changed += 1
    return changed


def restore(inst: Installation, *, dry_run: bool = False) -> dict[str, Any]:
    if dry_run:
        if manifest.load_pending(inst.state_dir) is not None:
            raise TransactionError(
                "an interrupted apply is pending; run restore without --dry-run to recover it first"
            )
        current = manifest.load_manifest(inst.state_dir)
        if current is None:
            raise ManifestError("no applied optimization manifest exists", details={"state_dir": str(inst.state_dir)})
        manifest.validate_manifest(current)
        manifest.verify_owner(inst.state_dir, current)
        if paths.game_running(inst.runtime):
            raise ConflictError("the game is running from the runtime directory; quit it before restoring")
        game = current.get("game", {})
        if str(paths.realpath(inst.runtime)) != game.get("runtime"):
            raise ManifestError(
                "manifest belongs to a different runtime directory",
                details={"manifest_runtime": game.get("runtime"), "runtime": str(paths.realpath(inst.runtime))},
            )
        jar = payload.validate_runtime_jar(
            inst.runtime,
            {"jar_sha256": game.get("jar_sha256"), "jar_size": game.get("jar_size")},
        )
        if not jar.get("match"):
            raise ManifestError("runtime jar no longer matches the manifest", details={"jar": jar})
        actions, errors = _restore_preflight(inst, current)
        if errors:
            raise ConflictError(
                "restore preflight failed; no files were changed",
                details={"errors": errors, "state_id": current.get("state_id")},
            )
        return {
            "ok": True,
            "dry_run": True,
            "state_id": current.get("state_id"),
            "actions": actions,
            "state_written": False,
        }

    with FileLock(inst.state_dir):
        recover_pending(inst.state_dir)
        layout = manifest.layouts(inst.state_dir)
        current = manifest.load_manifest(inst.state_dir)
        if current is None:
            raise ManifestError("no applied optimization manifest exists", details={"state_dir": str(inst.state_dir)})
        manifest.validate_manifest(current)
        manifest.verify_owner(inst.state_dir, current)
        if paths.game_running(inst.runtime):
            raise ConflictError("the game is running from the runtime directory; quit it before restoring")
        game = current.get("game", {})
        if str(paths.realpath(inst.runtime)) != game.get("runtime"):
            raise ManifestError(
                "manifest belongs to a different runtime directory",
                details={"manifest_runtime": game.get("runtime"), "runtime": str(paths.realpath(inst.runtime))},
            )
        jar = payload.validate_runtime_jar(
            inst.runtime,
            {"jar_sha256": game.get("jar_sha256"), "jar_size": game.get("jar_size")},
        )
        if not jar.get("match"):
            raise ManifestError("runtime jar no longer matches the manifest", details={"jar": jar})

        actions, errors = _restore_preflight(inst, current)
        if errors:
            raise ConflictError(
                "restore preflight failed; no files were changed",
                details={"errors": errors, "state_id": current.get("state_id")},
            )
        changed = _restore_apply(inst, current, actions)
        paths.remove_empty_dirs(inst.runtime, [Path(item) for item in current.get("created_dirs", [])])
        try:
            layout["manifest"].unlink()
        except FileNotFoundError:
            pass
        try:
            layout["owner"].unlink()
        except FileNotFoundError:
            pass
        manifest.fsync_dir(layout["state_dir"])
        manifest.remove_backup_set(inst.state_dir, str(current.get("state_id")))

    return {
        "ok": True,
        "dry_run": False,
        "state_id": current.get("state_id"),
        "changed": changed,
        "actions": actions,
        "state_written": True,
    }
