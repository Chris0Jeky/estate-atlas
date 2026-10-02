"""Fictional file/process controls for publication and record parsing."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from estate_atlas import docs, model, render

ROOT = Path(__file__).resolve().parents[1]
SHOP = ROOT / "examples/shop/atlas.json"


class BoundaryTests(unittest.TestCase):
    def test_markdown_fields_are_literal_text(self):
        doc = model.load(SHOP)
        payload = '<img src=x onerror=alert(1)> [open](javascript:alert%281%29) `code` | cell'
        doc["contracts"][0]["title"] = payload
        doc["layers"][0]["title"] = payload
        doc["layers"][0]["summary"] = '<!-- atlas:end layers --> ' + payload
        model.validate(doc)
        atlas = render.parse_atlas(doc)
        for block in (docs.owners_block(atlas), docs.layers_block(atlas)):
            self.assertNotIn('<img src=x', block)
            self.assertNotIn('[open](javascript:', block)
            self.assertIn('&lt;img', block)
            self.assertIn(r'\[open\]', block)
        self.assertNotIn('<!-- atlas:end layers -->', docs.layers_block(atlas))
        self.assertNotIn('<img src=x', docs.glance_svg(atlas))
        self.assertNotIn('<img src=x', render.render_html(atlas))
        with tempfile.TemporaryDirectory() as temp:
            source, target = Path(temp)/'atlas.json', Path(temp)/'ARCH.md'
            source.write_text(json.dumps(doc), encoding='utf-8')
            target.write_text('\n'.join(f'<!-- atlas:begin {name} -->\n<!-- atlas:end {name} -->'
                                        for name in docs.BLOCK_NAMES), encoding='utf-8')
            self.assertEqual(docs.write(source, target)[0], 0)
            saved = target.read_bytes()
            svg = target.with_name('atlas-glance.svg').read_bytes()
            self.assertEqual(docs.check_docs(source, target)[0], 0)
            self.assertEqual(docs.write(source, target)[0], 0)
            self.assertEqual(target.read_bytes(), saved)
            self.assertEqual(target.with_name('atlas-glance.svg').read_bytes(), svg)

    def test_strict_record_files_refuse_without_replacing_output(self):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        with tempfile.TemporaryDirectory(prefix="atlas boundary ") as temp:
            root = Path(temp)
            output = root / "traffic.json"
            journal = root / "journal.jsonl"
            links = root / "links.json"
            cases = [
                (journal, '--journal', '{"at":1,"verb":"wrong","verb":"enqueue"}'),
                (journal, '--journal', '{"at":NaN}'),
                (journal, '--events', '{"at":Infinity}'),
                (journal, '--journal', '{"at":1e999}'),
                (links, '--links', '[{"from":"service:web","from":"service:api"}]'),
                (links, '--links', '[{"at":-Infinity}]'),
            ]
            for path, option, text in cases:
                with self.subTest(option=option, text=text):
                    output.write_bytes(b'existing valid artifact\n')
                    path.write_text(text, encoding="utf-8")
                    result = subprocess.run([sys.executable, '-m', 'estate_atlas', 'route',
                                             str(SHOP), option, str(path), '--out', str(output)],
                                            cwd=ROOT, env=env, capture_output=True)
                    self.assertEqual(result.returncode, 2, result.stderr.decode('utf-8'))
                    self.assertEqual(result.stdout, b'')
                    self.assertNotIn(b'Traceback', result.stderr)
                    self.assertEqual(output.read_bytes(), b'existing valid artifact\n')
            # Bad timestamps and non-object links are deliberately skipped, not JSON errors.
            journal.write_text('{"at":"unknown"}\n', encoding='utf-8')
            result = subprocess.run([sys.executable, '-m', 'estate_atlas', 'route', str(SHOP),
                                     '--journal', str(journal)], cwd=ROOT, env=env, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['coverage']['records'], 0)

    def test_overflowing_json_number_is_not_silently_nonfinite(self):
        for text in ('{"value":1e999}', '{"value":-1e999}'):
            with self.subTest(text=text), self.assertRaises(model.AtlasError):
                model.strict_json(text)
        self.assertEqual(model.strict_json('{"value":1e3}'), {'value': 1000.0})

    def test_unrepresentable_integer_timestamp_refuses_without_replacing_output(self):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        good = {"at": 1_790_000_000, "producer": "api.main", "verb": "enqueue",
                "actor": "service:api", "subject": "service:worker"}
        with tempfile.TemporaryDirectory(prefix="atlas timestamp ") as temp:
            root = Path(temp)
            output = root / "traffic.json"
            for source in ("journal", "events", "links"):
                path = root / (source + (".json" if source == "links" else ".jsonl"))
                for value in (10**1000, -(10**1000)):
                    rows = [good, dict(good, at=value)]
                    path.write_text(json.dumps(rows) if source == "links" else
                                    "\n".join(json.dumps(row) for row in rows), encoding="utf-8")
                    for now in ([], ["--now", "1790000000"]):
                        with self.subTest(source=source, negative=value < 0, now=now):
                            output.write_bytes(b"existing valid artifact\n")
                            result = subprocess.run(
                                [sys.executable, "-m", "estate_atlas", "route", str(SHOP),
                                 "--" + source, str(path), *now, "--out", str(output)],
                                cwd=ROOT, env=env, capture_output=True)
                            self.assertEqual(result.returncode, 2, result.stderr)
                            self.assertEqual(result.stdout, b"")
                            self.assertEqual(output.read_bytes(), b"existing valid artifact\n")
                            self.assertEqual(result.stderr.splitlines(),
                                             [b"estate-atlas: timestamp is outside the supported numeric range"])

    def test_public_markdown_title_is_escaped_and_leak_checked(self):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        spec = json.loads((ROOT/'tests/fixtures/shop-overlay.json').read_text(encoding='utf-8'))
        spec['title'] = '<img src=x onerror=alert(1)> [open](javascript:alert%281%29)'
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'overlay.json'
            path.write_text(json.dumps(spec), encoding='utf-8')
            result = subprocess.run([sys.executable, '-m', 'estate_atlas', 'tour', str(SHOP),
                                     '--md', '--overlay', str(path)], cwd=ROOT, env=env, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            text = result.stdout.decode('utf-8')
            self.assertNotIn('<img src=x', text)
            self.assertNotIn('[open](javascript:', text)
            self.assertIn('&lt;img', text)


if __name__ == '__main__':
    unittest.main()
