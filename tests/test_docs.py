"""Docs checks against a fictional shop atlas.

Port of the docs checks: marker safety, escaping, determinism, traffic
wording, CRLF tolerance and the write/check round trip.
"""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from estate_atlas import docs


def fixture_atlas() -> dict:
    return {
        "schema": "estate-atlas@2",
        "updated": "2026-09-30",
        "repos": {},
        "layers": [
            {"id": "edge", "title": "Edge", "summary": "Entry points."},
            {"id": "core", "title": "Core", "summary": "Workers and store."},
        ],
        "components": [
            {"id": "gw", "title": "Gate<way> & co", "layer": "edge",
             "home": "shop", "status": "live", "summary": "Entry.",
             "surfaces": [{"kind": "cli", "name": "gw",
                           "evidence": [{"repo": "shop", "path": "README.md"},
                                        {"repo": "shop", "path": "docs/g.md"}]}],
             "owns": [], "evidence": [{"repo": "shop", "path": "README.md"}]},
            {"id": "arch", "title": "Archive|cold", "layer": "core",
             "home": "shop", "status": "planned", "summary": "Cold store.",
             "surfaces": [], "owns": [],
             "evidence": [],
             "expect": [{"repo": "shop", "path": "docs/plan.md"}]},
            {"id": "worker", "title": "Worker", "layer": "core",
             "home": "shop", "status": "live", "summary": "Does work.",
             "surfaces": [], "owns": [],
             "evidence": [{"repo": "shop", "path": "docs/w.md"}]},
        ],
        "contracts": [
            {"id": "k-one", "title": "Events|feed", "producer": "gw",
             "consumers": ["worker"], "format": "json", "status": "live",
             "summary": "Event feed.",
             "evidence": [{"repo": "shop", "path": "docs/a.md"}]},
            {"id": "k-two", "title": "Slots", "producer": "worker",
             "consumers": ["arch"], "format": "json", "status": "planned",
             "summary": "Slot export.",
             "evidence": []},
        ],
        "flows": [
            {"id": "live-flow", "from": "gw", "to": "worker", "contract": "k-one",
             "trigger": "push", "status": "live",
             "evidence": [{"repo": "shop", "path": "README.md"}]},
            {"id": "plan-flow", "from": "worker", "to": "arch", "contract": "k-two",
             "trigger": "manual", "status": "planned", "gap": "Not built.",
             "evidence": []},
        ],
    }


def parsed(atlas=None) -> dict:
    return docs.parse_atlas(copy.deepcopy(atlas or fixture_atlas()))


def flow_traffic(d7: int, d24: int = 1, declared: int | None = None, inferred: int = 0) -> dict:
    return {"status": "live", "rules": 1, "d24": d24, "d7": d7, "last_at": 1759190000.0,
            "heat": "hot" if d24 else ("warm" if d7 else "silent"),
            "by_basis": {"declared": d7 - inferred if declared is None else declared, "inferred": inferred},
            "by_source": {"journal": d7, "events": 0, "links": 0}}


def traffic_doc(flows: dict | None = None, silent: list | None = None) -> dict:
    """An estate-atlas-traffic@1 document for the tests."""
    return {
        "schema": "estate-atlas-traffic@1",
        "generated": 1759190400.0,  # 2025-09-30 00:00 UTC
        "source": {"atlas_sha": "abc", "ok": True, "error": None},
        "window": {"hours": 168, "bucket_s": 3600},
        "timing": {"refresh_ms": 3.0, "rebuild_ms": 900.0, "rebuilt_at": 1759190000.0},
        "sources": {k: {"ok": True, "records": 40, "error": None} for k in ("journal", "events", "links")},
        "coverage": {"records": 120, "declared": 70, "inferred": 20, "internal": 26, "unrouted": 4, "ambiguous": 0},
        "flows": {"live-flow": flow_traffic(50, 6, 40, 10)} if flows is None else flows,
        "crosschecks": {"silent": silent or [], "off_status": [], "rule_gaps": [], "unrouted": []},
    }


