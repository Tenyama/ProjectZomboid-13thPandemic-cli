"""Command-line interface for pz13th."""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from . import __version__, detection, launch, output, payload, platforms, transaction
from .errors import ConflictError, PzError, UsageError, ValidationError

DEFAULT_HOST = launch.DEFAULT_HOST


def _default_config_path() -> Path:
    return platforms.default_config_path()


def load_config(value: str | None) -> dict[str, Any]:
    path = Path(value).expanduser() if value else _default_config_path()
    if not path.is_file():
        if value:
            raise UsageError(f"config file does not exist: {path}", details={"path": str(path)})
        return {}
    try:
        import tomllib
    except ImportError as exc:  # pragma: no cover - Python < 3.11
        raise UsageError("Python 3.11 or newer is required for TOML config support") from exc
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError) as exc:
        raise UsageError(f"cannot read config {path}: {exc}", details={"path": str(path)}) from exc
    if not isinstance(data, dict):
        raise UsageError("config root must be a table", details={"path": str(path)})
    return data


def _section(config: dict[str, Any], name: str) -> dict[str, Any]:
    value = config.get(name, {})
    return value if isinstance(value, dict) else {}


def _pick(args: argparse.Namespace, name: str, default: Any = None) -> Any:
    return getattr(args, name, default)


def build_options(args: argparse.Namespace, config: dict[str, Any]) -> SimpleNamespace:
    paths_cfg = _section(config, "paths")
    server_cfg = _section(config, "server")
    launch_cfg = _section(config, "launch")

    steam_library = _pick(args, "steam_library") or paths_cfg.get("steam_library")
    workshop_item = _pick(args, "workshop_item") or paths_cfg.get("workshop_item")
    wrapper_root = _pick(args, "wrapper_root") or paths_cfg.get("wrapper_root")
    runtime = _pick(args, "runtime") or paths_cfg.get("runtime")
    payload_path = _pick(args, "payload") or paths_cfg.get("payload")
    data_dir = _pick(args, "data_dir") or paths_cfg.get("data_dir")
    state_dir = _pick(args, "state_dir") or paths_cfg.get("state_dir")
    if state_dir == "":
        state_dir = None

    port = _pick(args, "port")
    if port is None:
        port = server_cfg.get("port")
    host = _pick(args, "host") or server_cfg.get("host") or DEFAULT_HOST
    method = _pick(args, "method") or launch_cfg.get("method") or "applaunch"
    zombie_buddy = _pick(args, "zombie_buddy") or launch_cfg.get("zombie_buddy") or "auto"
    steam_binary = _pick(args, "steam_binary") or launch_cfg.get("steam_binary") or platforms.default_steam_binary()

    return SimpleNamespace(
        steam_library=steam_library,
        workshop_item=workshop_item,
        wrapper_root=wrapper_root,
        runtime=runtime,
        payload=payload_path,
        data_dir=data_dir,
        state_dir=state_dir,
        steam_user=_pick(args, "steam_user"),
        steam_binary=steam_binary,
        host=host,
        port=port,
        method=method,
        zombie_buddy=zombie_buddy,
        json=bool(_pick(args, "json", False)),
        verbose=bool(_pick(args, "verbose", False)),
        config_path=_pick(args, "config"),
    )


def _print_paths(report: dict[str, Any]) -> None:
    paths = report["paths"]
    output.print_kv(
        [
            ("Steam library", paths.get("steam_library") or "(not selected)"),
            ("Wrapper root", paths.get("wrapper")),
            ("Native runtime", paths.get("runtime")),
            ("Workshop item", paths.get("workshop_item") or "(missing)"),
            ("Mod directory", paths.get("mod_dir") or "(missing)"),
            ("Payload", paths.get("payload")),
            ("Data directory", paths.get("data_dir")),
            ("State directory", paths.get("state_dir")),
            ("Layout", report.get("layout")),
        ]
    )


def _print_payload(report: dict[str, Any]) -> None:
    info = report["payload"]
    targets = report["targets"]
    counts = targets["counts"]
    output.print_section("Payload")
    output.print_kv(
        [
            ("Total files", info["total_count"]),
            ("Included", info["included_count"]),
            ("Excluded", info["excluded_count"]),
            ("Custom videos", info["custom_video_count"]),
            ("Inventory SHA-256", info["inventory_sha256"]),
            ("New files", counts["new"]),
            ("Identical", counts["identical"]),
            ("Replacements", counts["replaced"]),
            ("Unsafe targets", counts["unsafe"]),
        ]
    )
    if info["excluded"]:
        output.print_section("Excluded payload files")
        for item in info["excluded"]:
            sys.stdout.write(f"{item['relative_path']}  [{item['reason']}]\n")
    if targets["conflicts"]:
        output.print_section("Replacement conflicts requiring backup")
        for item in targets["conflicts"]:
            sys.stdout.write(f"{item['relative_path']}  (target differs; backup required)\n")
    if targets["videos"]:
        output.print_section("Custom videos")
        for item in targets["videos"]:
            sys.stdout.write(f"{item['relative_path']}  [{item['action']}]\n")
    if targets["unsafe"]:
        output.print_section("Unsafe targets")
        for item in targets["unsafe"]:
            sys.stdout.write(f"{item['relative_path']}: {item['reason']}\n")
    unexpected = targets.get("unexpected", [])
    if unexpected:
        output.print_section("Untracked runtime overlay files")
        for item in unexpected:
            sys.stdout.write(f"{item}\n")


