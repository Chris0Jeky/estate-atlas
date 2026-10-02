"""Canonical file input has one strict decoding and validation boundary."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from estate_atlas import cli, docs, model, render

ROOT = Path(__file__).resolve().parent.parent
SHOP = ROOT / "examples" / "shop" / "atlas.json"


class InputTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "atlas.json"
        self.doc_path = Path(temp.name) / "README.md"
        self.doc_path.write_text("\n".join(
            f"<!-- atlas:begin {name} -->\n<!-- atlas:end {name} -->"
            for name in docs.BLOCK_NAMES), encoding="utf-8")
        self.doc = model.load(SHOP)
        self.doc["components"][0]["title"] = "Café storefront"
        self.raw = json.dumps(self.doc, ensure_ascii=False).encode("utf-8")

    def commands(self):
        p = str(self.path)
        return [
            ["validate", p], ["render", "html", p], ["render", "mermaid", p],
            ["explain", p, self.doc["components"][0]["id"], "--json"],
            ["tour", p], ["tour", p, "--md"],
            ["docs", "write", p, "--doc", str(self.doc_path)],
            ["docs", "check", p, "--doc", str(self.doc_path)],
        ]

    def test_subprocess_plain_and_bom_outputs_match(self):
        outputs = []
        for prefix in (b"", b"\xef\xbb\xbf"):
            self.path.write_bytes(prefix + self.raw)
            results = []
            for command in self.commands():
                with self.subTest(bom=bool(prefix), command=command[0:2]):
                    result = subprocess.run(
                        [sys.executable, "-m", "estate_atlas", *command], cwd=ROOT,
                        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                        capture_output=True, check=False)
                    stderr = result.stderr.decode("utf-8").replace("\r\n", "\n")
                    stdout = result.stdout.decode("utf-8").replace("\r\n", "\n")
                    self.assertEqual(result.returncode, 0, stderr)
                    results.append((stdout, stderr))
            results.append(self.doc_path.read_bytes())
            results.append(self.doc_path.with_name("atlas-glance.svg").read_bytes())
            outputs.append(results)
        self.assertEqual(outputs[0], outputs[1])
        self.assertIn("Café storefront", outputs[0][1][0])

    def test_normalizers_never_reopen_after_validation(self):
        for loader in (cli._parsed, docs.load_atlas):
            with self.subTest(loader=loader.__module__):
                self.path.write_bytes(self.raw)
                validate = model.validate
                read_bytes = Path.read_bytes
                reads = []

                def tracked_read(path):
                    if path == self.path:
                        reads.append(path)
                        if len(reads) > 1:
                            self.fail("atlas reopened after validated read")
                    return read_bytes(path)

                def replace_after_validation(doc):
                    validate(doc)
                    self.path.write_bytes(b'{"schema":"replaced"}')

                with mock.patch.object(model, "validate", side_effect=replace_after_validation), \
                        mock.patch.object(Path, "read_bytes", tracked_read):
                    parsed = loader(self.path)
                self.assertEqual(reads, [self.path])
                self.assertEqual(parsed, render.parse_atlas(self.doc))
                self.assertEqual(self.path.read_bytes(), b'{"schema":"replaced"}')

    def test_strict_json_readers_share_bom_and_rejections(self):
        for reader, error in ((model.read_json_file, model.AtlasError),
                              (render.read_json_file, render.AtlasError)):
            for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}',
                        b'{"x":-Infinity}', b'{"x":"\xff"}', b'\xff\xfe{}'):
                for prefix in (b"", b"\xef\xbb\xbf"):
                    with self.subTest(reader=reader.__module__, raw=raw, prefix=prefix):
                        self.path.write_bytes(prefix + raw)
                        with self.assertRaises(error):
                            reader(self.path)
            self.path.write_bytes(b'\xef\xbb\xbf{"x":"caf\xc3\xa9"}')
            self.assertEqual(reader(self.path), {"x": "café"})

    def test_canonical_commands_preserve_validation_restrictions(self):
        invalid = [b"[]", b'{"x":1,"x":2}', b'{"x":NaN}', b'\xff',
                   b" " * (model.MAX_ATLAS_BYTES + 1)]
        for field, value in (("extra", True), ("schema", "estate-atlas@1"),
                             ("components", {})):
            invalid.append(json.dumps({**self.doc, field: value}).encode("utf-8"))
        bad_status = json.loads(self.raw)
        bad_status["components"][0]["status"] = "unknown"
        invalid.append(json.dumps(bad_status).encode("utf-8"))
        for raw in invalid:
            self.path.write_bytes(raw)
            for command in self.commands():
                with self.subTest(raw=raw[:40], command=command[0:2]), \
                        contextlib.redirect_stdout(io.StringIO()), \
                        contextlib.redirect_stderr(io.StringIO()):
                    self.assertEqual(cli.main(command), 2)


if __name__ == "__main__":
    unittest.main()