def write_doc(tmp: Path, atlas: dict, traffic=None) -> tuple[Path, Path, Path]:
    atlas_path = tmp / "atlas.json"
    doc_path = tmp / "ARCH.md"
    atlas_path.write_text(json.dumps(atlas), encoding="utf-8")
    doc_path.write_text(
        "# Doc\n\n<!-- atlas:begin glance -->\n<!-- atlas:end glance -->\n\n"
        "<!-- atlas:begin owners -->\n<!-- atlas:end owners -->\n\n"
        "<!-- atlas:begin layers -->\n<!-- atlas:end layers -->\n\n"
        "<!-- atlas:begin traffic -->\n<!-- atlas:end traffic -->\n",
        encoding="utf-8")
    traffic_path = None
    if traffic is not None:
        traffic_path = tmp / "traffic.json"
        traffic_path.write_text(json.dumps(traffic), encoding="utf-8")
    code, message = docs.write(atlas_path, doc_path, traffic_path)
    assert code == 0, message
    return atlas_path, doc_path, tmp / "atlas-glance.svg"


class ReplaceBlocksTests(unittest.TestCase):
    def test_replaces_only_inside_markers(self) -> None:
        text = ("before\n<!-- atlas:begin glance -->\nold\n<!-- atlas:end glance -->\nafter\n")
        out = docs.replace_blocks(text, {"glance": "new"})
        self.assertTrue(out.startswith("before\n"))
        self.assertTrue(out.endswith("after\n"))
        self.assertIn("new", out)
        self.assertNotIn("old", out)

    def test_missing_end_raises(self) -> None:
        with self.assertRaises(ValueError):
            docs.replace_blocks("<!-- atlas:begin glance -->\nold\n", {"glance": "new"})

    def test_duplicated_begin_raises(self) -> None:
        text = ("<!-- atlas:begin glance -->\na\n<!-- atlas:end glance -->\n"
                "<!-- atlas:begin glance -->\nb\n<!-- atlas:end glance -->\n")
        with self.assertRaises(ValueError):
            docs.replace_blocks(text, {"glance": "new"})

    def test_name_mismatch_raises(self) -> None:
        text = "<!-- atlas:begin glance -->\na\n<!-- atlas:end owners -->\n"
        with self.assertRaises(ValueError):
            docs.replace_blocks(text, {"glance": "new"})


class OwnersBlockTests(unittest.TestCase):
    def test_escapes_pipe_and_marks_non_live(self) -> None:
        block = docs.owners_block(parsed())
        self.assertIn("Events\\|feed", block)
        self.assertNotIn("**Events", block)
        self.assertIn("**Slots** (planned)", block)
        self.assertIn("Archive\\|cold", block)

    def test_generated_line(self) -> None:
        block = docs.owners_block(parsed())
        self.assertIn("Generated by estate-atlas", block)


class LayersBlockTests(unittest.TestCase):
    def test_counts_evidence_with_surfaces_and_dash_for_no_expect(self) -> None:
        block = docs.layers_block(parsed())
        self.assertIn("#### Edge", block)
        self.assertIn("#### Core", block)
        self.assertIn("| Gate<way> & co | live | shop | 3 | \u2013 |", block)
        self.assertIn("| Archive\\|cold | planned | shop | 0 | 1 |", block)


class GlanceSvgTests(unittest.TestCase):
    def test_deterministic(self) -> None:
        atlas = parsed()
        self.assertEqual(docs.glance_svg(atlas), docs.glance_svg(atlas))

    def test_escapes_and_dashed_planned_and_parses(self) -> None:
        svg = docs.glance_svg(parsed())
        self.assertIn("Gate&lt;way&gt; &amp; co", svg)
        self.assertIn("card-planned", svg)
        self.assertIn("stroke-dasharray", svg)
        self.assertIn('role="img"', svg)
        ET.fromstring(svg)


