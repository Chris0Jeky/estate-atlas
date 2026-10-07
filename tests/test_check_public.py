"""Tests for scripts/check_public.py, the local go-public scan. All names here are fictional.

Every pattern the scanner hunts is assembled from fragments, so this file does not trip the scan it tests.
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_public.py"

WIN_HOME_FWD = "C:" + "/Users/" + "alice/project/notes.txt"
WIN_HOME_BACK = "D:" + "\\Users\\" + "alice\\project"
WIN_HOME_JSON = "C:" + "\\\\Users\\\\" + "alice\\\\project"
LINUX_HOME = "/ho" + "me/alice/work"
MAC_HOME = "/Us" + "ers/alice/work"
HOST = "DESKTOP" + "-AB12CD3"
LAPTOP = "LAPTOP" + "-9XYZ8W7"
EMAIL = "alice" + "@" + "acme-corp.io"
TAILNET = "build-box." + "tail1234" + ".ts" + ".net"


def git(cwd, *args):
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    subprocess.run(["git", "-c", "user.name=Dev", "-c", "user.email=dev@example.com", "-c", "commit.gpgsign=false",
                    *args], cwd=cwd, check=True, capture_output=True, env=env)


class Repo:
    def __init__(self, test):
        self.dir = tempfile.TemporaryDirectory()
        test.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name)
        git(self.path, "init", "-q", "-b", "main")

    def write(self, name, text):
        target = self.path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def commit(self, message="work"):
        git(self.path, "add", "-A")
        git(self.path, "commit", "-q", "-m", message)

    def scan(self, *args):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=self.path, capture_output=True,
                              text=True, encoding="utf-8", env=env)


class GenericPatternTests(unittest.TestCase):
    def hit(self, text, category, name="notes.md"):
        repo = Repo(self)
        repo.write(name, "line one\n" + text + "\n")
        repo.commit()
        result = repo.scan()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(f"{name}:2: {category}", result.stdout)
        return result

    def clean(self, text):
        repo = Repo(self)
        repo.write("notes.md", text + "\n")
        repo.commit()
        result = repo.scan()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("clean: 1 files", result.stdout)

    def test_windows_home_paths_in_every_slash_style(self):
        for text in (WIN_HOME_FWD, WIN_HOME_BACK, WIN_HOME_JSON):
            with self.subTest(text=text):
                result = self.hit("see " + text, "home-path")
                self.assertNotIn("alice", result.stdout)

    def test_unix_home_paths(self):
        self.hit("cd " + LINUX_HOME, "home-path")
        self.hit("cd " + MAC_HOME, "home-path")

    def test_placeholder_and_url_paths_are_not_home_paths(self):
        for text in ("C:" + "\\Users\\<name>\\x", "~/work", "/ho" + "me/runner/work", "/ho" + "me/<user>",
                     "https://example.org/ho" + "me/alice", "C:" + "\\Users\\Public\\Documents"):
            with self.subTest(text=text):
                self.clean(text)

    def test_windows_default_host_names(self):
        self.hit("host " + HOST, "windows-host")
        self.hit("host " + LAPTOP, "windows-host")
        self.clean("DESKTOP" + "-publishing is a category, not a host")

    def test_email_addresses(self):
        result = self.hit("mail " + EMAIL, "email")
        self.assertNotIn("alice", result.stdout)
        self.assertNotIn("acme-corp", result.stdout)

    def test_allowed_email_forms(self):
        for text in ("bob@" + "example.com", "bob@" + "example.org", "noreply@" + "acme-corp.io",
                     "12345+bob@users." + "noreply.github.com", "name@" + "mail.example.com",
                     "estate-atlas-overlay@1", "pkg@2.x", "git@" + "github.com:owner/name",
                     "https://token@" + "github.com/owner/name", "ssh://git@" + "github.com/owner/name"):
            with self.subTest(text=text):
                self.clean(text)

    def test_tailnet_hosts(self):
        self.hit("ssh " + TAILNET, "tailnet-host")

    def test_a_tracked_file_name_is_scanned_too(self):
        repo = Repo(self)
        repo.write("notes-" + HOST + ".txt", "harmless\n")
        repo.commit()
        result = repo.scan()
        self.assertEqual(result.returncode, 1)
        self.assertIn("windows-host", result.stdout)

    def test_binary_files_are_skipped_not_crashed_on(self):
        repo = Repo(self)
        (repo.path / "blob.bin").write_bytes(b"\x00\x01\x02" + EMAIL.encode() + b"\x00")
        repo.write("a.txt", "fine\n")
        repo.commit()
        result = repo.scan()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("clean: 2 files", result.stdout)


class LocalTermTests(unittest.TestCase):
    def repo_with(self, text, terms=None, name="doc.md"):
        repo = Repo(self)
        repo.write(name, text)
        repo.commit()
        if terms is not None:
            repo.write(".public-scan.local", terms)
        return repo

    def test_local_terms_file_is_read_and_terms_are_not_echoed(self):
        repo = self.repo_with("fine\nsee Acme-Internal for details\n", "# private\n\nacme-internal\nother-thing\n")
        result = repo.scan()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("doc.md:2: private-term #1", result.stdout)
        self.assertNotIn("acme", (result.stdout + result.stderr).lower())

    def test_term_index_counts_terms_not_comments_or_blanks(self):
        repo = self.repo_with("other-thing here\n", "# c\n\nacme-internal\nother-thing\n")
        result = repo.scan()
        self.assertIn("doc.md:1: private-term #2", result.stdout)

    def test_whole_word_ish_matching(self):
        cases = {"the acme-internal repo": True, "acme-internal-v2": True, "acme_internal": True,
                 "acme internal": True, "ACME-Internal": True, "acme-internals": True, "acme-internal2": True,
                 "acme-internally": False, "xacme-internal": False, "acme-internalise": False}
        for text, expected in cases.items():
            with self.subTest(text=text):
                repo = self.repo_with(text + "\n", "acme-internal\n")
                self.assertEqual(repo.scan().returncode, 1 if expected else 0)

    def test_short_term_matches_the_word_not_longer_words(self):
        for text, expected in {"Rho was here": True, "a rhombus": False, "rho-based": True,
                               "enrho": False, "rhos": False, "rhoda": False,
                               "rho_x": True}.items():
            with self.subTest(text=text):
                repo = self.repo_with(text + "\n", "rho\n")
                self.assertEqual(repo.scan().returncode, 1 if expected else 0)

    def test_a_longer_term_tolerates_one_trailing_letter_a_short_one_does_not(self):
        for term, text, expected in (("rho", "rhos", False), ("vortex", "vortexs", True),
                                     ("vortex", "vortexxx", False)):
            with self.subTest(term=term, text=text):
                repo = self.repo_with(text + "\n", term + "\n")
                self.assertEqual(repo.scan().returncode, 1 if expected else 0)

    def test_separator_variants_of_a_multi_word_term(self):
        for text in ("widget workshop", "widget-workshop", "widget_workshop", "widgetworkshop", "Widget  Workshop"):
            with self.subTest(text=text):
                repo = self.repo_with(text + "\n", "Widget Workshop\n")
                self.assertEqual(repo.scan().returncode, 1)

    def test_terms_override_replaces_the_default_file(self):
        repo = self.repo_with("acme-internal and other-thing\n", "acme-internal\n")
        repo.write("elsewhere.txt", "other-thing\n")
        result = repo.scan("--terms", str(repo.path / "elsewhere.txt"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("private-term #1", result.stdout)
        self.assertNotIn("acme", result.stdout.lower())

    def test_an_allow_line_accepts_one_term_in_one_file_and_is_counted(self):
        repo = self.repo_with("see acme-internal\n", "acme-internal\nother-thing\nallow: doc.md = Acme-Internal\n")
        result = repo.scan()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("1 hit(s) accepted", result.stdout)
        repo.write("second.md", "see acme-internal\n")
        repo.commit()
        self.assertEqual(repo.scan().returncode, 1)
        repo.write("doc.md", "see acme-internal and other-thing\n")
        repo.commit()
        self.assertEqual(repo.scan().returncode, 1)

    def test_missing_default_file_warns_and_still_runs_generic_patterns(self):
        repo = self.repo_with("nothing private\n")
        result = repo.scan()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(".public-scan.local", result.stderr)
        self.assertIn("clean: 1 files", result.stdout)
        repo.write("leak.md", EMAIL + "\n")
        repo.commit()
        self.assertEqual(repo.scan().returncode, 1)

    def test_missing_explicit_terms_file_is_a_usage_error(self):
        repo = self.repo_with("fine\n")
        result = repo.scan("--terms", str(repo.path / "nope.txt"))
        self.assertEqual(result.returncode, 2)

    def test_not_a_git_repository_is_a_usage_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run([sys.executable, str(SCRIPT), "--repo", tmp], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)

    def test_unknown_option_is_a_usage_error(self):
        repo = self.repo_with("fine\n")
        self.assertEqual(repo.scan("--bogus").returncode, 2)

    def test_an_untracked_file_is_not_scanned(self):
        repo = self.repo_with("fine\n", "acme-internal\n")
        repo.write("scratch.txt", "acme-internal\n")
        self.assertEqual(repo.scan().returncode, 0)


class HistoryTests(unittest.TestCase):
    def test_history_finds_a_term_only_in_an_old_commit(self):
        repo = Repo(self)
        repo.write("a.md", "see acme-internal\n")
        repo.commit("first")
        repo.write("a.md", "see the shared library\n")
        repo.commit("second")
        repo.write(".public-scan.local", "acme-internal\n")
        self.assertEqual(repo.scan().returncode, 0)
        result = repo.scan("--history")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("private-term #1", result.stdout)
        self.assertIn("a.md", result.stdout)
        self.assertNotIn("acme", result.stdout.lower())
        sha = subprocess.run(["git", "rev-list", "--max-parents=0", "HEAD"], cwd=repo.path, capture_output=True,
                             text=True).stdout.strip()
        self.assertIn(sha[:12], result.stdout)

    def test_history_finds_generic_patterns_and_commit_messages(self):
        repo = Repo(self)
        repo.write("a.md", "x\n" + LINUX_HOME + "\n")
        repo.commit("moved from " + HOST)
        repo.write("a.md", "x\n")
        repo.commit("clean")
        result = repo.scan("--history")
        self.assertEqual(result.returncode, 1)
        self.assertIn("home-path", result.stdout)
        self.assertIn("windows-host", result.stdout)
        self.assertIn("(commit message)", result.stdout)

    def test_history_reports_a_ref_name(self):
        repo = Repo(self)
        repo.write("a.md", "x\n")
        repo.commit()
        git(repo.path, "branch", "acme-internal-wave")
        repo.write(".public-scan.local", "acme-internal\n")
        result = repo.scan("--history")
        self.assertEqual(result.returncode, 1)
        self.assertIn("(ref name)", result.stdout)

    def test_clean_history_exits_zero(self):
        repo = Repo(self)
        repo.write("a.md", "x\n")
        repo.commit()
        result = repo.scan("--history")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("clean: ", result.stdout)

    def test_the_scan_never_changes_the_repository(self):
        repo = Repo(self)
        repo.write("a.md", EMAIL + "\n")
        repo.commit()
        status = ["git", "status", "--porcelain", "--ignored"]
        before = subprocess.run(status, cwd=repo.path, capture_output=True, text=True).stdout
        repo.scan()
        repo.scan("--history")
        after = subprocess.run(status, cwd=repo.path, capture_output=True, text=True).stdout
        self.assertEqual(before, after)


def load_script():
    spec = importlib.util.spec_from_file_location("check_public_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def head(repo):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo.path, capture_output=True, text=True).stdout.strip()


class ReviewRoundTests(unittest.TestCase):
    """Findings of the first review round on the scan itself."""

    def repo_with(self, files, terms="acme-internal\n"):
        repo = Repo(self)
        for name, text in files.items():
            repo.write(name, text)
        repo.commit()
        if terms is not None:
            repo.write(".public-scan.local", terms)
        return repo

    def assertNoTerm(self, result):
        text = (result.stdout + result.stderr).lower()
        for variant in ("acme-internal", "acme_internal", "acme internal", "acmeinternal"):
            self.assertNotIn(variant, text)

    def test_tree_scan_never_prints_a_private_file_name(self):
        repo = self.repo_with({"acme-internal.txt": "plain\n", "docs/acme-internal/notes.md": "acme-internal\n"})
        result = repo.scan()
        self.assertEqual(result.returncode, 1)
        self.assertNoTerm(result)
        self.assertIn("<masked:#", result.stdout)
        self.assertIn("private-term #1", result.stdout)

    def test_history_never_prints_a_private_path_or_ref_name(self):
        repo = self.repo_with({"acme-internal.txt": "acme-internal\n"})
        repo.write("acme-internal.txt", "gone\n")
        repo.commit()
        git(repo.path, "branch", "acme-internal")
        git(repo.path, "mv", "acme-internal.txt", "kept.txt")
        repo.commit("rename")
        result = repo.scan("--history")
        self.assertEqual(result.returncode, 1)
        self.assertNoTerm(result)
        self.assertIn("(ref name)", result.stdout)
        self.assertIn("(file name)", result.stdout)

    def test_history_runs_git_log_without_textconv(self):
        module = load_script()
        calls = []

        def fake(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

        with patch.object(module.subprocess, "run", fake):
            module.scan_history(ROOT, module.Terms([]))
        log = next(argv for argv in calls if "log" in argv)
        self.assertIn("--no-textconv", log)
        self.assertIn("--no-ext-diff", log)

    def test_history_finds_an_empty_or_binary_file_named_after_a_term(self):
        repo = Repo(self)
        repo.write("keep.txt", "x\n")
        repo.write("acme-internal-empty.txt", "")
        (repo.path / "acme-internal-blob.bin").write_bytes(b"\x00\x01\x02")
        repo.commit("add")
        git(repo.path, "rm", "-q", "acme-internal-empty.txt", "acme-internal-blob.bin")
        repo.commit("remove")
        repo.write(".public-scan.local", "acme-internal\n")
        self.assertEqual(repo.scan().returncode, 0)
        result = repo.scan("--history")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertNoTerm(result)
        self.assertEqual(result.stdout.count("(file name)"), 4)  # two files, each in the adding and removing commit

    def test_hunk_payload_that_looks_like_a_file_header_is_still_scanned(self):
        repo = Repo(self)
        repo.write("a.txt", "base\n-- acme-internal\n")
        repo.commit("added a double-dash line")
        first = head(repo)
        repo.write("a.txt", "base\n")
        repo.commit("deleted it")
        second = head(repo)
        repo.write("a.txt", "base\n++ acme-internal\n")
        repo.commit("added a double-plus line")
        third = head(repo)
        repo.write("a.txt", "base\n")
        repo.commit("deleted it")
        repo.write(".public-scan.local", "acme-internal\n")
        result = repo.scan("--history")
        self.assertEqual(result.returncode, 1)
        for sha in (first, second, third):
            self.assertRegex(result.stdout, sha[:12] + r" a\.txt:\d+: private-term #1")

    def test_generic_patterns_ignore_case_and_accept_unicode_accounts(self):
        cases = {
            "lower windows": "c:" + "\\users\\alice\\x",
            "lower host": "desktop" + "-ab12cd3",
            "upper tailnet": "node.TS" + ".NET",
            "lower letters-only host": "desktop" + "-abcdefg",
            "mixed-case host": "DESKTOP" + "-AbCdEfG",
            "word-like host suffix": "desktop" + "-support",
            "unicode home": "/ho" + "me/élodie/work",
            "account with a space": "C:" + "\\Users\\user name\\docs",
            "windows unicode": "C:" + "\\Users\\élodie\\docs",
        }
        for label, text in cases.items():
            with self.subTest(label):
                repo = self.repo_with({"n.md": text + "\n"}, terms=None)
                result = repo.scan()
                self.assertEqual(result.returncode, 1, result.stdout)
        for text in ("C:" + "\\Users\\user\\docs", "DESKTOP" + "-publishing"):
            with self.subTest(clean=text):
                repo = self.repo_with({"n.md": text + "\n"}, terms=None)
                self.assertEqual(repo.scan().returncode, 0)

    def test_an_unreadable_tracked_file_fails_closed_with_a_masked_name(self):
        repo = self.repo_with({"acme-internal.txt": "x\n", "b.txt": "y\n"})
        module = load_script()
        real = Path.read_bytes

        def deny(self_path):
            if self_path.name == "acme-internal.txt":
                raise PermissionError("denied")
            return real(self_path)

        out, err = io.StringIO(), io.StringIO()
        with patch.object(Path, "read_bytes", deny), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = module.main(["--repo", str(repo.path)])
        self.assertEqual(code, 2)
        self.assertNotIn("acme-internal", (out.getvalue() + err.getvalue()).lower())
        self.assertIn("<masked:#", err.getvalue())

    def test_a_tracked_file_deleted_from_disk_is_skipped_not_fatal(self):
        repo = self.repo_with({"a.txt": "x\n", "b.txt": "y\n"}, terms=None)
        (repo.path / "a.txt").unlink()
        self.assertEqual(repo.scan().returncode, 0)

    def test_an_unreadable_default_terms_file_is_a_usage_error(self):
        repo = self.repo_with({"a.txt": "x\n"})
        module = load_script()
        real = Path.read_bytes

        def deny(self_path):
            if self_path.name == ".public-scan.local":
                raise PermissionError("denied")
            return real(self_path)

        out, err = io.StringIO(), io.StringIO()
        with patch.object(Path, "read_bytes", deny), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            self.assertEqual(module.main(["--repo", str(repo.path)]), 2)

    def test_a_term_matches_itself_literally(self):
        repo = self.repo_with({"d.md": "see widget--workshop\n"}, terms="widget--workshop\n")
        self.assertEqual(repo.scan().returncode, 1)

    def test_inter_hunk_context_config_cannot_hide_a_later_change(self):
        repo = Repo(self)
        git(repo.path, "config", "diff.interHunkContext", "3")
        repo.write("a.txt", "".join(f"line {n}\n" for n in range(1, 11)))
        repo.commit("base")
        lines = [f"line {n}\n" for n in range(1, 11)]
        lines[1] = "changed two\n"
        lines[3] = "see acme-internal\n"
        repo.write("a.txt", "".join(lines))
        repo.commit("two nearby changes")
        sha = head(repo)
        repo.write("a.txt", "".join(f"line {n}\n" for n in range(1, 11)))
        repo.commit("revert")
        repo.write(".public-scan.local", "acme-internal\n")
        self.assertEqual(repo.scan().returncode, 0)
        result = repo.scan("--history")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertRegex(result.stdout, sha[:12] + r" a\.txt:\d+: private-term #1")

    def test_a_context_line_inside_a_hunk_is_consumed_not_a_reset(self):
        module = load_script()
        log = ("\x01" + "a" * 40 + "\nDev <dev@example.com>\nDev <dev@example.com>\nmsg\n\x02\n"
               "diff --git a/f.txt b/f.txt\n--- a/f.txt\n+++ b/f.txt\n@@ -1,3 +1,3 @@\n-old one\n+new one\n"
               " context\n-old three\n+see acme-internal\n")
        found = list(module.history_records(log, module.Terms(["acme-internal"])))
        self.assertEqual([(path, number, finding) for _, path, number, finding in found],
                         [("f.txt", 3, "private-term #1")])

    def test_a_masked_label_keeps_no_source_characters(self):
        repo = self.repo_with({"al.txt": "plain\n"}, terms="al\n")
        git(repo.path, "branch", "al")
        for result in (repo.scan(), repo.scan("--history")):
            self.assertEqual(result.returncode, 1, result.stdout)
            self.assertIn("<masked:#", result.stdout)
            for leftover in ("al***", "al.txt", "heads/al", "refs/"):
                self.assertNotIn(leftover, result.stdout + result.stderr)

    def test_history_file_names_survive_a_noprefix_or_custom_prefix_config(self):
        for key, value in (("diff.noprefix", "true"), ("diff.srcPrefix", "x/"), ("diff.mnemonicPrefix", "true")):
            with self.subTest(key):
                repo = Repo(self)
                git(repo.path, "config", key, value)
                git(repo.path, "config", "diff.dstPrefix", "y/")
                repo.write("keep.txt", "x\n")
                repo.write("acme-internal-empty.txt", "")
                repo.commit("add")
                git(repo.path, "rm", "-q", "acme-internal-empty.txt")
                repo.commit("remove")
                repo.write(".public-scan.local", "acme-internal\n")
                self.assertEqual(repo.scan().returncode, 0)
                result = repo.scan("--history")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("(file name)", result.stdout)

    def test_history_pins_the_diff_prefixes_and_inter_hunk_context(self):
        module = load_script()
        calls = []

        def fake(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

        with patch.object(module.subprocess, "run", fake):
            module.scan_history(ROOT, module.Terms([]))
        log = next(argv for argv in calls if "log" in argv)
        for flag in ("--src-prefix=a/", "--dst-prefix=b/", "--inter-hunk-context=0"):
            self.assertIn(flag, log)

    def test_very_short_lines_are_scanned(self):
        repo = self.repo_with({"d.md": "ok\nz\n"}, terms="z\n")
        result = repo.scan()
        self.assertEqual(result.returncode, 1)
        self.assertIn("d.md:2: private-term #1", result.stdout)
        repo = Repo(self)
        repo.write("d.md", "z\n")
        repo.commit()
        repo.write("d.md", "ok\n")
        repo.commit()
        repo.write(".public-scan.local", "z\n")
        self.assertEqual(repo.scan("--history").returncode, 1)


class WideEncodingTests(unittest.TestCase):
    """UTF-16/32 text carries NULs, so a BOM has to win over the binary skip. Names here stay fictional."""

    BODY = "line one\nmail " + EMAIL + "\n"

    def scan_bytes(self, raw):
        repo = Repo(self)
        (repo.path / "notes.txt").write_bytes(raw)
        repo.commit()
        return repo.scan()

    def test_utf16_bom_text_is_scanned_at_the_right_line(self):
        cases = {
            "utf-16-le": b"\xff\xfe" + self.BODY.encode("utf-16-le"),
            "utf-16-be": b"\xfe\xff" + self.BODY.encode("utf-16-be"),
            "utf-32-le": b"\xff\xfe\x00\x00" + self.BODY.encode("utf-32-le"),
            "utf-32-be": b"\x00\x00\xfe\xff" + self.BODY.encode("utf-32-be"),
        }
        for label, raw in cases.items():
            with self.subTest(label):
                result = self.scan_bytes(raw)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("notes.txt:2: email", result.stdout)
                self.assertNotIn("alice", result.stdout)

    def test_nul_bytes_without_a_bom_are_still_skipped(self):
        result = self.scan_bytes(b"\x00\x01" + EMAIL.encode("ascii") + b"\x00")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("clean: 1 files", result.stdout)


class EmailGuardTests(unittest.TestCase):
    def test_email_pattern_runs_only_when_the_line_has_an_at_sign(self):
        module = load_script()
        self.assertEqual(module.generic_hits("a" * 20000), [])
        found = module.generic_hits("mail " + EMAIL)
        self.assertEqual([category for category, _excerpt in found], ["email"])

        real = module.EMAIL_PATTERN
        seen = []

        class RefuseWithoutAt:
            def finditer(self, text):
                seen.append("@" in text)
                if "@" not in text:
                    raise AssertionError("email pattern ran on a line with no @")
                return real.finditer(text)

        with patch.object(module, "EMAIL_PATTERN", RefuseWithoutAt()):
            self.assertEqual(module.generic_hits("a" * 20000), [])
            found = module.generic_hits("mail " + EMAIL)
        self.assertEqual(seen, [True])
        self.assertEqual([category for category, _excerpt in found], ["email"])


if __name__ == "__main__":
    unittest.main()
