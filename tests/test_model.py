"""Validation tests for estate_atlas.model on fictional fixtures."""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from estate_atlas import model

HOST = "TESTHOST"


def minimal(alpha_path: str = "C:/nowhere/alpha", extra_path: str = "C:/nowhere/extra") -> dict:
    return {
        "schema": model.SCHEMA, "updated": "2026-09-29",
        "repos": {
            "alpha": {"remote": "Owner/alpha", "default_branch": "main",
                      "paths": {HOST: alpha_path}},
            "extra": {"remote": "Owner/extra", "default_branch": "main",
                      "paths": {HOST: extra_path}},
        },
        "layers": [{"id": "core", "title": "Core", "summary": "The core layer."}],
        "components": [
            {"id": "tool", "title": "Tool", "layer": "core", "home": "alpha", "status": "live",
             "summary": "A tool.", "surfaces": [{"kind": "cli", "name": "tool.py run",
                                                  "evidence": [{"repo": "alpha", "path": "tool.py",
                                                                "anchor": "def run"}]}],
             "owns": ["tool state"], "evidence": []},
            {"id": "sink", "title": "Sink", "layer": "core", "home": "extra", "status": "live",
             "summary": "A sink.", "surfaces": [], "owns": [],
             "evidence": [{"repo": "extra", "path": "docs/SINK.md"}]},
            {"id": "cloud", "title": "Cloud", "layer": "core", "home": "external:cloud",
             "status": "live", "summary": "Outside the scope.", "surfaces": [], "owns": [],
             "evidence": []},
        ],
        "contracts": [{"id": "tool.out/1", "title": "Tool output", "producer": "tool",
                       "consumers": ["sink"], "format": "json", "status": "live",
                       "summary": "What the tool writes.",
                       "evidence": [{"repo": "alpha", "path": "tool.py", "anchor": "tool.out/1"}]}],
        "flows": [
            {"id": "tool-sink", "from": "tool", "to": "sink", "contract": "tool.out/1",
             "trigger": "each run", "status": "live",
             "evidence": [{"repo": "extra", "path": "docs/SINK.md", "anchor": "reads tool.out/1"}]},
            {"id": "sink-cloud", "from": "sink", "to": "cloud", "contract": None,
             "trigger": "later", "status": "planned", "gap": "Not built.", "evidence": []},
        ],
    }


