"""Traffic history: daily rollups, weekly series, fading and surging flags, the transactional write."""
from __future__ import annotations

import datetime
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from estate_atlas import rollup as H
from estate_atlas import traffic as T

NOW = datetime.datetime(2026, 9, 30, tzinfo=datetime.timezone.utc).timestamp()
DAY = 86400.0


def day_at(day, hour=12, minute=0):
    return datetime.datetime.strptime(day, "%Y-%m-%d").replace(
        tzinfo=datetime.timezone.utc).timestamp() + hour * 3600 + minute * 60


D1, D2, D3 = "2026-09-27", "2026-09-28", "2026-09-29"  # three complete days before NOW

COMPS = [
    {"id": "front", "instances": ["client:*", "service:front"]},
    {"id": "back", "instances": ["service:*"]},
]

FLOWS = [
    {"id": "fa", "from": "front", "to": "back", "status": "live",
     "traffic": [{"source": "journal", "producer": "gate", "verb": "merged"}]},
    {"id": "fp", "from": "back", "to": "front", "status": "live",
     "traffic": [{"source": "journal", "producer": "beat", "pulse": True}]},
    {"id": "fx", "from": "back", "to": "front", "status": "live",
     "traffic": [{"source": "events", "producer": "cron"}]},
]

EXPORT = {"components": COMPS, "flows": FLOWS}


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def jrow(i, at, producer="gate", verb="merged", actor="user:gate", subject="order:o1"):
    return {"id": i, "at": at, "producer": producer, "verb": verb,
            "actor": actor, "subject": subject}


def erow(i, at, source="cron nightly", kind="ledger.wave", nodes=()):
    return {"id": i, "at": at, "source": source, "kind": kind, "nodes": list(nodes)}


def make_store(journal=(), events=(), links=(), clock=None):
    clock = clock or Clock()
    jrows = list(journal)
    erows = list(events)
    llist = list(links)

    def jreader(after_id, since):
        return [r for r in jrows if r["id"] > after_id and r["at"] >= since]

    def ereader(after_id, since):
        return [r for r in erows if r["id"] > after_id and r["at"] >= since]

    def lfn():
        return list(llist)

    def afn():
        return "s1", EXPORT

    index = T.TrafficIndex(journal_rows=jreader, event_rows=ereader, links=lfn,
                           atlas=afn, now=clock)
    index.refresh()
    tmp = Path(tempfile.mkdtemp(prefix="hist-test-"))
    hist = H.TrafficHistory(tmp / "traffic_history.db")
    return hist, tmp, index, jreader, ereader, lfn


def cell_of(hist, day, flow):
    con = sqlite3.connect(str(hist.path))
    try:
        row = con.execute("SELECT crossings, pulse, declared, inferred, journal, events, links"
                          " FROM flow_day WHERE day = ? AND flow = ?", (day, flow)).fetchone()
    finally:
        con.close()
    return row


