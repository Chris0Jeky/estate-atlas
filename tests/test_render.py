"""Render checks against a fictional shop atlas.

Port of the render checks: determinism, escaping, wording and structure,
without any private names.
"""
from __future__ import annotations

import copy
import json
import re
import tempfile
import unittest
from pathlib import Path

from estate_atlas import render

XSS_TITLE = '<script>alert(1)</script> "quoted"'


def fixture_atlas() -> dict:
    return {
        "schema": "estate-atlas@2",
        "updated": "2026-09-28",
        "repos": {
            "shop": {"remote": "Example/shop", "default_branch": "main",
                     "paths": {}},
            "pay-lib": {"remote": "Example/pay-lib",
                        "default_branch": "develop", "paths": {}},
            "extra-lib": {"remote": "Example/extra-lib",
                          "default_branch": "main", "paths": {}},
        },
        "layers": [
            {"id": "l-edge", "title": "Edge", "summary": "Entry points."},
            {"id": "l-core", "title": "Core", "summary": "Workers."},
            {"id": "l-store", "title": "Store", "summary": "Storage."},
        ],
        "components": [
            {"id": "c-web", "title": "Storefront", "layer": "l-edge",
             "home": "shop", "status": "live", "summary": "Entry storefront.",
             "surfaces": [{"kind": "cli", "name": "web",
                           "evidence": [{"repo": "shop", "path": "README.md"}]}],
             "owns": ["storefront"], "evidence": [{"repo": "shop", "path": "README.md"}]},
            {"id": "c-xss", "title": XSS_TITLE, "layer": "l-edge",
             "home": "external:PayCo", "status": "partial",
             "summary": "Third party widget.", "surfaces": [],
             "owns": [], "evidence": []},
            {"id": "c-worker", "title": "Worker", "layer": "l-core",
             "home": "shop", "status": "live", "summary": "Does work.",
             "surfaces": [], "owns": [],
             "evidence": [{"repo": "pay-lib", "path": "docs/w.md"}]},
            {"id": "c-sched", "title": "Scheduler", "layer": "l-core",
             "home": "pay-lib", "status": "partial", "summary": "Schedules.",
             "surfaces": [], "owns": [],
             "evidence": [{"repo": "extra-lib", "path": "spec.md"}]},
            {"id": "c-arch", "title": "Archive", "layer": "l-store",
             "home": "extra-lib", "status": "planned", "summary": "Cold store.",
             "surfaces": [], "owns": [],
             "evidence": [{"repo": "shop", "path": "docs/a.md",
                           "anchor": "usage", "note": "plan"}]},
            {"id": "c-leg", "title": "Legacy", "layer": "l-store",
             "home": "external:OldCo", "status": "retired", "summary": "Retired box.",
             "surfaces": [], "owns": [],
             "evidence": [{"repo": "shop", "path": "old.md",
                           "url": "https://github.com/Example/shop/blob/main/custom.md"}]},
        ],
        "contracts": [
            {"id": "k-one", "title": "Events", "producer": "c-web",
             "consumers": ["c-worker"], "format": "json", "status": "live",
             "summary": "Event feed.",
             "evidence": [{"repo": "shop", "path": "docs/a.md"}]},
            {"id": "k-two", "title": "Tasks", "producer": "c-worker",
             "consumers": ["c-sched", "c-arch"], "format": "json-lines",
             "status": "partial", "summary": "Task queue.",
             "evidence": [{"repo": "extra-lib", "path": "spec.md"}]},
            {"id": "k-three", "title": "Slots", "producer": "c-sched",
             "consumers": ["c-leg"], "format": "csv", "status": "planned",
             "summary": "Slot export.",
             "evidence": [{"repo": "shop", "path": "x.md",
                           "url": "https://github.com/Example/shop/blob/main/custom.md"}]},
        ],
        "flows": [
            {"id": "f-live", "from": "c-web", "to": "c-worker", "contract": "k-one",
             "trigger": "push", "status": "live", "evidence": [
                 {"repo": "shop", "path": "README.md"}]},
            {"id": "f-part", "from": "c-worker", "to": "c-sched",
             "contract": "k-two", "trigger": "tick", "status": "partial",
             "gap": "Slow on retry.", "evidence": [
                 {"repo": "pay-lib", "path": "docs/w.md"}]},
            {"id": "f-doc", "from": "c-worker", "to": "c-arch", "contract": None,
             "trigger": "nightly", "status": "documented", "gap": "Docs only.",
             "evidence": []},
            {"id": "f-plan", "from": "c-sched", "to": "c-leg", "contract": "k-three",
             "trigger": "manual", "status": "planned", "gap": "Not built.",
             "evidence": []},
            {"id": "f-abs", "from": "c-web", "to": "c-sched", "contract": None,
             "trigger": "alert", "status": "absent", "gap": "Missing link.",
             "evidence": []},
        ],
    }


