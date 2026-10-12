"""Receipt diagnostics retain unavailable causes without weakening failure policy."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from estate_atlas import check, cli, model
from tests.test_check import CheckFixture, HOST, git
from tests.test_proof import head, receipt

COMMIT = "a" * 40


class ProofDiagnosticTests(unittest.TestCase):
    def block(self, receipts=None):
        return {"claim": "source", "receipts": receipts or [receipt("source", COMMIT)]}

    def result(self, block, revision):
        return check._proof("components.tool", block, model.DEFAULT_LADDER, revision)

    def test_unresolved_cause_retains_receipt_repo_and_full_revision(self):
        item = self.result(self.block(), lambda repo, commit: ("unresolved", "no checkout on TESTHOST"))
        self.assertEqual(item["unresolved"], [{"receipt": "r-source", "repo": "alpha", "commit": COMMIT,
                                               "why": "no checkout on TESTHOST"}])
        self.assertEqual((item["status"], item["proven"], item["receipted"]), ("unresolved", None, "source"))
        self.assertEqual(item["revisions"], [])

    def test_mixed_unverified_and_unresolved_revisions_preserve_both_reasons(self):
        entry = receipt("source", COMMIT)
        entry["revisions"].append({"repo": "extra", "commit": "b" * 40})
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                revisions = list(reversed(entry["revisions"])) if reverse else entry["revisions"]
                item = self.result(self.block([dict(entry, revisions=revisions)]),
                                   lambda repo, commit: ("missing", "commit absent") if repo == "alpha"
                                   else ("unresolved", "checkout unavailable"))
                self.assertEqual(item["status"], "unverified")
                self.assertEqual(item["revisions"][0]["why"], "commit absent")
                self.assertEqual(item["unresolved"][0]["why"], "checkout unavailable")
                self.assertEqual(item["levels"][0]["state"], "unverified")

    def test_diagnostics_are_sorted_independently_of_receipt_input_order(self):
        entries = [receipt("source", COMMIT, "z"), receipt("source", COMMIT, "a")]
        revision = lambda repo, commit: ("unresolved", "unavailable")
        forward = self.result(self.block(entries), revision)
        reverse = self.result(self.block(list(reversed(entries))), revision)
        self.assertEqual(forward, reverse)
        self.assertEqual([entry["receipt"] for entry in forward["unresolved"]], ["a", "z"])

    def test_success_has_no_unresolved_diagnostics(self):
        item = self.result(self.block(), lambda repo, commit: ("ok", ""))
        self.assertEqual(item["unresolved"], [])
        self.assertEqual((item["status"], item["proven"]), ("ok", "source"))

    def test_unresolved_optional_receipt_does_not_downgrade_a_proven_claim(self):
        entries = [receipt("source", COMMIT), receipt("unit", "b" * 40)]
        item = self.result(self.block(entries), lambda repo, commit: ("ok", "") if commit == COMMIT
                           else ("unresolved", "checkout is not a git repository"))
        self.assertEqual((item["status"], item["proven"]), ("ok", "source"))
        text = check._summary({"status": "ok", "checked": 0, "ok": 0, "host": HOST, "missing": [], "unresolved": [], "proof": [item]})
        self.assertIn("receipt r-unit", text)
        self.assertIn("checkout is not a git repository", text)
        self.assertNotIn("under-claim", text)


class ProofDiagnosticIntegrationTests(CheckFixture):
    def proof_doc(self, repo="alpha", commit=None):
        doc = copy.deepcopy(self.doc)
        doc["components"][0]["proof"] = {
            "claim": "source", "receipts": [receipt("source", commit or head(self.alpha), repo=repo)]}
        return doc

    def test_worktree_non_repository_explains_unresolved_receipt_in_json_and_text(self):
        doc = self.proof_doc()
        # Plain source files still resolve, but the pinned commit cannot be checked without Git metadata.
        git_dir = self.alpha / ".git"
        git_dir.rename(self.alpha / ".saved-git")
        model.validate(doc)
        report = check.check(doc, check.repositories(doc, {}, HOST, worktree=True), HOST)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["unresolved"], [])  # still counts only ordinary evidence references
        self.assertEqual(report["proof"][0]["unresolved"][0]["why"], "checkout is not a git repository")
        path = self.root / "atlas.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        for json_mode in (False, True):
            with self.subTest(json_mode=json_mode):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = cli.main(["check", str(path), "--host", HOST, "--worktree"]
                                    + (["--json"] if json_mode else []))
                self.assertEqual(code, 0)
                self.assertIn("checkout is not a git repository", output.getvalue())

    def test_partial_clone_and_missing_checkout_keep_actionable_causes(self):
        doc = self.proof_doc()
        git(self.alpha, "config", "remote.origin.promisor", "true")
        report = check.check(doc, check.repositories(doc, {}, HOST, worktree=True), HOST)
        self.assertEqual(report["status"], "partial")
        self.assertIn("partial clone", report["proof"][0]["unresolved"][0]["why"])
        doc["repos"]["alpha"]["paths"] = {}
        report = self.run_check(doc)
        self.assertEqual(report["proof"][0]["unresolved"][0]["why"], "no checkout on TESTHOST")
        self.assertIn("no checkout on TESTHOST", check._summary(report))

    def test_git_execution_failures_remain_drift_for_blobs_and_receipts(self):
        doc = self.proof_doc()
        path = self.root / "atlas.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        original = check._git
        for error in (OSError("unavailable"), subprocess.TimeoutExpired("git", 30)):
            for operation in ("ls-tree", "revision"):
                with self.subTest(error=type(error).__name__, operation=operation):
                    def fail(selected, *args):
                        match = args[0] == "ls-tree" if operation == "ls-tree" else any(
                            str(arg).endswith("^{commit}") for arg in args)
                        if match:
                            raise error
                        return original(selected, *args)
                    with patch.object(check, "_git", fail):
                        output = io.StringIO()
                        with contextlib.redirect_stdout(output):
                            code = cli.main(["check", str(path), "--host", HOST, "--json"])
                    report = json.loads(output.getvalue())
                    self.assertEqual(code, 1)
                    self.assertEqual(report["status"], "drift")
                    problems = report["missing"] if operation == "ls-tree" else report["proof"][0]["revisions"]
                    self.assertTrue(problems)
                    self.assertEqual(problems[0]["why"], "git failed: " + type(error).__name__)


if __name__ == "__main__":
    unittest.main()
