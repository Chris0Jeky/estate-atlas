"""A bad numeric ledger timestamp must not partially publish or advance a source."""
from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from estate_atlas import traffic
from tests.test_source_health import AT, DOC, EVENT, JOURNAL, LINK, NOW


class TimestampIngestionTests(unittest.TestCase):
    def setUp(self):
        self.clock = NOW

    def cache(self, source, reader, *, links=None):
        readers = {'journal': lambda after, since: [], 'events': lambda after, since: []}
        readers[source] = reader
        return traffic.TrafficIndex(journal_rows=readers['journal'], event_rows=readers['events'],
                                   links=links, atlas=lambda: ('stable', DOC), now=lambda: self.clock)

    def rows(self, source, bad):
        template = JOURNAL if source == 'journal' else EVENT
        return [dict(template, id=1), dict(template, id=2, at=bad)]

    def test_invalid_numeric_timestamp_rejects_whole_source_and_keeps_other_sources_usable(self):
        for source in ('journal', 'events'):
            for label, bad in [('nan', float('nan')), ('positive infinity', float('inf')),
                               ('negative infinity', -float('inf')), ('overflow', 10 ** 400)]:
                with self.subTest(source=source, bad=label):
                    rows = self.rows(source, bad)
                    cache = self.cache(source, lambda after, since: [r for r in rows if r['id'] > after],
                                       links=lambda: [LINK])
                    self.assertFalse(cache.refresh()['throttled'])
                    snapshot = cache.snapshot(flow='request')
                    self.assertFalse(snapshot['sources'][source]['ok'])
                    self.assertIn('timestamp', snapshot['sources'][source]['error'])
                    self.assertEqual(snapshot['sources'][source]['records'], 0)
                    self.assertTrue(snapshot['sources']['links']['ok'])
                    self.assertEqual(snapshot['coverage']['records'], 1)
                    self.assertEqual([r['source'] for r in snapshot['records']], ['links'])
                    self.assertEqual(getattr(cache, '_' + ('event' if source == 'events' else source) + '_cursor'), 0)

    def test_bad_later_page_cannot_publish_earlier_pages(self):
        for source in ('journal', 'events'):
            with self.subTest(source=source), patch.object(traffic, 'PAGE', 1):
                rows = self.rows(source, float('nan'))
                calls = []
                def reader(after, since):
                    calls.append(after)
                    return [r for r in rows if r['id'] > after][:1]
                cache = self.cache(source, reader)
                cache.refresh()
                snapshot = cache.snapshot()
                self.assertFalse(snapshot['sources'][source]['ok'])
                self.assertEqual(snapshot['coverage']['records'], 0)
                self.assertEqual(calls[:2], [0, 1])

    def test_failed_increment_preserves_existing_counts_and_recovery_does_not_double_count(self):
        for source in ('journal', 'events'):
            with self.subTest(source=source):
                template = JOURNAL if source == 'journal' else EVENT
                rows = [dict(template, id=1)]
                cache = self.cache(source, lambda after, since: [r for r in rows if r['id'] > after])
                cache.refresh()
                rows.extend([dict(template, id=2), dict(template, id=3, at=float('inf'))])
                self.clock += 20
                baseline = cache.snapshot(flow='request')  # Compare at the same wall time.
                cache.refresh()
                rejected = cache.snapshot(flow='request')
                self.assertFalse(rejected['sources'][source]['ok'])
                for key in ('coverage', 'flows', 'records'):
                    self.assertEqual(rejected[key], baseline[key], key)
                self.assertEqual(getattr(cache, '_' + ('event' if source == 'events' else source) + '_cursor'), 1)
                rows[-1]['at'] = AT
                self.clock += 20
                cache.refresh()
                recovered = cache.snapshot()
                self.assertTrue(recovered['sources'][source]['ok'])
                self.assertEqual(recovered['coverage']['records'], 3)
                self.clock += 20
                cache.refresh()
                self.assertEqual(cache.snapshot()['coverage']['records'], 3)

    def test_nonnumeric_times_retain_the_documented_skip_behavior(self):
        for source in ('journal', 'events'):
            with self.subTest(source=source):
                template = JOURNAL if source == 'journal' else EVENT
                rows = [dict(template, id=n, at=value) for n, value in enumerate(
                    [None, 'not a timestamp', True, '2026-10-01T00:00:00Z', AT], 1)]
                cache = self.cache(source, lambda after, since: [r for r in rows if r['id'] > after])
                cache.refresh()
                snapshot = cache.snapshot()
                self.assertTrue(snapshot['sources'][source]['ok'])
                self.assertEqual(snapshot['coverage']['records'], 1)
                self.assertEqual(getattr(cache, '_' + ('event' if source == 'events' else source) + '_cursor'), 5)

    def test_timestamp_validation_does_not_modify_callback_rows(self):
        rows = [dict(JOURNAL, id=1, at=int(AT))]
        original = copy.deepcopy(rows)
        cache = self.cache('journal', lambda after, since: rows if after == 0 else [])
        cache.refresh()
        self.assertEqual(rows, original)
        self.assertIsInstance(rows[0]['at'], int)
        self.assertEqual(cache.snapshot()['coverage']['records'], 1)

    def test_unadmitted_bad_row_is_deferred_without_losing_the_admitted_cursor(self):
        for source in ('journal', 'events'):
            with self.subTest(source=source), patch.object(traffic, 'MAX_ROWS', 1):
                rows = self.rows(source, float('nan'))
                cache = self.cache(source, lambda after, since: [r for r in rows if r['id'] > after])
                cache.refresh()
                self.assertTrue(cache.snapshot()['sources'][source]['ok'])
                self.assertEqual(cache.snapshot()['coverage']['records'], 1)
                self.clock += 20
                cache.refresh()
                self.assertFalse(cache.snapshot()['sources'][source]['ok'])
                self.assertEqual(cache.snapshot()['coverage']['records'], 1)
                self.assertEqual(getattr(cache, '_' + ('event' if source == 'events' else source) + '_cursor'), 1)

    def test_invalid_timestamp_rejection_is_repeatable_until_producer_repairs_it(self):
        for source in ('journal', 'events'):
            with self.subTest(source=source):
                rows = self.rows(source, 10 ** 400)
                cache = self.cache(source, lambda after, since: [r for r in rows if r['id'] > after])
                for _ in range(3):
                    cache.refresh()
                    self.assertFalse(cache.snapshot()['sources'][source]['ok'])
                    self.assertEqual(cache.snapshot()['coverage']['records'], 0)
                    self.clock += 20
                rows[-1]['at'] = AT
                cache.refresh()
                self.assertEqual(cache.snapshot()['coverage']['records'], 2)
                self.assertTrue(cache.verify()['equal'])


if __name__ == '__main__':
    unittest.main()
