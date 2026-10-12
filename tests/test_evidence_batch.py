"""Repository batching preserves evidence meaning, literal paths and binary framing."""
from __future__ import annotations

import copy
import os
import subprocess
import unittest
from unittest.mock import patch

from estate_atlas import check, model
from tests.test_check import CheckFixture, HOST, git


def batch_module():
    from estate_atlas import _evidence_batch
    return _evidence_batch


class BatchEvidenceTests(CheckFixture):
    def document(self, files):
        doc = copy.deepcopy(self.doc)
        for component in doc['components']:
            component['status'] = 'planned'
            component['surfaces'] = []
            component['evidence'] = []
        for item in doc['contracts'] + doc['flows']:
            item['status'] = 'planned'
            item['evidence'] = []
        for flow in doc['flows']:
            flow['gap'] = 'Not built.'
        doc['components'][0]['evidence'] = [{'repo': 'alpha', 'path': name} for name in files]
        return doc

    def files(self, count=48, same=False):
        names = [f'evidence-{i:03}.txt' for i in range(count)]
        for i, name in enumerate(names):
            (self.alpha / name).write_bytes(b'same\n' if same else f'anchor {i}\n'.encode())
        git(self.alpha, 'add', '-A')
        git(self.alpha, 'commit', '-qm', 'batch fixture')
        git(self.alpha, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
        return names

    def test_many_distinct_files_use_one_metadata_and_one_content_process(self):
        names = self.files()
        with patch.object(check, '_git', wraps=check._git) as calls:
            report = self.run_check(self.document(names))
        self.assertEqual((report['status'], report['checked'], report['ok']), ('ok', 48, 48))
        self.assertEqual(len(calls.call_args_list), 5)
        self.assertEqual([c.args[1] for c in calls.call_args_list].count('ls-tree'), 1)
        self.assertEqual([c.args[1] for c in calls.call_args_list].count('cat-file'), 1)

    def test_repeated_references_and_shared_blobs_are_counted_but_not_reread(self):
        names = self.files(12, same=True)
        with patch.object(check, '_git', wraps=check._git) as calls:
            report = self.run_check(self.document(names * 2))
        self.assertEqual(report['ok'], 24)
        contents = [c for c in calls.call_args_list if c.args[1] == 'cat-file']
        self.assertEqual(len(contents), 1)
        self.assertEqual(len(contents[0].kwargs['input'].splitlines()), 1)

    def test_complete_report_equals_single_file_reference_path(self):
        names = self.files(3)
        doc = self.document(names + ['missing.txt', 'evidence-000.txt'])
        doc['components'][0]['evidence'][0]['anchor'] = 'absent anchor'
        doc['flows'][1]['expect'] = [{'repo': 'alpha', 'path': names[1]}]
        doc['vocabularies'] = [{'id': 'words', 'title': 'Words', 'owner': 'tool',
                               'source': {'repo': 'alpha', 'path': names[2], 'each': True},
                               'terms': [{'term': 'missing', 'means': 'Missing word.'}]}]
        # Both paths receive the same prevalidated document; no source metadata is rewritten.
        model.validate(doc)
        repos = check.repositories(doc, {}, HOST)
        actual = check.check(doc, repos, HOST)
        def individual(path, commit, paths):
            return {name: check._blob(path, commit, name) for name in paths}
        with patch.object(check, '_batch_blobs', side_effect=individual):
            expected = check.check(doc, repos, HOST)
        self.assertEqual(actual, expected)
        self.assertEqual(actual['status'], 'drift')
        self.assertEqual(actual['promotable'], [{'owner': 'flows.sink-cloud', 'refs': 1}])

    def test_directory_and_descendant_do_not_hide_each_other(self):
        module = batch_module()
        result = module.read_many(str(self.extra), 'HEAD', ['docs', 'docs/SINK.md'], check._git, 1000)
        self.assertEqual(result['docs'], (None, 'not a regular file at origin'))
        self.assertEqual(result['docs/SINK.md'], (b'The sink reads tool.out/1.\n', ''))

    def test_nested_checkout_uses_its_own_relative_paths(self):
        result = batch_module().read_many(str(self.extra / 'docs'), 'HEAD', ['SINK.md'], check._git, 1000)
        self.assertEqual(result, {'SINK.md': (b'The sink reads tool.out/1.\n', '')})

    def test_literal_pathspec_names_cannot_expand_or_be_reinterpreted(self):
        # Index-only entries also exercise names Windows cannot check out, without OS path creation.
        names = [':(top)claim.md', '*.txt', 'notes[1].md', 'café note.md']
        for name in names:
            oid = subprocess.check_output(['git', 'hash-object', '-w', '--stdin'], cwd=self.alpha,
                                          input=name.encode()).decode().strip()
            git(self.alpha, 'update-index', '--add', '--cacheinfo', '100644,' + oid + ',' + name)
        git(self.alpha, 'commit', '-qm', 'literal index fixture')
        with patch.dict(os.environ, {'GIT_GLOB_PATHSPECS': '1', 'GIT_ICASE_PATHSPECS': '1',
                                     'GIT_LITERAL_PATHSPECS': '0'}):
            result = batch_module().read_many(str(self.alpha), 'HEAD', names, check._git, 1000)
        self.assertEqual(result, {name: (name.encode(), '') for name in names})

    def test_path_count_and_argument_budgets_split_without_losing_files(self):
        module = batch_module()
        names = self.files(5)
        for count, budget in ((2, 8192), (64, 25)):
            with self.subTest(count=count, budget=budget), patch.object(module, 'MAX_PATHS', count), \
                    patch.object(module, 'MAX_PATH_BYTES', budget), patch.object(check, '_git', wraps=check._git) as calls:
                result = module.read_many(str(self.alpha), 'HEAD', names, check._git, 1000)
            self.assertTrue(all(value[0] is not None for value in result.values()))
            metadata = [c for c in calls.call_args_list if c.args[1] == 'ls-tree']
            self.assertGreater(len(metadata), 1)
            for call in metadata:
                paths = call.args[call.args.index('--') + 1:]
                self.assertLessEqual(len(paths), count)
                self.assertLessEqual(sum(len(p.encode('utf-8')) + 3 for p in paths), budget)

    def test_content_budget_splits_and_rejects_oversized_before_fetch(self):
        module = batch_module()
        names = self.files(3)
        with patch.object(module, 'MAX_CONTENT_BYTES', 10), patch.object(check, '_git', wraps=check._git) as calls:
            result = module.read_many(str(self.alpha), 'HEAD', names, check._git, 1000)
        self.assertTrue(all(value[0] is not None for value in result.values()))
        self.assertEqual(sum(c.args[1] == 'cat-file' for c in calls.call_args_list), 3)
        with patch.object(check, '_git', wraps=check._git) as calls:
            result = module.read_many(str(self.alpha), 'HEAD', names, check._git, 2)
        self.assertTrue(all(value[0] is None and 'larger than' in value[1] for value in result.values()))
        self.assertFalse(any(c.args[1] == 'cat-file' for c in calls.call_args_list))

    def test_no_cache_leaks_across_checks_after_origin_changes(self):
        names = self.files(2)
        doc = self.document(names)
        doc['components'][0]['evidence'][0]['anchor'] = 'anchor 0'
        self.assertEqual(self.run_check(doc)['status'], 'ok')
        (self.alpha / names[0]).write_bytes(b'changed\n')
        git(self.alpha, 'add', '-A')
        git(self.alpha, 'commit', '-qm', 'change origin')
        git(self.alpha, 'update-ref', 'refs/remotes/origin/main', 'HEAD')
        self.assertEqual(self.run_check(doc)['status'], 'drift')

    def test_worktree_does_not_prefetch_and_unresolved_expect_stays_optional(self):
        names = self.files(2)
        doc = self.document(names)
        doc['flows'][1]['expect'] = [{'repo': 'extra', 'path': 'absent.md'}]
        doc['repos']['extra']['paths'] = {}
        with patch.object(check, '_batch_blobs', side_effect=AssertionError('no Git blob read')):
            result = check.check(doc, check.repositories(doc, {}, HOST, worktree=True), HOST)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['unresolved'], [])
        self.assertEqual(result['promotable'], [])

    def test_partial_clone_does_not_request_any_evidence_objects(self):
        names = self.files(2)
        git(self.alpha, 'config', 'remote.origin.promisor', 'true')
        with patch.object(check, '_batch_blobs', side_effect=AssertionError('must not fetch objects')):
            result = self.run_check(self.document(names))
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['checked'], 0)