class RollupTests(unittest.TestCase):
    def test_rows_land_on_the_right_days_and_flows(self):
        hist, tmp, index, jr, er, lf = make_store(
            journal=[jrow(1, day_at(D1), actor="client:a", subject="service:api",
                            producer="cron.x", verb="ran"),
                     jrow(2, day_at(D2)),
                     jrow(3, day_at(D2, 13), producer="beat.x", verb="thump")],
            events=[erow(1, day_at(D2, 14))],
            links=[("shop-ledger", {"from": "client:a", "to": "service:api", "rel": "tracks",
                                 "at": day_at(D1, 15)})])
        try:
            out = hist.rollup(index, jr, er, lf, NOW)
            self.assertEqual(out["days_written"], 90)  # the first run covers the 90-day backfill
            self.assertEqual(out["rows_written"], 4)
            # the journal row and the link both reach fa by inference (fa's only rule is gate/merged)
            self.assertEqual(cell_of(hist, D1, "fa"), (2, 0, 0, 2, 1, 0, 1))
            self.assertEqual(cell_of(hist, D2, "fa"), (1, 0, 1, 0, 1, 0, 0))
            self.assertEqual(cell_of(hist, D2, "fp"), (0, 1, 0, 0, 1, 0, 0))
            self.assertEqual(cell_of(hist, D2, "fx"), (1, 0, 1, 0, 0, 1, 0))
            self.assertIsNone(cell_of(hist, D1, "fp"))
            self.assertEqual(hist.last_day(), D3)
            self.assertEqual(hist.days_stored(), 90)  # quiet days are history too
        finally:
            hist.close()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_pulse_and_declared_are_split(self):
        hist, tmp, index, jr, er, lf = make_store(
            journal=[jrow(1, day_at(D3)), jrow(2, day_at(D3, 13), producer="beat.x", verb="thump")])
        try:
            hist.rollup(index, jr, er, lf, NOW)
            fa = cell_of(hist, D3, "fa")
            fp = cell_of(hist, D3, "fp")
            self.assertEqual(fa[0], 1)
            self.assertEqual(fa[1], 0)
            self.assertEqual(fa[2], 1)
            self.assertEqual(fp[0], 0)
            self.assertEqual(fp[1], 1)
        finally:
            hist.close()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_todays_rows_are_ignored(self):
        hist, tmp, index, jr, er, lf = make_store(
            journal=[jrow(1, NOW), jrow(2, NOW + 3600), jrow(3, day_at(D3))])
        try:
            out = hist.rollup(index, jr, er, lf, NOW)
            self.assertEqual((out["days_written"], out["rows_written"]), (90, 1))
            self.assertEqual(cell_of(hist, D3, "fa"), (1, 0, 1, 0, 1, 0, 0))
            self.assertEqual(hist.last_day(), D3)
        finally:
            hist.close()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_undated_links_do_not_count(self):
        hist, tmp, index, jr, er, lf = make_store(
            links=[("shop-ledger", {"from": "client:a", "to": "service:api", "rel": "tracks"})])
        try:
            out = hist.rollup(index, jr, er, lf, NOW)
            self.assertEqual((out["days_written"], out["rows_written"]), (90, 0))
            self.assertEqual(hist.days_stored(), 90)  # 90 quiet days are still 90 days of history
            self.assertEqual(hist.last_day(), D3)
        finally:
            hist.close()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_rerun_writes_nothing(self):
        hist, tmp, index, jr, er, lf = make_store(journal=[jrow(1, day_at(D3))])
        try:
            first = hist.rollup(index, jr, er, lf, NOW)
            self.assertEqual(first["rows_written"], 1)
            second = hist.rollup(index, jr, er, lf, NOW)
            self.assertEqual(second["days_written"], 0)
            self.assertNotIn("rows_written", second)  # nothing was missing: no rollup ran
        finally:
            hist.close()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_new_day_writes_only_that_day_and_prunes(self):
        hist, tmp, index, jr, er, lf = make_store(journal=[jrow(1, day_at(D3))])
        try:
            hist.rollup(index, jr, er, lf, NOW)
            con = sqlite3.connect(str(hist.path))
            try:
                con.execute("INSERT OR IGNORE INTO flow_day VALUES('2020-01-01','fa',1,0,1,0,1,0,0)")
                con.commit()
            finally:
                con.close()
            jr_rows = [jrow(1, day_at(D3)), jrow(2, day_at("2026-09-30", 12))]

            def jr2(after_id, since):
                return [r for r in jr_rows if r["id"] > after_id and r["at"] >= since]

            out = hist.rollup(index, jr2, er, lf, NOW + DAY)
            self.assertEqual(out["days_written"], 1)
            self.assertEqual(cell_of(hist, "2026-09-30", "fa"), (1, 0, 1, 0, 1, 0, 0))
            self.assertEqual(cell_of(hist, D3, "fa"), (1, 0, 1, 0, 1, 0, 0))
            self.assertIsNone(cell_of(hist, "2020-01-01", "fa"))
            self.assertEqual(hist.last_day(), "2026-09-30")
        finally:
            hist.close()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_backfill_is_bounded_at_90_days(self):
        rows = [jrow(i + 1, day_at("2026-03-01") + i * DAY + 43200) for i in range(200)]

        def jr(after_id, since):
            return [r for r in rows if r["id"] > after_id and r["at"] >= since]

        hist, tmp, index, _jr, er, lf = make_store()
        try:
            out = hist.rollup(index, jr, er, lf, NOW)
            self.assertEqual(out["days_written"], 90)
            self.assertEqual(hist.last_day(), D3)
            self.assertEqual(hist.days_stored(), 90)
            con = sqlite3.connect(str(hist.path))
            try:
                earliest = con.execute("SELECT MIN(day) FROM flow_day").fetchone()[0]
            finally:
                con.close()
            self.assertEqual(earliest, "2026-07-02")
        finally:
            hist.close()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_route_record_uses_the_index_rules(self):
        hist, tmp, index, jr, er, lf = make_store()
        try:
            fid, basis, reason = index.route_record(
                {"source": "journal", "producer": "gate", "verb": "merged",
                 "actor": "user:gate", "subject": "order:o1"})
            self.assertEqual((fid, basis, reason), ("fa", "declared", None))
        finally:
            hist.close()
            shutil.rmtree(tmp, ignore_errors=True)


