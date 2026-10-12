"""Opt-in day-bounded readers finish larger backfills without sealing incomplete history."""
from __future__ import annotations

import copy
import inspect
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from estate_atlas import rollup, traffic
from tests.test_source_health import DOC, JOURNAL, EVENT, index

DAY = 86400.0
NOW = (1_800_057_600.0 // DAY) * DAY


class DailyBackfillTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.history = rollup.TrafficHistory(Path(folder.name) / 'history.db')
        self.addCleanup(self.history.close)
        self.cache = index()
        self.calls = []
        for name, value in (('READ_PAGE', 2), ('READ_MAX', 4), ('BACKFILL_DAYS', 3)):
            p = patch.object(rollup, name, value)
            p.start()
            self.addCleanup(p.stop)

    def reader(self, rows):
        def read(after, since, until):
            self.calls.append((after, since, until))
            return copy.deepcopy(sorted((r for r in rows if r['id'] > after
                and since <= r['at'] < until), key=lambda r: r['id'])[:2])
        return read

    def run_daily(self, journal=(), events=(), links=None, now=NOW):
        self.assertIn('by_day', inspect.signature(self.history.rollup).parameters,
                      'rollup needs an explicit day-bounded reader mode')
        return self.history.rollup(self.cache,
            journal if callable(journal) else self.reader(journal),
            events if callable(events) else self.reader(events), links, now, by_day=True)

    def unsealed(self):
        self.assertIsNone(self.history.last_day())
        self.assertEqual(self.history.days_stored(), 0)
        self.assertEqual(self.history.read_all(), [])

    def rows(self, template=JOURNAL):
        # Increasing IDs are deliberately not chronological. No timestamp-order assumption is valid.
        return [dict(template, id=i + 1, at=NOW - (1 + i % 3) * DAY + 10) for i in range(6)]

    def test_backfill_over_total_limit_completes_in_bounded_day_reads(self):
        rows = self.rows()
        legacy = lambda after, since: [r for r in rows if r['id'] > after and r['at'] >= since][:2]
        with self.assertRaisesRegex(ValueError, 'daily rollup record limit exceeded'):
            self.history.rollup(self.cache, legacy, lambda a, s: [], None, NOW)
        self.unsealed()
        result = self.run_daily(rows)
        self.assertEqual((result['days_written'], result['rows_written']), (3, 3))
        self.assertEqual(sum(r['crossings'] for r in self.history.read_all()), 6)
        self.assertEqual({hi - lo for _, lo, hi in self.calls}, {DAY})
        self.assertEqual({lo for _, lo, _ in self.calls}, {NOW - DAY, NOW - 2 * DAY, NOW - 3 * DAY})

    def test_events_are_partitioned_too_and_links_are_read_once(self):
        calls = []
        link = ('observer', {'from': 'client:a', 'to': 'service:b', 'rel': 'sent', 'at': NOW - DAY})
        def links():
            calls.append(True)
            return [link, ('observer', dict(link[1], at=NOW))]
        self.run_daily(self.rows(), self.rows(EVENT), links)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sum(r['crossings'] for r in self.history.read_all()), 13)

    def test_single_day_over_limit_is_still_rejected_without_sealing_other_days(self):
        rows = [dict(JOURNAL, id=i, at=NOW - DAY + 1) for i in range(1, 6)]
        rows += [dict(JOURNAL, id=6, at=NOW - 3 * DAY + 1)]
        with self.assertRaisesRegex(ValueError, 'daily rollup record limit exceeded'):
            self.run_daily(rows)
        self.unsealed()

    def test_exact_limit_gets_the_confirming_empty_page_and_midnight_is_exclusive(self):
        rows = [dict(JOURNAL, id=i, at=NOW - DAY) for i in range(1, 5)]
        rows.append(dict(JOURNAL, id=5, at=NOW))
        self.run_daily(rows)
        self.assertEqual(sum(r['crossings'] for r in self.history.read_all()), 4)
        self.assertIn((4, NOW - DAY, NOW), self.calls)

    def test_late_source_error_leaves_earlier_days_retryable(self):
        healthy = self.reader(self.rows())
        for source in ('journal', 'events'):
            def broken(after, since, until):
                if since == NOW - DAY:
                    raise OSError('fixture source unavailable')
                return healthy(after, since, until)
            kwargs = {source: broken}
            with self.subTest(source=source), self.assertRaisesRegex(OSError, 'fixture source unavailable'):
                self.run_daily(**kwargs)
            self.unsealed()
        self.run_daily(self.rows())
        self.assertEqual(sum(r['crossings'] for r in self.history.read_all()), 6)

    def test_bad_reader_shape_and_outside_window_rows_are_not_silent_empty_days(self):
        for rows in (None, {}, [dict(JOURNAL, at=NOW)], [dict(JOURNAL, at=NOW - 4 * DAY)],
                     [dict(JOURNAL, at=float('nan'))], [dict(JOURNAL, at=float('inf'))]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.run_daily(journal=lambda after, since, until: rows)
            self.unsealed()

    def test_conflicting_replays_remain_invalid_in_one_day(self):
        def reader(after, since, until):
            if until != NOW:
                return []
            if after == 0:
                return [dict(JOURNAL, id=1, at=since), dict(JOURNAL, id=2, at=since)]
            return [dict(JOURNAL, id=2, at=since, verb='changed'), dict(JOURNAL, id=3, at=since)]
        with self.assertRaisesRegex(ValueError, 'conflicting source id 2'):
            self.run_daily(journal=reader)
        self.unsealed()

    def test_routing_change_during_later_day_discards_all_accumulation(self):
        healthy = self.reader(self.rows())
        def changed(after, since, until):
            rows = healthy(after, since, until)
            if since == NOW - DAY:
                self.cache._rules = traffic.compile_rules([])
            return rows
        result = self.run_daily(journal=changed)
        self.assertEqual(result['skipped'], 'index changed during rollup')
        self.unsealed()

    def test_failed_sql_write_rolls_back_and_next_attempt_succeeds(self):
        write_days = self.history._write_days
        def fail(*args):
            write_days(*args)
            raise sqlite3.OperationalError('fixture commit path failure')
        with patch.object(self.history, '_write_days', fail):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'fixture commit path failure'):
                self.run_daily(self.rows())
        self.unsealed()
        self.run_daily(self.rows())
        self.assertEqual(sum(r['crossings'] for r in self.history.read_all()), 6)

    def test_rerun_is_idempotent_and_next_day_reads_only_the_new_window(self):
        self.run_daily(self.rows())
        previous = self.history.read_all()
        self.calls.clear()
        result = self.run_daily(self.rows())
        self.assertEqual(result['days_written'], 0)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.history.read_all(), previous)
        self.run_daily([dict(JOURNAL, at=NOW + 1)], now=NOW + DAY)
        self.assertEqual({(lo, hi) for _, lo, hi in self.calls}, {(NOW, NOW + DAY)})
        self.assertEqual(sum(r['crossings'] for r in self.history.read_all()), 7)

    def test_no_flows_or_unbuilt_index_does_not_call_sources(self):
        self.cache._built = False
        def forbidden(*args):
            self.fail('unbuilt index must not read sources')
        result = self.run_daily(forbidden, forbidden, forbidden)
        self.assertEqual(result['skipped'], 'index not built')
        self.unsealed()

    def test_nonboolean_mode_is_rejected_before_source_calls(self):
        for value in (1, 'yes', None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'by_day must be a boolean'):
                self.history.rollup(self.cache, lambda *a: self.fail('no read'),
                                    lambda *a: self.fail('no read'), None, NOW, by_day=value)
        self.unsealed()

    def test_day_mode_matches_every_stored_metric_from_the_legacy_reader(self):
        doc = copy.deepcopy(DOC)
        doc['flows'][0]['traffic'] = [
            {'source': 'journal', 'verb': 'declared'},
            {'source': 'events', 'verb': 'beat', 'pulse': True}]
        self.cache = traffic.TrafficIndex(journal_rows=lambda a, s: [], event_rows=lambda a, s: [],
            links=None, atlas=lambda: ('rules', doc), now=lambda: NOW)
        self.cache.refresh()
        journal = [dict(JOURNAL, id=i + 1, at=NOW - (i + 1) * DAY + 1,
                        verb='declared' if i % 2 else 'inferred') for i in range(3)]
        events = [dict(EVENT, id=i + 1, at=NOW - (i + 1) * DAY + 2, kind='beat') for i in range(3)]
        links = lambda: [('observer', {'from': 'client:a', 'to': 'service:b', 'rel': 'sent',
                                      'at': NOW - DAY + 3})]
        self.run_daily(journal, events, links)
        def legacy(rows):
            return lambda after, since: sorted((r for r in rows if r['id'] > after and r['at'] >= since),
                                               key=lambda r: r['id'])[:2]
        other = rollup.TrafficHistory(Path(self.history._con.execute('PRAGMA database_list').fetchone()[2]).parent / 'parity.db')
        try:
            other.rollup(self.cache, legacy(journal), legacy(events), links, NOW)
            def saved(history):
                return history._con.execute('SELECT * FROM flow_day ORDER BY day, flow').fetchall()
            self.assertEqual(saved(self.history), saved(other))
            self.assertEqual(self.history.last_day(), other.last_day())
            self.assertEqual(self.history.first_seen(), other.first_seen())
            self.assertEqual(sum(r['pulse'] for r in self.history.read_all()), 3)
        finally:
            other.close()

    def test_late_links_failure_leaves_all_daily_reads_retryable(self):
        def broken():
            raise OSError('fixture links failure')
        with self.assertRaisesRegex(OSError, 'fixture links failure'):
            self.run_daily(self.rows(), links=broken)
        self.unsealed()
        self.run_daily(self.rows())
        self.assertEqual(sum(r['crossings'] for r in self.history.read_all()), 6)

    def test_empty_days_are_sealed_only_after_both_sources_complete(self):
        result = self.run_daily()
        self.assertEqual(result['days_written'], 3)
        self.assertEqual(self.history.read_all(), [])
        self.assertEqual(self.history.days_stored(), 3)
        self.assertEqual(len(self.calls), 6)
        self.calls.clear()
        self.run_daily()
        self.assertEqual(self.calls, [])
