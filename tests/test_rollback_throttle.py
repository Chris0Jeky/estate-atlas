"""Clock correction stays immediate once, but repeated rollback work has a steady cooldown."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from estate_atlas import traffic

NOW = 1_800_000_000.0
DOC = {'components': [{'id': 'a', 'instances': ['client:*']},
                      {'id': 'b', 'instances': ['service:*']}],
       'flows': [{'id': 'ab', 'from': 'a', 'to': 'b', 'status': 'live'}]}


class RollbackThrottleTests(unittest.TestCase):
    def setUp(self):
        self.wall = NOW
        self.steady = 1000.0
        self.calls = 0
        self.fail = False
        def atlas():
            self.calls += 1
            if self.fail:
                raise OSError('fixture atlas unavailable')
            return 'revision', DOC
        self.cache = traffic.TrafficIndex(journal_rows=lambda after, since: [],
            event_rows=lambda after, since: [], links=None, atlas=atlas, now=lambda: self.wall)
        self.patcher = patch.object(traffic.time, 'monotonic', lambda: self.steady)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.cache.refresh()

    def rollback(self, seconds=1):
        self.wall -= seconds
        return self.cache.refresh()

    def test_first_correction_is_immediate_then_repeated_steps_are_throttled(self):
        self.assertFalse(self.rollback()['throttled'])
        for step in range(1, 15):
            self.steady += 1
            self.assertTrue(self.rollback()['throttled'])
        self.assertEqual(self.calls, 2)
        self.steady += 1
        self.assertFalse(self.rollback()['throttled'])
        self.assertEqual(self.calls, 3)

    def test_frozen_wall_clock_after_correction_does_not_disable_cooldown(self):
        self.assertFalse(self.rollback()['throttled'])
        self.steady += 1
        self.assertTrue(self.cache.refresh()['throttled'])
        self.assertEqual(self.calls, 2)

    def test_cooldown_does_not_reset_when_the_wall_clock_recovers(self):
        self.assertFalse(self.rollback()['throttled'])
        self.wall += 30
        self.assertFalse(self.cache.refresh()['throttled'])
        self.assertTrue(self.rollback()['throttled'])
        self.assertEqual(self.calls, 3)
        self.steady += traffic.THROTTLE_S
        self.assertFalse(self.rollback()['throttled'])

    def test_atlas_failure_during_rollback_also_consumes_the_cooldown(self):
        self.fail = True
        self.assertFalse(self.rollback()['throttled'])
        self.assertFalse(self.cache.snapshot()['source']['ok'])
        self.fail = False
        self.steady += 1
        self.assertTrue(self.rollback()['throttled'])
        self.steady += traffic.THROTTLE_S
        self.assertFalse(self.rollback()['throttled'])
        self.assertTrue(self.cache.snapshot()['source']['ok'])

    def test_a_second_backward_window_rebuilds_when_its_cooldown_expires(self):
        row = {'id': 1, 'at': NOW - traffic.WINDOW_S - 7200,
               'producer': 'app', 'verb': 'request', 'actor': 'client:x', 'subject': 'service:y'}
        self.cache._journal_rows = lambda after, since: [row] if after < 1 and row['at'] >= since else []
        self.assertFalse(self.rollback(3600)['throttled'])
        self.assertEqual(self.cache.snapshot()['coverage']['records'], 0)
        self.assertTrue(self.rollback(7200)['throttled'])
        self.steady += traffic.THROTTLE_S
        self.assertFalse(self.cache.refresh()['throttled'])
        self.assertEqual(self.cache.snapshot()['flows']['ab']['d7'], 1)

    def test_forward_clock_throttle_retains_the_existing_injected_clock_contract(self):
        self.wall += 10
        self.assertTrue(self.cache.refresh()['throttled'])
        self.wall += 5
        self.assertFalse(self.cache.refresh()['throttled'])

    def test_waiting_refresh_samples_wall_time_after_acquiring_the_lock(self):
        real = self.cache._lock
        owner = self
        class DelayedLock:
            def __enter__(self):
                real.acquire()
                owner.wall += 20
            def __exit__(self, *args):
                real.release()
        self.cache._lock = DelayedLock()
        try:
            result = self.cache.refresh()
        finally:
            self.cache._lock = real
        self.assertFalse(result['throttled'])
        self.assertEqual(self.cache._last_refresh, self.wall)

    def test_rollback_eligibility_is_per_index_not_global(self):
        self.assertFalse(self.rollback()['throttled'])
        other = traffic.TrafficIndex(journal_rows=lambda a, s: [], event_rows=lambda a, s: [],
                                    links=None, atlas=lambda: ('revision', DOC), now=lambda: self.wall)
        other.refresh()
        self.wall -= 1
        self.assertFalse(other.refresh()['throttled'])
        self.assertTrue(self.cache.refresh()['throttled'])
