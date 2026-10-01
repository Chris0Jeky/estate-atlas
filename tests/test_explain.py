"""Explain checks against a fictional shop atlas.

Port of the explain checks: plain-English lines, the tour, the Markdown
rendering and the tour-doc check, without any private names.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from estate_atlas import explain as explain_mod

EMD = "\u2014"
ELL = "\u2026"


def fixture_atlas() -> dict:
    hub_flows = [
        {"id": "h%d" % i, "from": "gw", "to": "hub", "contract": None,
         "trigger": "tick %d" % i, "status": "live",
         "evidence": [{"repo": "shop", "path": "README.md"}]}
        for i in range(7)
    ]
    return {
        "schema": "estate-atlas@2",
        "updated": "2026-09-30",
        "repos": {},
        "layers": [
            {"id": "edge", "title": "Edge", "summary": "Entry points."},
            {"id": "core", "title": "Core", "summary": "Workers and store."},
        ],
        "components": [
            {"id": "gw", "title": "Gateway", "layer": "edge",
             "home": "shop", "status": "live", "summary": "Entry point.",
             "surfaces": [], "owns": ["gate key"],
             "evidence": [{"repo": "shop", "path": "README.md"}]},
            {"id": "hub", "title": "Hub", "layer": "edge",
             "home": "shop", "status": "live", "summary": "Fan-in hub.",
             "surfaces": [], "owns": [],
             "evidence": [{"repo": "shop", "path": "README.md"}]},
            {"id": "worker", "title": "Worker", "layer": "core",
             "home": "shop", "status": "live", "summary": "Does the work.",
             "surfaces": [], "owns": ["slots", "leases"],
             "evidence": [{"repo": "shop", "path": "README.md"}]},
            {"id": "arch", "title": "Archive", "layer": "core",
             "home": "shop", "status": "planned", "summary": "Cold store.",
             "surfaces": [], "owns": ["snapshots", "indexes", "tapes"],
             "evidence": [],
             "expect": [{"repo": "shop", "path": "docs/ARCHIVE.md"}]},
            {"id": "sink", "title": "Drain", "layer": "core",
             "home": "shop", "status": "live", "summary": "Drain.",
             "surfaces": [], "owns": [],
             "evidence": [{"repo": "shop", "path": "README.md"}]},
        ],
        "contracts": [
            {"id": "k-one", "title": "Events", "producer": "gw",
             "consumers": ["worker", "hub"], "format": "json", "status": "live",
             "summary": "Event feed.",
             "evidence": [{"repo": "shop", "path": "docs/a.md"}]},
            {"id": "k-two", "title": "Slots", "producer": "worker",
             "consumers": ["arch"], "format": "json", "status": "planned",
             "summary": "Slot export.", "evidence": []},
        ],
        "flows": [
            {"id": "f-main", "from": "gw", "to": "worker", "contract": "k-one",
             "trigger": "push", "status": "live",
             "evidence": [{"repo": "shop", "path": "README.md"}]},
            {"id": "f-up", "from": "worker", "to": "arch", "contract": "k-two",
             "trigger": "nightly", "status": "planned", "gap": "Not built yet.",
             "evidence": []},
            {"id": "f-down", "from": "worker", "to": "sink", "contract": None,
             "trigger": "never", "status": "absent", "gap": "Removed.",
             "evidence": []},
            {"id": "f-pulse", "from": "gw", "to": "sink", "contract": None,
             "trigger": "heartbeat", "status": "live",
             "evidence": [{"repo": "shop", "path": "README.md"}],
             "traffic": [{"source": "events", "producer": "gw", "pulse": True}]},
            {"id": "f-quiet", "from": "gw", "to": "sink", "contract": None,
             "trigger": "manual", "status": "live",
             "evidence": [{"repo": "shop", "path": "README.md"}],
             "traffic": [{"source": "journal", "producer": "gw"}]},
            {"id": "f-doc", "from": "arch", "to": "worker", "contract": None,
             "trigger": "readback", "status": "documented", "gap": "Written up.",
             "evidence": []},
        ] + hub_flows,
    }


def flow_entry(d7: int, d24: int = 0, declared: int | None = None,
               pulse_d7: int = 0) -> dict:
    return {"status": "live", "rules": 1, "d24": d24, "d7": d7,
            "pulse_d7": pulse_d7, "last_at": 1759190000.0, "heat": "hot",
            "by_basis": {"declared": d7 if declared is None else declared,
                         "inferred": 0}}


def fixture_traffic() -> dict:
    hub = {"h%d" % i: flow_entry(v, 1, v)
           for i, v in enumerate([50, 40, 30, 20, 10, 5, 1])}
    flows = {
        "f-main": flow_entry(102, 9, 90),
        "f-up": flow_entry(0, 0, 0),
        "f-down": flow_entry(0, 0, 0),
        "f-pulse": flow_entry(0, 0, 0, 26914),
        "f-quiet": flow_entry(0, 0, 0),
    }
    flows.update(hub)
    return {
        "schema": "estate-atlas-traffic@1",
        "generated": 1759190400.0,  # 2025-09-30 00:00 UTC
        "coverage": {"records": 500, "declared": 300},
        "flows": flows,
        "crosschecks": {"silent": ["f-quiet"], "off_status": [{"flow": "f-up", "status": "planned", "d7": 4}]},
    }


def fixture_live() -> dict:
    return {
        "gw": {
            "instances": 3,
            "by_status": {"live": 2, "idle": 1},
            "failing": [
                {"intent": "serve", "subject": "api", "severity": "high"},
                {"intent": "pair", "subject": "phone", "severity": "low"},
                {"intent": "push", "subject": "ntfy", "severity": "low"},
                {"intent": "sync", "subject": "clock", "severity": "low"},
            ],
        },
        "worker": {"instances": 1, "by_status": {"live": 1}, "failing": []},
    }


def kinds(doc: dict) -> list[str]:
    return [line["kind"] for line in doc["lines"]]


def text_of(doc: dict, kind: str) -> str:
    for line in doc["lines"]:
        if line["kind"] == kind:
            return line["text"]
    raise AssertionError("no line %r" % (kind,))


def write_tour(tmp: Path, atlas: dict) -> Path:
    tour_path = tmp / "TOUR.md"
    tour_path.write_text(explain_mod.render_tour_md(atlas), encoding="utf-8", newline="\n")
    return tour_path


class ExplainTests(unittest.TestCase):
    def test_every_kind_appears_in_order_when_its_data_exists(self) -> None:
        doc = explain_mod.explain(fixture_atlas(), "worker",
                                  traffic=fixture_traffic(), live=fixture_live())
        self.assertEqual(doc["schema"], "estate-atlas-explain@1")
        self.assertEqual(kinds(doc), ["what", "fed_by", "feeds", "contracts",
                                     "running", "this_week", "gaps"])

    def test_quiet_part_omits_empty_kinds_but_keeps_what(self) -> None:
        doc = explain_mod.explain(fixture_atlas(), "sink")
        self.assertEqual(kinds(doc), ["what", "fed_by", "gaps"])
        self.assertTrue(text_of(doc, "what").startswith("Drain (shop) is live."))

    def test_non_live_part_mentions_its_gap_and_owns(self) -> None:
        doc = explain_mod.explain(fixture_atlas(), "arch")
        self.assertEqual(
            text_of(doc, "what"),
            "Archive (shop) is planned. Cold store. "
            "It is expected in shop docs/ARCHIVE.md. "
            "It owns snapshots, indexes and tapes.")
        gw = explain_mod.explain(fixture_atlas(), "gw")
        self.assertIn("It owns gate key.", text_of(gw, "what"))
        worker = explain_mod.explain(fixture_atlas(), "worker")
        self.assertIn("It owns slots and leases.", text_of(worker, "what"))

    def test_fed_by_orders_by_traffic_and_caps_at_six(self) -> None:
        doc = explain_mod.explain(fixture_atlas(), "hub",
                                  traffic=fixture_traffic())
        text = text_of(doc, "fed_by")
        self.assertTrue(
            text.startswith("Fed by Gateway (tick 0) %s 50 crossings this week." % (EMD,)),
            text)
        self.assertIn("tick 5) %s 5 crossings this week" % (EMD,), text)
        self.assertNotIn("tick 6", text)
        self.assertTrue(text.endswith("%s and 1 more." % (ELL,)), text)
        refs = [line for line in doc["lines"] if line["kind"] == "fed_by"][0]["refs"]
        self.assertIn("flow:h0", refs)
        self.assertNotIn("flow:h6", refs)

    def test_pulse_nothing_and_plain_flow_sentences(self) -> None:
        doc = explain_mod.explain(fixture_atlas(), "sink",
                                  traffic=fixture_traffic())
        self.assertEqual(
            text_of(doc, "fed_by"),
            "Fed by Gateway (heartbeat) %s \u2665 26,914 heartbeats this week. "
            "Fed by Worker (never). "
            "Fed by Gateway (manual) %s nothing this week." % (EMD, EMD))

    def test_feeds_uses_the_same_shape(self) -> None:
        doc = explain_mod.explain(fixture_atlas(), "worker")
        self.assertEqual(text_of(doc, "feeds"),
                         "Feeds Drain (never). Feeds Archive (nightly).")

    def test_contracts_names_writes_then_reads(self) -> None:
        doc = explain_mod.explain(fixture_atlas(), "worker")
        self.assertEqual(text_of(doc, "contracts"),
                         "It writes Slots. It reads Events.")

    def test_running_with_failing_conditions_capped_at_three(self) -> None:
        doc = explain_mod.explain(fixture_atlas(), "gw", live=fixture_live())
        self.assertEqual(
            text_of(doc, "running"),
            "Running now: 3 instances (2 live, 1 idle); "
            "4 failing conditions: serve on api, pair on phone, push on ntfy")
        worker = explain_mod.explain(fixture_atlas(), "worker", live=fixture_live())
        self.assertEqual(text_of(worker, "running"),
                         "Running now: 1 instances (1 live)")

    def test_this_week_counts_silent_and_off_status(self) -> None:
        doc = explain_mod.explain(fixture_atlas(), "worker",
                                  traffic=fixture_traffic())
        self.assertEqual(
            text_of(doc, "this_week"),
            "This week: 102 records crossed its flows (9 in the last 24 hours). "
            "f-up carried 4 records while marked planned.")
        sink = explain_mod.explain(fixture_atlas(), "sink",
                                   traffic=fixture_traffic())
        self.assertEqual(
            text_of(sink, "this_week"),
            "This week: 0 records crossed its flows (0 in the last 24 hours). "
            "It keeps a heartbeat of 26,914 pulses. f-quiet is silent.")

    def test_gaps_lists_non_live_flows_by_id(self) -> None:
        doc = explain_mod.explain(fixture_atlas(), "worker")
        self.assertEqual(text_of(doc, "gaps"),
                         "f-doc is documented: Written up. f-down is absent: Removed. f-up is planned: Not built yet.")

    def test_refs_are_the_sorted_union_of_line_refs(self) -> None:
        doc = explain_mod.explain(fixture_atlas(), "worker",
                                  traffic=fixture_traffic(), live=fixture_live())
        union: set[str] = set()
        for line in doc["lines"]:
            union |= set(line["refs"])
        self.assertEqual(doc["refs"], sorted(union))
        for ref in ("component:gw", "component:arch", "flow:f-main",
                    "flow:f-up", "contract:k-one", "contract:k-two"):
            self.assertIn(ref, doc["refs"])

    def test_unknown_id_raises_key_error(self) -> None:
        with self.assertRaises(KeyError):
            explain_mod.explain(fixture_atlas(), "nope")

    def test_explain_works_for_every_fixture_component(self) -> None:
        atlas = fixture_atlas()
        for comp in atlas["components"]:
            doc = explain_mod.explain(atlas, comp["id"])
            self.assertEqual(kinds(doc)[0], "what")
            self.assertTrue(text_of(doc, "what"))
            self.assertEqual(doc["refs"], sorted(doc["refs"]))


class TourTests(unittest.TestCase):
    def test_layer_order_and_first_three_without_traffic(self) -> None:
        atlas = fixture_atlas()
        doc = explain_mod.tour(atlas)
        self.assertEqual(doc["schema"], "estate-atlas-tour@1")
        layers = [s for s in doc["steps"] if s["kind"] == "layer"]
        self.assertEqual([s["id"] for s in layers], ["edge", "core"])
        ordered = explain_mod.order_cards(atlas)
        parts = [s for s in doc["steps"] if s["kind"] == "part"]
        self.assertEqual([s["id"] for s in parts[:2]], ordered["edge"][:3])
        self.assertEqual([s["id"] for s in parts[2:]], ordered["core"][:3])
        self.assertFalse([s for s in doc["steps"] if s["kind"] == "flow"])
        for step in parts:
            self.assertNotIn("This week:", step["text"])

    def test_busiest_three_parts_with_traffic(self) -> None:
        doc = explain_mod.tour(fixture_atlas(), traffic=fixture_traffic())
        parts = [s for s in doc["steps"] if s["kind"] == "part"]
        self.assertEqual([s["id"] for s in parts[:2]], ["gw", "hub"])
        self.assertEqual([s["id"] for s in parts[2:]], ["worker", "arch", "sink"])
        for step in parts:
            self.assertIn("This week:", step["text"])

    def test_five_busiest_declared_flows(self) -> None:
        doc = explain_mod.tour(fixture_atlas(), traffic=fixture_traffic())
        flows = [s for s in doc["steps"] if s["kind"] == "flow"]
        self.assertEqual([s["id"] for s in flows],
                         ["f-main", "h0", "h1", "h2", "h3"])
        self.assertEqual(
            flows[0]["text"],
            "Gateway %s Worker: push. 90 declared crossings this week." % ("\u2192",))
        self.assertEqual(flows[0]["title"], "Gateway \u2192 Worker")


class RenderTests(unittest.TestCase):
    def test_deterministic(self) -> None:
        atlas = fixture_atlas()
        self.assertEqual(explain_mod.render_tour_md(atlas),
                         explain_mod.render_tour_md(atlas))
        self.assertEqual(
            explain_mod.render_tour_md(atlas, fixture_traffic()),
            explain_mod.render_tour_md(atlas, fixture_traffic()))

    def test_escapes_pipes_and_angle_brackets(self) -> None:
        atlas = fixture_atlas()
        atlas["components"][0]["title"] = "Odd|Pipe <b>"
        atlas["components"][0]["summary"] = "A <tag> with | pipes."
        md = explain_mod.render_tour_md(atlas)
        self.assertIn("Odd\\|Pipe &lt;b>", md)
        self.assertIn("A &lt;tag> with \\| pipes.", md)
        self.assertNotIn("<b>", md)
        self.assertNotIn("<tag>", md)

    def test_as_of_line_only_with_traffic(self) -> None:
        plain = explain_mod.render_tour_md(fixture_atlas())
        self.assertTrue(plain.startswith("# A tour of the estate\n"), plain[:60])
        self.assertIn("Generated by estate-atlas", plain)
        self.assertIn("Do not edit", plain)
        self.assertNotIn("As of", plain)
        self.assertIn("## Busiest flows", plain)
        self.assertIn("No traffic snapshot yet.", plain)
        dated = explain_mod.render_tour_md(fixture_atlas(), fixture_traffic())
        self.assertIn("As of 2025-09-30 00:00 UTC", dated)


class CheckTests(unittest.TestCase):
    def test_clean_after_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tour_path = write_tour(Path(tmp), fixture_atlas())
            ok, message = explain_mod.check_tour(tour_path, fixture_atlas())
            self.assertTrue(ok, message)

    def test_hand_edit_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tour_path = write_tour(Path(tmp), fixture_atlas())
            text = tour_path.read_text(encoding="utf-8")
            tour_path.write_text(text.replace("Entry points.", "Entryways."),
                                 encoding="utf-8")
            ok, _message = explain_mod.check_tour(tour_path, fixture_atlas())
            self.assertFalse(ok)

    def test_check_accepts_a_crlf_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tour_path = write_tour(Path(tmp), fixture_atlas())
            tour_path.write_bytes(
                tour_path.read_bytes().replace(b"\n", b"\r\n"))
            ok, message = explain_mod.check_tour(tour_path, fixture_atlas())
            self.assertTrue(ok, message)

    def test_missing_doc_is_not_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ok, message = explain_mod.check_tour(
                Path(tmp) / "missing.md", fixture_atlas())
            self.assertFalse(ok)
            self.assertTrue(message)


class NoteTests(unittest.TestCase):
    NOTE = "Generated from x. Do not edit."

    def test_custom_note_replaces_the_default(self) -> None:
        md = explain_mod.render_tour_md(fixture_atlas(), note=self.NOTE)
        self.assertIn(self.NOTE, md)
        self.assertNotIn(explain_mod.GENERATED_NOTE, md)
        self.assertIn(explain_mod.GENERATED_NOTE, explain_mod.render_tour_md(fixture_atlas()))

    def test_check_passes_with_the_matching_note_and_fails_otherwise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tour_path = Path(tmp) / "TOUR.md"
            tour_path.write_text(explain_mod.render_tour_md(fixture_atlas(), note=self.NOTE),
                                 encoding="utf-8", newline="\n")
            ok, message = explain_mod.check_tour(tour_path, fixture_atlas(), note=self.NOTE)
            self.assertTrue(ok, message)
            ok, message = explain_mod.check_tour(tour_path, fixture_atlas(),
                                                 write_hint="run make tour")
            self.assertFalse(ok)
            self.assertIn("run make tour", message)
            self.assertNotIn(explain_mod.TOUR_WRITE_HINT, message)
            ok, message = explain_mod.check_tour(tour_path, fixture_atlas())
            self.assertFalse(ok)
            self.assertIn(explain_mod.TOUR_WRITE_HINT, message)

    def test_a_multi_line_or_empty_note_raises(self) -> None:
        for bad in ("a\nb", "a\rb", "", "  "):
            with self.assertRaises(ValueError):
                explain_mod.render_tour_md(fixture_atlas(), note=bad)

    def test_check_tour_reports_a_bad_note_or_write_hint_instead_of_raising(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tour_path = write_tour(Path(tmp), fixture_atlas())
            for bad in ("a\nb", "a\rb", "", "  "):
                ok, message = explain_mod.check_tour(tour_path, fixture_atlas(), note=bad)
                self.assertFalse(ok)
                self.assertIn("note", message)
                ok, message = explain_mod.check_tour(tour_path, fixture_atlas(), write_hint=bad)
                self.assertFalse(ok)
                self.assertIn("write_hint", message)


if __name__ == "__main__":
    unittest.main()