def fixture_check() -> dict:
    return {"schema": "estate-atlas-check@2", "host": "ci-example",
            "checked": 10, "ok": 8, "missing": ["a.md", "b.md"],
            "unresolved": ["c.md"], "heads": {}, "status": "partial"}


def json_block(page: str) -> str:
    match = re.search(
        r'<script type="application/json" id="atlas-data">(.*?)</script>',
        page, re.DOTALL)
    assert match is not None, "embedded JSON block is missing"
    return match.group(1)


class AtlasRenderTests(unittest.TestCase):
    def parsed(self, atlas=None):
        return render.parse_atlas(copy.deepcopy(atlas or fixture_atlas()))

    def test_deterministic_bytes(self) -> None:
        atlas = self.parsed()
        first = render.render_html(atlas, render.parse_check(fixture_check()))
        second = render.render_html(atlas, render.parse_check(fixture_check()))
        self.assertEqual(first, second)
        self.assertEqual(render.render_mermaid(atlas, "flows"),
                         render.render_mermaid(atlas, "flows"))
        self.assertEqual(render.render_mermaid(atlas, "components"),
                         render.render_mermaid(atlas, "components"))

    def test_embedded_json_round_trips_without_raw_close(self) -> None:
        atlas = self.parsed()
        page = render.render_html(atlas)
        block = json_block(page)
        self.assertNotIn("</", block)
        data = json.loads(block)
        self.assertEqual({c["id"] for c in data["components"]},
                         {"c-web", "c-xss", "c-worker", "c-sched", "c-arch", "c-leg"})
        titles = {c["id"]: c["title"] for c in data["components"]}
        self.assertEqual(titles["c-xss"], XSS_TITLE)

    def test_xss_title_is_escaped_in_visible_html(self) -> None:
        atlas = self.parsed()
        page = render.render_html(atlas)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)
        block = json_block(page)
        self.assertNotIn("</script>", block)

    def test_no_network_markers_and_github_only_links(self) -> None:
        atlas = self.parsed()
        page = render.render_html(atlas, render.parse_check(fixture_check()))
        for marker in ("<script src", "<link", "@import", "url(http"):
            self.assertNotIn(marker, page)
        for match in re.finditer("http", page):
            window = page[match.start():match.start() + 22]
            self.assertTrue(window.startswith("https://github.com/"),
                            "non-github http at %r" % (window,))

    def test_evidence_urls_use_atlas_repos_or_ref_url(self) -> None:
        atlas = self.parsed()
        page = render.render_html(atlas)
        self.assertIn("https://github.com/Example/pay-lib/blob/develop/docs/w.md", page)
        self.assertIn("https://github.com/Example/extra-lib/blob/main/spec.md", page)
        self.assertIn("https://github.com/Example/shop/blob/main/custom.md", page)
        self.assertIn("https://github.com/Example/shop/blob/main/docs/a.md#usage", page)

    def test_check_line_only_with_check(self) -> None:
        atlas = self.parsed()
        plain = render.render_html(atlas)
        self.assertNotIn("references present", plain)
        check = render.parse_check(fixture_check())
        lined = render.render_html(atlas, check)
        self.assertIn("Checked against origin/main on ci-example", lined)
        self.assertIn("8/10 references present", lined)
        self.assertIn("missing: 2", lined)
        self.assertIn("unresolved: 1", lined)

    def test_write_round_trip_and_detects_change(self) -> None:
        atlas = self.parsed()
        page = render.render_html(atlas, render.parse_check(fixture_check()))
        with tempfile.TemporaryDirectory() as tmp:
            out_path = Path(tmp) / "index.html"
            render._write(out_path, page)
            self.assertEqual(out_path.read_text(encoding="utf-8"), page)
            out_path.write_bytes(out_path.read_bytes() + b"\n")
            self.assertNotEqual(out_path.read_text(encoding="utf-8"), page)

    def test_mermaid_structure_and_escaping(self) -> None:
        atlas = self.parsed()
        mmd = render.render_mermaid(atlas, "flows")
        self.assertTrue(mmd.startswith("flowchart TB\n"))
        self.assertEqual(len(re.findall(r"^\s*subgraph ", mmd, re.MULTILINE)), 3)
        self.assertEqual(len(re.findall(r'\["', mmd)) - 3, 6)
        dotted = [ln for ln in mmd.splitlines() if "-.->" in ln]
        solid = [ln for ln in mmd.splitlines() if "-->" in ln and "-.->" not in ln]
        self.assertEqual(len(dotted), 3)
        self.assertEqual(len(solid), 2)
        self.assertNotIn("<script>", mmd)
        self.assertNotIn('"quoted"', mmd)
        self.assertIn("#quot;", mmd)
        slim = render.render_mermaid(atlas, "components")
        self.assertNotIn("-->", slim)
        self.assertNotIn("-.->", slim)
        self.assertEqual(len(re.findall(r"^\s*subgraph ", slim, re.MULTILINE)), 3)

    def test_duplicate_key_raises(self) -> None:
        with self.assertRaises(render.AtlasError) as ctx:
            render.strict_json('{"schema": "estate-atlas@2", "schema": "x"}')
        self.assertIn("duplicate", str(ctx.exception))

    def test_unknown_component_raises(self) -> None:
        bad = fixture_atlas()
        bad["flows"][0] = dict(bad["flows"][0], to="no-such")
        with self.assertRaises(render.AtlasError) as ctx:
            render.parse_atlas(bad)
        self.assertIn("unknown component", str(ctx.exception))

    def test_zero_flows(self) -> None:
        atlas = fixture_atlas()
        atlas["flows"] = []
        parsed = render.parse_atlas(copy.deepcopy(atlas))
        page = render.render_html(parsed)
        self.assertIn("No gaps", page)
        mmd = render.render_mermaid(parsed, "flows")
        self.assertNotIn("-->", mmd)

    def test_namespaced_schema_suffix_is_accepted(self) -> None:
        namespaced = fixture_atlas()
        namespaced["schema"] = "example-shop/atlas@2"
        render.parse_atlas(copy.deepcopy(namespaced))
        bad = fixture_atlas()
        bad["schema"] = "estate-atlas@9"
        with self.assertRaises(render.AtlasError):
            render.parse_atlas(bad)

    def test_atlas_v2_optional_fields_are_accepted(self) -> None:
        doc = fixture_atlas()
        doc["components"][0]["instances"] = ["svc:*"]
        doc["components"][4]["expect"] = [{"repo": "shop", "path": "docs/a.md"}]
        doc["flows"][3]["expect"] = [{"repo": "shop", "path": "docs/a.md"}]
        doc["vocabularies"] = [{"id": "node-kinds", "title": "Kinds", "owner": "c-web",
                                "source": {"repo": "shop", "path": "kinds.json",
                                           "select": "kinds/*/kind"},
                                "terms": [{"term": "order", "means": "A customer order."}]}]
        parsed = render.parse_atlas(copy.deepcopy(doc))
        page = render.render_html(parsed, render.parse_check(fixture_check()))
        self.assertIn("Estate Atlas", page)

    def test_vocabularies_view_present_only_with_vocabularies(self) -> None:
        atlas = self.parsed()
        plain = render.render_html(atlas)
        self.assertNotIn('data-view="vocabularies"', plain)
        self.assertNotIn("Vocabularies", plain)
        doc = fixture_atlas()
        doc["vocabularies"] = [{"id": "node-kinds", "title": "Kinds", "owner": "c-web",
                                "source": {"repo": "shop", "path": "kinds.json",
                                           "select": "kinds/*/kind"},
                                "terms": [{"term": "order", "means": "A customer order."}]}]
        with_vocab = render.parse_atlas(copy.deepcopy(doc))
        page = render.render_html(with_vocab)
        self.assertIn('data-view="vocabularies"', page)
        self.assertIn("Vocabularies", page)
        self.assertIn("Kinds", page)
        self.assertIn("order", page)
        self.assertIn("A customer order.", page)
        self.assertIn("Storefront", page)
        self.assertIn("shop/kinds.json", page)

    def test_vocab_drift_lists_render(self) -> None:
        doc = fixture_atlas()
        doc["vocabularies"] = [{"id": "node-kinds", "title": "Kinds", "owner": "c-web",
                                "source": {"repo": "shop", "path": "kinds.json",
                                           "select": "kinds/*/kind"},
                                "terms": [{"term": "order", "means": "A customer order."}]}]
        atlas = render.parse_atlas(copy.deepcopy(doc))
        check_doc = fixture_check()
        check_doc["vocabularies"] = [{"id": "node-kinds", "status": "drift",
                                     "missing_in_atlas": ["refund"],
                                     "missing_in_source": ["order"]}]
        check = render.parse_check(copy.deepcopy(check_doc))
        page = render.render_html(atlas, check)
        self.assertIn("drift", page)
        self.assertIn("Missing in atlas: refund", page)
        self.assertIn("Missing in source: order", page)
        self.assertIn("vocabularies: drift", page)

    def test_runs_as_for_component_with_instances(self) -> None:
        doc = fixture_atlas()
        doc["components"][0]["instances"] = ["svc:*"]
        atlas = render.parse_atlas(copy.deepcopy(doc))
        page = render.render_html(atlas)
        self.assertIn("Runs as", page)
        self.assertIn("svc:*", json_block(page))

    def test_promotable_flow_shows_pill_and_header_count(self) -> None:
        doc = fixture_atlas()
        doc["flows"][3]["expect"] = [{"repo": "shop", "path": "docs/a.md"}]
        atlas = render.parse_atlas(copy.deepcopy(doc))
        check_doc = fixture_check()
        check_doc["promotable"] = [{"owner": "flows.f-plan", "refs": 1}]
        check = render.parse_check(copy.deepcopy(check_doc))
        page = render.render_html(atlas, check)
        self.assertIn("Expected evidence:", page)
        self.assertIn("built: update the atlas", page)
        self.assertIn("1 promotable", page)

    def test_js_reapplies_state(self) -> None:
        js = render.JS_TEXT
        reset_at = js.find("function resetDim()")
        self.assertNotEqual(reset_at, -1, "resetDim is missing")
        clear_at = js.find("function clearDim()", reset_at)
        self.assertNotEqual(clear_at, -1, "clearDim is missing")
        self.assertIn("searchMatches", js[reset_at:clear_at])
        paint_at = js.find("function paintEdges()")
        self.assertNotEqual(paint_at, -1, "paintEdges is missing")
        inner_at = js.find("edgePaths.innerHTML=out;", paint_at)
        self.assertNotEqual(inner_at, -1, "paintEdges rebuild is missing")
        self.assertIn("hiddenStatus", js[inner_at:js.find("function resetDim()", inner_at)])

    def test_embedded_check_in_atlas_is_used(self) -> None:
        doc = fixture_atlas()
        doc["check"] = fixture_check()
        atlas = render.parse_atlas(copy.deepcopy(doc))
        page = render.render_html(atlas)
        self.assertIn("Checked against origin/main on ci-example", page)
        explicit_doc = fixture_check()
        explicit_doc["host"] = "other-host"
        explicit = render.parse_check(copy.deepcopy(explicit_doc))
        page_explicit = render.render_html(atlas, explicit)
        self.assertIn("other-host", page_explicit)
        self.assertNotIn("Checked against origin/main on ci-example", page_explicit)


