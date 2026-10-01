"""Evidence, vocabulary, expect, overlap and export tests on fictional repos."""
from __future__ import annotations

import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from estate_atlas import check, model

HOST = "TESTHOST"


def git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def make_checkout(path: Path, files: dict[str, str], remote: str) -> Path:
    """A checkout whose origin/main holds `files` (no network: the remote ref is set directly)."""
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@example.invalid")
    git(path, "config", "user.name", "t")
    git(path, "config", "commit.gpgsign", "false")
    for relative, text in files.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    git(path, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(path, "remote", "add", "origin", f"https://github.com/{remote}.git")
    return path


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


class CheckFixture(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.alpha = make_checkout(self.root / "alpha", {"tool.py": "def run():\n    return 'tool.out/1'\n"},
                                   "Owner/alpha")
        self.extra = make_checkout(self.root / "extra", {"docs/SINK.md": "The sink reads tool.out/1.\n"},
                                   "Owner/extra")
        self.doc = minimal(str(self.alpha), str(self.extra))

    def run_check(self, doc: dict | None = None) -> dict:
        doc = doc if doc is not None else self.doc
        model.validate(doc)
        return check.check(doc, check.repositories(doc, {}, HOST), HOST)


class WorktreeModeTests(CheckFixture):
    """`repositories(..., worktree=True)` reads the working files, not origin/<default branch>."""

    def test_an_unpushed_working_file_counts_only_in_worktree_mode(self) -> None:
        (self.alpha / "new.py").write_text("NEW_ANCHOR = 1\n", encoding="utf-8")
        doc = minimal(str(self.alpha), str(self.extra))
        doc["components"][0]["evidence"] = [{"repo": "alpha", "path": "new.py", "anchor": "NEW_ANCHOR"}]
        model.validate(doc)
        at_origin = check.check(doc, check.repositories(doc, {}, HOST), HOST)
        self.assertEqual(at_origin["status"], "drift")
        in_tree = check.check(doc, check.repositories(doc, {}, HOST, worktree=True), HOST)
        self.assertEqual(in_tree["status"], "ok")

    def test_a_path_that_leaves_the_checkout_is_refused(self) -> None:
        data, why = check._worktree_blob(str(self.alpha), "../extra/docs/SINK.md")
        self.assertIsNone(data)
        self.assertIn("leaves the checkout", why)


class CheckTests(CheckFixture):
    def test_all_references_present(self) -> None:
        report = self.run_check()
        self.assertEqual((report["status"], report["checked"], report["ok"]), ("ok", 4, 4))
        self.assertEqual(set(report["heads"]), {"alpha", "extra"})
        self.assertEqual(report["schema"], check.CHECK_SCHEMA)

    def test_missing_file_and_missing_anchor_are_drift(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["components"][1]["evidence"] = [{"repo": "extra", "path": "docs/GONE.md"}]
        doc["contracts"][0]["evidence"][0]["anchor"] = "tool.out/2"
        report = self.run_check(doc)
        self.assertEqual(report["status"], "drift")
        whys = sorted(item["why"] for item in report["missing"])
        self.assertEqual(whys, ["anchor text not found at origin", "file is absent at origin"])

    def test_checkout_absent_on_this_host_is_unresolved_not_drift(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["repos"]["extra"]["paths"] = {"OTHERHOST": str(self.extra)}
        report = self.run_check(doc)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["unresolved"], [{"repo": "extra", "why": f"no checkout on {HOST}", "refs": 2}])

    def test_wrong_remote_is_unresolved(self) -> None:
        git(self.extra, "remote", "set-url", "origin", "https://github.com/Someone/else.git")
        report = self.run_check()
        self.assertEqual(report["unresolved"][0]["repo"], "extra")
        self.assertIn("origin is not", report["unresolved"][0]["why"])

    def test_non_ascii_paths_resolve(self) -> None:
        doc = copy.deepcopy(self.doc)
        extra = make_checkout(self.root / "unicode", {"docs/caf\u00e9.md": "reads tool.out/1\n"},
                              "Owner/unicode")
        doc["repos"]["unicode"] = {"remote": "Owner/unicode", "default_branch": "main",
                                   "paths": {HOST: str(extra)}}
        doc["components"][1]["evidence"] = [{"repo": "unicode", "path": "docs/caf\u00e9.md",
                                             "anchor": "tool.out/1"}]
        report = self.run_check(doc)
        self.assertEqual(report["missing"], [])

    def test_only_the_origin_commit_counts(self) -> None:
        (self.extra / "docs" / "LOCAL.md").write_text("only local\n", encoding="utf-8")
        git(self.extra, "add", "-A")
        git(self.extra, "commit", "-q", "-m", "local only")
        doc = copy.deepcopy(self.doc)
        doc["components"][1]["evidence"] = [{"repo": "extra", "path": "docs/LOCAL.md"}]
        self.assertEqual(self.run_check(doc)["missing"][0]["why"], "file is absent at origin")

    def test_check_never_fetches(self) -> None:
        real = subprocess.run

        def guarded(argv, *args, **kwargs):
            if "fetch" in argv or "pull" in argv:
                raise AssertionError(f"network git call: {argv}")
            return real(argv, *args, **kwargs)

        with patch.object(check.subprocess, "run", guarded):
            self.assertEqual(self.run_check()["status"], "ok")

    def test_repositories_override_wins_then_host_then_unresolved(self) -> None:
        doc = copy.deepcopy(self.doc)
        other = self.root / "other"
        repos = check.repositories(doc, {"extra": str(other)}, HOST)
        self.assertEqual(repos["extra"]["path"], str(other))
        repos = check.repositories(doc, {}, HOST)
        self.assertEqual(repos["extra"]["path"], str(self.extra))
        doc["repos"]["extra"]["paths"] = {"OTHERHOST": str(self.extra)}
        repos = check.repositories(doc, {}, HOST)
        self.assertIsNone(repos["extra"]["path"])
        report = check.check(doc, repos, HOST)
        self.assertEqual(report["status"], "partial")

    def test_export_links_use_the_head_when_resolved_and_the_branch_otherwise(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["repos"]["extra"]["paths"] = {"OTHERHOST": str(self.extra)}
        model.validate(doc)
        out = check.export(doc, check.repositories(doc, {}, HOST))
        self.assertEqual(out["schema"], check.EXPORT_SCHEMA)
        head = subprocess.run(["git", "-C", str(self.alpha), "rev-parse", "HEAD"], capture_output=True,
                              text=True, check=True).stdout.strip()
        tool_ref = out["components"][0]["surfaces"][0]["evidence"][0]
        self.assertEqual(tool_ref["url"], f"https://github.com/Owner/alpha/blob/{head}/tool.py")
        sink_ref = out["components"][1]["evidence"][0]
        self.assertEqual(sink_ref["url"], "https://github.com/Owner/extra/blob/main/docs/SINK.md")
        self.assertNotIn("url", self.doc["components"][1]["evidence"][0], "export must not mutate its input")

    def test_export_with_check_embeds_report(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["flows"][1]["expect"] = [{"repo": "extra", "path": "docs/SINK.md"}]
        model.validate(doc)
        repos = check.repositories(doc, {}, HOST)
        report = check.check(doc, repos, HOST)
        out = check.export(doc, repos, report)
        self.assertEqual(out["schema"], check.EXPORT_SCHEMA)
        self.assertIn("check", out)
        self.assertEqual(out["check"]["schema"], check.CHECK_SCHEMA)
        self.assertEqual(out["check"]["status"], "ok")
        expect_ref = out["flows"][1]["expect"][0]
        self.assertTrue(expect_ref["url"].startswith("https://github.com/"))

    def test_export_without_check_has_no_check(self) -> None:
        model.validate(self.doc)
        out = check.export(self.doc, check.repositories(self.doc, {}, HOST))
        self.assertEqual(out["schema"], check.EXPORT_SCHEMA)
        self.assertNotIn("check", out)

    def test_summary_mentions_promotable(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["flows"][1]["expect"] = [{"repo": "extra", "path": "docs/SINK.md"}]
        report = self.run_check(doc)
        self.assertEqual(report["promotable"], [{"owner": "flows.sink-cloud", "refs": 1}])
        self.assertIn("- promotable:", check._summary(report))


class VocabularyTests(CheckFixture):
    def vocab(self, **over: object) -> dict:
        base: dict = {"id": "words", "title": "Words", "owner": "tool",
                      "source": {"repo": "alpha", "path": "kinds.json", "select": "kinds/*/kind"},
                      "terms": [{"term": "a", "means": "First kind."}]}
        base.update(over)
        return base

    def test_select_reports_both_ways(self) -> None:
        kinds = make_checkout(self.root / "kinds",
                              {"kinds.json": json.dumps({"kinds": [{"kind": "a"}, {"kind": "b"}]})},
                              "Owner/kinds")
        doc = copy.deepcopy(self.doc)
        doc["repos"]["kinds"] = {"remote": "Owner/kinds", "default_branch": "main",
                                 "paths": {HOST: str(kinds)}}
        doc["vocabularies"] = [self.vocab(source={"repo": "kinds", "path": "kinds.json",
                                                  "select": "kinds/*/kind"},
                                          terms=[{"term": "a", "means": "A."},
                                                 {"term": "c", "means": "C."}])]
        report = self.run_check(doc)
        entry = report["vocabularies"][0]
        self.assertEqual(entry["missing_in_atlas"], ["b"])
        self.assertEqual(entry["missing_in_source"], ["c"])
        self.assertEqual(entry["status"], "drift")
        self.assertEqual(report["status"], "drift")

    def test_each_true_finds_missing_quoted_term(self) -> None:
        verbs = make_checkout(self.root / "verbs", {"verbs.py": 'VERBS = ("a",)\n'}, "Owner/verbs")
        doc = copy.deepcopy(self.doc)
        doc["repos"]["verbs"] = {"remote": "Owner/verbs", "default_branch": "main",
                                 "paths": {HOST: str(verbs)}}
        doc["vocabularies"] = [self.vocab(id="journal-verbs",
                                          source={"repo": "verbs", "path": "verbs.py", "each": True},
                                          terms=[{"term": "a", "means": "A."},
                                                 {"term": "c", "means": "C."}])]
        report = self.run_check(doc)
        entry = report["vocabularies"][0]
        self.assertEqual(entry["missing_in_atlas"], [])
        self.assertEqual(entry["missing_in_source"], ["c"])
        self.assertEqual(entry["status"], "drift")

    def test_source_not_json_is_drift(self) -> None:
        plain = make_checkout(self.root / "plain", {"data.json": "not json\n"}, "Owner/plain")
        doc = copy.deepcopy(self.doc)
        doc["repos"]["plain"] = {"remote": "Owner/plain", "default_branch": "main",
                                 "paths": {HOST: str(plain)}}
        doc["vocabularies"] = [self.vocab(source={"repo": "plain", "path": "data.json",
                                                  "select": "a"})]
        report = self.run_check(doc)
        self.assertEqual(report["vocabularies"][0]["status"], "drift")
        self.assertEqual(report["vocabularies"][0]["why"], "source is not JSON")

    def test_unresolved_repository_is_unresolved_and_partial(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["repos"]["gone"] = {"remote": "Owner/gone", "default_branch": "main",
                                "paths": {"OTHERHOST": "C:/nowhere/gone"}}
        doc["vocabularies"] = [self.vocab(source={"repo": "gone", "path": "k.json",
                                                  "select": "a"})]
        report = self.run_check(doc)
        self.assertEqual(report["vocabularies"][0]["status"], "unresolved")
        self.assertEqual(report["status"], "partial")


class ExpectTests(CheckFixture):
    def test_expect_on_live_raises(self) -> None:
        doc = minimal()
        doc["components"][0]["expect"] = [{"repo": "alpha", "path": "tool.py"}]
        with self.assertRaises(model.AtlasError) as caught:
            model.validate(doc)
        self.assertIn("only a planned, documented or absent item may carry expect",
                      str(caught.exception))

    def test_promotable_when_expect_resolves(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["flows"][1]["expect"] = [{"repo": "extra", "path": "docs/SINK.md"}]
        report = self.run_check(doc)
        self.assertEqual(report["promotable"], [{"owner": "flows.sink-cloud", "refs": 1}])
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["missing"], [])

    def test_missing_expect_is_neither_promotable_nor_drift(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["flows"][1]["expect"] = [{"repo": "extra", "path": "docs/GONE.md"}]
        report = self.run_check(doc)
        self.assertEqual(report["promotable"], [])
        self.assertEqual(report["missing"], [])
        self.assertEqual(report["status"], "ok")

    def test_expect_in_an_unresolved_repository_keeps_the_check_ok(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["repos"]["ghost"] = {"remote": "Owner/ghost", "default_branch": "main",
                                 "paths": {HOST: str(self.root / "no-such-checkout")}}
        doc["flows"][1]["expect"] = [{"repo": "ghost", "path": "src/new.py"}]
        report = self.run_check(doc)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["unresolved"], [])
        self.assertEqual(report["promotable"], [])

    def test_evidence_refs_excludes_expect(self) -> None:
        doc = copy.deepcopy(self.doc)
        doc["flows"][1]["expect"] = [{"repo": "extra", "path": "docs/SINK.md"}]
        model.validate(doc)
        self.assertFalse(any(ref["path"] == "docs/SINK.md" and owner == "flows.sink-cloud"
                             for owner, ref in check.evidence_refs(doc)
                             if owner == "flows.sink-cloud"))
        self.assertEqual(check.expect_refs(doc),
                         [("flows.sink-cloud", {"repo": "extra", "path": "docs/SINK.md"})])


class TrafficOverlapTests(CheckFixture):
    def make_doc(self, first: list | None = None, second: list | None = None) -> dict:
        doc = minimal(str(self.alpha), str(self.extra))
        if first is not None:
            doc["flows"][0]["traffic"] = first
        if second is not None:
            doc["flows"][1]["traffic"] = second
        return doc

    def run_rules_check(self, doc: dict) -> dict:
        model.validate(doc)
        return check.check(doc, check.repositories(doc, {}, HOST), HOST)

    def test_patterns_overlap_truth_table(self) -> None:
        cases = [("*", "pr:1", True), ("pr:1", "*", True), ("pr:1", "pr:1", True),
                 ("pr:1", "pr:2", False), ("pr:*", "pr:9", True), ("pr:9", "pr:*", True),
                 ("pr:*", "order:x", False), ("ledger.*", "ledger.w*", True),
                 ("ledger.w*", "ledger.*", True), ("pr:*", "order:*", False)]
        for a, b, want in cases:
            with self.subTest(a=a, b=b):
                self.assertEqual(check.patterns_overlap(a, b), want)

    def test_rules_overlap(self) -> None:
        journal = {"source": "journal", "producer": "web"}
        self.assertFalse(check.rules_overlap(journal, {"source": "events", "producer": "web"}))
        self.assertTrue(check.rules_overlap(journal, {"source": "journal", "subject": "order:*"}),
                        "a missing field is a wildcard")
        self.assertFalse(check.rules_overlap(dict(journal, verb=["opened", "refused"]),
                                             dict(journal, verb=["requested"])))
        self.assertTrue(check.rules_overlap(dict(journal, verb=["opened", "refused"]),
                                            dict(journal, verb="o*")))
        self.assertTrue(check.rules_overlap({"source": "events", "producer": "svc", "subject": "svc:*"},
                                            {"source": "events", "producer": "svc",
                                             "subject": "worker:*"}))
        self.assertFalse(check.rules_overlap({"source": "events", "producer": "svc"},
                                             {"source": "events", "producer": "cron"}))

    def test_rules_overlap_ignores_pulse(self) -> None:
        plain = {"source": "journal", "producer": "web"}
        pulsed = {"source": "journal", "producer": "web", "pulse": True}
        self.assertTrue(check.rules_overlap(plain, pulsed))
        self.assertTrue(check.rules_overlap(dict(plain, pulse=False), dict(plain, pulse=True)))

    def test_overlap_across_flows_is_drift(self) -> None:
        report = self.run_rules_check(self.make_doc([{"source": "journal", "producer": "web"}],
                                               [{"source": "events"},
                                                {"source": "journal", "subject": "order:*"}]))
        self.assertEqual(report["status"], "drift")
        self.assertEqual(report["traffic"], [{"kind": "traffic-overlap", "flows": ["sink-cloud", "tool-sink"],
                                              "rules": [1, 0]}])
        self.assertEqual(report["summary"]["traffic_rules"], 3)
        self.assertEqual(report["summary"]["traffic_overlaps"], 1)

    def test_disjoint_rules_and_same_flow_overlap_stay_ok(self) -> None:
        report = self.run_rules_check(self.make_doc([{"source": "journal", "producer": "web"},
                                                {"source": "journal", "producer": "w*"}],
                                               [{"source": "journal", "producer": "api"}]))
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["traffic"], [])
        self.assertEqual(report["summary"]["traffic_overlaps"], 0)

    def test_export_carries_traffic(self) -> None:
        rules = [{"source": "journal", "producer": "web", "subject": "order:*"}]
        doc = self.make_doc(rules)
        model.validate(doc)
        out = check.export(doc, check.repositories(doc, {}, HOST))
        self.assertEqual(out["flows"][0]["traffic"], rules)


if __name__ == "__main__":
    unittest.main()
