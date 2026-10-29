"""CLI JSON and read-only tests using synthetic installations."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pz13thlib import cli
from tests.support import make_install


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fixture = make_install(self.root)
        self.env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root / "config")})
        self.env.start()
        self.roots = mock.patch("pz13thlib.steam.default_steam_roots", return_value=[])
        self.roots.start()

    def tearDown(self) -> None:
        self.roots.stop()
        self.env.stop()
        self.tmp.cleanup()

    def run_cli(self, args: list[str]) -> tuple[int, dict]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(args)
        text = buffer.getvalue()
        return code, json.loads(text) if text.strip() else {}

    def common(self) -> list[str]:
        return [
            "--steam-library",
            str(self.fixture["library"]),
            "--state-dir",
            str(self.fixture["state"]),
        ]

    def test_detect_json(self) -> None:
        code, data = self.run_cli(["detect", "--json", *self.common()])
        self.assertEqual(code, 0)
        self.assertTrue(data["ok"])
        self.assertEqual(data["paths"]["runtime"], str(self.fixture["inst"].runtime))
        self.assertEqual(data["payload"]["included_count"], 6)
        self.assertEqual(data["payload"]["excluded_count"], 5)
        self.assertEqual(data["targets"]["counts"]["replaced"], 1)

    def test_status_json(self) -> None:
        code, data = self.run_cli(["status", "--json", *self.common()])
        self.assertEqual(code, 0)
        self.assertEqual(data["applied"]["state"], "not_applied")
        self.assertFalse(self.fixture["state"].exists())

    def test_apply_dry_run_json_is_read_only(self) -> None:
        code, data = self.run_cli(["apply", "--dry-run", "--json", *self.common()])
        self.assertEqual(code, 0)
        self.assertTrue(data["dry_run"])
        self.assertFalse(self.fixture["state"].exists())
        self.assertFalse((self.fixture["runtime"] / "zombie").exists())

    def test_launch_dry_run_json(self) -> None:
        code, data = self.run_cli(
            [
                "launch",
                "--dry-run",
                "--json",
                "--port",
                "16261",
                "--steam-binary",
                "/bin/true",
                *self.common(),
            ]
        )
        self.assertEqual(code, 0)
        self.assertTrue(data["dry_run"])
        self.assertTrue(data["plan"]["zombie_buddy"]["enabled"])
        self.assertIn("-connect=pz.13thpandemic.net", data["plan"]["args"])
        self.assertIn("-port=16261", data["plan"]["args"])
        self.assertFalse(self.fixture["state"].exists())

    def test_launch_without_port_is_refused(self) -> None:
        code, data = self.run_cli(
            ["launch", "--json", "--port", "0", "--steam-binary", "/bin/true", *self.common()]
        )
        self.assertEqual(code, 6)
        self.assertFalse(data["ok"])
        self.assertIn("port", data["error"]["message"].lower())

    def test_default_config_is_created(self) -> None:
        config = self.root / "config" / "13thpandemic" / "config.toml"
        self.assertFalse(config.exists())
        with contextlib.redirect_stdout(io.StringIO()):
            data = cli.load_config(None, create_if_missing=True)
        self.assertTrue(config.is_file())
        self.assertEqual(data["server"]["port"], 16262)
        self.assertEqual(data["server"]["host"], "pz.13thpandemic.net")
        text = config.read_text(encoding="utf-8")
        self.assertIn("port = 16262", text)
        # Second invocation must not overwrite it.
        config.write_text(text + "\n# user edit\n", encoding="utf-8")
        cli.load_config(None, create_if_missing=True)
        self.assertIn("# user edit", config.read_text(encoding="utf-8"))

    def test_config_file_is_read(self) -> None:
        config = self.root / "config.toml"
        config.write_text(
            '[server]\nhost = "config.example"\nport = 16261\n\n[launch]\nmethod = "applaunch"\n',
            encoding="utf-8",
        )
        code, data = self.run_cli(
            [
                "launch",
                "--dry-run",
                "--json",
                "--config",
                str(config),
                "--steam-binary",
                "/bin/true",
                *self.common(),
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(data["plan"]["host"], "config.example")
        self.assertEqual(data["plan"]["port"], 16261)


if __name__ == "__main__":
    unittest.main()
