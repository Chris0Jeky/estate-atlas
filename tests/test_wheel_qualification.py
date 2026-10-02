"""Lightweight regression tests for the installed-consumer harness wiring."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WheelQualificationTests(unittest.TestCase):
    def runner(self):
        path = ROOT / "scripts/qualify_wheel.py"
        self.assertTrue(path.is_file(), "the offline wheel qualification runner is missing")
        return load_file("qualify_wheel", path)

    def test_installed_command_runs_outside_source_without_python_path_variables(self):
        consumer = load_file("consumer_qualification", ROOT / "tests/test_consumer_qualification.py")
        command = [sys.executable, "-c", (
            "import json,os,sys; print(json.dumps({'args':sys.argv[1:],"
            "'cwd':os.getcwd(),'path':os.getenv('PYTHONPATH'),'home':os.getenv('PYTHONHOME')}))"
        )]
        with patch.dict(os.environ, {"ATLAS_QUALIFICATION_COMMAND": json.dumps(command),
                                     "ATLAS_QUALIFICATION_ROOT": str(ROOT),
                                     "PYTHONPATH": "source poison", "PYTHONHOME": "home poison"}):
            case = consumer.ConsumerQualificationTests("test_workspace_and_stale_tour_through_positional_cli")
            case.setUp()
            try:
                result = case.cli("--help")
                self.assertEqual(result.returncode, 0, result.stderr)
                observed = json.loads(result.stdout)
                self.assertEqual(observed["args"], ["--help"])
                self.assertEqual(Path(observed["cwd"]), case.directory)
                self.assertIsNone(observed["path"])
                self.assertIsNone(observed["home"])
            finally:
                case.doCleanups()

    def test_clean_environment_is_used_by_real_subprocess(self):
        runner = self.runner()
        env = runner.clean_environment({**os.environ, "PYTHONPATH": "poison", "PYTHONHOME": "poison",
                                        "PIP_FIND_LINKS": "https://example.invalid/wheels", "PIP_TARGET": "wrong location"})
        result = subprocess.run([sys.executable, "-c", "import os; print(any(n in os.environ for n in ['PYTHONPATH','PYTHONHOME','PIP_FIND_LINKS','PIP_TARGET']))"],
                                env=env, capture_output=True, check=True)
        self.assertEqual(result.stdout.strip(), b"False")

    def test_installed_identity_checks_survive_optimized_python(self):
        runner = self.runner()
        self.assertTrue(callable(getattr(runner, "validate_installed", None)), "installed proof needs explicit identity checks")
        with tempfile.TemporaryDirectory(prefix="atlas identity ") as temp:
            prefix = Path(temp) / "consumer env"
            site = prefix / "lib/site-packages"
            valid = {"prefix": str(prefix), "site_packages": str(site),
                     "module": str(site / "estate_atlas/__init__.py"), "runtime_requirements": []}
            code = "import runpy,json,sys; from pathlib import Path; runpy.run_path(sys.argv[1])['validate_installed'](json.loads(sys.argv[2]),Path(sys.argv[3]))"
            for change in ({}, {"prefix": str(Path(temp) / "wrong env")},
                           {"site_packages": str(Path(temp) / "outside site")},
                           {"module": str(Path(temp) / "source/estate_atlas/__init__.py")},
                           {"runtime_requirements": ["unexpected-dependency"]}):
                with self.subTest(change=change):
                    result = subprocess.run([sys.executable, "-O", "-c", code,
                                             str(ROOT / "scripts/qualify_wheel.py"), json.dumps({**valid, **change}), str(prefix)],
                                            capture_output=True, env=runner.clean_environment(), timeout=30)
                    self.assertEqual(result.returncode == 0, not bool(change), result.stderr)

    def test_empty_real_suite_receipt_and_missing_receipt_are_rejected(self):
        runner = self.runner()
        self.assertTrue(callable(getattr(runner, "validate_consumer_result", None)), "qualification needs nonempty known-suite validation")
        with tempfile.TemporaryDirectory(prefix="atlas empty suite ") as temp:
            root = Path(temp)
            script = root / "empty_consumer.py"
            script.write_text((ROOT / "tests/test_consumer_qualification.py").read_text(encoding="utf-8").replace(
                "    def test_", "    def proof_"), encoding="utf-8")
            receipt = root / "result.json"
            env = dict(runner.clean_environment(), ATLAS_QUALIFICATION_RESULT=str(receipt))
            result = subprocess.run([sys.executable, script], env=env, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(receipt.is_file(), "consumer runner must produce a structured result")
            observed = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(observed["tests_run"], 0)
            with self.assertRaisesRegex(ValueError, "suite"):
                runner.validate_consumer_result(observed, [{"command": ["example"]}])
            with self.assertRaisesRegex(ValueError, "suite"):
                runner.validate_consumer_result({}, [])

    def test_consumer_result_requires_all_known_tests_and_cli_commands(self):
        runner = self.runner()
        self.assertTrue(callable(getattr(runner, "validate_consumer_result", None)), "qualification needs nonempty known-suite validation")
        names = sorted(runner.CONSUMER_TEST_NAMES)
        valid = {"tests_run": len(names), "test_names": names, "successful": True,
                 "failures": 0, "errors": 0, "skipped": 0}
        runner.validate_consumer_result(valid, [{"command": ["example"]}])
        for change, commands in (({"test_names": names[:-1]}, [{"command": ["example"]}]),
                                 ({"skipped": 1}, [{"command": ["example"]}]),
                                 ({"errors": 1}, [{"command": ["example"]}]),
                                 ({"failures": 1}, [{"command": ["example"]}]), ({}, [])):
            with self.subTest(change=change, commands=commands), self.assertRaises(ValueError):
                runner.validate_consumer_result({**valid, **change}, commands)

    def test_installed_cli_records_actual_command_and_result(self):
        consumer = load_file("consumer_receipt", ROOT / "tests/test_consumer_qualification.py")
        command = [sys.executable, "-c", "import sys; print('receipt proof'); sys.exit(2)"]
        with tempfile.TemporaryDirectory(prefix="atlas command receipt ") as temp:
            log = Path(temp) / "commands.jsonl"
            with patch.dict(os.environ, {"ATLAS_QUALIFICATION_COMMAND": json.dumps(command),
                                         "ATLAS_QUALIFICATION_ROOT": str(ROOT),
                                         "ATLAS_QUALIFICATION_COMMAND_LOG": str(log)}):
                case = consumer.ConsumerQualificationTests("test_workspace_and_stale_tour_through_positional_cli")
                case.setUp()
                try:
                    result = case.cli("--help")
                    self.assertEqual(result.returncode, 2)
                    self.assertTrue(log.is_file(), "installed proof must record its actual CLI commands")
                    entry = json.loads(log.read_text(encoding="utf-8"))
                    self.assertEqual(entry["command"], [*command, "--help"])
                    self.assertEqual(entry["returncode"], 2)
                    self.assertEqual(entry["stdout"].strip(), "receipt proof")
                    self.assertEqual(entry["stderr"], "")
                finally:
                    case.doCleanups()

    def test_source_copy_excludes_git_and_build_products(self):
        runner = self.runner()
        with tempfile.TemporaryDirectory(prefix="atlas harness ") as temp:
            root = Path(temp)
            source = root / "source"
            source.mkdir()
            for name in (".git", "build", "dist", "pkg.egg-info", "__pycache__", ".venv"):
                (source / name).mkdir()
                (source / name / "ignored").write_text("ignored", encoding="utf-8")
            (source / "pyproject.toml").write_text("source", encoding="utf-8")
            runner.copy_source(source, root / "copy")
            self.assertEqual([p.name for p in (root / "copy").iterdir()], ["pyproject.toml"])
            self.assertTrue((source / ".git/ignored").is_file())

    def test_cached_http_wheel_is_identified_without_installing(self):
        runner = self.runner()
        with tempfile.TemporaryDirectory(prefix="atlas cache ") as temp:
            root = Path(temp)
            body = root / "cache.body"
            with zipfile.ZipFile(body, "w") as archive:
                archive.writestr("setuptools-80.0.0.dist-info/METADATA", "Name: setuptools\nVersion: 80.0.0\n")
                archive.writestr("setuptools-80.0.0.dist-info/WHEEL", "Wheel-Version: 1.0\nTag: py3-none-any\n")
                archive.writestr("setuptools/_vendor/packaging-25.0.dist-info/METADATA", "Name: packaging\nVersion: 25.0\n")
            (root / "junk.body").write_bytes(b"not a wheel")
            found = runner.discover_build_wheels([root])
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0]["path"], str(body))
            self.assertEqual(found[0]["filename"], "setuptools-80.0.0-py3-none-any.whl")

    def test_receipt_cannot_be_written_anywhere_under_a_git_checkout(self):
        runner = self.runner()
        with tempfile.TemporaryDirectory(prefix="atlas receipts ") as temp:
            root = Path(temp)
            (root / ".git").mkdir()
            with self.assertRaisesRegex(ValueError, "outside"):
                runner.require_outside_git(root / "ignored receipts/report.json")
            runner.require_outside_git(root.parent / "atlas receipt.json")


if __name__ == "__main__":
    unittest.main()