class TrafficBlockTests(unittest.TestCase):
    def test_wording(self) -> None:
        doc = traffic_doc(flows={"live-flow": flow_traffic(50, 6, 40, 10), "plan-flow": flow_traffic(4, 0, 1, 3)})
        block = docs.traffic_block(parsed(), doc)
        self.assertIn("As of 2025-09-30 00:00 UTC: 120 records this week, 58% declared, 17% inferred, "
                      "22% internal, 3% unrouted.", block)
        self.assertIn("| live-flow |", block)
        self.assertIn("| 50 | 6 | 40 / 10 |", block)
        self.assertLess(block.index("| live-flow |"), block.index("| plan-flow |"))
        self.assertIn("Silent live flows: none", block)
        self.assertIn("Generated by estate-atlas", block)
        self.assertIn("rewrite the doc blocks", block)

    def test_silent_comes_from_the_crosschecks(self) -> None:
        block = docs.traffic_block(parsed(), traffic_doc(flows={}, silent=["live-flow"]))
        self.assertIn("Silent live flows: live-flow", block)
        self.assertIn("(no flow carried a record)", block)

    def test_top_15_cap_skips_zero_weeks(self) -> None:
        flows = {"f-%02d" % i: flow_traffic(100 - i) for i in range(16)}
        flows["quiet"] = flow_traffic(0, 0)
        block = docs.traffic_block(parsed(), traffic_doc(flows=flows))
        self.assertEqual(block.count("| f-"), 15)
        self.assertNotIn("| f-15 |", block)
        self.assertNotIn("| quiet |", block)

    def test_pulse_in_the_as_of_line_only_when_positive(self) -> None:
        doc = traffic_doc()
        self.assertNotIn("pulse", docs.traffic_block(parsed(), doc).splitlines()[2])
        zero = traffic_doc()
        zero["coverage"] = dict(zero["coverage"], pulse=0)
        self.assertNotIn("pulse", docs.traffic_block(parsed(), zero).splitlines()[2])
        some = traffic_doc()
        some["coverage"] = dict(some["coverage"], pulse=12)
        line = docs.traffic_block(parsed(), some).splitlines()[2]
        self.assertIn("10% pulse", line)

    def test_pulse_column_and_pulse_only_flow_last(self) -> None:
        busy = flow_traffic(50, 6, 40, 10)
        pulse_only = dict(flow_traffic(0, 0, 0, 0), pulse_d7=9, heat="pulse")
        doc = traffic_doc(flows={"live-flow": busy, "plan-flow": pulse_only})
        block = docs.traffic_block(parsed(), doc)
        self.assertIn("| Pulse |", block)
        self.assertIn("| 50 | 6 | 40 / 10 | 0 |", block)
        self.assertIn("| plan-flow |", block)
        self.assertLess(block.index("| live-flow |"), block.index("| plan-flow |"))

    def test_rejects_another_document(self) -> None:
        with self.assertRaises(ValueError):
            docs.traffic_block(parsed(), {"schema": "estate-atlas@1"})

    def test_placeholder(self) -> None:
        block = docs.traffic_block(parsed(), None)
        self.assertIn("No traffic snapshot yet.", block)
        self.assertIn("rewrite the doc blocks", block)


