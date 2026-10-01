"""A fictional design map must not imply observed deployment or traffic."""
from __future__ import annotations

import copy
from pathlib import Path
import unittest

from estate_atlas import model, render

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "workspace" / "atlas.json"


class WorkspaceExampleTests(unittest.TestCase):
    def load_example(self) -> dict:
        self.assertTrue(EXAMPLE.is_file(), "The workspace example is missing")
        return model.load(EXAMPLE)

    def test_example_validates_and_renders_without_live_claims(self) -> None:
        doc = self.load_example()
        model.validate(doc)
        for collection in ("components", "contracts", "flows"):
            self.assertTrue(doc[collection])
            self.assertEqual({item["status"] for item in doc[collection]}, {"planned"})
        parsed = render.parse_atlas(doc)
        self.assertIn("Human workspace", render.render_html(parsed))
        diagram = render.render_mermaid(parsed)
        self.assertIn("Focus surface", diagram)
        self.assertEqual(diagram, render.render_mermaid(parsed))
        self.assertTrue(all(flow["gap"] for flow in doc["flows"]))
        self.assertTrue(all("traffic" not in flow for flow in doc["flows"]))

    def test_intent_result_and_attention_have_distinct_contract_owners(self) -> None:
        contracts = {c["id"]: c for c in self.load_example()["contracts"]}
        self.assertEqual(contracts["workspace.intent/1"]["producer"], "workspace")
        self.assertEqual(contracts["operations.result/1"]["producer"], "operations")
        self.assertEqual(contracts["workspace.attention/1"]["producer"], "workspace")
        self.assertEqual(contracts["workspace.receipt/1"]["producer"], "workspace")

    def test_dangling_target_is_rejected(self) -> None:
        doc = copy.deepcopy(self.load_example())
        doc["flows"][0]["to"] = "missing"
        with self.assertRaises(model.AtlasError):
            model.validate(doc)

    def test_planned_flow_without_a_gap_is_rejected(self) -> None:
        doc = copy.deepcopy(self.load_example())
        del doc["flows"][0]["gap"]
        with self.assertRaises(model.AtlasError):
            model.validate(doc)

    def test_example_uses_only_fictional_repository_names_and_no_host_paths(self) -> None:
        doc = self.load_example()
        for repo in doc["repos"].values():
            self.assertTrue(repo["remote"].startswith("example/"))
            self.assertEqual(repo["paths"], {})
        for flow in doc["flows"]:
            self.assertEqual(flow["evidence"], [])


if __name__ == "__main__":
    unittest.main()
