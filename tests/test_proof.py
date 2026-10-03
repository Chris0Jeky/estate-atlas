"""Evidence levels (proof blocks, schema @3): validation, ladder order, the check rule, rendering and
back-compatibility. Real disposable git repositories with fictional names; no network."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from estate_atlas import check, cli, explain, model, overlay, render
from tests.test_check import HOST, CheckFixture, git, minimal

ROOT = Path(__file__).resolve().parent.parent
SHOP = ROOT / "examples" / "shop"
LADDER = list(model.DEFAULT_LADDER)


def head(path: Path) -> str:
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], check=True,
                          capture_output=True, text=True).stdout.strip()


def receipt(level: str, commit: str, rid: str | None = None, outcome: str = "passed", repo: str = "alpha") -> dict:
    return {"id": rid or f"r-{level}", "level": level, "revisions": [{"repo": repo, "commit": commit}],
            "check": f"fictional {level} check", "outcome": outcome, "unavailable": "nothing"}


def upto(top: str, commit: str, ladder: list[str] = LADDER) -> list[dict]:
    return [receipt(level, commit) for level in ladder[:ladder.index(top) + 1]]


class ProofValidationTests(unittest.TestCase):
    """The block's shape; every rejection names where it is."""

    def doc(self, proof: dict | None = None) -> dict:
        doc = minimal()
        doc["components"][0]["proof"] = proof if proof is not None else {
            "claim": "unit", "receipts": upto("unit", "a" * 40)}
        return doc

    def rejects(self, doc: dict, fragment: str) -> None:
        with self.assertRaises(model.AtlasError) as raised:
            model.validate(doc)
        self.assertIn(fragment, str(raised.exception))
        # the renderer's parser refuses the same atlas
        with self.assertRaises(render.AtlasError):
            render.parse_atlas(doc)

    def test_a_valid_block_on_a_component_contract_and_flow(self) -> None:
        doc = self.doc()
        doc["contracts"][0]["proof"] = {"claim": "source", "receipts": upto("source", "b" * 64)}
        doc["flows"][1]["proof"] = {"claim": "source", "receipts": []}
        model.validate(doc)
        parsed = render.parse_atlas(doc)
        self.assertEqual(parsed["components"][0]["proof"]["claim"], "unit")
        self.assertNotIn("proof", parsed["components"][1])

    def test_claim_and_receipt_levels_must_be_on_the_ladder(self) -> None:
        self.rejects(self.doc({"claim": "shipped", "receipts": []}), "proof.claim")
        bad = self.doc()
        bad["components"][0]["proof"]["receipts"][0]["level"] = "shipped"
        self.rejects(bad, "receipts[0].level")

    def test_commit_must_be_a_full_lowercase_id(self) -> None:
        for commit in ("a" * 39, "A" * 40, "a" * 41, "HEAD", "-a" + "a" * 38, "main"):
            doc = self.doc({"claim": "source", "receipts": [receipt("source", commit)]})
            self.rejects(doc, "revisions[0].commit")

    def test_revision_repo_must_be_declared(self) -> None:
        self.rejects(self.doc({"claim": "source", "receipts": [receipt("source", "a" * 40, repo="nowhere")]}),
                     "revisions[0].repo")

    def test_receipt_fields(self) -> None:
        cases = [
            ("unavailable", None, "missing key(s) ['unavailable']"),
            ("outcome", "ok", "receipts[0].outcome"),
            ("check", "", "receipts[0].check"),
            ("check", "two\nlines", "receipts[0].check"),
            ("date", "2026-02-30", "receipts[0].date"),
            ("revisions", [], "receipts[0].revisions"),
            ("id", "has space", "receipts[0].id"),
            ("extra", 1, "unknown key(s) ['extra']"),
        ]
        for key, value, fragment in cases:
            with self.subTest(key=key, value=value):
                doc = self.doc({"claim": "source", "receipts": [receipt("source", "a" * 40)]})
                if value is None:
                    del doc["components"][0]["proof"]["receipts"][0][key]
                else:
                    doc["components"][0]["proof"]["receipts"][0][key] = value
                self.rejects(doc, fragment)

    def test_duplicates_are_refused(self) -> None:
        doc = self.doc({"claim": "unit", "receipts": [receipt("source", "a" * 40, "same"),
                                                       receipt("unit", "a" * 40, "same")]})
        self.rejects(doc, "duplicate receipt id")
        twice = receipt("source", "a" * 40)
        twice["revisions"].append(dict(twice["revisions"][0]))
        self.rejects(self.doc({"claim": "source", "receipts": [twice]}), "duplicate revision")

    def test_a_declared_ladder(self) -> None:
        doc = self.doc({"claim": "reviewed", "receipts": [receipt("merged", "a" * 40)]})
        doc["proof_ladder"] = ["merged", "reviewed"]
        model.validate(doc)
        for ladder, fragment in (([], "1-16 levels"), (["a", "a"], "duplicate id"), (["Bad"], "invalid level"),
                                 ([f"l{i}" for i in range(17)], "1-16 levels"), ("merged", "must be a list")):
            with self.subTest(ladder=ladder):
                bad = copy.deepcopy(doc)
                bad["proof_ladder"] = ladder
                self.rejects(bad, "proof_ladder")
        # a level of the default ladder is not a level of a declared one
        doc["components"][0]["proof"]["claim"] = "unit"
        self.rejects(doc, "proof.claim")

    def test_proof_needs_schema_3_and_old_atlases_stay_valid(self) -> None:
        old = minimal()
        old["schema"] = "estate-atlas@2"
        model.validate(old)
        render.parse_atlas(old)
        with_proof = self.doc()
        with_proof["schema"] = "example/atlas@2"
        self.rejects(with_proof, "needs schema estate-atlas@3")
        ladder_only = copy.deepcopy(old)
        ladder_only["proof_ladder"] = ["source"]
        self.rejects(ladder_only, "needs schema estate-atlas@3")
        with_proof["schema"] = "example/atlas@3"
        model.validate(with_proof)


