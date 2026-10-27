from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(*parts: str) -> dict:
    return json.loads(ROOT.joinpath(*parts).read_text(encoding="utf-8"))


class PluginMarketplaceTests(unittest.TestCase):
    def test_codex_marketplace_points_to_plugin_root(self) -> None:
        marketplace = load(".agents", "plugins", "marketplace.json")
        plugin_manifest = load(".codex-plugin", "plugin.json")

        self.assertEqual(marketplace["name"], "beyondee")
        self.assertEqual(marketplace["interface"]["displayName"], "BEYONDEE")

        self.assertEqual(len(marketplace["plugins"]), 1)
        entry = marketplace["plugins"][0]
        self.assertEqual(entry["name"], plugin_manifest["name"])
        self.assertEqual(entry["source"], {"source": "local", "path": "./"})
        self.assertEqual(entry["policy"]["installation"], "AVAILABLE")
        self.assertEqual(entry["policy"]["authentication"], "ON_INSTALL")
        self.assertNotIn("products", entry["policy"])
        self.assertEqual(entry["category"], "Productivity")

    def test_claude_marketplace_points_to_plugin_root(self) -> None:
        marketplace = load(".claude-plugin", "marketplace.json")
        plugin_manifest = load(".claude-plugin", "plugin.json")

        self.assertEqual(marketplace["name"], "beyondee")
        self.assertTrue(marketplace["owner"]["name"])
        self.assertEqual(len(marketplace["plugins"]), 1)
        entry = marketplace["plugins"][0]
        self.assertEqual(entry["name"], plugin_manifest["name"])
        self.assertEqual(entry["source"], "./")
        self.assertNotIn("version", entry, "version lives only in plugin.json")

    def test_codex_and_claude_manifests_stay_in_sync(self) -> None:
        codex = load(".codex-plugin", "plugin.json")
        claude = load(".claude-plugin", "plugin.json")

        self.assertEqual(codex["name"], "common-skills")
        self.assertEqual(claude["name"], codex["name"])
        self.assertEqual(claude["version"], codex["version"])
        self.assertEqual(claude["description"], codex["description"])
        self.assertEqual(codex["skills"], "./skills/")
        self.assertTrue((ROOT / "skills").is_dir())


if __name__ == "__main__":
    unittest.main()