class WeeklyTests(unittest.TestCase):
    def test_windows_oldest_first(self):
        rows = [{"day": D1, "flow": "fa", "crossings": 2, "pulse": 1},
                {"day": D3, "flow": "fa", "crossings": 5, "pulse": 0}]
        got = H.weekly(rows, D3, 2, ["fa", "ghost"])
        self.assertEqual([w["week_end"] for w in got["fa"]], ["2026-09-22", "2026-09-29"])
        self.assertEqual(got["fa"][0], {"week_end": "2026-09-22", "crossings": 0, "pulse": 0})
        self.assertEqual(got["fa"][1], {"week_end": "2026-09-29", "crossings": 7, "pulse": 1})
        self.assertEqual(got["ghost"],
                         [{"week_end": "2026-09-22", "crossings": 0, "pulse": 0},
                          {"week_end": "2026-09-29", "crossings": 0, "pulse": 0}])

    def test_flow_with_no_rows_still_appears(self):
        got = H.weekly([], D3, 1, ["fa"])
        self.assertEqual(got, {"fa": [{"week_end": D3, "crossings": 0, "pulse": 0}]})


class FlagsTests(unittest.TestCase):
    @staticmethod
    def series(cross, pulse=None):
        weeks = [{"week_end": "2026-08-%02d" % (i + 1), "crossings": c,
                  "pulse": (pulse[i] if pulse else 0)} for i, c in enumerate(cross)]
        return {"f1": weeks}

    def test_fading_at_the_threshold(self):
        fading, surging = H.flags(self.series([20, 20, 20, 20, 7]))
        self.assertEqual(fading, [{"flow": "f1", "last_week": 7, "avg4": 20.0}])
        self.assertEqual(surging, [])
        fading, _ = H.flags(self.series([20, 20, 20, 20, 8]))
        self.assertEqual(fading, [])

    def test_avg4_below_10_never_fades(self):
        self.assertEqual(H.flags(self.series([8, 8, 8, 8, 0])), ([], []))
        self.assertEqual(H.flags(self.series([10, 10, 10, 10, 3]))[0],
                         [{"flow": "f1", "last_week": 3, "avg4": 10.0}])

    def test_surging_at_the_threshold(self):
        fading, surging = H.flags(self.series([6, 6, 6, 6, 20]))
        self.assertEqual(surging, [{"flow": "f1", "last_week": 20, "avg4": 6.0}])
        self.assertEqual(fading, [])
        self.assertEqual(H.flags(self.series([6, 6, 6, 6, 19])), ([], []))
        self.assertEqual(H.flags(self.series([7, 7, 7, 7, 20])), ([], []))

    def test_pulse_metric_fallback(self):
        fading, _ = H.flags(self.series([0, 0, 0, 0, 0], [20, 20, 20, 20, 2]))
        self.assertEqual(fading, [{"flow": "f1", "last_week": 2, "avg4": 20.0}])

    def test_short_history_gets_no_flags(self):
        self.assertEqual(H.flags(self.series([20, 20, 20, 20, 7]), 34), ([], []))
        fading, _ = H.flags(self.series([20, 20, 20, 20, 7]), 35)
        self.assertEqual(len(fading), 1)
        short = {"f1": self.series([20, 20, 20, 20, 7])["f1"][:4]}
        self.assertEqual(H.flags(short, 100), ([], []))

    def test_sorted_by_flow(self):
        s = dict(self.series([20, 20, 20, 20, 7]))
        s["a0"] = list(s["f1"])
        fading, _ = H.flags(s, 100)
        self.assertEqual([f["flow"] for f in fading], ["a0", "f1"])


