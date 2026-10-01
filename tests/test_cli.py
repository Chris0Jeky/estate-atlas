"""The command line, end to end on examples/shop, through cli.main([...])."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from estate_atlas import cli

ROOT = Path(__file__).resolve().parent.parent
SHOP = ROOT / "examples" / "shop"
ATLAS = str(SHOP / "atlas.json")
JOURNAL = str(SHOP / "journal.jsonl")
EVENTS = str(SHOP / "events.jsonl")


def need(*names: str):
    """Skip a test when a module the verb needs cannot be imported."""
    missing = [n for n in names if importlib.util.find_spec("estate_atlas." + n) is None]
    return unittest.skipUnless(not missing, "not importable yet: %s" % ", ".join(missing))


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cli-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))


@need("model")
class ValidateTests(TempCase):
    def test_the_example_is_valid(self):
        code, out, _ = run("validate", ATLAS)
        self.assertEqual(code, 0)
        self.assertIn("6 components", out)

    def test_a_broken_atlas_is_exit_2(self):
        bad = self.tmp / "bad.json"
        bad.write_text('{"schema": "estate-atlas@2"}', encoding="utf-8")
        code, _, err = run("validate", str(bad))
        self.assertEqual(code, 2)
        self.assertIn("estate-atlas:", err)

    def test_a_missing_file_is_exit_2(self):
        self.assertEqual(run("validate", str(self.tmp / "nope.json"))[0], 2)

    def test_usage_errors_are_exit_2(self):
        self.assertEqual(run("no-such-verb")[0], 2)
        self.assertEqual(run()[0], 2)


@need("model", "check")
class CheckTests(TempCase):
    def test_worktree_reads_prove_every_reference(self):
        code, out, _ = run("check", ATLAS, "--repo", "shop=" + str(SHOP), "--worktree")
        self.assertEqual(code, 0)
        self.assertIn("(ok)", out)
        self.assertIn("0 missing", out)

    def test_worktree_json_report(self):
        code, out, _ = run("check", ATLAS, "--repo", "shop=" + str(SHOP), "--worktree", "--json")
        report = json.loads(out)
        self.assertEqual((code, report["status"], report["missing"]), (0, "ok", []))
        self.assertEqual(report["checked"], report["ok"])
        self.assertGreater(report["checked"], 10)

    def test_the_example_command_exits_0_even_when_the_checkout_is_not_the_remote(self):
        # without --worktree the evidence is read at origin/<default branch>; a checkout whose origin is some
        # other repository leaves the repo unresolved (partial), which is not drift
        code, out, _ = run("check", ATLAS, "--repo", "shop=" + str(SHOP))
        self.assertEqual(code, 0)
        self.assertIn("unresolved", out)

    def test_drift_is_exit_1(self):
        copy = self.tmp / "shop"
        shutil.copytree(SHOP, copy)
        (copy / "src" / "db.py").unlink()
        code, out, _ = run("check", str(copy / "atlas.json"), "--repo", "shop=" + str(copy), "--worktree")
        self.assertEqual(code, 1)
        self.assertIn("missing", out)
        self.assertIn("src/db.py", out)

    def test_a_moved_anchor_is_drift(self):
        copy = self.tmp / "shop"
        shutil.copytree(SHOP, copy)
        path = copy / "src" / "jobs.py"
        path.write_text(path.read_text(encoding="utf-8").replace("def enqueue", "def put"), encoding="utf-8")
        code, out, _ = run("check", str(copy / "atlas.json"), "--repo", "shop=" + str(copy), "--worktree")
        self.assertEqual(code, 1)
        self.assertIn("def enqueue", out)

    def test_worktree_mode_is_restored_afterwards(self):
        from estate_atlas import check
        before = (check.checkout_identity, check.remote_head, check._blob)
        run("check", ATLAS, "--repo", "shop=" + str(SHOP), "--worktree")
        self.assertEqual((check.checkout_identity, check.remote_head, check._blob), before)

    def test_a_bad_repo_option_is_exit_2(self):
        code, _, err = run("check", ATLAS, "--repo", "shop")
        self.assertEqual(code, 2)
        self.assertIn("name=path", err)

    def test_export_adds_hosted_links(self):
        code, out, _ = run("export", ATLAS)
        doc = json.loads(out)
        self.assertEqual(code, 0)
        self.assertTrue(doc["flows"][0]["evidence"][0]["url"].startswith("https://github.com/example/shop/blob/main/"))

    def test_export_check_writes_a_file(self):
        target = self.tmp / "export.json"
        code, out, _ = run("export", ATLAS, "--repo", "shop=" + str(SHOP), "--check", "--out", str(target))
        self.assertEqual((code, out), (0, ""))
        self.assertIn("check", json.loads(target.read_text(encoding="utf-8")))


@need("model", "traffic")
class RouteTests(TempCase):
    def route(self, *extra: str) -> dict:
        code, out, err = run("route", ATLAS, "--journal", JOURNAL, "--events", EVENTS, *extra)
        self.assertEqual((code, err), (0, ""))
        return json.loads(out)

    def test_the_example_lights_the_flows(self):
        doc = self.route()
        self.assertEqual(doc["schema"], "estate-atlas-traffic@1")
        heats = {fid: flow["heat"] for fid, flow in doc["flows"].items()}
        self.assertEqual(heats["api-to-queue"], "hot")
        self.assertEqual(heats["queue-to-worker"], "hot")
        self.assertEqual(heats["worker-to-db"], "warm")
        self.assertEqual(heats["worker-heartbeat"], "pulse")
        self.assertEqual(heats["api-to-payments"], "silent")
        self.assertEqual(doc["crosschecks"]["silent"], ["api-to-payments"])
        self.assertEqual(doc["flows"]["web-to-api"]["by_basis"]["inferred"], 14)  # no rule: inferred
        self.assertGreater(doc["coverage"]["pulse"], 0)
        self.assertGreater(doc["coverage"]["unrouted"], 0)

    def test_the_output_is_stable(self):
        a, b = self.route(), self.route()
        self.assertNotIn("timing", a)
        self.assertEqual(a, b)

    def test_the_bytes_are_stable_for_the_same_now(self):
        now = str(self.route()["generated"])
        outs = [run("route", ATLAS, "--journal", JOURNAL, "--events", EVENTS, "--now", now)[1] for _ in range(2)]
        self.assertTrue(outs[0])
        self.assertEqual(outs[0], outs[1])
        self.assertNotIn('"timing"', outs[0])

    def test_a_non_finite_now_is_exit_2_without_a_traceback(self):
        for value in ("inf", "nan"):
            code, out, err = run("route", ATLAS, "--journal", JOURNAL, "--now", value)
            self.assertEqual((code, out), (2, ""), value)
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertNotIn("Traceback", err)

    def test_an_explicit_now_moves_the_window(self):
        later = self.route("--now", str(self.route()["generated"] + 40 * 86400))
        self.assertEqual(later["coverage"]["records"], 0)

    def test_the_traffic_feeds_explain_and_tour(self):
        target = self.tmp / "traffic.json"
        self.assertEqual(run("route", ATLAS, "--journal", JOURNAL, "--events", EVENTS, "--out", str(target))[0], 0)
        if importlib.util.find_spec("estate_atlas.explain") is None:
            self.skipTest("explain is not importable yet")
        code, out, _ = run("explain", ATLAS, "api", "--traffic", str(target))
        self.assertEqual(code, 0)
        self.assertIn("Shop API", out)
        self.assertEqual(run("tour", ATLAS, "--md", "--traffic", str(target))[0], 0)

    def test_no_input_files_is_exit_2(self):
        self.assertEqual(run("route", ATLAS)[0], 2)

    def test_bad_input_is_exit_2(self):
        bad = self.tmp / "bad.jsonl"
        bad.write_text("{oops\n", encoding="utf-8")
        code, _, err = run("route", ATLAS, "--journal", str(bad))
        self.assertEqual(code, 2)
        self.assertIn("bad.jsonl:1", err)
        self.assertEqual(run("route", ATLAS, "--journal", JOURNAL, "--now", "later")[0], 2)

    def test_links_file(self):
        links = self.tmp / "links.json"
        links.write_text(json.dumps([{"source": "docs", "from": "service:web", "rel": "calls",
                                      "to": "service:api", "at": 1_790_000_000}]), encoding="utf-8")
        code, out, _ = run("route", ATLAS, "--links", str(links))
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["flows"]["web-to-api"]["d24"], 1)


@need("model", "render", "explain")
class ExplainTests(unittest.TestCase):
    def test_plain_text_has_the_what_line(self):
        code, out, _ = run("explain", ATLAS, "worker")
        self.assertEqual(code, 0)
        self.assertIn("Fulfilment worker (shop) is live.", out)
        self.assertIn("Fed by Job queue", out)

    def test_json(self):
        code, out, _ = run("explain", ATLAS, "worker", "--json")
        doc = json.loads(out)
        self.assertEqual((code, doc["schema"], doc["lines"][0]["kind"]), (0, "estate-atlas-explain@1", "what"))

    def test_an_unknown_component_is_exit_2(self):
        code, _, err = run("explain", ATLAS, "nope")
        self.assertEqual(code, 2)
        self.assertIn("unknown component", err)

    def test_a_traffic_file_of_the_wrong_schema_is_exit_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            wrong = Path(tmp) / "t.json"
            wrong.write_text('{"schema": "something-else"}', encoding="utf-8")
            self.assertEqual(run("explain", ATLAS, "worker", "--traffic", str(wrong))[0], 2)


@need("model", "render", "explain")
class TourTests(TempCase):
    def test_json_and_markdown(self):
        code, out, _ = run("tour", ATLAS)
        self.assertEqual((code, json.loads(out)["schema"]), (0, "estate-atlas-tour@1"))
        code, out, _ = run("tour", ATLAS, "--md")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("# A tour"))

    def test_check_passes_then_goes_stale(self):
        doc = self.tmp / "TOUR.md"
        self.assertEqual(run("tour", ATLAS, "--md", "--out", str(doc))[0], 0)
        self.assertEqual(run("tour", ATLAS, "--check", str(doc))[0], 0)
        doc.write_text(doc.read_text(encoding="utf-8") + "\nextra\n", encoding="utf-8")
        code, _, err = run("tour", ATLAS, "--check", str(doc))
        self.assertEqual(code, 1)
        self.assertIn("stale", err)


@need("model", "render")
class RenderTests(TempCase):
    def test_html_and_mermaid(self):
        code, out, _ = run("render", "html", ATLAS)
        self.assertEqual(code, 0)
        self.assertIn("<html", out)
        self.assertIn("Fulfilment worker", out)
        code, out, _ = run("render", "mermaid", ATLAS)
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("flowchart TB"))
        self.assertIn("-->", out)
        self.assertEqual(run("render", "mermaid", ATLAS, "--view", "components")[0], 0)

    def test_out_writes_a_file_deterministically(self):
        a, b = self.tmp / "a.html", self.tmp / "b.html"
        run("render", "html", ATLAS, "--out", str(a))
        run("render", "html", ATLAS, "--out", str(b))
        self.assertEqual(a.read_bytes(), b.read_bytes())

    def test_a_bad_kind_is_exit_2(self):
        self.assertEqual(run("render", "pdf", ATLAS)[0], 2)


@need("model", "render", "docs")
class DocsTests(TempCase):
    DOC = ("# Shop\n\n"
           "<!-- atlas:begin glance -->\n<!-- atlas:end glance -->\n\n"
           "<!-- atlas:begin owners -->\n<!-- atlas:end owners -->\n\n"
           "<!-- atlas:begin layers -->\n<!-- atlas:end layers -->\n\n"
           "<!-- atlas:begin traffic -->\n<!-- atlas:end traffic -->\n")

    def test_write_then_check_then_stale(self):
        doc = self.tmp / "ARCH.md"
        doc.write_text(self.DOC, encoding="utf-8")
        self.assertEqual(run("docs", "check", ATLAS, "--doc", str(doc))[0], 1)  # blocks are empty: stale
        code, out, _ = run("docs", "write", ATLAS, "--doc", str(doc))
        self.assertEqual((code, out.strip()), (0, "ok"))
        self.assertTrue((self.tmp / "atlas-glance.svg").is_file())
        self.assertEqual(run("docs", "check", ATLAS, "--doc", str(doc))[0], 0)
        (self.tmp / "atlas-glance.svg").write_text("<svg/>", encoding="utf-8")
        code, _, err = run("docs", "check", ATLAS, "--doc", str(doc))
        self.assertEqual(code, 1)
        self.assertIn("stale", err)

    def test_a_traffic_snapshot_fills_the_traffic_block(self):
        traffic = self.tmp / "traffic.json"
        run("route", ATLAS, "--journal", JOURNAL, "--events", EVENTS, "--out", str(traffic))
        doc = self.tmp / "ARCH.md"
        doc.write_text(self.DOC, encoding="utf-8")
        self.assertEqual(run("docs", "write", ATLAS, "--doc", str(doc), "--traffic", str(traffic))[0], 0)
        self.assertIn("As of ", doc.read_text(encoding="utf-8"))
        self.assertEqual(run("docs", "check", ATLAS, "--doc", str(doc))[0], 0)

    def test_an_absurd_generated_time_is_exit_2_without_a_traceback(self):
        traffic = self.tmp / "traffic.json"
        run("route", ATLAS, "--journal", JOURNAL, "--events", EVENTS, "--out", str(traffic))
        data = json.loads(traffic.read_text(encoding="utf-8"))
        data["generated"] = 1e300
        traffic.write_text(json.dumps(data), encoding="utf-8")
        doc = self.tmp / "ARCH.md"
        doc.write_text(self.DOC, encoding="utf-8")
        code, _, err = run("docs", "write", ATLAS, "--doc", str(doc), "--traffic", str(traffic))
        self.assertEqual(code, 2)
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertNotIn("Traceback", err)

    def test_a_doc_without_blocks_is_exit_2(self):
        doc = self.tmp / "EMPTY.md"
        doc.write_text("# nothing here\n", encoding="utf-8")
        self.assertEqual(run("docs", "check", ATLAS, "--doc", str(doc))[0], 2)


class MainModuleTests(unittest.TestCase):
    def test_python_dash_m_runs(self):
        import subprocess
        import sys
        done = subprocess.run([sys.executable, "-m", "estate_atlas", "validate", ATLAS], cwd=str(ROOT),
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)


if __name__ == "__main__":
    unittest.main()
