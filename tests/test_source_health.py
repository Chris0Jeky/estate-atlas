"""Malformed sources are failures, not evidence of an empty successful read."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from estate_atlas import rollup, traffic

NOW = 1_800_000_000.0
AT = NOW - 86400
DOC = {"components": [{"id": "front", "instances": ["client:*"]},
                      {"id": "back", "instances": ["service:*"]}],
       "flows": [{"id": "request", "from": "front", "to": "back", "status": "live"}]}
JOURNAL = {"id": 1, "at": AT, "producer": "observer", "verb": "sent",
           "actor": "client:a", "subject": "service:b"}
EVENT = {"id": 1, "at": AT, "source": "observer", "kind": "sent", "nodes": ["client:a", "service:b"]}
LINK = ("observer", {"from": "client:a", "to": "service:b", "rel": "sent", "at": AT})
BAD = (None, {}, {"wrong": "shape"}, (), "", False, 0)


def empty(after, since):
    return []


def once(row):
    return lambda after, since: [dict(row)] if after < row["id"] and since <= row["at"] else []


def index(*, journal=empty, events=empty, links=None, clock=None):
    result = traffic.TrafficIndex(journal_rows=journal, event_rows=events, links=links,
                                  atlas=lambda: ("stable", DOC), now=clock or (lambda: NOW))
    result.refresh()
    return result


class LiveSourceHealthTests(unittest.TestCase):
    def test_explicitly_disabled_links_are_a_valid_empty_source(self):
        snapshot = index(journal=once(JOURNAL), links=None).snapshot()
        self.assertEqual(snapshot["sources"]["links"], {"ok": True, "records": 0, "undated": 0, "error": None})
        self.assertEqual(snapshot["flows"]["request"]["d7"], 1)

    def test_malformed_link_result_never_reports_healthy(self):
        for bad in BAD:
            with self.subTest(bad=bad):
                snapshot = index(journal=once(JOURNAL), links=lambda: bad).snapshot()
                source = snapshot["sources"]["links"]
                self.assertFalse(source["ok"])
                self.assertEqual(source["error"], "ValueError: links source must return a list")
                self.assertEqual(source["records"], 0)
                self.assertTrue(snapshot["sources"]["journal"]["ok"])
                self.assertEqual(snapshot["flows"]["request"]["d7"], 1)

    def test_empty_link_list_is_healthy(self):
        source = index(links=lambda: []).snapshot()["sources"]["links"]
        self.assertTrue(source["ok"])
        self.assertIsNone(source["error"])

    def test_failed_link_recount_removes_old_links_and_recovery_recounts_once(self):
        clock = [NOW]
        value = [[LINK]]
        cache = index(journal=once(JOURNAL), links=lambda: value[0], clock=lambda: clock[0])
        self.assertEqual(cache.snapshot()["flows"]["request"]["d7"], 2)
        value[0] = {}
        clock[0] += 20
        cache.refresh()
        snapshot = cache.snapshot()
        self.assertFalse(snapshot["sources"]["links"]["ok"])
        self.assertEqual(snapshot["flows"]["request"]["d7"], 1)
        value[0] = [LINK]
        clock[0] += 20
        cache.refresh()
        snapshot = cache.snapshot()
        self.assertTrue(snapshot["sources"]["links"]["ok"])
        self.assertEqual(snapshot["flows"]["request"]["d7"], 2)

    def test_malformed_paginated_sources_never_report_healthy(self):
        for source in ("journal", "events"):
            for bad in BAD:
                with self.subTest(source=source, bad=bad):
                    cache = index(**{source: lambda after, since: bad}, links=lambda: [LINK])
                    snapshot = cache.snapshot()
                    self.assertFalse(snapshot["sources"][source]["ok"])
                    self.assertEqual(snapshot["sources"][source]["error"], "ValueError: source reader must return a list")
                    self.assertEqual(snapshot["flows"]["request"]["d7"], 1)

    def test_late_malformed_page_does_not_ingest_a_partial_refresh(self):
        for source, row in (("journal", JOURNAL), ("events", EVENT)):
            with self.subTest(source=source), patch.object(traffic, "PAGE", 1):
                reader = lambda after, since: [dict(row)] if after == 0 else {}
                cache = index(**{source: reader}, links=lambda: [])
                snapshot = cache.snapshot()
                self.assertFalse(snapshot["sources"][source]["ok"])
                self.assertEqual(snapshot["coverage"]["records"], 0)


class HistorySourceHealthTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.history = rollup.TrafficHistory(Path(directory.name) / "history.db")
        self.addCleanup(self.history.close)
        self.cache = index(links=lambda: [])

    def assert_unsealed(self):
        self.assertIsNone(self.history.last_day())
        self.assertEqual(self.history.days_stored(), 0)
        self.assertEqual(self.history.read_all(), [])

    def test_explicitly_disabled_links_do_not_block_sealing(self):
        result = self.history.rollup(self.cache, once(JOURNAL), empty, None, NOW)
        self.assertGreater(result["days_written"], 0)
        self.assertEqual(sum(row["crossings"] for row in self.history.read_all()), 1)

    def test_malformed_link_callback_result_is_not_disabled(self):
        for bad in BAD:
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, "links source must return a list"):
                    self.history.rollup(self.cache, once(JOURNAL), empty, lambda: bad, NOW)
                self.assert_unsealed()
        result = self.history.rollup(self.cache, once(JOURNAL), empty, lambda: [], NOW)
        self.assertEqual(result["rows_written"], 1)

    def test_malformed_paginated_sources_leave_all_days_retryable(self):
        for source in ("journal", "events"):
            for bad in BAD:
                with self.subTest(source=source, bad=bad):
                    readers = {"journal_rows": empty, "event_rows": empty}
                    readers["journal_rows" if source == "journal" else "event_rows"] = lambda after, since: bad
                    with self.assertRaisesRegex(ValueError, "source reader must return a list"):
                        self.history.rollup(self.cache, **readers, links=lambda: [], now=NOW)
                    self.assert_unsealed()

    def test_late_malformed_page_leaves_all_days_retryable(self):
        for source, row in (("journal", JOURNAL), ("events", EVENT)):
            with self.subTest(source=source), patch.object(rollup, "READ_PAGE", 1):
                readers = {"journal_rows": empty, "event_rows": empty}
                readers["journal_rows" if source == "journal" else "event_rows"] = (
                    lambda after, since: [dict(row)] if after == 0 else {})
                with self.assertRaisesRegex(ValueError, "source reader must return a list"):
                    self.history.rollup(self.cache, **readers, links=lambda: [], now=NOW)
                self.assert_unsealed()


if __name__ == "__main__":
    unittest.main()
