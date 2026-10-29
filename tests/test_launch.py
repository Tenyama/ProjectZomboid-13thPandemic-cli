"""Launch argument construction tests (no Steam process is started)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from pz13thlib import launch
from pz13thlib.errors import LaunchError


class LaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "Zomboid"
        self.data.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_applaunch_arguments(self) -> None:
        plan = launch.build_plan(
            data_dir=self.data,
            host="pz.13thpandemic.net",
            port=16261,
            method="applaunch",
            steam_binary="/bin/true",
            launch_options={"entries": []},
        )
        self.assertEqual(plan["args"][0], f"-cachedir={self.data.resolve()}")
        self.assertEqual(plan["args"][1], "-connect=pz.13thpandemic.net")
        self.assertEqual(plan["args"][2], "-port=16261")
        self.assertEqual(plan["argv"][:3], ["/bin/true", "-applaunch", "108600"])

    def test_run_url(self) -> None:
        plan = launch.build_plan(
            data_dir=self.data,
            host="example.test",
            port=27015,
            method="run",
            steam_binary="/bin/true",
            launch_options={"entries": []},
        )
        self.assertTrue(plan["url"].startswith("steam://run/108600//"))
        self.assertIn("-cachedir%3D", plan["url"])
        self.assertIn("-connect%3Dexample.test", plan["url"])
        self.assertIn("-port%3D27015", plan["url"])

    def test_rungameid_warns_about_missing_arguments(self) -> None:
        plan = launch.build_plan(
            data_dir=self.data,
            host="example.test",
            port=27015,
            method="rungameid",
            steam_binary="/bin/true",
            launch_options={"entries": []},
        )
        self.assertEqual(plan["argv"][-1], "steam://rungameid/108600")
        self.assertTrue(any("cannot pass" in item for item in plan["warnings"]))

    def test_port_is_required_and_validated(self) -> None:
        with self.assertRaises(LaunchError):
            launch.build_plan(
                data_dir=self.data,
                host="example.test",
                port=None,
                method="applaunch",
                steam_binary="/bin/true",
                launch_options={"entries": []},
            )
        with self.assertRaises(LaunchError):
            launch.build_plan(
                data_dir=self.data,
                host="example.test",
                port=70000,
                method="applaunch",
                steam_binary="/bin/true",
                launch_options={"entries": []},
            )

    def test_existing_cachedir_is_not_duplicated(self) -> None:
        with self.assertRaises(LaunchError):
            launch.build_plan(
                data_dir=self.data,
                host="example.test",
                port=16261,
                method="applaunch",
                steam_binary="/bin/true",
                launch_options={
                    "entries": [
                        {
                            "path": "/fake/localconfig.vdf",
                            "options": "-cachedir=/somewhere --",
                        }
                    ]
                },
            )

    def test_invalid_method(self) -> None:
        with self.assertRaises(LaunchError):
            launch.build_plan(
                data_dir=self.data,
                host="example.test",
                port=16261,
                method="nope",
                steam_binary="/bin/true",
                launch_options={"entries": []},
            )

    def test_zombie_buddy_uses_pzexeconfig(self) -> None:
        runtime = Path(self.tmp.name) / "runtime"
        runtime.mkdir()
        (runtime / "ZombieBuddy.jar").write_bytes(b"agent")
        (runtime / "ProjectZomboid64.json").write_text(
            json.dumps({"mainClass": "zombie/gameStates/MainScreenState", "classpath": [".", "projectzomboid.jar"], "vmArgs": ["-Xmx2g"]}),
            encoding="utf-8",
        )
        state = Path(self.tmp.name) / "state"
        plan = launch.build_plan(
            data_dir=self.data,
            host="example.test",
            port=16261,
            method="applaunch",
            steam_binary="/bin/true",
            launch_options={"entries": [{"path": "/tmp/localconfig", "options": "-javaagent:ZombieBuddy.jar"}]},
            runtime=runtime,
            state_dir=state,
            zombie_buddy="auto",
        )
        zombie = plan["zombie_buddy"]
        self.assertTrue(zombie["enabled"])
        self.assertEqual(plan["args"][0], "-pzexeconfig")
        self.assertEqual(plan["args"][1], zombie["config_path"])
        self.assertEqual(plan["args"][2], "--")
        self.assertIn("-javaagent:ZombieBuddy.jar", zombie["config"]["vmArgs"])
        self.assertFalse(state.exists())

        prepared = launch.prepare(plan)
        self.assertIsNotNone(prepared)
        self.assertTrue(Path(str(prepared)).is_file())
        written = json.loads(Path(str(prepared)).read_text(encoding="utf-8"))
        self.assertIn("-javaagent:ZombieBuddy.jar", written["vmArgs"])

    def test_zombie_buddy_cannot_use_rungameid(self) -> None:
        runtime = Path(self.tmp.name) / "runtime"
        runtime.mkdir()
        (runtime / "ZombieBuddy.jar").write_bytes(b"agent")
        (runtime / "ProjectZomboid64.json").write_text(
            json.dumps({"mainClass": "zombie/gameStates/MainScreenState", "classpath": [".", "projectzomboid.jar"], "vmArgs": []}),
            encoding="utf-8",
        )
        with self.assertRaises(LaunchError):
            launch.build_plan(
                data_dir=self.data,
                host="example.test",
                port=16261,
                method="rungameid",
                steam_binary="/bin/true",
                launch_options={"entries": []},
                runtime=runtime,
                state_dir=Path(self.tmp.name) / "state",
                zombie_buddy="on",
            )

    def test_mods_alias_backs_up_lowercase_files(self) -> None:
        (self.data / "Mods" / "ModA").mkdir(parents=True)
        (self.data / "Mods" / "ModA" / "mod.info").write_text("id=ModA\n", encoding="utf-8")
        (self.data / "mods").mkdir()
        (self.data / "mods" / "default.txt").write_text("VERSION = 1,\n", encoding="utf-8")
        state = Path(self.tmp.name) / "state"
        before = launch.inspect_mods_alias(self.data)
        self.assertEqual(before["status"], "files_only")
        result = launch.prepare_mods_alias(self.data, state)
        self.assertEqual(result["status"], "linked")
        self.assertTrue((self.data / "mods").is_symlink())
        self.assertEqual(os.path.realpath(self.data / "mods"), os.path.realpath(self.data / "Mods"))
        backup = Path(str(result["backup"]))
        self.assertTrue((backup / "default.txt").is_file())

    def test_mod_info_case_normalization(self) -> None:
        (self.data / "Mods" / "BadCase" / "42").mkdir(parents=True)
        source = self.data / "Mods" / "BadCase" / "42" / "Mod.info"
        source.write_text("id=BadCase\n", encoding="utf-8")
        pending = launch.inspect_mod_info_case(self.data)
        self.assertEqual(pending, ["BadCase/42/Mod.info"])
        changed = launch.prepare_mod_info_case(self.data)
        self.assertEqual(len(changed), 1)
        lower = source.parent / "mod.info"
        self.assertTrue(lower.is_symlink())
        self.assertEqual(os.path.realpath(lower), os.path.realpath(source))

    def test_active_mod_is_added_after_anchor(self) -> None:
        (self.data / "Mods").mkdir(parents=True, exist_ok=True)
        default = self.data / "Mods" / "default.txt"
        default.write_text(
            "VERSION = 1,\n\nmods\n{\n    mod = ProjectVarietyChapterOne,\n}\n\nmaps\n{\n}\n",
            encoding="utf-8",
        )
        state = Path(self.tmp.name) / "state"
        pending = launch.inspect_active_mods(self.data)
        self.assertEqual(pending, [{"mod_id": "ProjectVarietyChapterTwo", "after": "ProjectVarietyChapterOne"}])
        changed = launch.prepare_active_mods(self.data, state)
        self.assertEqual(len(changed), 1)
        text = default.read_text(encoding="utf-8")
        self.assertIn("    mod = ProjectVarietyChapterTwo,\n", text)
        self.assertLess(text.index("ProjectVarietyChapterOne"), text.index("ProjectVarietyChapterTwo"))


if __name__ == "__main__":
    unittest.main()
