"""Render checks against a fictional shop atlas.

Covers determinism, escaping, wording and structure.
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


class ContractExpectRenderTests(unittest.TestCase):
    """A planned contract shows its expected evidence and the promotable pill."""

    def planned_contract_doc(self) -> dict:
        doc = fixture_atlas()
        doc["contracts"][2]["expect"] = [
            {"repo": "shop", "path": "docs/slots.md", "anchor": "slots"}]
        return doc

    def contracts_table(self, page: str) -> str:
        match = re.search(r'<main id="view-contracts"[^>]*>(.*?)</main>', page, re.DOTALL)
        assert match is not None, "contracts view is missing"
        return match.group(1)

    def parsed_default(self) -> dict:
        return render.parse_atlas(copy.deepcopy(fixture_atlas()))

    def test_planned_contract_shows_expected_evidence_and_pill(self) -> None:
        atlas = render.parse_atlas(copy.deepcopy(self.planned_contract_doc()))
        check_doc = fixture_check()
        check_doc["promotable"] = [{"owner": "contracts.k-three", "refs": 1}]
        check = render.parse_check(copy.deepcopy(check_doc))
        page = render.render_html(atlas, check)
        table = self.contracts_table(page)
        row = next(r for r in table.split("<tr>") if "<td>k-three</td>" in r)
        self.assertIn(
            'Expected evidence: <a href="https://github.com/Example/shop/blob/main/docs/slots.md#slots" '
            'rel="noopener">docs/slots.md#slots</a>.', row)
        self.assertIn('<span class="pill planned">built: update the atlas</span>', row)
        other = next(r for r in table.split("<tr>") if "<td>k-one</td>" in r)
        self.assertNotIn("Expected evidence", other)
        self.assertNotIn("built: update the atlas", other)
        self.assertIn("1 promotable", page)

    def test_expect_without_promotable_has_no_pill(self) -> None:
        atlas = render.parse_atlas(copy.deepcopy(self.planned_contract_doc()))
        table = self.contracts_table(render.render_html(atlas, render.parse_check(fixture_check())))
        self.assertIn("Expected evidence:", table)
        self.assertNotIn("built: update the atlas", table)

    def test_promotable_contract_without_expect_still_gets_pill(self) -> None:
        check_doc = fixture_check()
        check_doc["promotable"] = [{"owner": "contracts.k-three", "refs": 0}]
        table = self.contracts_table(
            render.render_html(self.parsed_default(), render.parse_check(copy.deepcopy(check_doc))))
        self.assertNotIn("Expected evidence", table)
        self.assertEqual(table.count("built: update the atlas"), 1)

    def test_contracts_without_expect_or_promotable_are_unchanged(self) -> None:
        table = self.contracts_table(
            render.render_html(self.parsed_default(), render.parse_check(fixture_check())))
        self.assertNotIn("Expected evidence", table)
        self.assertNotIn("built: update the atlas", table)
        self.assertIn("<td>k-three</td><td>csv</td><td>planned</td><td>c-sched</td><td>c-leg</td>"
                      '<td><a href="https://github.com/Example/shop/blob/main/custom.md" '
                      'rel="noopener">x.md</a></td></tr>', table)


HOSTILE = "<img src=x onerror=alert(1)>"
HOSTILE_ESCAPED = "&lt;img src=x onerror=alert(1)&gt;"
HOSTILE_JSON = "\\u003cimg src=x onerror=alert(1)>"


def hostile(tag: str) -> str:
    """The hostile string plus a visible tag, so each sink is found on its own."""
    return "%s[%s]" % (HOSTILE, tag)


def hostile_ref(tag: str, repo: str = "shop") -> dict:
    return {"repo": repo, "path": hostile(tag + ".path"), "anchor": hostile(tag + ".anchor"),
            "note": hostile(tag + ".note")}


# Where each sink's text lands in the page: "html" (server-rendered markup, entity-escaped),
# "island" (only inside the JSON island, escaped there), or "none" (the model accepts the
# text but the page never prints it; the raw string must still never appear).
HOSTILE_SINKS = {
    "layer.title": "html", "layer.summary": "html",
    "component.title": "html", "component.summary": "island",
    "component.planned.summary": "html",
    "component.surface.name": "island",
    "component.owns": "island",
    "surface.evidence.path": "island", "surface.evidence.anchor": "island",
    "surface.evidence.note": "island",
    "component.evidence.path": "island", "component.evidence.anchor": "island",
    "component.evidence.note": "island",
    "contract.title": "none", "contract.summary": "none",
    "flow.trigger": "island", "flow.gap": "html",
    "vocab.title": "html", "vocab.term": "html", "vocab.means": "html",
    "vocab.source.path": "html",
    "check.host": "html",
    "check.drift.missing_in_atlas": "html", "check.drift.missing_in_source": "html",
    "check.drift.why": "html",
    "expect.component.path": "html", "expect.component.anchor": "html",
    "expect.component.note": "html",
    "expect.contract.path": "html", "expect.contract.anchor": "html",
    "expect.contract.note": "html",
    "expect.flow.path": "html", "expect.flow.anchor": "html", "expect.flow.note": "html",
}

# Skipped, because the model forbids the characters or the page never carries the field:
#  - repo names in refs, the name in external:<name> homes, the ref url key, contract format
#    and the traffic rule patterns: the model requires a declared repo id, an id-like external
#    name, no url, a fixed format set and patterns with no whitespace (the hostile string has
#    a space). (The page's own parser is looser;
#    test_input_only_the_page_parser_accepts_is_escaped covers that path.)
#  - instances (the "Runs as" list): INSTANCE_RE allows only [A-Za-z0-9._/#@:+-], so "<" cannot occur.
#  - ids (layer, component, contract, flow, vocabulary) and the producer, consumer, from, to
#    and owner references to them: ID_RE rejects "<". The home repo id: REPO_ID_RE rejects "<".
#  - surface kind: the model allows a fixed set of kinds.
#  - traffic.source: limited to journal, links or events.
# Fed in but never printed (so only the raw-string-absent assertions apply):
#  - promotable owners and reasons: the page counts owners and matches them by exact id,
#    and the model drops "reason".
# The Flows view prints a flow's trigger only for flows that carry traffic rules, so the
# trigger is an island sink here and an html sink in the traffic test below.
# Evidence refs on surfaces and components are links built by the page script, so the server
# only carries them in the island.


def hostile_atlas() -> dict:
    doc = fixture_atlas()
    doc["layers"][0]["title"] = hostile("layer.title")
    doc["layers"][0]["summary"] = hostile("layer.summary")
    # the shared fixture's external names are not model-valid (capitals); the model wants id-like names
    doc["components"][1]["home"] = "external:paycorp"
    doc["components"][5]["home"] = "external:oldco"
    web = doc["components"][0]
    web["title"] = hostile("component.title")
    web["summary"] = hostile("component.summary")
    web["surfaces"] = [{"kind": "cli",
                        "name": hostile("component.surface.name"),
                        "evidence": [hostile_ref("surface.evidence")]}]
    web["owns"] = [hostile("component.owns")]
    web["evidence"] = [hostile_ref("component.evidence")]
    web["instances"] = ["svc:*"]
    arch = doc["components"][4]  # planned component: listed under "planned components"
    arch["summary"] = hostile("component.planned.summary")
    arch["expect"] = [hostile_ref("expect.component")]
    contract = doc["contracts"][2]  # planned contract
    contract["title"] = hostile("contract.title")
    contract["summary"] = hostile("contract.summary")
    doc["contracts"][0]["format"] = "json"  # the model allows a fixed set of formats
    doc["contracts"][1]["format"] = "jsonl"
    contract["format"] = "yaml"
    contract["expect"] = [hostile_ref("expect.contract")]
    contract["evidence"] = [{"repo": "shop", "path": "x.md"}]
    doc["components"][5]["evidence"] = [{"repo": "shop", "path": "old.md"}]
    flow = doc["flows"][3]  # planned flow
    flow["trigger"] = hostile("flow.trigger")
    flow["gap"] = hostile("flow.gap")
    flow["expect"] = [hostile_ref("expect.flow")]
    doc["vocabularies"] = [
        {"id": "v-lists", "title": hostile("vocab.title"), "owner": "c-web",
         "source": {"repo": "shop", "path": hostile("vocab.source.path"), "each": True},
         "terms": [{"term": hostile("vocab.term"), "means": hostile("vocab.means")}]},
        {"id": "v-why", "title": "Second", "owner": "c-web",
         "source": {"repo": "shop", "path": "terms.md", "each": True},
         "terms": [{"term": "plain", "means": "ordinary"}]},
    ]
    return doc


def hostile_check() -> dict:
    check = fixture_check()
    check["host"] = hostile("check.host")
    check["vocabularies"] = [
        {"id": "v-lists", "status": "drift",
         "missing_in_atlas": [hostile("check.drift.missing_in_atlas")],
         "missing_in_source": [hostile("check.drift.missing_in_source")]},
        {"id": "v-why", "status": "drift", "why": hostile("check.drift.why")},
    ]
    check["promotable"] = [
        {"owner": "contracts.k-three", "refs": 1, "reason": hostile("promotable.reason")},
        {"owner": hostile("promotable.owner"), "refs": 2},
    ]
    return check


class HostileInputTests(unittest.TestCase):
    """The page escapes `<img src=x onerror=alert(1)>` in every text sink it prints."""

    def render_page(self) -> str:
        atlas = render.parse_atlas(copy.deepcopy(hostile_atlas()))
        check = render.parse_check(copy.deepcopy(hostile_check()))
        return render.render_html(atlas, check)

    @staticmethod
    def split_island(page: str) -> tuple[str, str]:
        island = json_block(page)
        return page.replace(island, "", 1), island

    def test_fixture_is_valid_for_the_model(self) -> None:
        from estate_atlas import model
        model.validate(copy.deepcopy(hostile_atlas()))

    def test_raw_string_never_appears_outside_the_island(self) -> None:
        outside, island = self.split_island(self.render_page())
        self.assertNotIn(HOSTILE, outside)
        self.assertNotIn("<img", outside)
        self.assertNotIn(HOSTILE, island)

    def test_every_html_sink_is_entity_escaped_at_least_once(self) -> None:
        outside, _ = self.split_island(self.render_page())
        for sink, where in HOSTILE_SINKS.items():
            if where != "html":
                continue
            with self.subTest(sink=sink):
                self.assertIn(HOSTILE_ESCAPED + "[" + sink + "]", outside)

    def test_island_only_sinks_are_escaped_inside_the_island(self) -> None:
        outside, island = self.split_island(self.render_page())
        for sink, where in HOSTILE_SINKS.items():
            if where != "island":
                continue
            with self.subTest(sink=sink):
                self.assertIn(HOSTILE_JSON + "[" + sink + "]", island)
                self.assertNotIn("[" + sink + "]", outside)

    def test_unprinted_sinks_stay_unprinted(self) -> None:
        page = self.render_page()
        for sink, where in HOSTILE_SINKS.items():
            if where == "none":
                with self.subTest(sink=sink):
                    self.assertNotIn("[" + sink + "]", page)
        for tag in ("promotable.reason", "promotable.owner"):
            with self.subTest(tag=tag):
                self.assertNotIn("[" + tag + "]", page)

    def test_island_has_no_raw_angle_bracket(self) -> None:
        _, island = self.split_island(self.render_page())
        self.assertNotIn("<", island)
        data = json.loads(island)
        web = next(c for c in data["components"] if c["id"] == "c-web")
        self.assertEqual(web["title"], hostile("component.title"))

    def test_input_only_the_page_parser_accepts_is_escaped(self) -> None:
        # The page parser accepts a ref to a repo the atlas does not declare, any external
        # name and a ref url (the model validator does not), and then prints them.
        doc = hostile_atlas()
        doc["components"][1]["home"] = "external:" + hostile("external.home")
        doc["contracts"][2]["format"] = hostile("contract.format")
        doc["flows"][3]["traffic"] = [{
            "source": "journal", "producer": hostile("traffic.producer"),
            "actor": hostile("traffic.actor"), "subject": hostile("traffic.subject"),
            "verb": [hostile("traffic.verb"), "merged"]}]
        doc["contracts"][2]["evidence"] = [{
            "repo": "shop", "path": "x.md",
            "url": "https://github.com/Example/shop/blob/main/" + hostile("evidence.url")}]
        doc["components"][4]["expect"] = [hostile_ref("undeclared.expect", repo=hostile("undeclared.expect.repo"))]
        doc["components"][0]["evidence"] = [hostile_ref("undeclared.evidence", repo=hostile("undeclared.evidence.repo"))]
        doc["vocabularies"][1]["source"]["repo"] = hostile("undeclared.vocab.repo")
        page = render.render_html(render.parse_atlas(copy.deepcopy(doc)),
                                  render.parse_check(copy.deepcopy(hostile_check())))
        outside, island = self.split_island(page)
        self.assertNotIn(HOSTILE, outside)
        self.assertNotIn("<", island)
        self.assertIn(HOSTILE_ESCAPED + "[evidence.url]", outside)
        for tag in ("undeclared.expect.repo", "undeclared.vocab.repo", "external.home",
                    "contract.format", "traffic.producer", "traffic.actor", "traffic.subject",
                    "traffic.verb", "flow.trigger"):
            with self.subTest(tag=tag):
                self.assertIn(HOSTILE_ESCAPED + "[" + tag + "]", outside)
        self.assertIn(HOSTILE_JSON + "[undeclared.evidence.repo]", island)

    def test_hostile_page_is_byte_identical_across_renders(self) -> None:
        self.assertEqual(self.render_page(), self.render_page())

    def test_every_fed_tag_is_accounted_for(self) -> None:
        fed = set(re.findall(r"\[([a-z][a-z._]*)\]",
                             json.dumps(hostile_atlas()) + json.dumps(hostile_check())))
        fed_not_asserted = {"promotable.reason", "promotable.owner"}
        self.assertEqual(fed - fed_not_asserted, set(HOSTILE_SINKS))


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

