"""Steam library discovery and read-only launch-option inspection."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

from .errors import DetectionError
from . import paths, platforms


class VDFError(ValueError):
    pass


_TOKEN_RE = re.compile(
    r'\s*(?:(//[^\n]*)|([{}])|"((?:\\.|[^"\\])*)"|([^\s{}"]+))',
    re.DOTALL,
)


def _unescape(value: str) -> str:
    output: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            nxt = value[index + 1]
            output.append(
                {
                    "n": "\n",
                    "t": "\t",
                    "r": "\r",
                    '"': '"',
                    "\\": "\\",
                }.get(nxt, nxt)
            )
            index += 2
        else:
            output.append(char)
            index += 1
    return "".join(output)


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    position = 0
    length = len(text)
    while position < length:
        match = _TOKEN_RE.match(text, position)
        if not match:
            if text[position:].strip() == "":
                break
            raise VDFError(f"cannot tokenize VDF near offset {position}")
        position = match.end()
        if match.group(1) is not None:
            continue
        if match.group(2) is not None:
            tokens.append(("brace", match.group(2)))
        elif match.group(3) is not None:
            tokens.append(("string", _unescape(match.group(3))))
        else:
            tokens.append(("string", match.group(4)))
    return tokens


def _parse_block(tokens: list[tuple[str, str]], index: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while index < len(tokens):
        kind, value = tokens[index]
        if kind == "brace":
            if value == "}":
                return result, index + 1
            raise VDFError("unexpected opening brace in VDF value position")
        if kind != "string":
            raise VDFError("unexpected token in VDF key position")
        key = value
        index += 1
        if index >= len(tokens):
            raise VDFError("VDF key without value")
        next_kind, next_value = tokens[index]
        if next_kind == "brace":
            if next_value != "{":
                raise VDFError("unexpected closing brace in VDF value position")
            value, index = _parse_block(tokens, index + 1)
        elif next_kind == "string":
            value = next_value
            index += 1
        else:
            raise VDFError("unexpected token in VDF value position")
        result[key] = value
    return result, index


def parse_vdf(text: str) -> dict[str, Any]:
    if text.startswith("\ufeff"):
        text = text[1:]
    tokens = _tokenize(text)
    if not tokens:
        return {}
    result, index = _parse_block(tokens, 0)
    if index != len(tokens):
        raise VDFError("trailing tokens after VDF root block")
    return result


def default_steam_roots() -> list[Path]:
    return platforms.steam_roots()


def _library_paths_from_vdf(vdf_path: Path) -> list[Path]:
    try:
        text = vdf_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    try:
        parsed = parse_vdf(text)
    except VDFError:
        return []
    container = parsed.get("libraryfolders")
    if not isinstance(container, dict):
        container = parsed.get("LibraryFolders")
    if not isinstance(container, dict):
        container = parsed
    result: list[Path] = []
    for key, value in container.items():
        if isinstance(value, dict) and isinstance(value.get("path"), str):
            result.append(Path(value["path"]))
        elif isinstance(value, str) and (str(key).isdigit() or str(key).startswith("/")):
            if value.startswith("/"):
                result.append(Path(value))
    return result


def discover_libraries(explicit: str | Path | None = None) -> list[Path]:
    if explicit:
        library = paths.resolve_dir(explicit, "Steam library")
        return [library]

    roots = default_steam_roots()
    if not roots:
        raise DetectionError(
            "no Steam installation was found",
            details={
                "checked": [
                    "~/.local/share/Steam",
                    "~/.steam/steam",
                    "~/.steam/root",
                    "Flatpak Steam locations",
                ]
            },
        )

    candidates: list[Path] = []
    for root in roots:
        candidates.append(root)
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if vdf.is_file():
            candidates.extend(_library_paths_from_vdf(vdf))

    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            if not candidate.is_dir():
                continue
            real = paths.realpath(candidate)
        except OSError:
            continue
        if str(real) in seen:
            continue
        seen.add(str(real))
        result.append(real)

    if not result:
        raise DetectionError("Steam libraries were found, but none are usable directories")
    return result


def _walk_apps(obj: Any, appid: str) -> Iterable[str]:
    if isinstance(obj, dict):
        apps = obj.get("apps")
        if isinstance(apps, dict):
            app = apps.get(appid)
            if isinstance(app, dict):
                option = app.get("LaunchOptions")
                if isinstance(option, str):
                    yield option
        for value in obj.values():
            yield from _walk_apps(value, appid)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk_apps(value, appid)


def find_launch_options(
    steam_roots: Iterable[Path],
    appid: str,
    steam_user: str | None = None,
) -> dict[str, Any]:
    """Return LaunchOptions entries and parse warnings.  Read-only."""

    entries: list[dict[str, Any]] = []
    warnings: list[str] = []
    seen_paths: set[str] = set()

    for root in steam_roots:
        userdata = root / "userdata"
        if not userdata.is_dir():
            continue
        for user_dir in sorted(userdata.iterdir()):
            if not user_dir.is_dir():
                continue
            if steam_user and user_dir.name != steam_user:
                continue
            config = user_dir / "config" / "localconfig.vdf"
            if not config.is_file():
                continue
            real = str(config)
            if real in seen_paths:
                continue
            seen_paths.add(real)
            try:
                parsed = parse_vdf(config.read_text(encoding="utf-8", errors="replace"))
            except (OSError, VDFError) as exc:
                warnings.append(f"could not parse {config}: {exc}")
                continue
            options = list(dict.fromkeys(_walk_apps(parsed, appid)))
            for option in options:
                entries.append(
                    {
                        "userdata": user_dir.name,
                        "path": str(config),
                        "options": option,
                    }
                )
    return {"entries": entries, "warnings": warnings}
