"""File routing has byte budgets before decoding, independent of its row limit."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from estate_atlas import cli, model, traffic

ROOT = Path(__file__).resolve().parents[1]


class RecordByteLimitTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.path = self.root / 'rows.jsonl'
        self.doc = model.load(ROOT / 'examples/shop/atlas.json')
        for name, value in [('MAX_JSONL_LINE_BYTES', 64), ('MAX_JSONL_FILE_BYTES', 256),
                            ('MAX_LINKS_FILE_BYTES', 128)]:
            p = patch.object(traffic, name, value, create=True)
            p.start()
            self.addCleanup(p.stop)

    def test_one_oversized_valid_record_is_rejected_before_parsing(self):
        self.path.write_bytes(json.dumps({'value': 'a' * 80}).encode())
        with self.assertRaisesRegex(ValueError, r'rows.jsonl:1: line byte limit exceeded'):
            traffic._read_jsonl(self.path)

    def test_exact_line_limit_and_multibyte_characters(self):
        raw = json.dumps({'value': 'é' * 12}, ensure_ascii=False).encode()
        self.path.write_bytes(raw)
        with patch.object(traffic, 'MAX_JSONL_LINE_BYTES', len(raw), create=True):
            self.assertEqual(traffic._read_jsonl(self.path), [{'value': 'é' * 12}])
        with patch.object(traffic, 'MAX_JSONL_LINE_BYTES', len(raw) - 1, create=True):
            with self.assertRaisesRegex(ValueError, 'line byte limit'):
                traffic._read_jsonl(self.path)

    def test_total_file_budget_counts_blank_lines_and_bom(self):
        raw = b'\xef\xbb\xbf\n' + b' \n' * 50 + b'{"at":1}\n'
        self.path.write_bytes(raw)
        with patch.object(traffic, 'MAX_JSONL_FILE_BYTES', len(raw), create=True):
            self.assertEqual(traffic._read_jsonl(self.path), [{'at': 1}])
        with patch.object(traffic, 'MAX_JSONL_FILE_BYTES', len(raw) - 1, create=True):
            with self.assertRaisesRegex(ValueError, 'file byte limit exceeded'):
                traffic._read_jsonl(self.path)

    def test_total_file_budget_counts_many_small_records(self):
        self.path.write_bytes(b'{}\n' * 20)
        with patch.object(traffic, 'MAX_JSONL_FILE_BYTES', 59, create=True):
            with self.assertRaisesRegex(ValueError, 'file byte limit exceeded'):
                traffic._read_jsonl(self.path)

    def test_oversized_blank_line_is_not_exempt_from_line_budget(self):
        self.path.write_bytes(b' ' * 65 + b'\n{}')
        with self.assertRaisesRegex(ValueError, 'line byte limit exceeded'):
            traffic._read_jsonl(self.path)

    def test_all_newline_forms_keep_physical_line_numbers(self):
        for sep in (b'\n', b'\r\n', b'\r'):
            with self.subTest(sep=sep):
                self.path.write_bytes(b'\xef\xbb\xbf{}' + sep + b' ' + sep + b'{"at":2}')
                self.assertEqual(traffic._read_jsonl(self.path), [{}, {'at': 2}])
                self.path.write_bytes(b'{}' + sep + b' ' + sep + b'not json')
                with self.assertRaisesRegex(ValueError, 'rows.jsonl:3: not valid JSON'):
                    traffic._read_jsonl(self.path)

    def test_later_bom_is_not_treated_as_another_file_header(self):
        self.path.write_bytes(b'{}\n\xef\xbb\xbf{}')
        with self.assertRaisesRegex(ValueError, 'rows.jsonl:2: not valid JSON'):
            traffic._read_jsonl(self.path)

    def test_jsonl_reads_are_binary_bounded_and_do_not_use_iteration(self):
        testcase = self
        class Guarded(io.BytesIO):
            def __iter__(self):
                raise AssertionError('unbounded line iteration')
            def read(self, size=-1):
                testcase.assertGreater(size, 0)
                testcase.assertLessEqual(size, 65)
                return super().read(size)
            def readline(self, size=-1):
                testcase.assertGreater(size, 0)
                testcase.assertLessEqual(size, 65)
                return super().readline(size)
        stream = Guarded(b'{"text":"' + b'a' * 1000 + b'"}')
        def opened(path, mode='r', *args, **kwargs):
            self.assertEqual(mode, 'rb')
            return stream
        with patch.object(Path, 'open', opened):
            with self.assertRaisesRegex(ValueError, 'line byte limit exceeded'):
                traffic._read_jsonl(self.path)
        self.assertTrue(stream.closed)

    def test_links_exact_limit_and_overlimit_document(self):
        raw = b'[{"at":1}]'
        self.path.write_bytes(raw)
        with patch.object(traffic, 'MAX_LINKS_FILE_BYTES', len(raw), create=True):
            self.assertEqual(traffic.route_files(self.doc, links=self.path)['sources']['links']['records'], 1)
        with patch.object(traffic, 'MAX_LINKS_FILE_BYTES', len(raw) - 1, create=True):
            with self.assertRaisesRegex(ValueError, 'links file byte limit exceeded'):
                traffic.route_files(self.doc, links=self.path)

    def test_links_read_is_bounded_binary_and_does_not_use_read_text(self):
        testcase = self
        class Guarded(io.BytesIO):
            def read(self, size=-1):
                testcase.assertGreater(size, 0)
                testcase.assertLessEqual(size, 129)
                return super().read(size)
        stream = Guarded(b'[' + b' ' * 1000 + b']')
        def opened(path, mode='r', *args, **kwargs):
            self.assertEqual(mode, 'rb')
            return stream
        with patch.object(Path, 'open', opened), patch.object(Path, 'read_text', side_effect=AssertionError('unbounded read')):
            with self.assertRaisesRegex(ValueError, 'links file byte limit exceeded'):
                traffic.route_files(self.doc, links=self.path)
        self.assertTrue(stream.closed)

    def test_rejection_preserves_cli_output_and_names_the_source(self):
        out = self.root / 'traffic.json'
        for option in ('--journal', '--events', '--links'):
            with self.subTest(option=option):
                self.path.write_text(json.dumps([{'v': 'a' * 300}]) if option == '--links'
                                     else json.dumps({'v': 'a' * 300}), encoding='utf-8')
                out.write_bytes(b'previous result\n')
                stdout, stderr = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    code = cli.main(['route', str(ROOT / 'examples/shop/atlas.json'), option,
                                     str(self.path), '--out', str(out)])
                self.assertEqual(code, 2)
                self.assertEqual(stdout.getvalue(), '')
                self.assertIn('byte limit exceeded', stderr.getvalue())
                self.assertNotIn('Traceback', stderr.getvalue())
                self.assertEqual(out.read_bytes(), b'previous result\n')

    def test_row_limit_remains_independent_and_strict_json_is_preserved(self):
        self.path.write_bytes(b'{}\n{}\n')
        with patch.object(traffic, 'MAX_ROWS', 1):
            with self.assertRaisesRegex(ValueError, 'rows.jsonl:2: record limit exceeded'):
                traffic._read_jsonl(self.path)
        for raw in (b'{"a":1,"a":2}', b'{"at":NaN}', b'{"at":1e999}', b'{"a":"\xff"}'):
            self.path.write_bytes(raw)
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                traffic._read_jsonl(self.path)

    def test_chunk_boundaries_do_not_split_utf8_bom_or_crlf_records(self):
        from estate_atlas import _record_io
        raw = '\ufeff{"text":"é"}\r\n{}\r{}\n'.encode('utf-8')
        self.path.write_bytes(raw)
        for size in range(1, len(raw) + 1):
            with self.subTest(size=size), patch.object(_record_io, '_READ_CHUNK', size):
                self.assertEqual(traffic._read_jsonl(self.path), [{'text': 'é'}, {}, {}])

    def test_exact_line_limit_excludes_only_newline_bytes(self):
        raw = b'{"at":1}'
        for end in (b'', b'\n', b'\r', b'\r\n'):
            self.path.write_bytes(raw + end)
            with self.subTest(end=end), patch.object(traffic, 'MAX_JSONL_LINE_BYTES', len(raw)):
                self.assertEqual(traffic._read_jsonl(self.path), [{'at': 1}])

    def test_budget_errors_precede_json_parse_for_oversized_first_payload(self):
        self.path.write_bytes(b'[' + b' ' * 300 + b']')
        with patch.object(traffic, 'strict_json', side_effect=AssertionError('must not parse')):
            with self.assertRaisesRegex(ValueError, 'links file byte limit exceeded'):
                traffic.route_files(self.doc, links=self.path)
        self.path.write_bytes(b'{"v":"' + b'x' * 100 + b'"}')
        with patch.object(traffic, 'strict_json', side_effect=AssertionError('must not parse')):
            with self.assertRaisesRegex(ValueError, 'line byte limit exceeded'):
                traffic._read_jsonl(self.path)

    def test_interior_io_failure_is_reported_and_stream_is_closed(self):
        class Faulty(io.BytesIO):
            def read(self, size=-1):
                raise OSError('fixture read error')
        stream = Faulty()
        with patch.object(Path, 'open', return_value=stream):
            with self.assertRaisesRegex(ValueError, 'cannot read'):
                traffic._read_jsonl(self.path)
        self.assertTrue(stream.closed)