def _print_warnings(report: dict[str, Any]) -> None:
    warnings = report.get("warnings", [])
    if warnings:
        output.print_section("Warnings")
        for warning in warnings:
            sys.stdout.write(f"- {warning}\n")


def print_detection(report: dict[str, Any], *, verbose: bool = False) -> None:
    _print_paths(report)
    jar = report["jar"]
    output.print_section("Runtime JAR")
    output.print_kv(
        [
            ("Path", jar["path"]),
            ("Size", jar["size"]),
            ("SHA-256", jar["sha256"]),
            ("Expected size", jar["expected_size"]),
            ("Expected SHA-256", jar["expected_sha256"]),
            ("Matches payload", "yes" if jar["match"] else "NO"),
        ]
    )
    _print_payload(report)
    windows = report.get("windows_launcher", {})
    if windows.get("present"):
        output.print_section("Windows launcher state (read-only)")
        output.print_kv(
            [
                ("Path", windows.get("path")),
                ("Entries", windows.get("entry_count")),
                ("States", windows.get("states")),
                ("Backups recorded", windows.get("backups")),
                ("Owner hash matches", windows.get("owner_hash_matches")),
            ]
        )
    legacy = report.get("legacy_wrapper", {})
    if legacy.get("present"):
        output.print_section("Legacy wrapper-root files (read-only)")
        output.print_kv([(name, "present" if value else "absent") for name, value in legacy.get("markers", {}).items()])
        sys.stdout.write(legacy.get("note", "") + "\n")
    _print_warnings(report)


def print_status(report: dict[str, Any], applied: dict[str, Any], *, verbose: bool = False) -> None:
    print_detection(report, verbose=verbose)
    output.print_section("Applied optimization")
    output.print_kv(
        [
            ("State", applied.get("state")),
            ("State id", applied.get("state_id")),
            ("Created", applied.get("created_at")),
            ("Runtime matches", applied.get("runtime_matches")),
            ("JAR matches", applied.get("jar_matches")),
            ("Entries", applied.get("entry_count")),
            ("Missing", len(applied.get("missing", []))),
            ("Modified", len(applied.get("modified", []))),
            ("Untracked overlay", applied.get("untracked_overlay_count")),
            ("Source changed", applied.get("source_changed")),
            ("Backup directory", applied.get("backup_directory")),
        ]
    )
    if applied.get("missing"):
        output.print_section("Missing targets")
        for item in applied["missing"]:
            sys.stdout.write(f"{item}\n")
    if applied.get("modified"):
        output.print_section("Modified targets")
        for item in applied["modified"]:
            sys.stdout.write(f"{item}\n")
    if applied.get("error"):
        output.print_section("State error")
        sys.stdout.write(str(applied["error"]) + "\n")


def _print_plan_summary(summary: dict[str, Any], *, verbose: bool, plan: dict[str, Any] | None = None) -> None:
    counts = summary["counts"]
    output.print_section("Plan")
    output.print_kv(
        [
            ("New", counts.get("new", 0)),
            ("Replacements", counts.get("replaced", 0)),
            ("Identical", counts.get("identical", 0)),
            ("Applied", counts.get("applied", 0)),
            ("Excluded", len(summary.get("excluded", []))),
        ]
    )
    for item in summary.get("conflicts", []):
        sys.stdout.write(f"REPLACE {item['relative_path']} (backup required)\n")
    if verbose and plan is not None:
        output.print_section("Actions")
        for entry in plan.get("entries", []):
            sys.stdout.write(f"{entry['action']:<9} {entry['relative_path']}\n")


def print_apply_result(result: dict[str, Any], *, verbose: bool = False) -> None:
    if result.get("dry_run"):
        _print_plan_summary(result["summary"], verbose=verbose, plan=result.get("plan"))
        sys.stdout.write("\nDry run: no files, directories, locks, manifests, or backups were created.\n")
        return
    if result.get("already_applied"):
        sys.stdout.write("Optimization is already applied; no files changed.\n")
        return
    summary = result.get("summary", {})
    counts = summary.get("counts", {})
    output.print_kv(
        [
            ("State id", result.get("state_id")),
            ("Applied", counts.get("applied", 0)),
            ("New", counts.get("new", 0)),
            ("Replacements", counts.get("replaced", 0)),
            ("Manifest", result.get("manifest")),
            ("Backups", result.get("backup_directory")),
        ]
    )


