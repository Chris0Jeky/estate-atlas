"""Full-suite CI must execute real tests and reject empty discovery."""
from __future__ import annotations

import importlib.util
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/run_tests.py"


class CIRunnerTests(unittest.TestCase):
    def run_fixture(self, body):
        self.assertTrue(SCRIPT.is_file(), "the full-suite runner must exist")
        spec = importlib.util.spec_from_file_location("atlas_ci_runner", SCRIPT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        output = io.StringIO()
        old_path = sys.path[:]
        try:
            with tempfile.TemporaryDirectory() as directory:
                if body is not None:
                    (Path(directory) / "test_ci_fixture_sample.py").write_text(body, encoding="utf-8")
                code = module.run_tests(Path(directory), stream=output)
        finally:
            sys.path[:] = old_path
            sys.modules.pop("test_ci_fixture_sample", None)
        return code, output.getvalue()

    def test_empty_discovery_is_an_error(self):
        code, output = self.run_fixture(None)
        self.assertEqual(code, 2)
        self.assertIn("no tests discovered", output)

    def test_passing_tests_execute(self):
        code, output = self.run_fixture(
            "import unittest\nclass Sample(unittest.TestCase):\n"
            "    def test_actual_execution(self):\n        self.assertEqual(2 + 2, 4)\n")
        self.assertEqual(code, 0)
        self.assertIn("test_actual_execution", output)
        self.assertIn("Ran 1 test", output)

    def test_failed_assertions_fail_the_runner(self):
        code, output = self.run_fixture(
            "import unittest\nclass Sample(unittest.TestCase):\n"
            "    def test_actual_failure(self):\n        self.fail('intentional failure')\n")
        self.assertEqual(code, 1)
        self.assertIn("intentional failure", output)

    def test_discovery_import_errors_fail_the_runner(self):
        code, output = self.run_fixture("raise RuntimeError('intentional import error')\n")
        self.assertEqual(code, 1)
        self.assertIn("intentional import error", output)

    def test_documented_platform_skips_remain_visible(self):
        code, output = self.run_fixture(
            "import unittest\nclass Sample(unittest.TestCase):\n"
            "    @unittest.skip('platform boundary')\n"
            "    def test_skipped(self):\n        pass\n")
        self.assertEqual(code, 0)
        self.assertIn("skipped=1", output)
        self.assertIn("platform boundary", output)


class CIWorkflowTests(unittest.TestCase):
    @staticmethod
    def assert_gate(text):
        assert re.search(r"^        run: python scripts/run_tests\.py$", text, re.M)
        assert "          fetch-depth: 0\n" in text
        assert "          persist-credentials: false\n" in text
        assert "permissions:\n  contents: read\n" in text
        assert "      fail-fast: false\n" in text
        assert "os: ubuntu-latest" in text and 'python: "3.11"' in text
        assert "os: windows-latest" in text and 'python: "3.13"' in text
        assert "  pull_request:\n" in text and "  push:\n" in text
        assert not re.search(r"^\s*(if|continue-on-error|paths|paths-ignore):", text, re.M)
        assert "pull_request_target:" not in text
        actions = re.findall(r"^      - uses: ([^\n]+)$", text, re.M)
        assert len(actions) == 2
        assert all(re.match(r"actions/(checkout|setup-python)@[0-9a-f]{40}(?: |$)", action)
                   for action in actions)

    def test_workflow_runs_full_suite_on_both_platforms(self):
        path = ROOT / ".github/workflows/tests.yml"
        self.assertTrue(path.is_file(), "a full-suite workflow must exist")
        self.assert_gate(path.read_text(encoding="utf-8"))

    def test_gate_assertions_reject_false_green_mutations(self):
        path = ROOT / ".github/workflows/tests.yml"
        self.assertTrue(path.is_file(), "a full-suite workflow must exist")
        text = path.read_text(encoding="utf-8")
        for mutation in (text.replace("run: python", "run: echo python"),
                         text.replace("run: python", "# run: python"),
                         text.replace("fetch-depth: 0", "fetch-depth: 1"),
                         text.replace("contents: read", "contents: write"),
                         text.replace("run: python", "if: false\n        run: python")):
            with self.subTest(mutation=mutation):
                with self.assertRaises(AssertionError):
                    self.assert_gate(mutation)


if __name__ == "__main__":
    unittest.main()
