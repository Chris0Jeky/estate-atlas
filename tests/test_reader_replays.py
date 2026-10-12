"""Conflicting cross-page replays fail closed; identical replays still deduplicate."""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from estate_atlas import rollup, traffic
from tests.test_source_health import DOC, JOURNAL, EVENT, NOW, empty, index


class ReaderReplayTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.history = rollup.TrafficHistory(Path(directory.name) / "history.db")
        self.addCleanup(self.history.close)
        self.cache = index(links=None)
        for module, name in ((traffic, "PAGE"), (rollup, "READ_PAGE")):
            patcher = patch.object(module, name, 2)
            patcher.start()
            self.addCleanup(patcher.stop)

    def readers(self, row, *, conflict=False):
        first = [dict(row, id=1), dict(row, id=2)]
        replay = dict(first[-1])
        if conflict:
            replay["at"] -= 1
        second = [replay, dict(row, id=3)]
        return lambda after, since: copy.deepcopy(first if after == 0 else second if after == 2 else [])

    def test_conflicting_replay_is_rejected_in_both_readers(self):
        for read in (lambda reader: self.cache._drain(reader, 0, 0),
                     lambda reader: self.history._paged(reader, 0)):
            with self.subTest(read=read):
                with self.assertRaisesRegex(ValueError, "conflicting source id 2 across reader pages"):
                    read(self.readers(JOURNAL, conflict=True))

    def test_identical_replays_are_deduplicated_in_both_readers(self):
        rows, cursor = self.cache._drain(self.readers(JOURNAL), 0, 0)
        self.assertEqual(([row["id"] for row in rows], cursor), ([1, 2, 3], 3))
        rows = self.history._paged(self.readers(JOURNAL), 0)
        self.assertEqual([row["id"] for row in rows], [1, 2, 3])

    def test_conflicting_replay_of_older_not_just_last_id_is_rejected(self):
        def reader(after, since):
            if after == 0:
                return [dict(JOURNAL, id=1), dict(JOURNAL, id=2)]
            return [dict(JOURNAL, id=1, verb="changed"), dict(JOURNAL, id=3)]
        for read in (lambda: self.cache._drain(reader, 0, 0), lambda: self.history._paged(reader, 0)):
            with self.subTest(read=read):
                with self.assertRaisesRegex(ValueError, "conflicting source id 1"):
                    read()

    def test_reused_mutable_reader_rows_do_not_rewrite_the_comparison_snapshot(self):
        for read in (lambda reader: self.cache._drain(reader, 0, 0),
                     lambda reader: self.history._paged(reader, 0)):
            row = copy.deepcopy(dict(EVENT, id=2))
            def reader(after, since):
                if after == 0:
                    return [dict(EVENT, id=1), row]
                row["nodes"].append("agent:new")
                return [row, dict(EVENT, id=3)]
            with self.subTest(read=read):
                with self.assertRaisesRegex(ValueError, "conflicting source id 2"):
                    read(reader)

    def test_identical_replay_only_page_terminates_without_extra_counts(self):
        calls = []
        def reader(after, since):
            calls.append(after)
            return [dict(JOURNAL, id=1), dict(JOURNAL, id=2)]
        for read in (lambda: self.cache._drain(reader, 0, 0)[0], lambda: self.history._paged(reader, 0)):
            calls.clear()
            self.assertEqual([row["id"] for row in read()], [1, 2])
            self.assertEqual(calls, [0, 2])

    def test_duplicate_fresh_ids_inside_one_page_still_fail(self):
        reader = lambda after, since: [dict(JOURNAL), dict(JOURNAL)]
        for read in (lambda: self.cache._drain(reader, 0, 0), lambda: self.history._paged(reader, 0)):
            with self.subTest(read=read):
                with self.assertRaisesRegex(ValueError, "duplicate source id in reader page"):
                    read()

    def test_live_conflict_does_not_ingest_partial_rows_or_advance_the_cursor(self):
        for source, row in (("journal", JOURNAL), ("events", EVENT)):
            with self.subTest(source=source):
                valid = [False]
                clock = [NOW]
                faulty, healthy = self.readers(row, conflict=True), self.readers(row)
                reader = lambda after, since: (healthy if valid[0] else faulty)(after, since)
                cache = index(**{source: reader}, links=None, clock=lambda: clock[0])
                snapshot = cache.snapshot()
                self.assertFalse(snapshot["sources"][source]["ok"])
                self.assertEqual(snapshot["coverage"]["records"], 0)
                valid[0] = True
                clock[0] += 20
                cache.refresh()
                snapshot = cache.snapshot()
                self.assertTrue(snapshot["sources"][source]["ok"])
                self.assertEqual(snapshot["flows"]["request"]["d7"], 3)

    def test_history_conflict_seals_nothing_and_can_be_retried(self):
        with self.assertRaisesRegex(ValueError, "conflicting source id 2"):
            self.history.rollup(self.cache, self.readers(JOURNAL, conflict=True), empty, None, NOW)
        self.assertIsNone(self.history.last_day())
        self.assertEqual(self.history.read_all(), [])
        self.history.rollup(self.cache, self.readers(JOURNAL), empty, None, NOW)
        self.assertEqual(sum(row["crossings"] for row in self.history.read_all()), 3)

    def test_refresh_cap_keeps_the_last_admitted_cursor(self):
        rows = [dict(JOURNAL, id=i) for i in (3, 1, 2)]
        with patch.object(traffic, "MAX_ROWS", 2):
            kept, cursor = self.cache._drain(lambda after, since: rows, 0, 0)
        self.assertEqual(([row["id"] for row in kept], cursor), ([1, 2], 2))


if __name__ == "__main__":
    unittest.main()
