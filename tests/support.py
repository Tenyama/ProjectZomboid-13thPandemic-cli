"""Synthetic installation fixtures for the test suite.

Everything is created under a TemporaryDirectory.  No real Steam or game files
are read or written by these helpers.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from pz13thlib import detection


def write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)


def make_install(root: Path) -> dict:
    library = root / "Steam"
    steamapps = library / "steamapps"
    steamapps.mkdir(parents=True, exist_ok=True)

    write(
        steamapps / "libraryfolders.vdf",
        '"libraryfolders"\n{\n  "0"\n  {\n    "path" "' + str(library) + '"\n  }\n}\n',
    )

    wrapper = steamapps / "common" / "ProjectZomboid"
    runtime = wrapper / "projectzomboid"
    runtime.mkdir(parents=True, exist_ok=True)
    jar = b"fake-projectzomboid-jar"
    write(runtime / "projectzomboid.jar", jar)
    write(runtime / "ProjectZomboid64", b"elf")
    write(runtime / "ZombieBuddy.jar", b"fake-zombie-buddy-agent")
    write(
        runtime / "ProjectZomboid64.json",
        json.dumps(
            {
                "mainClass": "zombie/gameStates/MainScreenState",
                "classpath": [".", "projectzomboid.jar"],
                "vmArgs": ["-Xmx8192m", "-Djava.library.path=natives/"],
            }
        )
        + "\n",
    )
    write(runtime / "media" / "videos" / "title_screen_background.bik", b"stock-title")

    item = steamapps / "workshop" / "content" / "108600" / "3487726294"
    mod = item / "mods" / "13thPandemic"
    payload = mod / "ProjectZomboid"
    data = mod / "Zomboid"
    data.mkdir(parents=True, exist_ok=True)
    write(data / "options.ini", "version=42\n")
    write(data / "Mods" / "ModA" / "mod.info", "id=ModA\n")
    write(data / "Mods" / "BadCase" / "42" / "Mod.info", "id=BadCase\n")
    write(
        data / "Mods" / "default.txt",
        "VERSION = 1,\n\nmods\n{\n    mod = ProjectVarietyChapterOne,\n}\n\nmaps\n{\n}\n",
    )
    write(data / "mods" / "default.txt", "VERSION = 1,\n")

    jar_sha = hashlib.sha256(jar).hexdigest()
    write(
        payload / "pzopt" / "build-info.properties",
        f"revision=test-revision\njar.sha256={jar_sha}\njar.size={len(jar)}\nrelease=1\n",
    )
    write(payload / "pzopt" / "Other.class", b"other-class")
    write(payload / "pzopt" / "uninstall" / "install.bash", "#!/bin/sh\n")
    write(payload / "pzopt" / "uninstall" / "install.ps1", "# windows\n")
    write(payload / "pzopt-files.txt", "zombie/Foo.class\n")
    write(payload / "jre64.xml", b"windows-jre")
    write(payload / "ProjectZomboid64.json", '{"java.library.path":"win64"}\n')
    write(payload / "zombie" / "Foo.class", b"class-payload")
    write(payload / "media" / "lua" / "client" / "pzopt" / "a.lua", "return {}\n")
    write(payload / "media" / "videos" / "new.bik", b"new-video")
    write(payload / "media" / "videos" / "title_screen_background.bik", b"payload-title")

    state = root / "state"
    opts = SimpleNamespace(
        steam_library=str(library),
        workshop_item=None,
        wrapper_root=None,
        runtime=None,
        payload=None,
        data_dir=None,
        state_dir=str(state),
        steam_user=None,
    )
    inst = detection.resolve_installation(opts)
    return {
        "root": root,
        "library": library,
        "wrapper": wrapper,
        "runtime": runtime,
        "item": item,
        "payload": payload,
        "data": data,
        "state": state,
        "opts": opts,
        "inst": inst,
        "jar_sha": jar_sha,
    }