class TrafficRenderTests(unittest.TestCase):
    def test_rule_lines_are_escaped(self) -> None:
        doc = fixture_atlas()
        doc["flows"][0]["traffic"] = [{"source": "journal", "producer": "<b>gate", "verb": ["merged", "refused"],
                                       "subject": "pr:*"}]
        atlas = render.parse_atlas(copy.deepcopy(doc))
        page = render.render_html(atlas)
        self.assertIn("Traffic rules", page)
        self.assertIn("journal · &lt;b&gt;gate · merged, refused · * → pr:*", page)
        self.assertNotIn("<b>gate", page)

    def test_pulse_rule_line_ends_with_pulse(self) -> None:
        line = render._traffic_rule_line({"source": "events", "producer": "scheduler",
                                          "verb": "scheduler.tick", "pulse": True})
        self.assertTrue(line.endswith(" (pulse)"))
        plain = render._traffic_rule_line({"source": "events", "producer": "scheduler",
                                           "verb": "scheduler.tick"})
        self.assertNotIn("(pulse)", plain)


if __name__ == "__main__":
    unittest.main()


class ModelParityTests(unittest.TestCase):
    """Every atlas the model accepts renders: statuses and title lengths agree."""

    def test_status_sets_match_the_model(self) -> None:
        from estate_atlas import model
        self.assertEqual(set(render.COMPONENT_STATUSES), model.COMPONENT_STATUS)
        self.assertEqual(set(render.FLOW_STATUSES), model.FLOW_STATUS)
        self.assertEqual(set(render.CONTRACT_STATUSES), model.CONTRACT_STATUS)
        self.assertEqual(set(render.EXPECT_STATUSES), model.EXPECT_STATUSES)

    def test_retired_and_planned_components_with_long_titles_render(self) -> None:
        from estate_atlas import model
        shop = Path(__file__).resolve().parents[1] / "examples" / "shop" / "atlas.json"
        doc = json.loads(shop.read_text(encoding="utf-8"))
        doc["components"][0]["status"] = "retired"
        doc["components"][0]["evidence"] = []
        doc["components"][1]["status"] = "planned"
        doc["components"][1]["evidence"] = []
        doc["components"][1]["title"] = "T" * 300
        model.validate(doc)
        parsed = render.parse_atlas(doc)
        html = render.render_html(parsed)
        self.assertIn('class="dot retired"', html)
        self.assertIn('class="dot planned"', html)
        mmd = render.render_mermaid(parsed, "flows")
        self.assertIn("classDef retired", mmd)