class WriteTests(unittest.TestCase):
    def test_write_is_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            atlas_path, doc_path, svg_path = write_doc(Path(tmp), fixture_atlas(),
                                                       traffic_doc())
            first_doc = doc_path.read_bytes()
            first_svg = svg_path.read_bytes()
            code, message = docs.write(atlas_path, doc_path, None)
            self.assertEqual(code, 0, message)
            self.assertEqual(doc_path.read_bytes(), first_doc)
            self.assertEqual(svg_path.read_bytes(), first_svg)

    def test_write_rejects_a_bad_atlas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            atlas_path, doc_path, _ = write_doc(Path(tmp), fixture_atlas())
            atlas_path.write_text('{"schema": "estate-atlas@2", "schema": "x"}',
                                  encoding="utf-8")
            code, _message = docs.write(atlas_path, doc_path, None)
            self.assertEqual(code, 2)

    def test_write_rejects_a_bad_traffic_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            atlas_path = Path(tmp) / "atlas.json"
            doc_path = Path(tmp) / "ARCH.md"
            atlas_path.write_text(json.dumps(fixture_atlas()), encoding="utf-8")
            doc_path.write_text(
                "# Doc\n\n<!-- atlas:begin glance -->\n<!-- atlas:end glance -->\n\n"
                "<!-- atlas:begin owners -->\n<!-- atlas:end owners -->\n\n"
                "<!-- atlas:begin layers -->\n<!-- atlas:end layers -->\n\n"
                "<!-- atlas:begin traffic -->\n<!-- atlas:end traffic -->\n",
                encoding="utf-8")
            traffic_path = Path(tmp) / "traffic.json"
            traffic_path.write_text(json.dumps({"schema": "estate-atlas@1"}),
                                    encoding="utf-8")
            code, _message = docs.write(atlas_path, doc_path, traffic_path)
            self.assertEqual(code, 2)


class CheckTests(unittest.TestCase):
    def test_clean_after_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            atlas_path, doc_path, _ = write_doc(Path(tmp), fixture_atlas(),
                                                traffic_doc())
            code, message = docs.check_docs(atlas_path, doc_path)
            self.assertEqual(code, 0, message)

    def test_hand_edit_in_owners_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            atlas_path, doc_path, _ = write_doc(Path(tmp), fixture_atlas(),
                                                traffic_doc())
            text = doc_path.read_text(encoding="utf-8")
            doc_path.write_text(text.replace("Events\\|feed", "EventsX"),
                                encoding="utf-8")
            code, _message = docs.check_docs(atlas_path, doc_path)
            self.assertEqual(code, 1)

    def test_stale_svg_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            atlas_path, doc_path, svg_path = write_doc(Path(tmp), fixture_atlas(),
                                                       traffic_doc())
            svg_path.write_bytes(svg_path.read_bytes() + b"\n")
            code, _message = docs.check_docs(atlas_path, doc_path)
            self.assertEqual(code, 1)

    def test_ignores_different_traffic_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            atlas = fixture_atlas()
            atlas_path, doc_path, _ = write_doc(Path(tmp), atlas, traffic_doc())
            text = doc_path.read_text(encoding="utf-8")
            doc_path.write_text(text.replace("50 | 6 |", "51 | 7 |"),
                                encoding="utf-8")
            code, message = docs.check_docs(atlas_path, doc_path)
            self.assertEqual(code, 0, message)


class LineEndingTests(unittest.TestCase):
    def test_check_accepts_a_crlf_checkout(self) -> None:
        # a checkout that stores LF may hold the committed files as CRLF
        with tempfile.TemporaryDirectory() as tmp:
            atlas_path, doc_path, svg_path = write_doc(Path(tmp), fixture_atlas())
            for path in (doc_path, svg_path):
                path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
            code, message = docs.check_docs(atlas_path, doc_path)
            self.assertEqual(code, 0, message)


CUSTOM = docs.Style(
    generated_line="<!-- Generated by the shop build; edit the atlas -->",
    refresh_line="Refresh: run make atlas.",
    write_hint="run make atlas",
    page_href="site/map.html",
)


EMPTY_DOC = """# Doc

<!-- atlas:begin glance -->
<!-- atlas:end glance -->

<!-- atlas:begin owners -->
<!-- atlas:end owners -->

<!-- atlas:begin layers -->
<!-- atlas:end layers -->

<!-- atlas:begin traffic -->
<!-- atlas:end traffic -->
"""


def empty_doc(tmp: Path, atlas: dict) -> tuple[Path, Path]:
    """A doc whose blocks are all empty, so the first write fills every block in the given style."""
    atlas_path = tmp / "atlas.json"
    doc_path = tmp / "ARCH.md"
    atlas_path.write_text(json.dumps(atlas), encoding="utf-8")
    doc_path.write_text(EMPTY_DOC, encoding="utf-8")
    return atlas_path, doc_path


