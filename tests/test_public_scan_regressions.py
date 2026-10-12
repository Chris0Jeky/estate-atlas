"""Public scan regressions using disposable repositories and fictional terms."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_check_public import Repo, git, load_script

TERM = "North Cedar"


class PublicScanRegressions(unittest.TestCase):
    def setUp(self):
        self.script = load_script()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.terms_file = Path(self.directory.name) / "terms.txt"
        self.terms_file.write_text(TERM + "\n", encoding="utf-8")

    def scan(self, repo, *args):
        return repo.scan("--terms", str(self.terms_file), *args)

    def test_committed_scan_is_not_hidden_by_dirty_cleanup_or_deletion(self):
        repo = Repo(self)
        repo.write("notes.md", TERM + "\n")
        repo.commit()
        for deleted in (False, True):
            with self.subTest(deleted=deleted):
                if deleted:
                    (repo.path / "notes.md").unlink()
                else:
                    repo.write("notes.md", "reviewed text\n")
                self.assertEqual(self.scan(repo).returncode, 0)
                result = self.scan(repo, "--committed")
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("notes.md:1: private-term #1", result.stdout)
                self.assertNotIn(TERM, result.stdout)

    def test_committed_scan_does_not_confuse_index_with_head(self):
        repo = Repo(self)
        repo.write("notes.md", "reviewed text\n")
        repo.commit()
        repo.write("notes.md", TERM + "\n")
        git(repo.path, "add", "notes.md")
        before = subprocess.check_output(["git", "status", "--porcelain=v1"], cwd=repo.path)
        self.assertEqual(self.scan(repo, "--committed").returncode, 0)
        self.assertEqual(self.scan(repo).returncode, 1)
        self.assertEqual(before, subprocess.check_output(["git", "status", "--porcelain=v1"], cwd=repo.path))

    def test_committed_scan_reads_symlink_text_and_wide_encoded_blobs(self):
        repo = Repo(self)
        repo.stage_symlink("alias", TERM)
        repo.write("alias", "unrelated working file")
        (repo.path / "wide.txt").write_bytes(("first\n" + TERM + "\n").encode("utf-16"))
        repo.commit()
        repo.write("wide.txt", "clean")
        result = self.scan(repo, "--committed")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("wide.txt:2: private-term #1", result.stdout)
        # The second commit replaced the index symlink with a regular file; inspect the original separately.
        git(repo.path, "reset", "--soft", "HEAD~1")
        result = self.scan(repo, "--committed")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("alias:1: private-term #1", result.stdout)

    def test_committed_mode_refuses_an_unborn_head_and_conflicting_modes(self):
        repo = Repo(self)
        self.assertEqual(self.scan(repo, "--committed").returncode, 2)
        repo.write("notes.md", "clean")
        repo.commit()
        self.assertEqual(self.scan(repo, "--history", "--committed").returncode, 2)

    def test_promisor_checkout_is_refused_without_fetching_missing_objects(self):
        repo = Repo(self)
        repo.write("notes.md", "clean")
        repo.commit()
        git(repo.path, "config", "remote.origin.promisor", "true")
        for mode in ([], ["--committed"], ["--history"]):
            with self.subTest(mode=mode):
                result = self.scan(repo, *mode)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertIn("complete local repository", result.stderr)

    def test_wrapped_terms_keep_the_starting_line_and_unicode_folding(self):
        terms = self.script.Terms([TERM])
        for separator in ("\n", "\r\n", "\n\n \n", "\r"):
            with self.subTest(separator=repr(separator)):
                text = "first\nＮｏｒｔｈ" + separator + "Ｃｅｄａｒ\n"
                self.assertEqual(list(self.script.scan_text(text, terms)), [(2, "private-term #1")])

    def test_wrapped_hits_are_not_hidden_by_an_inline_occurrence(self):
        terms = self.script.Terms([TERM, "A B"])
        text = TERM + "\nNorth\nCedar\nA\nBB\n"
        self.assertEqual(list(self.script.scan_text(text, terms)),
                         [(1, "private-term #1"), (2, "private-term #1")])
        self.assertEqual(list(self.script.scan_text("xNorth\nCedarxy", terms)), [])

    def test_worktree_and_history_scan_wrapped_terms(self):
        repo = Repo(self)
        repo.write("notes.md", "first\nNorth\nCedar\n")
        repo.commit()
        result = self.scan(repo)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("notes.md:2: private-term #1", result.stdout)
        result = self.scan(repo, "--history")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("notes.md:2: private-term #1", result.stdout)
        self.assertNotIn(TERM, result.stdout)

    def test_history_does_not_join_opposite_sides_or_distinct_files(self):
        repo = Repo(self)
        repo.write("notes.md", "North\n")
        repo.write("other.md", "Cedar\n")
        repo.commit()
        repo.write("notes.md", "Cedar\n")
        repo.commit()
        result = self.scan(repo, "--history")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_history_scans_wrapped_commit_messages_but_not_author_message_boundaries(self):
        repo = Repo(self)
        repo.write("notes.md", "clean")
        repo.commit("North\nCedar")
        result = self.scan(repo, "--history")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("(commit message)", result.stdout)
        # Synthetic parser control: an author line and message must not form one term.
        stream = "\x01" + "1" * 40 + "\nDev\nNorth\nCedar\n\x02\n"
        self.assertEqual(list(self.script.history_records(stream, self.script.Terms([TERM]))), [])

    def test_history_includes_merge_resolution_and_counts_commits_once(self):
        repo = Repo(self)
        repo.write("notes.md", "base\n")
        repo.commit()
        git(repo.path, "checkout", "-q", "-b", "side")
        repo.write("notes.md", "side\n")
        repo.commit()
        git(repo.path, "checkout", "-q", "main")
        repo.write("notes.md", "main\n")
        repo.commit()
        with self.assertRaises(subprocess.CalledProcessError):
            git(repo.path, "merge", "--no-edit", "side")
        repo.write("notes.md", TERM + "\n")
        repo.commit("resolve")
        merge = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo.path).decode().strip()
        result = self.scan(repo, "--history")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(merge[:12] + " notes.md:1: private-term #1", result.stdout)
        self.assertIn("1 of 4 commit(s)", result.stdout)
        self.assertEqual(result.stdout.count("notes.md:1: private-term #1"), 1)

    def test_history_allow_lines_match_paths_and_never_author_metadata(self):
        repo = Repo(self)
        repo.write("notes.md", TERM + "\n")
        repo.commit()
        self.terms_file.write_text(TERM + "\nallow: notes.md = " + TERM + "\n", encoding="utf-8")
        result = self.scan(repo, "--history")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("1 hit(s) accepted by allow lines", result.stdout)
        git(repo.path, "commit", "--allow-empty", "-q", "-m", TERM)
        with self.terms_file.open("a", encoding="utf-8") as stream:
            stream.write("allow: (commit message) = " + TERM + "\n")
        result = self.scan(repo, "--history")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("(commit message)", result.stdout)

    def test_git_quoted_paths_decode_without_losing_spaces_unicode_or_escapes(self):
        cases = [
            (r'"a/notes\t\".md" "b/notes\t\".md"', ['notes\t".md']),
            (r'"a/notes\n.md" "b/notes\n.md"', ['notes\n.md']),
            (r'"a/notes\\file.md" "b/notes\\file.md"', ['notes\\file.md']),
            (r'"a/caf\303\251.md" "b/caf\303\251.md"', ['café.md']),
            ('a/notes b/copy.md b/notes b/copy.md', ['notes b/copy.md']),
            (r'"a/old\tname" b/new name', ['old\tname', 'new name']),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(self.script.diff_header_paths(raw), expected)

    @unittest.skipIf(os.name == "nt", "Windows disallows quote characters in file names")
    def test_history_scans_quoted_file_names_and_applies_exact_allow_paths(self):
        repo = Repo(self)
        name = 'North Cedar\t".txt'
        repo.write(name, "clean")
        repo.commit()
        result = self.scan(repo, "--history")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("(file name)", result.stdout)
        self.assertNotIn(TERM, result.stdout)
        self.terms_file.write_text(TERM + "\nallow: " + name + " = " + TERM + "\n", encoding="utf-8")
        self.assertEqual(self.scan(repo, "--history").returncode, 0)


if __name__ == "__main__":
    unittest.main()