def print_restore_result(result: dict[str, Any]) -> None:
    if result.get("dry_run"):
        output.print_section("Restore plan")
        for action in result.get("actions", []):
            sys.stdout.write(f"{action['action']:<15} {action['relative_path']}\n")
        sys.stdout.write("\nDry run: no files were changed.\n")
        return
    output.print_kv(
        [
            ("State id", result.get("state_id")),
            ("Files changed", result.get("changed")),
        ]
    )


def print_launch(plan: dict[str, Any], *, dry_run: bool, result: dict[str, Any] | None = None) -> None:
    output.print_kv(
        [
            ("Method", plan.get("method")),
            ("Data directory", plan.get("cachedir")),
            ("Server", plan.get("host")),
            ("Port", plan.get("port")),
            ("Steam binary", plan.get("steam_binary_resolved") or plan.get("steam_binary")),
        ]
    )
    zombie = plan.get("zombie_buddy", {})
    output.print_section("ZombieBuddy")
    output.print_kv(
        [
            ("Enabled", zombie.get("enabled")),
            ("Mode", zombie.get("mode")),
            ("Method", zombie.get("method")),
            ("JAR", zombie.get("jar")),
            ("pzexe config", zombie.get("config_path")),
            ("Existing Steam option", zombie.get("existing_steam_option")),
        ]
    )
    mods = plan.get("mods_alias", {})
    output.print_section("Mods directory case")
    output.print_kv(
        [
            ("Uppercase Mods", mods.get("upper")),
            ("Lowercase mods", mods.get("lower")),
            ("Status", mods.get("status")),
            ("Action", mods.get("action")),
            ("Entries", mods.get("entry_count")),
            ("Case-fixed Mod.info", len(plan.get("mod_info_case", []))),
            ("Active list additions", len(plan.get("active_mods", []))),
        ]
    )
    output.print_section("Our arguments")
    for arg in plan.get("args", []):
        sys.stdout.write(f"{arg}\n")
    output.print_section("Command")
    if plan.get("url") and plan.get("method") == "run":
        sys.stdout.write(plan["url"] + "\n")
    else:
        sys.stdout.write(" ".join(plan.get("argv", [])) + "\n")
    entries = plan.get("launch_options", [])
    output.print_section("Existing Steam launch options")
    if not entries:
        sys.stdout.write("(none found)\n")
    else:
        for entry in entries:
            sys.stdout.write(f"{entry.get('path')}: {entry.get('options')}\n")
    warnings = plan.get("warnings", [])
    if warnings:
        output.print_section("Warnings")
        for warning in warnings:
            sys.stdout.write(f"- {warning}\n")
    if dry_run:
        sys.stdout.write("\nDry run: Steam was not started.\n")
    elif result is not None:
        output.print_section("Launch result")
        output.print_kv(
            [
                ("Started", result.get("started")),
                ("Steam return code", result.get("returncode")),
            ]
        )
        sys.stdout.write(result.get("note", "") + "\n")


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="emit JSON")
    parser.add_argument("--verbose", action="store_true", default=argparse.SUPPRESS, help="verbose output")
    parser.add_argument("--steam-library", default=argparse.SUPPRESS, help="Steam library root override")
    parser.add_argument("--workshop-item", default=argparse.SUPPRESS, help="Workshop item directory override")
    parser.add_argument("--wrapper-root", default=argparse.SUPPRESS, help="game wrapper-root override")
    parser.add_argument("--runtime", default=argparse.SUPPRESS, help="native runtime directory override")
    parser.add_argument("--payload", default=argparse.SUPPRESS, help="optimization payload override")
    parser.add_argument("--data-dir", default=argparse.SUPPRESS, help="preconfigured Zomboid data directory override")
    parser.add_argument("--state-dir", default=argparse.SUPPRESS, help="CLI state directory override")
    parser.add_argument("--config", default=argparse.SUPPRESS, help="config.toml path")
    parser.add_argument("--steam-user", default=argparse.SUPPRESS, help="Steam userdata account id")
    parser.add_argument("--steam-binary", default=argparse.SUPPRESS, help="Steam executable or command")
    parser.add_argument("--host", default=argparse.SUPPRESS, help="server host")
    parser.add_argument("--port", type=int, default=argparse.SUPPRESS, help="server port (required for launch)")
    parser.add_argument(
        "--method",
        choices=launch.METHODS,
        default=argparse.SUPPRESS,
        help="Steam launch method",
    )
    parser.add_argument(
        "--zombie-buddy",
        choices=("auto", "on", "off"),
        default=argparse.SUPPRESS,
        help="inject ZombieBuddy (pzexeconfig on Linux, launch arguments on macOS; default: auto)",
    )


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    _add_common(common)
    parser = argparse.ArgumentParser(
        prog="pz13th",
        description="Native Linux/macOS launcher for the 13thPandemic Project Zomboid modpack",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")

    detect_parser = sub.add_parser("detect", parents=[common], help="detect installation paths (read-only)")
    detect_parser.set_defaults(command="detect")

    status_parser = sub.add_parser("status", parents=[common], help="show detection and optimization status (read-only)")
    status_parser.set_defaults(command="status")

    launch_parser = sub.add_parser("launch", parents=[common], help="launch through Steam")
    launch_parser.add_argument("--dry-run", action="store_true", help="print the command without launching")
    launch_parser.set_defaults(command="launch")

    apply_parser = sub.add_parser("apply", parents=[common], help="apply the optimization overlay")
    apply_parser.add_argument("--dry-run", action="store_true", help="plan only; create nothing")
    apply_parser.set_defaults(command="apply")

    restore_parser = sub.add_parser("restore", parents=[common], help="restore files owned by this CLI")
    restore_parser.add_argument("--dry-run", action="store_true", help="plan only; create nothing")
    restore_parser.set_defaults(command="restore")

    return parser


def _run(args: argparse.Namespace, opts: SimpleNamespace) -> int:
    inst = detection.resolve_installation(opts)

    if args.command == "detect":
        inventory = payload.inventory(inst.payload)
        report = detection.build_detection(inst, opts, inventory)
        if opts.json:
            output.emit_json(report)
        else:
            print_detection(report, verbose=opts.verbose)
        return 0

    if args.command == "status":
        inventory = payload.inventory(inst.payload)
        report = detection.build_detection(inst, opts, inventory)
        applied = detection.applied_status(inst, inventory)
        if opts.json:
            report["applied"] = applied
            output.emit_json(report)
        else:
            print_status(report, applied, verbose=opts.verbose)
        return 0

    if args.command == "launch":
        options = detection.launch_options_for(inst, opts)
        plan = launch.build_plan(
            data_dir=inst.data_dir,
            host=opts.host,
            port=opts.port,
            method=opts.method,
            steam_binary=opts.steam_binary,
            launch_options=options,
            runtime=inst.runtime,
            state_dir=inst.state_dir,
            zombie_buddy=opts.zombie_buddy,
        )
        if opts.json:
            if getattr(args, "dry_run", False):
                output.emit_json({"ok": True, "plan": plan, "dry_run": True})
                return 0
            result = launch.run(plan)
            output.emit_json({"ok": True, "plan": plan, "result": result})
            return 0
        if getattr(args, "dry_run", False):
            print_launch(plan, dry_run=True)
            return 0
        result = launch.run(plan)
        print_launch(plan, dry_run=False, result=result)
        return 0

    if args.command == "apply":
        if inst.layout not in platforms.NATIVE_LAYOUTS:
            raise ConflictError(
                "optimization may only be applied inside a verified native runtime directory",
                details={"layout": inst.layout, "wrapper": str(inst.wrapper), "runtime": str(inst.runtime)},
            )
        inventory = payload.inventory(inst.payload)
        info = payload.build_info(inst.payload)
        jar = payload.validate_runtime_jar(inst.runtime, info)
        if not jar.get("match"):
            raise ConflictError(
                "runtime jar does not match the payload build info; refusing optimization",
                details={"jar": jar},
            )
        result = transaction.apply(inst, inventory, dry_run=bool(getattr(args, "dry_run", False)))
        if opts.json:
            output.emit_json(result)
        else:
            print_apply_result(result, verbose=opts.verbose)
        return 0

    if args.command == "restore":
        result = transaction.restore(inst, dry_run=bool(getattr(args, "dry_run", False)))
        if opts.json:
            output.emit_json(result)
        else:
            print_restore_result(result)
        return 0

    raise UsageError(f"unknown command {args.command!r}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2

    as_json = bool(getattr(args, "json", False))
    try:
        config = load_config(getattr(args, "config", None))
        opts = build_options(args, config)
        return _run(args, opts)
    except PzError as exc:
        output.emit_error(exc, as_json)
        return exc.exit_code
    except KeyboardInterrupt:
        sys.stderr.write("interrupted\n")
        return 130
    except Exception as exc:  # noqa: BLE001
        if getattr(args, "verbose", False):
            traceback.print_exc()
        wrapped = ValidationError(f"unexpected error: {exc}")
        output.emit_error(wrapped, as_json)
        return wrapped.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
