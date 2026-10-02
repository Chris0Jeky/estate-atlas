"""The selected fictional checkout owns evidence despite inherited Git overrides."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from estate_atlas import model

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://github.com/example/shop.git"


class CheckGitEnvironmentTests(unittest.TestCase):
    def setUp(self):
        if not shutil.which("git"):
            self.skipTest("Git is required for checkout isolation proof")
        temporary = tempfile.TemporaryDirectory(prefix="atlas git environment ")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.env = {key: value for key, value in os.environ.items()
                    if not key.upper().startswith("GIT_") and key.upper() not in {"PYTHONHOME", "PYTHONPATH"}}
        self.env.update(PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8", GIT_OPTIONAL_LOCKS="0",
                        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
        hooks = self.directory / "empty hooks"
        hooks.mkdir()
        self.complete = self.make_repo("complete checkout", hooks, complete=True)
        self.missing = self.make_repo("missing checkout", hooks, complete=False)
        self.doc = {
            "schema": model.SCHEMA, "updated": "2026-09-29",
            "repos": {"shop": {"remote": "example/shop", "default_branch": "main", "paths": {}}},
            "layers": [{"id": "core", "title": "Core", "summary": "Fictional core."}],
            "components": [{"id": "tool", "title": "Tool", "layer": "core", "home": "shop",
                            "status": "live", "summary": "A fictional tool.", "surfaces": [], "owns": [],
                            "evidence": [{"repo": "shop", "path": "tool.py", "anchor": "REQUIRED_ANCHOR"}]}],
            "contracts": [], "flows": [],
        }
        model.validate(self.doc)

    def git(self, repo, *args):
        return subprocess.run(["git", "-C", str(repo), *args], env=self.env,
                              capture_output=True, check=True, timeout=30).stdout

    def make_repo(self, name, hooks, *, complete):
        repo = self.directory / name
        repo.mkdir()
        (repo / "README.md").write_text("Fictional checkout.\n", encoding="utf-8")
        if complete:
            (repo / "tool.py").write_text("REQUIRED_ANCHOR = True\n", encoding="utf-8")
        self.git(repo, "init", "-q", "-b", "main")
        self.git(repo, "config", "user.name", "Fictional Shop")
        self.git(repo, "config", "user.email", "shop@example.invalid")
        self.git(repo, "config", "commit.gpgsign", "false")
        self.git(repo, "config", "core.hooksPath", str(hooks))
        self.git(repo, "config", "core.excludesFile", os.devnull)
        self.git(repo, "add", ".")
        self.git(repo, "commit", "-q", "-m", "Add fictional checkout")
        self.git(repo, "remote", "add", "origin", ORIGIN)
        self.git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        (repo / "draft.txt").write_text("Staged fictional draft.\n", encoding="utf-8")
        self.git(repo, "add", "draft.txt")
        (repo / "README.md").write_text("Uncommitted fictional edit.\n", encoding="utf-8")
        return repo

    def git_state(self, repo):
        return {"HEAD": self.git(repo, "rev-parse", "HEAD"),
                "refs": self.git(repo, "for-each-ref", "--format=%(refname) %(objectname)"),
                "status": self.git(repo, "status", "--porcelain=v1", "--untracked-files=all"),
                "contents": {path.relative_to(repo).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                             for path in repo.rglob("*") if path.is_file()}}

    def check(self, repo, *, mode="cli", extra=None, doc=None):
        atlas = self.directory / "atlas.json"
        atlas.write_text(json.dumps(self.doc if doc is None else doc), encoding="utf-8")
        if mode == "cli":
            command = [sys.executable, "-m", "estate_atlas", "check", str(atlas),
                       "--repo", "shop=" + str(repo), "--host", "fictional", "--json"]
        else:
            command = [sys.executable, "-c", (
                "import json,sys; from estate_atlas import check,model; doc=model.load(sys.argv[1]); "
                "model.validate(doc); repos=check.repositories(doc,{'shop':sys.argv[2]},'fictional'); "
                "print(json.dumps(check.check(doc,repos,'fictional')))"
            ), str(atlas), str(repo)]
        before = {str(path): self.git_state(path) for path in (self.complete, self.missing)}
        result = subprocess.run(command, cwd=self.directory, env={**self.env, **(extra or {})},
                                capture_output=True, timeout=30)
        after = {str(path): self.git_state(path) for path in (self.complete, self.missing)}
        self.assertEqual(after, before, "evidence checking changed a fictional checkout")
        self.assertEqual(result.stderr, b"", result.stderr)
        return result.returncode, json.loads(result.stdout)

    def assert_selected_checkout(self, mode):
        self.assertEqual(self.check(self.complete, mode=mode)[1]["status"], "ok")
        code, expected = self.check(self.missing, mode=mode)
        self.assertEqual((code, expected["status"], expected["checked"], expected["ok"]),
                         (1 if mode == "cli" else 0, "drift", 1, 0))
        self.assertEqual(expected["missing"][0]["why"], "file is absent at origin")
        for extra in ({"GIT_DIR": str(self.complete / ".git")},
                      {"GIT_DIR": str(self.complete / ".git"), "GIT_WORK_TREE": str(self.complete)},
                      {"GIT_COMMON_DIR": str(self.complete / ".git")},
                      {"GIT_OBJECT_DIRECTORY": str(self.complete / ".git/objects")}):
            with self.subTest(mode=mode, variables=sorted(extra)):
                self.assertEqual(self.check(self.missing, mode=mode, extra=extra), (code, expected))

    def test_cli_proves_selected_checkout_despite_repository_and_object_overrides(self):
        self.assert_selected_checkout("cli")

    def test_api_proves_selected_checkout_despite_repository_and_object_overrides(self):
        self.assert_selected_checkout("api")

    def test_inherited_command_config_cannot_rewrite_origin_identity(self):
        doc = json.loads(json.dumps(self.doc))
        doc["repos"]["shop"]["remote"] = "example/other"
        key = "url.https://github.com/example/other.git.insteadOf"
        for mode in ("cli", "api"):
            code, expected = self.check(self.complete, mode=mode, doc=doc)
            self.assertEqual((code, expected["status"], expected["checked"]), (0, "partial", 0))
            for extra in ({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": key, "GIT_CONFIG_VALUE_0": ORIGIN},
                          {"GIT_CONFIG_PARAMETERS": "'" + key + "=" + ORIGIN + "'"}):
                with self.subTest(mode=mode, variables=sorted(extra)):
                    self.assertEqual(self.check(self.complete, mode=mode, doc=doc, extra=extra), (code, expected))

    def test_normal_parent_repository_discovery_still_works(self):
        nested = self.missing / "nested directory"
        nested.mkdir()
        baseline = self.check(nested)
        self.assertEqual(baseline[1]["status"], "drift")
        self.assertEqual(self.check(nested, extra={"GIT_DIR": str(self.complete / ".git")}), baseline)

    def test_explicit_global_user_config_is_retained(self):
        config = self.directory / "user git config"
        config.write_text('[url "https://github.com/example/other.git"]\n'
                          '\tinsteadOf = https://github.com/example/shop.git\n', encoding="utf-8")
        doc = json.loads(json.dumps(self.doc))
        doc["repos"]["shop"]["remote"] = "example/other"
        code, report = self.check(self.complete, doc=doc, extra={"GIT_CONFIG_GLOBAL": str(config)})
        self.assertEqual((code, report["status"], report["checked"], report["ok"]), (0, "ok", 1, 1))

    def test_inherited_trace_targets_cannot_write_into_either_checkout(self):
        for mode in ("cli", "api"):
            for variable in ("GIT_TRACE", "GIT_TRACE2", "GIT_TRACE2_EVENT", "GIT_TRACE2_PERF"):
                with self.subTest(mode=mode, variable=variable):
                    target = self.complete / f"{mode}-{variable.lower()}.log"
                    code, report = self.check(self.complete, mode=mode, extra={variable: str(target)})
                    self.assertEqual((code, report["status"]), (0, "ok"))
                    self.assertFalse(target.exists(), "evidence reads must not create inherited trace files")
        doc = json.loads(json.dumps(self.doc))
        doc["repos"]["shop"]["remote"] = "example/other"
        for mode in ("cli", "api"):
            for scope in ("GLOBAL", "SYSTEM"):
                for setting in ("normalTarget", "eventTarget", "perfTarget"):
                    with self.subTest(mode=mode, scope=scope, setting=setting):
                        target = self.complete / f"{mode}-{scope.lower()}-{setting.lower()}.log"
                        config = self.directory / f"{mode}-{scope.lower()}-{setting.lower()}.config"
                        config.write_text(f'[trace2]\n\t{setting} = "{target.as_posix()}"\n'
                                          '[url "https://github.com/example/other.git"]\n'
                                          '\tinsteadOf = https://github.com/example/shop.git\n', encoding="utf-8")
                        extra = {"GIT_CONFIG_" + scope: str(config)}
                        if scope == "SYSTEM":
                            extra["GIT_CONFIG_NOSYSTEM"] = "0"
                        code, report = self.check(self.complete, mode=mode, doc=doc, extra=extra)
                        self.assertEqual((code, report["status"], report["checked"], report["ok"]),
                                         (0, "ok", 1, 1), "owner URL configuration must remain effective")
                        self.assertFalse(target.exists(), "evidence reads must not create configured trace files")


if __name__ == "__main__":
    unittest.main()
