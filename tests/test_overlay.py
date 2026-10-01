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
        self.assertEqual(overlay.leaks("a webhook and a webinar2", [], ["web"]), [])
        self.assertEqual(overlay.leaks("(api)", [], ["api"]), ["api"])

    def test_a_hyphen_or_underscore_splits_tokens(self):
        self.assertEqual(overlay.leaks("the web-based part", [], ["web"]), ["web"])
        self.assertEqual(overlay.leaks("orders-api and api_v2", [], ["api"]), ["api"])
        self.assertEqual(overlay.leaks("see payments-gateway-v2 now", [], ["payments-gateway"]), ["payments-gateway"])
        self.assertEqual(overlay.leaks("ledger_store", [], ["ledger"]), ["ledger"])
        self.assertEqual(overlay.leaks("ledgerstore and ledger9", [], ["ledger"]), [])

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
        self.assertEqual(overlay.leaks(clean + " via web-to-api", subs, words), ["api", "web", "web-to-api"])


class FoldingTests(unittest.TestCase):
    def test_case_width_compatibility_and_spacing_do_not_hide_a_term(self):
        self.assertEqual(overlay.leaks("the \uff33tore\u00a0 front\tpage", ["Store  Front"]), ["Store  Front"])
        self.assertEqual(overlay.leaks("the \uff41\uff50\uff49 service", [], ["api"]), ["api"])  # fullwidth
        self.assertEqual(overlay.leaks("STRASSE", ["Stra\u00dfe"]), ["Stra\u00dfe"])  # casefold, not lower
        self.assertEqual(overlay.leaks("the \ufb01nal ledger", ["final ledger"]), ["final ledger"])  # ligature

    def test_a_term_with_a_pipe_is_found(self):
        self.assertEqual(overlay.leaks("Billing | Invoices", ["billing | invoices"]), ["billing | invoices"])
        self.assertEqual(overlay.leaks("Billing  |  Invoices", ["billing | invoices"]), ["billing | invoices"])


class OriginalTextTermTests(unittest.TestCase):
    def doc(self):
        doc = shop()
        doc["components"][0]["summary"] = "Private storefront wording about the secret checkout."
        doc["layers"][0]["summary"] = "Short one."
        flow = doc["flows"][0]
        flow["status"], flow["gap"] = "partial", "Only the private reads are wired so far."
        flow["trigger"] = "A shopper opens the private page."
        return doc

    def spec(self):
        spec = fixture()
        spec["flows"]["web-to-api"]["gap"] = "Reads only."
        return spec

    def test_a_differing_original_summary_trigger_and_gap_become_terms(self):
        subs, _ = overlay.leak_terms(self.doc(), self.spec())
        for original in ("Private storefront wording about the secret checkout.",
                         "Only the private reads are wired so far.", "A shopper opens the private page."):
            self.assertIn(original, subs)

    def test_short_texts_and_texts_equal_to_the_public_one_are_not_terms(self):
        subs, _ = overlay.leak_terms(self.doc(), self.spec())
        self.assertNotIn("Short one.", subs)  # under MIN_TEXT_TERM characters
        self.assertEqual(overlay.MIN_TEXT_TERM, 12)
        doc = shop()
        spec = fixture()
        spec["components"]["web"]["summary"] = doc["components"][0]["summary"]
        self.assertNotIn(doc["components"][0]["summary"], overlay.leak_terms(doc, spec)[0])

    def test_republishing_an_original_summary_is_caught(self):
        doc = self.doc()
        spec = self.spec()
        # another entry's public text reuses the original wording, in other letter case
        spec["components"]["api"]["summary"] = doc["components"][0]["summary"].upper()
        public = overlay.apply_overlay(doc, spec)
        found = overlay.find_leaks(doc, spec, public, explain.render_tour_md(public), markdown=True)
        self.assertTrue(any(term == doc["components"][0]["summary"] for term, _ in found), found)