class SchemaTests(unittest.TestCase):
    def test_schema_constant_and_matcher(self) -> None:
        self.assertEqual(model.SCHEMA, "estate-atlas@2")
        self.assertTrue(model.is_atlas_schema("estate-atlas@2"))
        self.assertTrue(model.is_atlas_schema("example/atlas@2"))
        self.assertFalse(model.is_atlas_schema("estate-atlas@1"))
        self.assertFalse(model.is_atlas_schema("other"))
        self.assertFalse(model.is_atlas_schema(None))

    def test_namespaced_schema_validates(self) -> None:
        doc = minimal()
        doc["schema"] = "example/atlas@2"
        model.validate(doc)

    def test_load_round_trip_and_strictness(self) -> None:
        import json

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "atlas.json"
            path.write_text(json.dumps(minimal()), encoding="utf-8")
            doc = model.load(path)
            model.validate(doc)
            self.assertEqual(doc["schema"], model.SCHEMA)

    def test_duplicate_json_keys_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "atlas.json"
            path.write_text('{"schema": "estate-atlas@2", "schema": "x"}', encoding="utf-8")
            with self.assertRaises(model.AtlasError):
                model.load(path)

    def test_load_rejects_missing_file_and_bad_top_level(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(model.AtlasError):
                model.load(Path(temp) / "gone.json")
            path = Path(temp) / "list.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaises(model.AtlasError):
                model.load(path)


class ValidationTests(unittest.TestCase):
    def assert_rejected(self, mutate, fragment: str) -> None:
        doc = minimal()
        mutate(doc)
        with self.assertRaises(model.AtlasError) as caught:
            model.validate(doc)
        self.assertIn(fragment, str(caught.exception))

    def test_minimal_atlas_is_valid(self) -> None:
        model.validate(minimal())

    def test_each_rule_rejects(self) -> None:
        cases = [
            (lambda d: d.update(extra=1), "unknown key"),
            (lambda d: d.update(schema="estate-atlas@1"), "atlas.schema"),
            (lambda d: d.update(updated="29/09/2026"), "atlas.updated"),
            (lambda d: d["components"].append(copy.deepcopy(d["components"][0])), "duplicate id 'tool'"),
            (lambda d: d["components"][0].update(status="shipping"), "status"),
            (lambda d: d["components"][0].update(layer="nowhere"), "not a declared layer"),
            (lambda d: d["components"][0].update(home="gamma"), "is not a declared repo"),
            (lambda d: d["flows"][0].update(to="ghost"), "is not a component"),
            (lambda d: d["flows"][1].pop("gap"), "required when the status is not live"),
            (lambda d: d["flows"][0].update(gap="none"), "a live flow has no gap"),
            (lambda d: d["components"][0]["surfaces"][0]["evidence"][0].update(path="../escape.py"),
             "repository-relative"),
            (lambda d: d["components"][0]["surfaces"][0]["evidence"][0].update(path="C:/abs.py"),
             "repository-relative"),
            (lambda d: d["components"][0]["surfaces"][0]["evidence"][0].update(repo="gamma"),
             "is not a declared repo"),
            (lambda d: d["components"][1].update(evidence=[]), "needs at least one evidence"),
            (lambda d: d["contracts"][0].update(evidence=[]), "needs at least one evidence"),
            (lambda d: d["contracts"][0].update(format="xml"), "format"),
            (lambda d: d["repos"]["extra"].update(paths={HOST: "relative/path"}), "absolute path"),
            (lambda d: d["components"][2].update(home="external:Bad Name"), "external:<name>"),
            (lambda d: d["flows"][0].update(trigger="two\nlines"), "single line"),
        ]
        for mutate, fragment in cases:
            with self.subTest(fragment=fragment):
                self.assert_rejected(mutate, fragment)

    def test_wrong_types_are_invalid_not_a_crash(self) -> None:
        cases = [
            lambda d: d["components"][0].update(layer=[]),
            lambda d: d["components"][0].update(status={}),
            lambda d: d["components"][0]["surfaces"][0]["evidence"][0].update(repo=[]),
            lambda d: d["flows"][0].update(to={}),
            lambda d: d["contracts"][0].update(format=["json"]),
            lambda d: d["components"][0].update(home=[]),
        ]
        for mutate in cases:
            doc = minimal()
            mutate(doc)
            with self.assertRaises(model.AtlasError):
                model.validate(doc)

    def test_documented_and_absent_statuses_are_accepted(self) -> None:
        doc = minimal()
        doc["components"][1].update(status="documented")
        doc["components"][1]["evidence"] = []
        doc["components"][1]["expect"] = [{"repo": "extra", "path": "docs/SINK.md"}]
        model.validate(doc)


class InstancesTests(unittest.TestCase):
    def test_valid_pattern_passes(self) -> None:
        doc = minimal()
        doc["components"][0]["instances"] = ["svc:*", "order:refund"]
        model.validate(doc)

    def test_bad_patterns_raise(self) -> None:
        for bad in ("Svc:*", "svc:a*b", ":x"):
            with self.subTest(bad=bad):
                doc = minimal()
                doc["components"][0]["instances"] = [bad]
                with self.assertRaises(model.AtlasError):
                    model.validate(doc)

    def test_duplicate_across_components_names_both(self) -> None:
        doc = minimal()
        doc["components"][0]["instances"] = ["svc:*"]
        doc["components"][1]["instances"] = ["svc:*"]
        with self.assertRaises(model.AtlasError) as caught:
            model.validate(doc)
        message = str(caught.exception)
        self.assertIn("tool", message)
        self.assertIn("sink", message)

    def test_match_instance_exact_beats_wildcard_and_unknown_is_none(self) -> None:
        doc = minimal()
        doc["components"][0]["instances"] = ["svc:*"]
        doc["components"][1]["instances"] = ["svc:second"]
        model.validate(doc)
        index = model.instance_index(doc)
        self.assertEqual(model.match_instance("svc:second", index), "sink")
        self.assertEqual(model.match_instance("svc:other", index), "tool")
        self.assertIsNone(model.match_instance("disk:x", index))


class TrafficValidationTests(unittest.TestCase):
    def doc(self, first: list | None = None, second: list | None = None) -> dict:
        doc = minimal()
        if first is not None:
            doc["flows"][0]["traffic"] = first
        if second is not None:
            doc["flows"][1]["traffic"] = second
        return doc

    def test_validate_rejects_bad_rules(self) -> None:
        bad = [
            [{"source": "journal", "who": "x"}],
            [{"source": "mail"}],
            [{"source": "events", "actor": "svc:*"}],
            [{"source": "journal", "subject": "p*r"}],
            [{"source": "journal", "subject": "p**"}],
            [{"source": "journal", "subject": "has space"}],
            [{"source": "journal", "verb": []}],
            [],
        ]
        for rules in bad:
            with self.subTest(rules=rules):
                with self.assertRaises(model.AtlasError):
                    model.validate(self.doc(rules))

    def test_pulse_must_be_a_json_boolean(self) -> None:
        model.validate(self.doc([{"source": "journal", "producer": "web", "pulse": True}]))
        model.validate(self.doc([{"source": "journal", "producer": "web", "pulse": False}]))
        with self.assertRaises(model.AtlasError):
            model.validate(self.doc([{"source": "journal", "producer": "web", "pulse": "yes"}]))

    def test_verbs_are_checked_against_their_vocabulary(self) -> None:
        base = self.doc([{"source": "journal", "verb": "opened"}])
        base["vocabularies"] = [
            {"id": "journal-verbs", "title": "Journal verbs", "owner": "tool",
             "source": {"repo": "alpha", "path": "tool.py", "each": True},
             "terms": [{"term": "opened", "means": "An open."}, {"term": "ran", "means": "A run."}]},
            {"id": "link-relations", "title": "Link relations", "owner": "tool",
             "source": {"repo": "alpha", "path": "tool.py", "each": True},
             "terms": [{"term": "found", "means": "A finding."}]}]
        model.validate(base)
        for rules in ([{"source": "journal", "verb": "exploded"}], [{"source": "journal", "verb": "x*"}],
                      [{"source": "links", "verb": "opened"}]):
            doc = copy.deepcopy(base)
            doc["flows"][0]["traffic"] = rules
            with self.subTest(rules=rules):
                with self.assertRaises(model.AtlasError):
                    model.validate(doc)
        doc = copy.deepcopy(base)
        doc["flows"][0]["traffic"] = [{"source": "journal", "verb": "r*"},
                                      {"source": "links", "verb": "found"},
                                      {"source": "events", "verb": "ledger.*"}]
        model.validate(doc)

    def test_node_kinds_unclaimed_raises(self) -> None:
        doc = minimal()
        doc["components"][0]["instances"] = ["svc:*"]
        doc["vocabularies"] = [{"id": "node-kinds", "title": "Kinds", "owner": "tool",
                                "source": {"repo": "alpha", "path": "tool.py", "each": True},
                                "terms": [{"term": "other", "means": "Other kind."}]}]
        with self.assertRaises(model.AtlasError) as caught:
            model.validate(doc)
        self.assertIn("other", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
