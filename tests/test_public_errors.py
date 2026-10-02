"""Public tours must not disclose fictional private inputs when they fail."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from estate_atlas import cli, model, overlay

ROOT = Path(__file__).resolve().parent.parent
ATLAS = ROOT / "examples" / "shop" / "atlas.json"
OVERLAY = ROOT / "tests" / "fixtures" / "shop-overlay.json"
MARKER = "cobalt-vault-sentinel"


def run(*args: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(args))
    return code, out.getvalue(), err.getvalue()


class PublicErrorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def write(self, name: str, obj) -> str:
        path = self.directory / name
        path.write_text(json.dumps(obj), encoding="utf-8")
        return str(path)

    def refused(self, *args: str):
        target = self.directory / "output.md"
        target.write_text("previous public output", encoding="utf-8")
        code, out, err = run("tour", *args, "--out", str(target))
        self.assertEqual((code, out), (2, ""))
        self.assertIn("public tour refused", err)
        self.assertNotIn(MARKER, err)
        self.assertNotIn(str(self.directory), err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(target.read_text(encoding="utf-8"), "previous public output")

    def test_denied_term_is_not_repeated_in_stderr(self):
        spec = json.loads(OVERLAY.read_text(encoding="utf-8"))
        spec["denylist"] = [MARKER]
        spec["components"]["api"]["summary"] = "Serves " + MARKER
        path = self.write("overlay.json", spec)
        for mode in ([], ["--md"]):
            with self.subTest(mode=mode):
                self.refused(str(ATLAS), "--overlay", path, *mode)

    def test_invalid_atlas_and_overlay_keys_are_private_diagnostics(self):
        doc = json.loads(ATLAS.read_text(encoding="utf-8"))
        spec = json.loads(OVERLAY.read_text(encoding="utf-8"))
        for obj, other, atlas in ((doc, str(OVERLAY), True), (spec, str(ATLAS), False)):
            obj[MARKER] = "fictional private value"
            path = self.write(MARKER + ".json", obj)
            with self.subTest(atlas=atlas):
                self.refused(path if atlas else other, "--overlay", other if atlas else path)

    def test_missing_private_path_is_not_repeated_in_stderr(self):
        self.refused(str(ATLAS), "--overlay", str(self.directory / (MARKER + ".json")))

    def test_parser_error_does_not_echo_private_arguments(self):
        self.refused(str(ATLAS), "--overlay=" + str(OVERLAY), "--unknown", MARKER)

    def test_valid_public_tour_is_still_deterministic(self):
        for mode in ([], ["--md"]):
            args = ("tour", str(ATLAS), "--overlay", str(OVERLAY), *mode)
            first = run(*args)
            self.assertEqual(first[0], 0, first[2])
            self.assertEqual(first[2], "")
            self.assertTrue(first[1])
            self.assertEqual(first, run(*args))


class TrafficInputTests(unittest.TestCase):
    def test_malformed_overlay_traffic_is_not_converted_to_empty_output(self):
        spec = overlay.load_overlay(OVERLAY)
        malformed = [
            {"flows": [1, 2]},
            {"flows": {"web-to-api": 1}},
            {"crosschecks": ["bad"]},
            {"crosschecks": {"silent": "web-to-api"}},
            {"crosschecks": {"silent": [3]}},
            {"crosschecks": {"off_status": 3}},
            {"crosschecks": {"off_status": ["web-to-api"]}},
            {"crosschecks": {"off_status": [{}]}},
        ]
        for fields in malformed:
            doc = {"schema": "estate-atlas-traffic@1", **fields}
            with self.subTest(fields=fields):
                with self.assertRaises(model.AtlasError):
                    overlay.apply_overlay_traffic(doc, spec)
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "traffic.json"
                    path.write_text(json.dumps(doc), encoding="utf-8")
                    code, out, err = run("tour", str(ATLAS), "--overlay", str(OVERLAY),
                                         "--traffic", str(path))
                    self.assertEqual((code, out), (2, ""))
                    self.assertIn("public tour refused", err)

    def test_malformed_crosschecks_fail_explicitly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "traffic.json"
            for cross in (["bad"], {"off_status": 3}):
                path.write_text(json.dumps({"schema": "estate-atlas-traffic@1", "crosschecks": cross}),
                                encoding="utf-8")
                for args in (("explain", str(ATLAS), "api"), ("tour", str(ATLAS))):
                    with self.subTest(cross=cross, args=args):
                        code, out, err = run(*args, "--traffic", str(path))
                        self.assertEqual((code, out), (2, ""))
                        self.assertIn("crosschecks", err)
                        self.assertNotIn("Traceback", err)
