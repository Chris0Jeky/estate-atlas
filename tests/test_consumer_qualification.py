"""Offline consumer proof through the real CLI and a disposable fictional Git repo."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SHOP = ROOT / "examples" / "shop"
OVERLAY = ROOT / "tests" / "fixtures" / "shop-overlay.json"


class ConsumerQualificationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="atlas consumer ")
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.doc = json.loads((SHOP / "atlas.json").read_text(encoding="utf-8"))
        self.atlas = self.write_json("atlas.json", self.doc)
        self.env = os.environ.copy()
        self.env["PYTHONPATH"] = str(ROOT)
        self.env["PYTHONIOENCODING"] = "utf-8"
        self.env["GIT_OPTIONAL_LOCKS"] = "0"

    def write_json(self, name, doc):
        path = self.directory / name
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return path

    def cli(self, *args):
        return subprocess.run([sys.executable, "-m", "estate_atlas", *map(str, args)],
                              cwd=self.directory, env=self.env, capture_output=True, timeout=30)

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], env=self.env,
                              capture_output=True, check=True, timeout=30).stdout

    def make_repo(self):
        if not shutil.which("git"):
            self.skipTest("Git is required for source qualification")
        self.repo = self.directory / "shop checkout"
        self.repo.mkdir()
        shutil.copytree(SHOP / "src", self.repo / "src")
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fictional Shop")
        self.git("config", "user.email", "shop@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        hooks = self.directory / "empty hooks"
        hooks.mkdir()
        self.git("config", "core.hooksPath", str(hooks))
        self.git("add", "src")
        self.git("commit", "-q", "-m", "Add fictional shop sources")
        self.git("remote", "add", "origin", "https://github.com/example/shop.git")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")

    def git_state(self):
        # Disable optional locks even for the observer: status must not refresh the index.
        status = self.git("status", "--porcelain=v1", "--untracked-files=all")
        return {
            "HEAD": self.git("rev-parse", "HEAD"),
            "refs": self.git("for-each-ref", "--format=%(refname) %(objectname)"),
            "index": hashlib.sha256((self.repo / ".git/index").read_bytes()).digest(),
            "config": (self.repo / ".git/config").read_bytes(),
            "status": status,
        }

    def checked(self, atlas=None, *extra):
        before = self.git_state()
        result = self.cli("check", atlas or self.atlas, "--repo", "shop=" + str(self.repo),
                          "--host", "fictional", "--json", *extra)
        self.assertEqual(self.git_state(), before, "check changed Git state")
        self.assertEqual(result.stderr, b"", result.stderr)
        return result.returncode, json.loads(result.stdout)

    def test_origin_and_worktree_proof_leave_git_unchanged(self):
        self.make_repo()
        for options in ((), ("--worktree",)):
            with self.subTest(options=options):
                code, report = self.checked(None, *options)
                self.assertEqual((code, report["status"], report["checked"], report["ok"]),
                                 (0, "ok", 19, 19))
        # Preserve a dirty index and working file too; origin proof reads the saved ref.
        (self.repo / "staged.txt").write_text("fictional draft\n", encoding="utf-8")
        self.git("add", "staged.txt")
        (self.repo / "src/db.py").write_text("uncommitted replacement\n", encoding="utf-8")
        self.assertEqual(self.checked()[1]["status"], "ok")
        self.assertEqual(self.checked(None, "--worktree")[0], 1)

    def test_unresolved_origin_is_partial_not_drift(self):
        self.make_repo()
        self.git("remote", "set-url", "origin", "https://github.com/example/other.git")
        code, report = self.checked()
        self.assertEqual((code, report["status"], report["missing"]), (0, "partial", []))
        self.assertIn("origin is not", report["unresolved"][0]["why"])
        self.git("remote", "set-url", "origin", "https://github.com/example/shop.git")
        self.git("update-ref", "-d", "refs/remotes/origin/main")
        code, report = self.checked()
        self.assertEqual((code, report["status"], report["missing"]), (0, "partial", []))
        self.assertTrue(report["unresolved"])

    def test_missing_origin_file_or_anchor_is_drift(self):
        self.make_repo()
        for reference, why in (({"repo": "shop", "path": "src/missing.py"}, "file is absent at origin"),
                               ({"repo": "shop", "path": "src/db.py", "anchor": "absent_anchor"},
                                "anchor text not found at origin")):
            with self.subTest(reference=reference):
                doc = copy.deepcopy(self.doc)
                doc["components"][0]["evidence"].append(reference)
                atlas = self.write_json("drift.json", doc)
                code, report = self.checked(atlas)
                self.assertEqual((code, report["status"]), (1, "drift"))
                self.assertIn(why, [item["why"] for item in report["missing"]])

    def test_expect_and_observations_do_not_promote_a_planned_flow(self):
        self.make_repo()
        doc = copy.deepcopy(self.doc)
        flow = next(f for f in doc["flows"] if f["id"] == "api-to-queue")
        flow.update(status="planned", gap="Runtime delivery remains unqualified.",
                    expect=copy.deepcopy(flow["evidence"]))
        atlas = self.write_json("planned.json", doc)
        before = atlas.read_bytes()
        code, report = self.checked(atlas)
        self.assertEqual((code, report["status"]), (0, "ok"))
        self.assertIn({"owner": "flows.api-to-queue", "refs": 1}, report["promotable"])
        traffic = self.cli("route", atlas, "--journal", SHOP / "journal.jsonl",
                           "--events", SHOP / "events.jsonl")
        self.assertEqual((traffic.returncode, traffic.stderr), (0, b""))
        snapshot = json.loads(traffic.stdout)
        self.assertEqual(snapshot["flows"]["api-to-queue"]["by_basis"]["declared"], 8)
        self.assertEqual(snapshot["flows"]["web-to-api"]["by_basis"]["inferred"], 14)
        self.assertEqual(snapshot["flows"]["worker-heartbeat"]["heat"], "pulse")
        self.assertGreater(snapshot["coverage"]["unrouted"], 0)
        self.assertEqual(snapshot["coverage"]["internal"], 6)
        self.assertIn({"flow": "api-to-queue", "status": "planned", "d7": 8},
                      snapshot["crosschecks"]["off_status"])
        self.assertEqual(atlas.read_bytes(), before)
        unchanged = json.loads(atlas.read_bytes())
        self.assertEqual(next(f for f in unchanged["flows"] if f["id"] == flow["id"]), flow)

    def test_empty_and_expired_observations_are_not_health_proof(self):
        empty = self.directory / "empty.jsonl"
        empty.write_bytes(b"")
        result = self.cli("route", self.atlas, "--journal", empty)
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = json.loads(result.stdout)
        self.assertEqual((snapshot["generated"], snapshot["coverage"]["records"]), (0, 0))
        observed = self.cli("route", self.atlas, "--journal", SHOP / "journal.jsonl")
        self.assertEqual(observed.returncode, 0, observed.stderr)
        later = str(json.loads(observed.stdout)["generated"] + 30 * 86400)
        expired = self.cli("route", self.atlas, "--journal", SHOP / "journal.jsonl", "--now", later)
        self.assertEqual(expired.returncode, 0, expired.stderr)
        self.assertEqual(json.loads(expired.stdout)["coverage"]["records"], 0)

    def test_public_tour_is_deterministic_and_refusal_preserves_output(self):
        output = self.directory / "public.md"
        args = ("tour", self.atlas, "--md", "--overlay", OVERLAY)
        first, second = self.cli(*args), self.cli(*args)
        self.assertEqual((first.returncode, first.stderr), (0, b""))
        self.assertTrue(first.stdout)
        self.assertEqual(first.stdout, second.stdout)
        result = self.cli(*args, "--out", output)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))
        saved = output.read_bytes()
        self.assertNotIn(b"\r", saved)  # Files use LF; Windows stdout can use CRLF.
        self.assertEqual(first.stdout.replace(b"\r\n", b"\n"), saved)
        self.assertEqual(self.cli(*args, "--out", output).returncode, 0)
        self.assertEqual(output.read_bytes(), saved)
        marker = "cobalt-vault-sentinel"
        denied = json.loads(OVERLAY.read_text(encoding="utf-8"))
        denied["denylist"] = [marker]
        denied["components"]["api"]["summary"] = marker
        invalid = copy.deepcopy(denied)
        invalid[marker] = "invalid field"
        for name, spec in (("denied.json", denied), ("invalid.json", invalid)):
            with self.subTest(name=name):
                overlay = self.write_json(name, spec)
                result = self.cli("tour", self.atlas, "--md", "--overlay", overlay, "--out", output)
                self.assertEqual((result.returncode, result.stdout), (2, b""))
                self.assertEqual(result.stderr.strip(), b"estate-atlas: public tour refused; inspect the inputs privately.")
                self.assertNotIn(marker.encode(), result.stderr)
                self.assertEqual(output.read_bytes(), saved)

    def test_workspace_and_stale_tour_through_positional_cli(self):
        workspace = ROOT / "examples/workspace/atlas.json"
        result = self.cli("validate", workspace)
        self.assertEqual((result.returncode, result.stderr), (0, b""))
        output = self.directory / "tour.md"
        self.assertEqual(self.cli("tour", self.atlas, "--md", "--out", output).returncode, 0)
        self.assertEqual(self.cli("tour", self.atlas, "--check", output).returncode, 0)
        output.write_text("stale fictional tour\n", encoding="utf-8")
        result = self.cli("tour", self.atlas, "--check", output)
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"stale", result.stderr)


if __name__ == "__main__":
    unittest.main()
