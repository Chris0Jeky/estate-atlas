"""Evidence must be document bytes, not Git symlink or gitlink metadata."""
from __future__ import annotations

import subprocess
from unittest.mock import patch

from estate_atlas import check
from tests import test_check as fixtures


class EvidenceModeTests(fixtures.CheckFixture):
    def stage_entry(self, path, mode, value):
        if mode == "160000":
            oid = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.extra).decode().strip()
        else:
            oid = subprocess.check_output(["git", "hash-object", "-w", "--stdin"],
                                          cwd=self.extra, input=value.encode("utf-8")).decode().strip()
        fixtures.git(self.extra, "update-index", "--add", "--cacheinfo", mode + "," + oid + "," + path)
        fixtures.git(self.extra, "commit", "-q", "-m", "entry fixture")
        fixtures.git(self.extra, "update-ref", "refs/remotes/origin/main", "HEAD")

    def test_symlink_target_text_cannot_prove_an_anchor(self):
        self.stage_entry("docs/claim.md", "120000", "docs/proof-accepted.md")
        self.assertFalse((self.extra / "docs/proof-accepted.md").exists())
        self.doc["components"][1]["evidence"] = [
            {"repo": "extra", "path": "docs/claim.md", "anchor": "proof-accepted"}]
        report = self.run_check()
        self.assertEqual(report["status"], "drift")
        self.assertEqual(report["ok"], 3)
        self.assertEqual(report["missing"][0]["why"], "not a regular file at origin")

    def test_unanchored_symlink_does_not_count_as_file_evidence(self):
        self.stage_entry("docs/claim.md", "120000", "docs/SINK.md")
        self.doc["components"][1]["evidence"] = [{"repo": "extra", "path": "docs/claim.md"}]
        self.assertEqual(self.run_check()["status"], "drift")

    def test_symlink_does_not_make_expected_evidence_promotable(self):
        self.stage_entry("docs/claim.md", "120000", "docs/proof-accepted.md")
        self.doc["flows"][1]["expect"] = [
            {"repo": "extra", "path": "docs/claim.md", "anchor": "proof-accepted"}]
        report = self.run_check()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["promotable"], [])

    def test_gitlinks_and_directories_are_not_read_as_blobs(self):
        self.stage_entry("nested", "160000", "")
        for name in ("nested", "docs"):
            with self.subTest(name=name), patch.object(check, "_git", wraps=check._git) as calls:
                data, why = check._blob(str(self.extra), "HEAD", name)
                self.assertIsNone(data)
                self.assertEqual(why, "not a regular file at origin")
                self.assertEqual(len(calls.call_args_list), 1)

    def test_executable_and_unicode_regular_files_still_resolve(self):
        for name, mode in (("docs/café note.md", "100644"), ("run-script", "100755")):
            self.stage_entry(name, mode, "actual document anchor\n")
            with self.subTest(name=name):
                self.assertEqual(check._blob(str(self.extra), "HEAD", name),
                                 (b"actual document anchor\n", ""))

    def test_nested_selected_checkout_keeps_relative_path_semantics(self):
        self.assertEqual(check._blob(str(self.extra / "docs"), "HEAD", "SINK.md"),
                         (b"The sink reads tool.out/1.\n", ""))

    def test_regular_file_needs_only_tree_metadata_and_one_blob_read(self):
        with patch.object(check, "_git", wraps=check._git) as calls:
            data, why = check._blob(str(self.extra), "HEAD", "docs/SINK.md")
        self.assertIsNotNone(data)
        self.assertEqual(why, "")
        self.assertEqual(len(calls.call_args_list), 2)
        self.assertEqual([call.args[1] for call in calls.call_args_list], ["ls-tree", "cat-file"])

    def test_oversized_blob_is_rejected_before_content_is_read(self):
        with patch.object(check, "MAX_EVIDENCE_BYTES", 2), patch.object(check, "_git", wraps=check._git) as calls:
            data, why = check._blob(str(self.extra), "HEAD", "docs/SINK.md")
        self.assertIsNone(data)
        self.assertIn("larger than", why)
        self.assertEqual(len(calls.call_args_list), 1)

    def test_empty_and_exact_limit_blobs_remain_valid(self):
        self.stage_entry("docs/empty.md", "100644", "")
        self.stage_entry("docs/exact.md", "100644", "1234")
        with patch.object(check, "MAX_EVIDENCE_BYTES", 4):
            self.assertEqual(check._blob(str(self.extra), "HEAD", "docs/empty.md"), (b"", ""))
            self.assertEqual(check._blob(str(self.extra), "HEAD", "docs/exact.md"), (b"1234", ""))

    def test_malformed_tree_metadata_fails_before_reading_content(self):
        oid = b"a" * 40
        cases = [
            b"100644 blob " + oid + b" nope\tdocs/SINK.md\0",
            b"100644 blob " + oid + b" -1\tdocs/SINK.md\0",
            b"100644 blob invalid 4\tdocs/SINK.md\0",
            b"100644 tree " + oid + b" 4\tdocs/SINK.md\0",
            (b"100644 blob " + oid + b" 4\tdocs/SINK.md\0") * 2,
            b"invalid\tdocs/SINK.md\0",
        ]
        for output in cases:
            with self.subTest(output=output), patch.object(check, "_git", return_value=
                    subprocess.CompletedProcess([], 0, output, b"")) as calls:
                data, why = check._blob(str(self.extra), "HEAD", "docs/SINK.md")
                self.assertIsNone(data)
                self.assertTrue(why)
                self.assertEqual(calls.call_count, 1)

    def test_truncated_or_extra_blob_bytes_fail_closed(self):
        oid = b"a" * 40
        listing = b"100644 blob " + oid + b" 4\tdocs/SINK.md\0"
        for content in (b"123", b"12345"):
            with self.subTest(content=content), patch.object(check, "_git", side_effect=[
                    subprocess.CompletedProcess([], 0, listing, b""),
                    subprocess.CompletedProcess([], 0, content, b"")]):
                self.assertEqual(check._blob(str(self.extra), "HEAD", "docs/SINK.md"),
                                 (None, "git cat-file returned an unexpected size"))
