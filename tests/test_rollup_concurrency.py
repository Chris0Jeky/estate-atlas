"""Two history connections must not publish against an obsolete sealed-day watermark."""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from estate_atlas import rollup

NOW = 1_800_000_000.0
TODAY = NOW // rollup.DAY_S * rollup.DAY_S
AT = TODAY - 100
ROW = {'id': 1, 'at': AT, 'producer': 'fixture', 'verb': 'observed'}


class Index:
    built = True
    flow_count = 1

    def __init__(self, flow):
        self.flow = flow
        self.routing_revision = object()

    def route_record(self, record):
        return self.flow, 'declared', None


def rows(after, since, *until):
    return [ROW] if after < ROW['id'] and since <= AT and (not until or AT < until[0]) else []


def empty(*args):
    return []


class RollupConcurrencyTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / 'history.db'
        self.first = rollup.TrafficHistory(path)
        self.second = rollup.TrafficHistory(path)
        self.addCleanup(self.first.close)
        self.addCleanup(self.second.close)
        limit = patch.object(rollup, 'BACKFILL_DAYS', 1)
        limit.start()
        self.addCleanup(limit.stop)

    def compete(self, *, by_day=False, later=False, first_flow='stale', winner_flow='winner'):
        called = False
        def reader(after, since, *until):
            nonlocal called
            if not called:
                called = True
                self.second.rollup(Index(winner_flow), rows, empty, None,
                                   NOW + (rollup.DAY_S if later else 0), by_day=by_day)
            return rows(after, since, *until)
        result = self.first.rollup(Index(first_flow), reader, empty, None, NOW, by_day=by_day)
        self.assertTrue(called)
        return result

    def assert_stale_skipped(self, result):
        self.assertEqual(result.get('skipped'), 'history changed during rollup')
        self.assertEqual(result['days_written'], 0)
        self.assertEqual(result['rows_written'], 0)
        self.assertFalse(self.first._con.in_transaction)

    def test_competing_different_routes_never_mix_sealed_day_rows(self):
        result = self.compete()
        self.assert_stale_skipped(result)
        self.assertEqual(self.first.read_all(), [
            {'day': rollup._day_of(AT), 'flow': 'winner', 'crossings': 1, 'pulse': 0}])
        self.assertEqual(self.first.days_stored(), 1)

    def test_day_bounded_readers_obey_the_same_shared_database_guard(self):
        self.assert_stale_skipped(self.compete(by_day=True))
        self.assertEqual([row['flow'] for row in self.first.read_all()], ['winner'])

    def test_older_rollup_cannot_regress_a_newer_sealed_watermark(self):
        self.assert_stale_skipped(self.compete(later=True))
        self.assertEqual(self.first.last_day(), rollup._day_of(TODAY))
        self.assertEqual(self.first.read_all(), [])
        self.assertEqual(self.first.days_stored(), 1)

    def test_same_flow_race_reports_no_new_days_and_retry_is_idempotent(self):
        self.assert_stale_skipped(self.compete(first_flow='same', winner_flow='same'))
        result = self.first.rollup(Index('same'), rows, empty, None, NOW)
        self.assertEqual(result['days_written'], 0)
        self.assertEqual(sum(row['crossings'] for row in self.first.read_all()), 1)

    def test_failed_competitor_leaves_unchanged_watermark_available(self):
        def bad(*args):
            raise ValueError('fixture source unavailable')
        def reader(after, since):
            with self.assertRaisesRegex(ValueError, 'fixture source unavailable'):
                self.second.rollup(Index('other'), bad, empty, None, NOW)
            return rows(after, since)
        result = self.first.rollup(Index('first'), reader, empty, None, NOW)
        self.assertEqual(result['days_written'], 1)
        self.assertNotIn('skipped', result)
        self.assertEqual([row['flow'] for row in self.first.read_all()], ['first'])

    def test_already_present_day_seal_rechecks_after_taking_write_lock(self):
        # Exercise the existing recovery path where rows exist but metadata was not sealed.
        self.first._con.execute(
            'INSERT INTO flow_day VALUES(?,?,?,?,?,?,?,?,?)',
            (rollup._day_of(AT), 'existing', 1, 0, 1, 0, 1, 0, 0))
        con = self.first._con
        second = self.second
        class BeforeBegin:
            def __getattr__(self, name):
                return getattr(con, name)
            def execute(self, sql, *args):
                if sql == 'BEGIN IMMEDIATE':
                    second.rollup(Index('other'), empty, empty, None, NOW + rollup.DAY_S)
                return con.execute(sql, *args)
        self.first._con = BeforeBegin()
        self.assert_stale_skipped(self.first.rollup(Index('first'), empty, empty, None, NOW))
        self.assertEqual(self.first.last_day(), rollup._day_of(TODAY))
        self.assertEqual([row['flow'] for row in self.first.read_all()], ['existing'])

    def test_watermark_read_error_rolls_back_and_does_not_wedge_retry(self):
        con = self.first._con
        class FailRecheck:
            failed = False
            def __getattr__(self, name):
                return getattr(con, name)
            def execute(self, sql, *args):
                if con.in_transaction and sql.startswith('SELECT value FROM meta') and not self.failed:
                    self.failed = True
                    raise sqlite3.OperationalError('fixture watermark read error')
                return con.execute(sql, *args)
        self.first._con = FailRecheck()
        with self.assertRaisesRegex(sqlite3.OperationalError, 'fixture watermark read error'):
            self.first.rollup(Index('first'), rows, empty, None, NOW)
        self.assertFalse(con.in_transaction)
        self.assertIsNone(self.first.last_day())
        self.assertEqual(self.first.read_all(), [])
        self.assertEqual(self.first.rollup(Index('first'), rows, empty, None, NOW)['days_written'], 1)


if __name__ == '__main__':
    unittest.main()
