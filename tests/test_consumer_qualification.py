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


class ConsumerQualificationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="atlas consumer ")
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.root = Path(os.environ.get("ATLAS_QUALIFICATION_ROOT", ROOT))
        self.shop = self.root / "examples/shop"
        self.overlay = self.root / "tests/fixtures/shop-overlay.json"
        self.doc = json.loads((self.shop / "atlas.json").read_text(encoding="utf-8"))
        self.atlas = self.write_json("atlas.json", self.doc)
        self.env = {name: value for name, value in os.environ.items() if not name.upper().startswith("GIT_")}
        command = os.environ.get("ATLAS_QUALIFICATION_COMMAND")
        self.command = json.loads(command) if command else [sys.executable, "-m", "estate_atlas"]
        if command:
            for name in list(self.env):
                if name.upper() in {"PYTHONPATH", "PYTHONHOME"}:
                    del self.env[name]
        else:
            self.env["PYTHONPATH"] = str(ROOT)
        self.env["PYTHONIOENCODING"] = "utf-8"
        self.env.update(GIT_OPTIONAL_LOCKS="0", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")

    def write_json(self, name, doc):
        path = self.directory / name
        path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
        return path

    def cli(self, *args):
        command = [*self.command, *map(str, args)]
        result = subprocess.run(command, cwd=self.directory, env=self.env, capture_output=True, timeout=30)
        log = self.env.get("ATLAS_QUALIFICATION_COMMAND_LOG")
        if log:
            with Path(log).open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"command": command, "cwd": str(self.directory),
                                         "returncode": result.returncode,
                                         "stdout": result.stdout.decode("utf-8", errors="replace"),
                                         "stderr": result.stderr.decode("utf-8", errors="replace")}) + "\n")
        return result

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], env=self.env,
                              capture_output=True, check=True, timeout=30).stdout

    def make_repo(self):
        if not shutil.which("git"):
            self.skipTest("Git is required for source qualification")
        self.repo = self.directory / "shop checkout"
        self.repo.mkdir()
        shutil.copytree(self.shop / "src", self.repo / "src")
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.name", "Fictional Shop")
        self.git("config", "user.email", "shop@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.excludesFile", os.devnull)
        hooks = self.directory / "empty hooks"
        hooks.mkdir()
        self.git("config", "core.hooksPath", str(hooks))
        self.git("add", "src")
        self.git("commit", "-q", "-m", "Add fictional shop sources")
        self.git("remote", "add", "origin", "https://github.com/example/shop.git")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")
        # The example's receipts pin commits of the estate-atlas repository; this checkout has its own history.
        commit = self.git("rev-parse", "HEAD").decode().strip()
        for kind in ("components", "contracts", "flows"):
            for item in self.doc[kind]:
                for receipt in item.get("proof", {}).get("receipts", []):
                    receipt["revisions"] = [{"repo": "shop", "commit": commit}]
        self.atlas = self.write_json("atlas.json", self.doc)

    def git_state(self):
        # Disable optional locks even for the observer: status must not refresh the index.
        status = self.git("status", "--porcelain=v1", "--untracked-files=all")
        return {
            "HEAD": self.git("rev-parse", "HEAD"),
            "refs": self.git("for-each-ref", "--format=%(refname) %(objectname)"),
            "index": hashlib.sha256((self.repo / ".git/index").read_bytes()).digest(),
            "config": (self.repo / ".git/config").read_bytes(),
            "status": status,
            "contents": {p.relative_to(self.repo).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in self.repo.rglob("*") if p.is_file() and ".git" not in p.relative_to(self.repo).parts},
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
                self.assertEqual({item["status"] for item in report["proof"]}, {"ok"})
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
        traffic = self.cli("route", atlas, "--journal", self.shop / "journal.jsonl",
                           "--events", self.shop / "events.jsonl")
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
        observed = self.cli("route", self.atlas, "--journal", self.shop / "journal.jsonl")
        self.assertEqual(observed.returncode, 0, observed.stderr)
        later = str(json.loads(observed.stdout)["generated"] + 30 * 86400)
        expired = self.cli("route", self.atlas, "--journal", self.shop / "journal.jsonl", "--now", later)
        self.assertEqual(expired.returncode, 0, expired.stderr)
        self.assertEqual(json.loads(expired.stdout)["coverage"]["records"], 0)

    def test_public_tour_is_deterministic_and_refusal_preserves_output(self):
        output = self.directory / "public.md"
        args = ("tour", self.atlas, "--md", "--overlay", self.overlay)
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
        denied = json.loads(self.overlay.read_text(encoding="utf-8"))
        denied["denylist"] = [marker]
        denied["components"]["api"]["summary"] = marker
        invalid = copy.deepcopy(denied)
        invalid[marker] = "invalid field"
        for name, spec in (("denied.json", denied), ("invalid.json", invalid)):
            with self.subTest(name=name):
                overlay = self.write_json(name, spec)
                refused = self.cli("tour", self.atlas, "--md", "--overlay", overlay)
                self.assertEqual((refused.returncode, refused.stdout), (2, b""))
                self.assertEqual(refused.stderr.strip(), b"estate-atlas: public tour refused; inspect the inputs privately.")
                result = self.cli("tour", self.atlas, "--md", "--overlay", overlay, "--out", output)
                self.assertEqual((result.returncode, result.stdout), (2, b""))
                self.assertEqual(result.stderr.strip(), b"estate-atlas: public tour refused; inspect the inputs privately.")
                self.assertNotIn(marker.encode(), result.stderr)
                self.assertEqual(output.read_bytes(), saved)

    def test_workspace_and_stale_tour_through_positional_cli(self):
        workspace = self.root / "examples/workspace/atlas.json"
        result = self.cli("validate", workspace)
        self.assertEqual((result.returncode, result.stderr), (0, b""))
        output = self.directory / "tour.md"
        self.assertEqual(self.cli("tour", self.atlas, "--md", "--out", output).returncode, 0)
        self.assertEqual(self.cli("tour", self.atlas, "--check", output).returncode, 0)
        output.write_text("stale fictional tour\n", encoding="utf-8")
        result = self.cli("tour", self.atlas, "--check", output)
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"stale", result.stderr)

    def test_positional_route_explain_render_and_docs_are_deterministic(self):
        traffic = self.directory / "traffic.json"
        args = ("route", self.atlas, "--journal", self.shop / "journal.jsonl",
                "--events", self.shop / "events.jsonl", "--now", "1790000000", "--out", traffic)
        self.assertEqual(self.cli(*args).returncode, 0)
        saved = traffic.read_bytes()
        self.assertGreater(json.loads(saved)["coverage"]["records"], 0)
        self.assertEqual(self.cli(*args).returncode, 0)
        self.assertEqual(traffic.read_bytes(), saved)
        for args in (("explain", self.atlas, "worker", "--traffic", traffic),
                     ("render", "html", self.atlas), ("render", "mermaid", self.atlas)):
            with self.subTest(command=args):
                first, second = self.cli(*args), self.cli(*args)
                self.assertEqual((first.returncode, first.stderr), (0, b""))
                self.assertTrue(first.stdout)
                self.assertEqual(first.stdout, second.stdout)
                self.assertEqual((second.returncode, second.stderr), (0, b""))
        output = self.directory / "atlas.html"
        args = ("render", "html", self.atlas, "--out", output)
        self.assertEqual(self.cli(*args).returncode, 0)
        saved = output.read_bytes()
        self.assertEqual(self.cli(*args).returncode, 0)
        self.assertEqual(output.read_bytes(), saved)
        doc = self.directory / "architecture.md"
        doc.write_text("# Fictional architecture\n\n" + "\n\n".join(
            f"<!-- atlas:begin {block} -->\n<!-- atlas:end {block} -->"
            for block in ("glance", "owners", "layers", "traffic")) + "\n", encoding="utf-8")
        svg = self.directory / "atlas-glance.svg"
        args = (self.atlas, "--doc", doc, "--svg", svg)
        self.assertEqual(self.cli("docs", "write", *args).returncode, 0)
        saved_doc, saved_svg = doc.read_bytes(), svg.read_bytes()
        self.assertEqual(self.cli("docs", "write", *args).returncode, 0)
        self.assertEqual((doc.read_bytes(), svg.read_bytes()), (saved_doc, saved_svg))
        self.assertEqual(self.cli("docs", "check", *args).returncode, 0)
        self.assertEqual((doc.read_bytes(), svg.read_bytes()), (saved_doc, saved_svg))
        svg.write_bytes(saved_svg + b"\n<!-- stale -->\n")
        before = doc.read_bytes(), svg.read_bytes()
        self.assertEqual(self.cli("docs", "check", *args).returncode, 1)
        self.assertEqual((doc.read_bytes(), svg.read_bytes()), before)
        bad = self.write_json("invalid-traffic.json", {"schema": "invalid"})
        self.assertEqual(self.cli("docs", "write", *args, "--traffic", bad).returncode, 2)
        self.assertEqual((doc.read_bytes(), svg.read_bytes()), before)


class QualificationResult(unittest.TextTestResult):
    """Machine-readable evidence for the installed runner, alongside unittest output."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.test_names = []

    def startTest(self, test):
        self.test_names.append(test._testMethodName)
        super().startTest(test)


if __name__ == "__main__":
    receipt = os.environ.get("ATLAS_QUALIFICATION_RESULT")
    if not receipt:
        unittest.main()
    else:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(ConsumerQualificationTests)
        result = unittest.TextTestRunner(verbosity=2, resultclass=QualificationResult).run(suite)
        Path(receipt).write_text(json.dumps({
            "tests_run": result.testsRun, "test_names": result.test_names,
            "successful": result.wasSuccessful(), "failures": len(result.failures),
            "errors": len(result.errors), "skipped": len(result.skipped),
            "expected_failures": len(result.expectedFailures),
            "unexpected_successes": len(result.unexpectedSuccesses),
        }, indent=2) + "\n", encoding="utf-8")
        raise SystemExit(0 if result.wasSuccessful() else 1)