class FindLeaksTests(unittest.TestCase):
    def setUp(self):
        self.doc, self.spec = shop(), fixture()

    def find(self, spec=None, *, markdown=True):
        spec = spec or self.spec
        public = overlay.apply_overlay(self.doc, spec)
        text = explain.render_tour_md(public) if markdown else json.dumps(explain.tour(public), ensure_ascii=False)
        return overlay.find_leaks(self.doc, spec, public, text, markdown=markdown)

    def test_a_clean_overlay_has_no_hits(self):
        self.assertEqual(self.find(), [])
        self.assertEqual(self.find(markdown=False), [])

    def test_a_term_with_a_pipe_is_caught_before_and_after_escaping(self):
        spec = fixture()
        spec["denylist"] = ["Billing | Invoices"]
        spec["components"]["db"]["summary"] = "Keeps every order for Billing | Invoices."
        public = overlay.apply_overlay(self.doc, spec)
        text = explain.render_tour_md(public)
        self.assertIn("Billing \\| Invoices", text)  # the renderer escapes the pipe...
        self.assertEqual(overlay.leaks(text, spec["denylist"]), [])  # ...which is why the output alone is not enough
        found = overlay.find_leaks(self.doc, spec, public, text, markdown=True)
        self.assertIn(("Billing | Invoices", "components[order-store].summary"), found)
        self.assertIn(("Billing | Invoices", "the rendered tour (unescaped)"), found)

    def test_the_unescaped_render_alone_catches_it_when_the_fields_are_clean(self):
        spec = fixture()
        spec["denylist"] = ["a<b | c"]
        public = overlay.apply_overlay(self.doc, spec)
        found = overlay.find_leaks(self.doc, spec, public, "see a&lt;b \\| c", markdown=True)
        self.assertEqual(found, [("a<b | c", "the rendered tour (unescaped)")])

    def test_the_overlay_title_and_every_field_kind_is_checked(self):
        spec = fixture()
        spec["denylist"] = ["zzsecret"]
        spec["title"] = "the zzsecret shop"
        self.assertIn(("zzsecret", "overlay.title"), self.find(spec))
        for kind, key, field in (("layers", "edge", "title"), ("layers", "edge", "summary"),
                                 ("components", "web", "home"), ("contracts", "shop-http", "summary"),
                                 ("flows", "web-to-api", "trigger")):
            spec = fixture()
            spec["denylist"] = ["zzsecret"]
            spec[kind][key][field] = "has zzsecret inside"
            found = self.find(spec, markdown=False)
            self.assertTrue(any(term == "zzsecret" and where.startswith(kind + "[") and where.endswith("." + field)
                                for term, where in found), (kind, field, found))

    def test_a_glued_id_is_caught(self):
        spec = fixture()
        spec["components"]["api"]["summary"] = "Runs as the api-v2 process."
        self.assertIn(("api", "components[orders-service].summary"), self.find(spec))

    def test_a_json_tour_is_walked_after_parsing(self):
        term = 'say "cheese"\\now'
        spec = fixture()
        spec["denylist"] = [term]
        spec["components"]["db"]["summary"] = "Keeps orders, " + term + "."
        public = overlay.apply_overlay(self.doc, spec)
        text = json.dumps(explain.tour(public), ensure_ascii=False)
        self.assertEqual(overlay.leaks(text, [term]), [])  # the quote and backslash are escaped in the JSON text
        found = overlay.find_leaks(self.doc, spec, public, text, markdown=False)
        self.assertTrue(any(t == term and where.startswith("tour.steps[") for t, where in found), found)

    def test_a_private_key_in_the_json_output_is_caught(self):
        public = overlay.apply_overlay(self.doc, self.spec)
        out = json.dumps({"steps": [{"extra": 1, "web-to-api": 2}]})
        found = overlay.find_leaks(self.doc, self.spec, public, out, markdown=False)
        self.assertIn(("web-to-api", "tour.steps[0].web-to-api (key)"), found)

    def test_unparseable_json_is_refused(self):
        public = overlay.apply_overlay(self.doc, self.spec)
        with self.assertRaises(model.AtlasError):
            overlay.find_leaks(self.doc, self.spec, public, "{not json", markdown=False)


class HardeningTests(unittest.TestCase):
    def test_a_non_string_flow_in_off_status_is_an_atlas_error(self):
        spec = fixture()
        traffic = {"schema": overlay.TRAFFIC_SCHEMA, "flows": {},
                   "crosschecks": {"silent": [], "off_status": [{"flow": ["web-to-api"], "status": "planned"}]}}
        with self.assertRaises(model.AtlasError) as ctx:
            overlay.apply_overlay_traffic(traffic, spec)
        self.assertIn("must be a string", str(ctx.exception))
        traffic["crosschecks"]["off_status"] = [{"flow": 7}]
        with self.assertRaises(model.AtlasError):
            overlay.apply_overlay_traffic(traffic, spec)

    def test_the_contract_format_is_dropped(self):
        doc = shop()
        doc["contracts"][0]["format"] = "PRIVATE-FORMAT"
        out = overlay.apply_overlay(doc, fixture())
        self.assertTrue(all("format" not in c for c in out["contracts"]))
        self.assertNotIn("PRIVATE-FORMAT", json.dumps(out))


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

    def test_an_empty_overlay_path_fails_closed(self):
        for mode in (("--md",), ()):
            target = self.tmp / "TOUR.md"
            code, out, _ = run("tour", ATLAS, *mode, "--overlay", "", "--out", str(target))
            self.assertEqual(code, 2)
            self.assertEqual(out, "")
            self.assertFalse(target.exists())

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

    def test_a_private_term_with_a_pipe_is_exit_2_in_both_formats_and_names_the_field(self):
        spec = fixture()
        spec["denylist"] = ["Billing | Invoices"]
        spec["components"]["db"]["summary"] = "Keeps every order for Billing | Invoices."
        path = self.write("pipe.json", spec)
        for extra in (("--md",), ()):
            code, out, err = run("tour", ATLAS, "--overlay", path, *extra)
            self.assertEqual((code, out), (2, ""), extra)
            self.assertIn("'Billing | Invoices' in", err)
            self.assertIn("order-store", err)

    def test_a_glued_id_is_exit_2(self):
        spec = fixture()
        spec["components"]["worker"]["summary"] = "Runs as worker-v2 and picks up tasks."
        code, out, err = run("tour", ATLAS, "--md", "--overlay", self.write("glued.json", spec))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("'worker' in", err)

    def test_a_leak_in_the_overlay_title_is_exit_2(self):
        spec = fixture()
        spec["title"] = "the Storefront"
        code, out, err = run("tour", ATLAS, "--md", "--overlay", self.write("title.json", spec))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("overlay.title", err)

    def test_a_folded_leak_is_exit_2(self):
        spec = fixture()
        spec["components"]["web"]["summary"] = "Serves pages from the \uff33TOREFRONT  via \uff41pi calls."
        code, out, err = run("tour", ATLAS, "--md", "--overlay", self.write("folded.json", spec))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("'Storefront'", err)

    def test_a_non_string_traffic_flow_is_exit_2_not_a_traceback(self):
        traffic = {"schema": overlay.TRAFFIC_SCHEMA, "flows": {},
                   "crosschecks": {"silent": [], "off_status": [{"flow": {"a": 1}, "status": "planned"}]}}
        code, out, err = self.tour("--traffic", self.write("badtraffic.json", traffic))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("must be a string", err)

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