class LadderOrderTests(unittest.TestCase):
    def test_receipts_count_only_from_the_bottom_up(self) -> None:
        c = "a" * 40
        self.assertEqual(model.receipted_level({"claim": "unit", "receipts": []}, model.DEFAULT_LADDER), -1)
        gap = {"claim": "native", "receipts": [receipt("source", c), receipt("integrated", c),
                                                receipt("native", c)]}
        self.assertEqual(model.receipted_level(gap, model.DEFAULT_LADDER), 0)
        failed = {"claim": "unit", "receipts": [receipt("source", c), receipt("unit", c, outcome="failed"),
                                                 receipt("unit", c, "r-unit-2", outcome="partial")]}
        self.assertEqual(model.receipted_level(failed, model.DEFAULT_LADDER), 0)
        # order is the ladder's, never the order receipts are listed in
        shuffled = {"claim": "integrated", "receipts": list(reversed(upto("integrated", c)))}
        self.assertEqual(model.receipted_level(shuffled, model.DEFAULT_LADDER), 2)

    def test_a_declared_ladder_decides_the_order(self) -> None:
        block = {"claim": "b", "receipts": [receipt("b", "a" * 40)]}
        self.assertEqual(model.receipted_level(block, ("b", "a")), 0)
        self.assertEqual(model.receipted_level(block, ("a", "b")), -1)


