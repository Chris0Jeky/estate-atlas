"""Real-Git origin identity controls; no network, owner repository or remote authentication."""
import json
import unittest

from tests.test_check import CheckFixture, git


class OriginIdentityTests(CheckFixture):
    def test_remote_identity_requires_the_actual_github_host(self) -> None:
        origins = [
            "https://mirror.invalid/Owner/extra.git",
            "https://github.com.mirror.invalid/Owner/extra.git",
            "https://github.com@mirror.invalid/Owner/extra.git",
            "ssh://git@mirror.invalid/Owner/extra.git",
            "git@mirror.invalid:Owner/extra.git",
            "/tmp/Owner/extra.git",
            "../Owner/extra.git",
            "https://github.com/ignored/Owner/extra.git",
            "https://mirror.invalid/github.com/Owner/extra.git",
        ]
        for origin in origins:
            with self.subTest(origin=origin):
                git(self.extra, "remote", "set-url", "origin", origin)
                report = self.run_check()
                self.assertEqual(report["status"], "partial")
                self.assertNotIn("extra", report["heads"])
                self.assertEqual(report["unresolved"][0]["repo"], "extra")
                self.assertEqual(report["ok"], 2)

    def test_standard_github_origin_forms_still_resolve(self) -> None:
        for origin in ("https://github.com/Owner/extra.git", "https://GITHUB.COM/owner/EXTRA",
                       "git@github.com:Owner/extra.git", "ssh://git@github.com/Owner/extra.git",
                       "ssh://git@github.com:22/Owner/extra.git", "https://github.com:443/Owner/extra.git"):
            with self.subTest(origin=origin):
                git(self.extra, "remote", "set-url", "origin", origin)
                self.assertEqual(self.run_check()["status"], "ok")

    def test_rejected_origins_do_not_echo_credentials(self) -> None:
        for origin in ("https://synthetic-secret@mirror.invalid/Owner/extra.git?secret=private-value",
                       "https://synthetic-secret@github.com/Owner/extra.git?secret=private-value"):
            with self.subTest(origin=origin):
                git(self.extra, "remote", "set-url", "origin", origin)
                report = self.run_check()
                self.assertEqual(report["status"], "partial")
                diagnostic = json.dumps(report)
                self.assertNotIn("synthetic-secret", diagnostic)
                self.assertNotIn("private-value", diagnostic)



if __name__ == "__main__":
    unittest.main()