class BatchFramingTests(unittest.TestCase):
    def read(self, output, *, listing=None, code=0):
        module = batch_module()
        one, two = b'a' * 40, b'b' * 40
        listing = listing if listing is not None else (
            b'100644 blob ' + one + b' 4\tone.md\0' + b'100644 blob ' + two + b' 0\ttwo.md\0')
        calls = []
        def run(path, *args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, listing, b'') if args[0] == 'ls-tree' else \
                subprocess.CompletedProcess(args, code, output, b'')
        return module.read_many('.', 'HEAD', ['one.md', 'two.md'], run, 1000), calls

    def valid(self):
        return b'a' * 40 + b' blob 4\n\0\nxy\n' + b'b' * 40 + b' blob 0\n\n'

    def test_empty_and_binary_content_frames_match_exact_lengths(self):
        result, calls = self.read(self.valid())
        self.assertEqual(result, {'one.md': (b'\0\nxy', ''), 'two.md': (b'', '')})
        self.assertEqual(calls[-1], (('cat-file', '--batch'), {'input': b'a' * 40 + b'\n' + b'b' * 40 + b'\n'}))

    def test_wrong_ids_types_sizes_and_trailing_bytes_refuse_the_entire_group(self):
        good = self.valid()
        bad = [good[:-1], good + b'extra', good.replace(b'blob 0', b'tree 0'),
               good.replace(b'blob 4', b'blob 5'), good.replace(b'blob 4', b'blob -1'),
               good.replace(b'b' * 40, b'c' * 40), good.replace(b'blob 0\n\n', b'missing\n'),
               good.replace(b'blob 4', b'blob 999999999999999999999')]
        for output in bad:
            with self.subTest(output=output):
                result, _ = self.read(output)
                self.assertTrue(all(value[0] is None and value[1] for value in result.values()))

    def test_failed_content_command_does_not_accept_even_valid_stdout(self):
        result, _ = self.read(self.valid(), code=1)
        self.assertTrue(all(value == (None, 'git cat-file failed') for value in result.values()))

    def test_malformed_or_truncated_tree_metadata_never_reads_content(self):
        oid = b'a' * 40
        for listing in (b'broken\0', b'100644 blob ' + oid + b' 4\tone.md',
                        b'100644 blob ' + oid + b' -1\tone.md\0',
                        (b'100644 blob ' + oid + b' 4\tone.md\0') * 2,
                        b'100644 blob invalid 4\tone.md\0',
                        b'100644 blob ' + oid + b' 4\tunrequested.md\0'):
            with self.subTest(listing=listing):
                result, calls = self.read(self.valid(), listing=listing)
                self.assertTrue(all(value[0] is None for value in result.values()))
                self.assertEqual(len(calls), 1)

    def test_links_gitlinks_and_missing_entries_are_not_requested_as_content(self):
        oid = b'a' * 40
        listing = b'120000 blob ' + oid + b' 4\tone.md\0' + b'160000 commit ' + oid + b' -\ttwo.md\0'
        result, calls = self.read(b'', listing=listing)
        self.assertTrue(all(value == (None, 'not a regular file at origin') for value in result.values()))
        self.assertEqual(len(calls), 1)
        result, calls = self.read(b'', listing=b'')
        self.assertTrue(all(value == (None, 'file is absent at origin') for value in result.values()))
        self.assertEqual(len(calls), 1)

    def test_one_overbudget_argument_is_refused_without_spawning_git(self):
        module = batch_module()
        def forbidden(*args, **kwargs):
            raise AssertionError('overbudget argument reached Git')
        with patch.object(module, 'MAX_PATH_BYTES', 8):
            result = module.read_many('.', 'HEAD', ['long-name.txt'], forbidden, 1000)
        self.assertEqual(result, {'long-name.txt': (None, 'evidence path exceeds Git argument budget')})

    def test_metadata_timeout_retains_failure_not_absence(self):
        module = batch_module()
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired('git', 30)
        result = module.read_many('.', 'HEAD', ['one.md'], timeout, 1000)
        self.assertEqual(result, {'one.md': (None, 'git failed: TimeoutExpired')})


if __name__ == '__main__':
    unittest.main()
