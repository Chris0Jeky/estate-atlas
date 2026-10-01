"""The public overlay: total coverage, the rewrite, the traffic rename and the leak check, on examples/shop."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from estate_atlas import cli, explain, model, overlay

ROOT = Path(__file__).resolve().parent.parent
ATLAS = str(ROOT / "examples" / "shop" / "atlas.json")
FIXTURE = ROOT / "tests" / "fixtures" / "shop-overlay.json"
JOURNAL = ROOT / "examples" / "shop" / "journal.jsonl"
EVENTS = ROOT / "examples" / "shop" / "events.jsonl"


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def shop() -> dict:
    return model.load(ATLAS)


def fixture() -> dict:
    return overlay.load_overlay(FIXTURE)


class TempCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="overlay-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def write(self, name: str, obj) -> str:
        path = self.tmp / name
        path.write_text(obj if isinstance(obj, str) else json.dumps(obj), encoding="utf-8")
        return str(path)


class ValidateTests(unittest.TestCase):
    def test_the_fixture_covers_the_shop(self):
        overlay.validate_overlay(fixture(), shop())

    def rejects(self, spec: dict, *needles: str, doc: dict | None = None) -> str:
        with self.assertRaises(model.AtlasError) as ctx:
            overlay.validate_overlay(spec, doc or shop())
        for needle in needles:
            self.assertIn(needle, str(ctx.exception))
        return str(ctx.exception)

    def test_every_missing_id_is_listed(self):
        spec = fixture()
        del spec["components"]["web"], spec["components"]["db"], spec["flows"]["api-to-db"], spec["layers"]["edge"]
        message = self.rejects(spec, "'web'", "'db'", "'api-to-db'", "'edge'")
        self.assertIn("no entry for", message)

    def test_every_unknown_id_is_listed(self):
        spec = fixture()
        spec["components"]["ghost"] = dict(spec["components"]["web"], id="ghost-public")
        spec["contracts"]["phantom"] = dict(spec["contracts"]["charge-api"], id="phantom-public")
        self.rejects(spec, "unknown id(s) ['ghost']", "unknown id(s) ['phantom']")

    def test_missing_and_unknown_are_reported_together(self):
        spec = fixture()
        del spec["layers"]["data"]
        spec["flows"]["nope"] = dict(spec["flows"]["web-to-api"], id="nope-public")
        self.rejects(spec, "'data'", "['nope']")

    def test_a_wrong_schema_or_shape(self):
        spec = fixture()
        spec["schema"] = "estate-atlas-overlay@2"
        self.rejects(spec, "overlay.schema")
        spec = fixture()
        spec["extra"] = 1
        self.rejects(spec, "unknown key")
        spec = fixture()
        del spec["flows"]
        self.rejects(spec, "missing key")
        spec = fixture()
        spec["layers"] = []
        self.rejects(spec, "must be an object")

    def test_entries_must_be_complete_and_known(self):
        spec = fixture()
        del spec["components"]["api"]["home"]
        spec["layers"]["edge"]["colour"] = "red"
        self.rejects(spec, "components.api: missing key(s) ['home']", "layers.edge: unknown key(s) ['colour']")

    def test_text_limits_follow_the_model(self):
        spec = fixture()
        spec["layers"]["edge"]["summary"] = "x" * 301
        spec["flows"]["web-to-api"]["trigger"] = "x" * 201
        spec["components"]["web"]["title"] = "two\nlines"
        spec["contracts"]["shop-http"]["summary"] = "   "
        self.rejects(spec, "layers.edge.summary", "flows.web-to-api.trigger",
                     "components.web.title", "contracts.shop-http.summary")

    def test_public_ids_follow_the_id_patterns_and_are_unique_per_kind(self):
        spec = fixture()
        spec["components"]["web"]["id"] = "Bad Id"
        spec["contracts"]["shop-http"]["id"] = "has space"
        spec["flows"]["api-to-db"]["id"] = spec["flows"]["web-to-api"]["id"]
        self.rejects(spec, "components.web.id: invalid public id", "contracts.shop-http.id: invalid public id",
                     "already the public id")
        spec = fixture()
        spec["contracts"]["shop-http"]["id"] = "orders.v1/site@2"  # contracts may use . / @
        overlay.validate_overlay(spec, shop())
        spec = fixture()
        spec["layers"]["edge"]["id"] = "orders.v1"  # layers may not
        self.rejects(spec, "layers.edge.id")

    def test_the_same_public_id_may_serve_two_kinds(self):
        spec = fixture()
        spec["layers"]["edge"]["id"] = "customer-site"  # also a component's public id
        overlay.validate_overlay(spec, shop())

    def test_a_home_label_and_the_denylist(self):
        spec = fixture()
        spec["components"]["web"]["home"] = "Not A Label"
        spec["denylist"] = ["", "two\nlines"]
        spec["title"] = ""
        self.rejects(spec, "components.web.home", "denylist[0]", "denylist[1]", "overlay.title")

    def test_a_gap_belongs_to_a_flow_that_has_one(self):
        spec = fixture()
        spec["flows"]["web-to-api"]["gap"] = "not wired"
        self.rejects(spec, "unknown key(s) ['gap']", "the flow has no gap")
        doc = shop()
        flow = next(f for f in doc["flows"] if f["id"] == "web-to-api")
        flow["status"], flow["gap"] = "partial", "Only reads are wired."
        self.rejects(fixture(), "flows.web-to-api: missing key(s) ['gap']", doc=doc)
        spec = fixture()
        spec["flows"]["web-to-api"]["gap"] = "Only reads are wired."
        overlay.validate_overlay(spec, doc)
        spec["flows"]["web-to-api"]["gap"] = "x" * 301
        self.rejects(spec, "flows.web-to-api.gap", doc=doc)


class LoadTests(TempCase):
    def test_it_loads_the_fixture(self):
        self.assertEqual(fixture()["schema"], overlay.OVERLAY_SCHEMA)

    def test_loading_is_strict(self):
        with self.assertRaises(model.AtlasError) as ctx:
            overlay.load_overlay(self.write("dup.json", '{"schema": "a", "schema": "b"}'))
        self.assertIn("duplicate JSON key", str(ctx.exception))
        for name, body in (("nan.json", '{"schema": NaN}'), ("list.json", "[]"), ("bad.json", "{")):
            with self.assertRaises(model.AtlasError, msg=name):
                overlay.load_overlay(self.write(name, body))
        with self.assertRaises(model.AtlasError):
            overlay.load_overlay(self.tmp / "missing.json")


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.doc = shop()
        self.spec = fixture()
        self.out = overlay.apply_overlay(self.doc, self.spec)

    def test_text_and_ids_are_public(self):
        by_id = {c["id"]: c for c in self.out["components"]}
        self.assertEqual(set(by_id), {"customer-site", "orders-service", "task-buffer", "fulfiller", "order-store",
                                      "card-processor"})
        self.assertEqual(by_id["customer-site"]["title"], "Customer site")
        self.assertEqual(by_id["customer-site"]["home"], "main-repo")
        self.assertEqual(by_id["card-processor"]["home"], "vendor-service")
        self.assertEqual([layer["id"] for layer in self.out["layers"]], ["front", "core", "state", "partners"])
        self.assertEqual(self.out["layers"][0]["summary"], "What customers touch: the public site.")
        self.assertEqual(self.out["contracts"][0]["title"], "Site calls")

    def test_references_are_renamed_everywhere(self):
        layers = {layer["id"] for layer in self.out["layers"]}
        comps = {c["id"] for c in self.out["components"]}
        contracts = {c["id"] for c in self.out["contracts"]}
        for comp in self.out["components"]:
            self.assertIn(comp["layer"], layers)
        for contract in self.out["contracts"]:
            self.assertIn(contract["producer"], comps)
            self.assertTrue(set(contract["consumers"]) <= comps)
        for flow in self.out["flows"]:
            self.assertIn(flow["from"], comps)
            self.assertIn(flow["to"], comps)
            self.assertTrue(flow["contract"] is None or flow["contract"] in contracts)
        flow = next(f for f in self.out["flows"] if f["id"] == "site-to-orders")
        self.assertEqual((flow["from"], flow["to"], flow["contract"]), ("customer-site", "orders-service", "site-calls"))

    def test_a_flow_without_a_contract_keeps_none(self):
        doc = shop()
        doc["flows"][0]["contract"] = None
        self.assertIsNone(overlay.apply_overlay(doc, self.spec)["flows"][0]["contract"])

    def test_what_a_public_tour_must_not_carry_is_stripped(self):
        self.assertEqual(self.out["repos"], {})
        for comp in self.out["components"]:
            for key in ("evidence", "expect", "surfaces", "owns", "instances"):
                self.assertNotIn(key, comp)
        for contract in self.out["contracts"]:
            self.assertNotIn("evidence", contract)
        for flow in self.out["flows"]:
            for key in ("evidence", "expect", "traffic"):
                self.assertNotIn(key, flow)
        self.assertNotIn("vocabularies", self.out)
        with_vocab = dict(self.doc, vocabularies=[{"id": "x"}])
        self.assertNotIn("vocabularies", overlay.apply_overlay(with_vocab, self.spec))

    def test_the_input_is_left_alone(self):
        before = copy.deepcopy(self.doc)
        overlay.apply_overlay(self.doc, self.spec)
        self.assertEqual(self.doc, before)
        self.out["components"][0]["title"] = "changed"
        self.assertEqual(self.doc, before)

    def test_a_gap_is_replaced_and_a_live_flow_has_none(self):
        doc = shop()
        flow = doc["flows"][0]
        flow["status"], flow["gap"] = "partial", "Only reads are wired."
        spec = fixture()
        spec["flows"]["web-to-api"]["gap"] = "Reads only."
        out = overlay.apply_overlay(doc, spec)
        self.assertEqual(out["flows"][0]["gap"], "Reads only.")
        self.assertNotIn("gap", out["flows"][1])

    def test_the_result_is_the_minimum_the_tour_reads_and_it_is_not_a_valid_atlas(self):
        text = explain.render_tour_md(self.out)
        self.assertIn("Customer site (main-repo) is live.", text)
        with self.assertRaises(model.AtlasError):
            model.validate(self.out)

    def test_it_works_on_the_normalised_atlas_too(self):
        from estate_atlas import render
        parsed = render.parse_atlas(self.doc)
        self.assertEqual(overlay.apply_overlay(parsed, self.spec), self.out)


class TrafficTests(unittest.TestCase):
    def setUp(self):
        code, text, _ = run("route", ATLAS, "--journal", str(JOURNAL), "--events", str(EVENTS))
        self.assertEqual(code, 0)
        self.traffic = json.loads(text)
        self.spec = fixture()
        self.out = overlay.apply_overlay_traffic(self.traffic, self.spec)

    def test_flow_ids_are_renamed_and_the_counts_kept(self):
        self.assertEqual(set(self.out["flows"]), {f["id"] for f in self.spec["flows"].values()})
        self.assertEqual(self.out["flows"]["orders-to-store"], self.traffic["flows"]["api-to-db"])
        self.assertEqual(self.out["generated"], self.traffic["generated"])

    def test_crosschecks_are_renamed_and_unrouted_examples_dropped(self):
        self.assertEqual(self.out["crosschecks"]["silent"], ["orders-to-processor"])
        self.assertNotIn("unrouted", self.out["crosschecks"])
        self.assertNotIn("rule_gaps", self.out["crosschecks"])
        text = json.dumps(self.out)
        for private in ("scheduler", "cron:nightly", "service:db", "api-to-db"):
            self.assertNotIn(private, text)

    def test_off_status_entries_are_renamed(self):
        traffic = copy.deepcopy(self.traffic)
        traffic["crosschecks"]["off_status"] = [{"flow": "web-to-api", "status": "planned", "d7": 3},
                                                {"flow": "unknown-flow", "status": "planned", "d7": 9}]
        out = overlay.apply_overlay_traffic(traffic, self.spec)
        self.assertEqual(out["crosschecks"]["off_status"], [{"flow": "site-to-orders", "status": "planned", "d7": 3}])

    def test_a_flow_the_overlay_does_not_name_is_dropped(self):
        traffic = copy.deepcopy(self.traffic)
        traffic["flows"]["secret-flow"] = {"d7": 1}
        self.assertNotIn("secret-flow", json.dumps(overlay.apply_overlay_traffic(traffic, self.spec)))

    def test_it_must_be_a_traffic_document(self):
        with self.assertRaises(model.AtlasError):
            overlay.apply_overlay_traffic({"schema": "something-else"}, self.spec)

    def test_the_tour_shows_this_week_with_public_names(self):
        text = explain.render_tour_md(overlay.apply_overlay(shop(), self.spec), self.out)
        self.assertIn("This week:", text)
        self.assertIn("orders-to-processor is silent.", text)
        self.assertIn("Task buffer → Fulfiller", text)


class LeakTests(unittest.TestCase):
    def test_a_denylisted_term_is_found_case_insensitively_and_sorted(self):
        self.assertEqual(overlay.leaks("The Zebra met an aardvark.", ["zebra", "Aardvark", "lion"]),
                         ["Aardvark", "zebra"])
        self.assertEqual(overlay.leaks("nothing here", ["zebra"]), [])
        self.assertEqual(overlay.leaks("anything", []), [])

    def test_an_empty_term_matches_nothing(self):
        self.assertEqual(overlay.leaks("text", [""]), [])

    def test_word_terms_match_whole_words_only(self):
        self.assertEqual(overlay.leaks("the web layer", [], ["web"]), ["web"])
        self.assertEqual(overlay.leaks("The Web.", [], ["web"]), ["web"])
        self.assertEqual(overlay.leaks("a webhook and the web-based part", [], ["web"]), [])
        self.assertEqual(overlay.leaks("orders-api and api_v2", [], ["api"]), [])
        self.assertEqual(overlay.leaks("(api)", [], ["api"]), ["api"])

    def test_terms_cover_the_ids_titles_remotes_and_repo_keys(self):
        subs, words = overlay.leak_terms(shop(), fixture())
        self.assertIn("Storefront", subs)
        self.assertIn("Job queue", subs)
        self.assertIn("example/shop", subs)
        self.assertIn("internal-only", subs)
        for original in ("web", "api", "queue", "db", "shop", "edge", "web-to-api", "shop-http", "external:payments"):
            self.assertIn(original, words)
        self.assertNotIn("Customer site", subs)

    def test_an_id_the_overlay_keeps_is_not_a_leak(self):
        spec = fixture()
        spec["components"]["web"]["id"] = "web"
        spec["components"]["queue"]["home"] = "shop"
        _, words = overlay.leak_terms(shop(), spec)
        self.assertNotIn("web", words)
        self.assertNotIn("shop", words)  # a repo key the overlay publishes as a label

    def test_an_injected_original_title_is_caught_and_clean_output_passes(self):
        doc, spec = shop(), fixture()
        subs, words = overlay.leak_terms(doc, spec)
        clean = explain.render_tour_md(overlay.apply_overlay(doc, spec))
        self.assertEqual(overlay.leaks(clean, subs, words), [])
        self.assertEqual(overlay.leaks(clean + "\nSee the Fulfilment worker.\n", subs, words),
                         ["Fulfilment worker", "worker"])  # the title, and the id as a whole word
        self.assertEqual(overlay.leaks(clean + "\nSee the SHOP API.\n", subs, words), ["Shop API", "api", "shop"])
        self.assertEqual(overlay.leaks(clean + " via web-to-api", subs, words), ["web-to-api"])


class CliTests(TempCase):
    def tour(self, *extra: str):
        return run("tour", ATLAS, "--md", "--overlay", str(FIXTURE), *extra)

    def test_the_round_trip_exits_0_and_names_no_original_id(self):
        code, out, err = self.tour()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("# A tour of a small online store", out)
        self.assertIn("Customer site", out)
        subs, words = overlay.leak_terms(shop(), fixture())
        self.assertEqual(overlay.leaks(out, subs, words), [])
        for original in ("web", "api", "queue", "worker", "db", "payments", "shop"):
            self.assertIsNone(re.search(r"\b%s\b" % original, out, re.IGNORECASE), original)
        self.assertEqual(out, self.tour()[1])  # byte-stable

    def test_with_traffic_the_numbers_stay_and_the_ids_go(self):
        _, route, _ = run("route", ATLAS, "--journal", str(JOURNAL), "--events", str(EVENTS))
        traffic = self.write("traffic.json", route)
        code, out, err = self.tour("--traffic", traffic)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("declared crossings this week", out)
        self.assertIn("orders-to-processor is silent.", out)
        for original in ("api-to-payments", "web-to-api", "scheduler"):
            self.assertNotIn(original, out)

    def test_the_json_tour_is_overlaid_too(self):
        code, out, _ = run("tour", ATLAS, "--overlay", str(FIXTURE))
        self.assertEqual(code, 0)
        steps = json.loads(out)["steps"]
        self.assertEqual(steps[0]["id"], "front")
        self.assertNotIn("Storefront", out)

    def test_out_writes_the_public_tour(self):
        target = self.tmp / "TOUR.md"
        code, out, _ = self.tour("--out", str(target))
        self.assertEqual((code, out), (0, ""))
        self.assertIn("# A tour of a small online store", target.read_text(encoding="utf-8"))

    def test_without_an_overlay_nothing_changes(self):
        code, out, _ = run("tour", ATLAS, "--md")
        self.assertEqual(code, 0)
        self.assertIn("# A tour of the estate", out)
        self.assertIn("Storefront (shop) is live.", out)

    def test_a_leak_through_a_summary_is_exit_2_and_prints_nothing(self):
        spec = fixture()
        spec["components"]["web"]["summary"] = "Serves pages from the Storefront and passes requests inward."
        target = self.tmp / "TOUR.md"
        code, out, err = run("tour", ATLAS, "--md", "--overlay", self.write("leaky.json", spec), "--out", str(target))
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("leaks private terms", err)
        self.assertIn("'Storefront'", err)
        self.assertFalse(target.exists())

    def test_a_leaked_id_in_a_trigger_is_exit_2(self):
        spec = fixture()
        spec["flows"]["api-to-db"]["trigger"] = "The api reads an order."
        traffic = self.write("t.json", run("route", ATLAS, "--journal", str(JOURNAL))[1])
        code, out, err = run("tour", ATLAS, "--md", "--overlay", self.write("leaky.json", spec),
                             "--traffic", traffic)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("'api'", err)

    def test_the_overlays_own_denylist_is_enforced(self):
        spec = fixture()
        spec["components"]["db"]["summary"] = "Keeps every order. This is internal-only."
        code, _, err = run("tour", ATLAS, "--md", "--overlay", self.write("leaky.json", spec))
        self.assertEqual(code, 2)
        self.assertIn("'internal-only'", err)

    def test_a_bad_overlay_is_exit_2(self):
        spec = fixture()
        del spec["layers"]["edge"]
        code, out, err = run("tour", ATLAS, "--md", "--overlay", self.write("short.json", spec))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("'edge'", err)
        self.assertEqual(run("tour", ATLAS, "--md", "--overlay", str(self.tmp / "missing.json"))[0], 2)

    def test_overlay_and_check_do_not_combine(self):
        code, _, err = run("tour", ATLAS, "--overlay", str(FIXTURE), "--check", self.write("t.md", "x"))
        self.assertEqual(code, 2)
        self.assertIn("--check", err)


if __name__ == "__main__":
    unittest.main()