class ProofCheckTests(CheckFixture):
    """The check rule against real disposable repositories."""

    def setUp(self) -> None:
        super().setUp()
        self.commit = head(self.alpha)

    def claim(self, proof: dict, doc: dict | None = None) -> dict:
        doc = copy.deepcopy(doc if doc is not None else self.doc)
        doc["components"][0]["proof"] = proof
        return doc

    def only(self, report: dict) -> dict:
        self.assertEqual(len(report["proof"]), 1)
        return report["proof"][0]

    def test_a_claim_with_resolving_receipts_is_proven(self) -> None:
        report = self.run_check(self.claim({"claim": "integrated", "receipts": upto("integrated", self.commit)}))
        item = self.only(report)
        self.assertEqual((item["status"], item["claimed"], item["proven"]), ("ok", "integrated", "integrated"))
        self.assertEqual([lvl["state"] for lvl in item["levels"]], ["proven"] * 3)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["summary"]["proof_claims"], 1)
        self.assertEqual(report["schema"], "estate-atlas-check@3")

    def test_an_over_claim_is_drift(self) -> None:
        receipts = upto("unit", self.commit) + [receipt("native", self.commit)]
        report = self.run_check(self.claim({"claim": "native", "receipts": receipts}))
        item = self.only(report)
        self.assertEqual((item["status"], item["proven"], item["receipted"]), ("over-claim", "unit", "unit"))
        self.assertEqual([lvl["state"] for lvl in item["levels"]], ["proven", "proven", "none", "proven"])
        self.assertEqual(report["status"], "drift")
        self.assertEqual(report["summary"]["proof_over_claims"], 1)
        text = check._summary(report)
        self.assertIn("- proof over-claim: components.tool: claimed native, proven unit", text)

    def test_a_failed_receipt_does_not_hold_a_level(self) -> None:
        receipts = [receipt("source", self.commit), receipt("unit", self.commit, outcome="failed")]
        item = self.only(self.run_check(self.claim({"claim": "unit", "receipts": receipts})))
        self.assertEqual((item["status"], item["levels"][1]["state"]), ("over-claim", "not-passed"))

    def test_a_missing_revision_is_drift_even_above_the_claim(self) -> None:
        receipts = upto("source", self.commit) + [receipt("unit", "0" * 40)]
        report = self.run_check(self.claim({"claim": "source", "receipts": receipts}))
        item = self.only(report)
        self.assertEqual(item["status"], "unverified")
        self.assertEqual(item["revisions"], [{"receipt": "r-unit", "repo": "alpha", "commit": "0" * 40,
                                              "why": "commit is not in the repository"}])
        self.assertEqual(report["status"], "drift")
        self.assertIn("receipt r-unit: alpha@000000000000: commit is not in the repository", check._summary(report))

    def test_a_commit_off_the_default_branch_does_not_resolve_at_origin(self) -> None:
        (self.alpha / "local.txt").write_text("local\n", encoding="utf-8")
        git(self.alpha, "add", "-A")
        git(self.alpha, "commit", "-q", "-m", "local only")
        local = head(self.alpha)
        doc = self.claim({"claim": "source", "receipts": [receipt("source", local)]})
        item = self.only(self.run_check(doc))
        self.assertEqual(item["revisions"][0]["why"], "commit is not on origin/main")
        # worktree mode reads the checkout as it is: the commit exists, so it resolves
        model.validate(doc)
        in_tree = check.check(doc, check.repositories(doc, {}, HOST, worktree=True), HOST)
        self.assertEqual(self.only(in_tree)["status"], "ok")

    def test_a_tree_or_blob_id_is_not_a_commit(self) -> None:
        tree = subprocess.run(["git", "-C", str(self.alpha), "rev-parse", "HEAD^{tree}"], check=True,
                              capture_output=True, text=True).stdout.strip()
        item = self.only(self.run_check(self.claim({"claim": "source", "receipts": [receipt("source", tree)]})))
        self.assertEqual(item["status"], "unverified")

    def test_an_unresolved_repository_leaves_the_claim_unresolved(self) -> None:
        doc = self.claim({"claim": "unit", "receipts": upto("unit", self.commit)})
        doc["repos"]["alpha"]["paths"] = {}
        report = self.run_check(doc)
        item = self.only(report)
        self.assertEqual((item["status"], item["proven"], item["receipted"]), ("unresolved", None, "unit"))
        self.assertEqual(report["status"], "partial")
        # an over-claim is drift even when nothing can be read
        doc["components"][0]["proof"]["claim"] = "integrated"
        self.assertEqual(self.run_check(doc)["status"], "drift")

    def test_a_worktree_that_is_not_a_repository_is_unresolved(self) -> None:
        plain = self.root / "plain"
        plain.mkdir()
        (plain / "tool.py").write_text("def run():\n    return 'tool.out/1'\n", encoding="utf-8")
        doc = self.claim({"claim": "source", "receipts": [receipt("source", self.commit)]})
        doc["repos"]["alpha"]["paths"] = {HOST: str(plain)}
        model.validate(doc)
        report = check.check(doc, check.repositories(doc, {}, HOST, worktree=True), HOST)
        self.assertEqual(self.only(report)["status"], "unresolved")

    def test_check_reads_git_read_only_and_never_fetches(self) -> None:
        doc = self.claim({"claim": "unit", "receipts": upto("unit", self.commit) + [receipt("native", "1" * 40)]})
        git_dir = self.alpha / ".git"

        def snapshot() -> dict[str, str]:
            files = sorted(p for p in git_dir.rglob("*") if p.is_file())
            return {str(p.relative_to(git_dir)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}

        before = snapshot()
        calls: list[tuple[str, ...]] = []
        real = check._git

        def spy(path, *args):
            calls.append(args)
            completed = real(path, *args)
            return completed

        with patch.object(check, "_git", side_effect=spy):
            self.run_check(doc)
        self.assertEqual(snapshot(), before)
        verbs = {args[0] for args in calls}
        self.assertLessEqual(verbs, {"remote", "rev-parse", "ls-tree", "cat-file", "merge-base"})
        self.assertIn("merge-base", verbs)
        for args in calls:
            if args[0] == "merge-base":
                self.assertEqual(args[1], "--is-ancestor")

    def test_git_reads_never_fetch_lazily(self) -> None:
        captured: dict = {}

        def fake_run(argv, env, capture_output, timeout):
            captured.update(env)
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        with patch.object(check.subprocess, "run", side_effect=fake_run):
            check._git(self.alpha, "cat-file", "-t", self.commit)
        self.assertEqual(captured.get("GIT_NO_LAZY_FETCH"), "1")

    def test_report_text_and_export_are_deterministic(self) -> None:
        doc = self.claim({"claim": "native", "receipts": list(reversed(upto("integrated", self.commit)))})
        doc["flows"][0]["proof"] = {"claim": "source", "receipts": [receipt("source", head(self.extra),
                                                                            repo="extra")]}
        first = json.dumps(self.run_check(doc), sort_keys=True)
        self.assertEqual(first, json.dumps(self.run_check(copy.deepcopy(doc)), sort_keys=True))
        self.assertEqual([item["owner"] for item in json.loads(first)["proof"]],
                         ["components.tool", "flows.tool-sink"])
        repos = check.repositories(doc, {}, HOST)
        exported = check.export(doc, repos)
        self.assertEqual(json.dumps(exported, sort_keys=True), json.dumps(check.export(doc, repos), sort_keys=True))
        self.assertEqual(exported["schema"], "estate-atlas-export@3")
        rev = exported["components"][0]["proof"]["receipts"][0]["revisions"][0]
        self.assertEqual(rev["url"], f"https://github.com/Owner/alpha/commit/{self.commit}")
        self.assertNotIn("url", doc["components"][0]["proof"]["receipts"][0]["revisions"][0])
        # an export, with its commit links, still parses for rendering
        render.parse_atlas(dict(exported, schema=model.SCHEMA))


class ProofRenderTests(unittest.TestCase):
    def atlas(self) -> dict:
        doc = model.load(SHOP / "atlas.json")
        model.validate(doc)
        return render.parse_atlas(doc)

    def test_outputs_are_byte_stable(self) -> None:
        atlas = self.atlas()
        self.assertEqual(render.render_html(atlas), render.render_html(self.atlas()))
        self.assertEqual(explain.render_tour_md(atlas), explain.render_tour_md(self.atlas()))
        self.assertEqual(json.dumps(explain.explain(atlas, "api")), json.dumps(explain.explain(self.atlas(), "api")))

    def test_html_shows_claimed_receipted_and_checked(self) -> None:
        atlas = self.atlas()
        page = render.render_html(atlas)
        self.assertIn('data-view="evidence"', page)
        self.assertIn("<td>components.worker</td><td>unit</td><td>integrated</td><td>not checked</td>", page)
        result = {"schema": "estate-atlas-check@3", "host": "h", "checked": 0, "ok": 0, "missing": [],
                  "unresolved": [], "status": "drift",
                  "proof": [{"owner": "components.api", "claimed": "installed", "proven": "unit",
                             "receipted": "installed", "status": "unverified", "levels": [], "revisions": []}]}
        checked = render.render_html(atlas, render.parse_check(result))
        self.assertIn('<td>installed</td><td>installed</td><td>unit <span class="pill absent">unverified</span>',
                      checked)
        self.assertIn("evidence levels proven: 0/1", checked)

    def test_an_over_claim_is_flagged_without_git(self) -> None:
        doc = model.load(SHOP / "atlas-over-claim.json")
        model.validate(doc)
        atlas = render.parse_atlas(doc)
        self.assertIn("claims more than its receipts", render.render_html(atlas))
        self.assertIn("Evidence: claimed installed, but passed receipts reach only integrated.",
                      explain.render_tour_md(atlas))

    def test_atlases_without_proof_render_as_before(self) -> None:
        doc = model.load(SHOP / "atlas.json")
        for item in doc["components"] + doc["contracts"] + doc["flows"]:
            item.pop("proof", None)
        doc["schema"] = "estate-atlas@2"
        model.validate(doc)
        atlas = render.parse_atlas(doc)
        page = render.render_html(atlas)
        self.assertNotIn("view-evidence", page.replace('"evidence"]', ""))
        self.assertNotIn('data-view="evidence"', page)
        self.assertNotIn("Evidence:", explain.render_tour_md(atlas))
        self.assertEqual([line["kind"] for line in explain.explain(atlas, "api")["lines"]][:2], ["what", "fed_by"])
        # a check@2 report from an older engine still renders
        old_report = {"schema": "estate-atlas-check@2", "host": "h", "checked": 1, "ok": 1, "missing": [],
                      "unresolved": [], "status": "ok"}
        self.assertEqual(render.parse_check(old_report)["proof"], [])

    def test_a_public_overlay_drops_proof(self) -> None:
        atlas = self.atlas()
        spec = overlay.load_overlay(ROOT / "tests" / "fixtures" / "shop-overlay.json")
        overlay.validate_overlay(spec, atlas)
        public = overlay.apply_overlay(atlas, spec)
        self.assertFalse(any("proof" in item for kind in ("components", "contracts", "flows")
                             for item in public[kind]))
        self.assertNotIn("Evidence:", explain.render_tour_md(public))


class ProofExampleTests(unittest.TestCase):
    """The fictional example: every level claimed, one under-claim, and a separate over-claim the check rejects."""

    def run_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(list(argv))
        return code, out.getvalue()

    def test_every_level_is_claimed_and_the_storefront_has_them_all(self) -> None:
        doc = model.load(SHOP / "atlas.json")
        model.validate(doc)
        claims = {block["claim"] for _, block in model.proof_entries(doc)}
        self.assertEqual(claims, set(model.DEFAULT_LADDER))
        web = next(c for c in doc["components"] if c["id"] == "web")
        self.assertEqual(model.receipted_level(web["proof"], model.DEFAULT_LADDER), len(model.DEFAULT_LADDER) - 1)

    def test_the_example_proves_in_worktree_mode(self) -> None:
        code, out = self.run_cli("check", str(SHOP / "atlas.json"), "--repo", f"shop={SHOP}", "--worktree",
                                 "--host", HOST, "--json")
        report = json.loads(out)
        self.assertEqual((code, report["status"]), (0, "ok"))
        self.assertEqual({item["status"] for item in report["proof"]}, {"ok"})
        worker = next(item for item in report["proof"] if item["owner"] == "components.worker")
        self.assertEqual((worker["claimed"], worker["proven"]), ("unit", "integrated"))

    def test_the_over_claim_example_is_rejected(self) -> None:
        for extra in ((), ("--worktree",)):
            with self.subTest(extra=extra):
                code, out = self.run_cli("check", str(SHOP / "atlas-over-claim.json"), "--repo", f"shop={SHOP}",
                                         "--host", HOST, *extra)
                self.assertEqual(code, 1)
                self.assertIn("- proof over-claim: components.api: claimed installed", out)


if __name__ == "__main__":
    unittest.main()