class StyleTests(unittest.TestCase):
    def test_default_style_reproduces_the_blocks(self) -> None:
        atlas = parsed()
        default = docs.DEFAULT_STYLE
        self.assertEqual(docs.owners_block(atlas), docs.owners_block(atlas, style=default))
        self.assertEqual(docs.glance_block(atlas), docs.glance_block(atlas, style=default))
        self.assertEqual(docs.layers_block(atlas), docs.layers_block(atlas, style=default))
        self.assertEqual(docs.traffic_block(atlas, None), docs.traffic_block(atlas, None, style=default))
        self.assertEqual(docs.traffic_block(atlas, traffic_doc()),
                         docs.traffic_block(atlas, traffic_doc(), style=default))
        self.assertIn(docs.GLANCE_IMAGE, docs.glance_block(atlas))

    def test_custom_lines_appear_in_every_block(self) -> None:
        atlas = parsed()
        for block in (docs.owners_block(atlas, style=CUSTOM),
                      docs.layers_block(atlas, style=CUSTOM),
                      docs.glance_block(atlas, style=CUSTOM),
                      docs.traffic_block(atlas, None, style=CUSTOM),
                      docs.traffic_block(atlas, traffic_doc(), style=CUSTOM)):
            self.assertEqual(block.splitlines()[0], CUSTOM.generated_line)
            self.assertNotIn(docs.GENERATED_LINE, block)
        self.assertIn("(site/map.html)", docs.glance_block(atlas, style=CUSTOM))
        self.assertNotIn("(atlas.html)", docs.glance_block(atlas, style=CUSTOM))
        for traffic in (None, traffic_doc()):
            block = docs.traffic_block(atlas, traffic, style=CUSTOM)
            self.assertEqual(block.splitlines()[-1], CUSTOM.refresh_line)
            self.assertNotIn(docs.REFRESH_LINE, block)

    def test_write_then_check_with_the_same_style(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            atlas_path, doc_path = empty_doc(Path(tmp), fixture_atlas())
            code, message = docs.write(atlas_path, doc_path, style=CUSTOM)
            self.assertEqual(code, 0, message)
            code, message = docs.check_docs(atlas_path, doc_path, style=CUSTOM)
            self.assertEqual(code, 0, message)

    def test_check_with_the_default_style_is_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            atlas_path, doc_path = empty_doc(Path(tmp), fixture_atlas())
            code, message = docs.write(atlas_path, doc_path, style=CUSTOM)
            self.assertEqual(code, 0, message)
            code, message = docs.check_docs(atlas_path, doc_path)
            self.assertNotEqual(code, 0)
            self.assertIn("stale", message)

    def test_check_names_the_styles_write_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            atlas_path, doc_path, _svg = write_doc(tmp_path, fixture_atlas())
            code, message = docs.check_docs(atlas_path, doc_path, style=CUSTOM)
            self.assertEqual(code, 1)
            self.assertIn("run make atlas", message)
            self.assertNotIn(docs.WRITE_HINT, message)

    def test_a_custom_empty_traffic_block_gets_the_styled_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            atlas_path, doc_path = empty_doc(Path(tmp), fixture_atlas())
            docs.write(atlas_path, doc_path, style=CUSTOM)
            text = doc_path.read_text(encoding="utf-8")
            self.assertIn(docs.PLACEHOLDER_LINE, text)
            self.assertIn(CUSTOM.refresh_line, text)
            code, message = docs.write(atlas_path, doc_path, style=CUSTOM)
            self.assertEqual(code, 0, message)
            self.assertEqual(text, doc_path.read_text(encoding="utf-8"))

    def test_invalid_styles_raise(self) -> None:
        with self.assertRaises(ValueError):
            docs.Style(generated_line="not a comment")
        with self.assertRaises(ValueError):
            docs.Style(generated_line="<!-- a --> <!-- b -->")
        with self.assertRaises(ValueError):
            docs.Style(refresh_line="a\nb")
        with self.assertRaises(ValueError):
            docs.Style(write_hint="  ")
        with self.assertRaises(ValueError):
            docs.Style(page_href="a b")
        with self.assertRaises(ValueError):
            docs.Style(page_href="a(b)")

    def test_a_style_line_cannot_carry_an_atlas_marker(self) -> None:
        marker = "<!-- atlas:end traffic -->"
        for kwargs in ({"generated_line": "<!-- x %s -->" % marker},
                       {"generated_line": "<!-- atlas:begin glance -->"},
                       {"refresh_line": "Refresh %s" % marker},
                       {"write_hint": "run %s" % marker}):
            with self.assertRaises(ValueError, msg=str(kwargs)):
                docs.Style(**kwargs)

    def test_generated_line_must_be_a_whole_comment(self) -> None:
        with self.assertRaises(ValueError):
            docs.Style(generated_line="<!-->")
        with self.assertRaises(ValueError):
            docs.Style(generated_line="<!--->")
        docs.Style(generated_line="<!---->")

    def test_page_href_rejects_whitespace_and_link_breaking_characters(self) -> None:
        for bad in ("a\tb", "a b", "a\nb", "a[b", "a]b", "a<b", "a>b", 'a"b', "a'b", "a)b"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                docs.Style(page_href=bad)
        docs.Style(page_href="site/map.html#top")

    def test_page_href_rejects_every_control_character(self) -> None:
        for code in list(range(32)) + [127]:
            bad = "a%sb" % chr(code)
            with self.assertRaises(ValueError, msg=repr(bad)):
                docs.Style(page_href=bad)
        docs.Style(page_href="site/map.html#top")

    def test_write_returns_2_when_the_traffic_snapshot_time_overflows(self) -> None:
        for generated in (1e300, -1e300):
            with self.subTest(generated=generated), tempfile.TemporaryDirectory() as tmp:
                atlas_path, doc_path = empty_doc(Path(tmp), fixture_atlas())
                bad = traffic_doc()
                bad["generated"] = generated
                traffic_path = Path(tmp) / "traffic.json"
                traffic_path.write_text(json.dumps(bad), encoding="utf-8")
                before = doc_path.read_text(encoding="utf-8")
                code, message = docs.write(atlas_path, doc_path, traffic_path)
                self.assertEqual(code, 2, message)
                self.assertTrue(message)
                self.assertEqual(doc_path.read_text(encoding="utf-8"), before)

    def test_switching_style_on_a_default_written_doc_refills_the_traffic_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            atlas_path, doc_path = empty_doc(Path(tmp), fixture_atlas())
            self.assertEqual(docs.write(atlas_path, doc_path)[0], 0)
            code, message = docs.write(atlas_path, doc_path, style=CUSTOM)
            self.assertEqual(code, 0, message)
            code, message = docs.check_docs(atlas_path, doc_path, style=CUSTOM)
            self.assertEqual(code, 0, message)
            self.assertNotIn(docs.REFRESH_LINE, doc_path.read_text(encoding="utf-8"))

    def test_a_real_snapshot_is_not_replaced_when_the_style_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            atlas_path, doc_path = empty_doc(Path(tmp), fixture_atlas())
            traffic_path = Path(tmp) / "traffic.json"
            traffic_path.write_text(json.dumps(traffic_doc()), encoding="utf-8")
            self.assertEqual(docs.write(atlas_path, doc_path, traffic_path)[0], 0)
            before = docs.parse_blocks(doc_path.read_text(encoding="utf-8"))["traffic"]
            self.assertEqual(docs.write(atlas_path, doc_path, style=CUSTOM)[0], 0)
            after = docs.parse_blocks(doc_path.read_text(encoding="utf-8"))["traffic"]
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