class BadPathTests(unittest.TestCase):
    def test_store_open_on_a_bad_path_raises(self):
        tmp = Path(tempfile.mkdtemp(prefix="hist-bad-"))
        try:
            parent = tmp / "parent-file"
            parent.write_text("not a dir", encoding="utf-8")
            with self.assertRaises(Exception):
                H.TrafficHistory(parent / "traffic_history.db")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_the_store_path_is_the_callers(self):
        tmp = Path(tempfile.mkdtemp(prefix="hist-path-"))
        try:
            hist = H.TrafficHistory(tmp / "deep" / "dir" / "my-history.db")
            try:
                self.assertTrue((tmp / "deep" / "dir" / "my-history.db").is_file())
                self.assertIsNone(hist.last_day())
                self.assertEqual(hist.days_stored(), 0)
            finally:
                hist.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class OwnHistoryTests(unittest.TestCase):
    """A flow is flagged only on 35 days of its own history (events are kept 14 days: a backfill must not
    make an event-fed flow look like it surged)."""

    def test_a_short_own_history_is_never_flagged(self):
        series = {"ev": [{"week_end": e, "crossings": c, "pulse": 0} for e, c in
                         zip(["2026-08-26", "2026-09-02", "2026-09-09", "2026-09-16", "2026-09-23"],
                             [0, 0, 0, 5, 300])]}
        self.assertEqual(H.flags(series, 90, {"ev": "2026-09-12"}), ([], []))
        fading, surging = H.flags(series, 90, {"ev": "2026-08-01"})
        self.assertEqual([s["flow"] for s in surging], ["ev"])
        self.assertEqual(fading, [])



class AtomicRollupTests(unittest.TestCase):
    """A rollup is one transaction: a failure mid-write leaves no half-written day."""

    def test_a_failed_write_rolls_back_everything(self):
        hist, tmp, index, jr, er, lf = make_store(journal=[jrow(1, day_at(D3))])
        try:
            real = hist._write_days

            def boom(acc, yesterday, today_start):
                real(acc, yesterday, today_start)  # the rows and last_day are written...
                raise RuntimeError("disk full")    # ...then the write fails

            hist._write_days = boom
            with self.assertRaises(RuntimeError):
                hist.rollup(index, jr, er, lf, NOW)
            self.assertIsNone(cell_of(hist, D3, "fa"))
            self.assertIsNone(hist.last_day())
            hist._write_days = real
            out = hist.rollup(index, jr, er, lf, NOW)  # the next run rolls the same days up again
            self.assertEqual(out["rows_written"], 1)
            self.assertEqual(cell_of(hist, D3, "fa"), (1, 0, 1, 0, 1, 0, 0))
        finally:
            hist.close()
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
