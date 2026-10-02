"""Traffic engine: match, route, the incremental index, pulse and route_files."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from estate_atlas import traffic as T

NOW = 1_800_000_000.0
H = 3600.0

COMPS = [
    {"id": "front", "instances": ["client:*", "service:front"]},
    {"id": "back", "instances": ["service:*"]},
]

FLOWS = [
    {"id": "f1", "from": "front", "to": "back", "status": "live",
     "traffic": [{"source": "journal", "producer": "gate", "verb": "merged"}]},
    {"id": "f2", "from": "back", "to": "front", "status": "planned", "traffic": []},
    {"id": "f3", "from": "front", "to": "front", "status": "live", "traffic": []},
]

EXPORT = {"components": COMPS, "flows": FLOWS}


def idx():
    return T.instance_index(COMPS)


def rules():
    return T.compile_rules(FLOWS)


class Clock:
    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t


def make_index(journal=(), events=(), links=(), sha="s1", clock=None, fail_journal=False,
               fail_events=False):
    clock = clock or Clock()
    jrows = list(journal)
    erows = list(events)
    llist = list(links)

    def jreader(after_id, since):
        if fail_journal:
            raise RuntimeError("journal down")
        return [r for r in jrows if r["id"] > after_id and r["at"] >= since]

    def ereader(after_id, since):
        if fail_events:
            raise RuntimeError("events down")
        return [r for r in erows if r["id"] > after_id and r["at"] >= since]

    def lfn():
        return list(llist)

    def afn():
        return sha, EXPORT

    ti = T.TrafficIndex(journal_rows=jreader, event_rows=ereader, links=lfn,
                        atlas=afn, now=clock)
    ti.refresh()
    return ti, clock, jrows, erows, llist


def jrow(i, at, producer="gate", verb="merged", actor="user:gate", subject="order:o1"):
    return {"id": i, "at": at, "producer": producer, "verb": verb,
            "actor": actor, "subject": subject}


class MatchTests(unittest.TestCase):
    def test_truth_table(self):
        self.assertTrue(T.match(None, None))
        self.assertTrue(T.match(None, "x"))
        self.assertTrue(T.match("*", None))
        self.assertTrue(T.match("*", "anything"))
        self.assertTrue(T.match("client:*", "client:x"))
        self.assertFalse(T.match("client:*", "repo:x"))
        self.assertTrue(T.match("client:x", "client:x"))
        self.assertFalse(T.match("client:x", "client:y"))
        self.assertFalse(T.match("client:x", None))


class RouteTests(unittest.TestCase):
    def test_declared(self):
        fid, basis, reason = T.route(
            {"source": "journal", "producer": "gate", "verb": "merged",
             "actor": "user:gate", "subject": "order:o1"}, rules(), idx(), FLOWS)
        self.assertEqual((fid, basis, reason), ("f1", "declared", None))

    def test_verb_list(self):
        r = T.compile_rules([{"id": "g", "from": "a", "to": "b", "status": "live",
                              "traffic": [{"source": "journal", "verb": ["merged", "refused"]}]}])
        fid, basis, _ = T.route({"source": "journal", "producer": "gate", "verb": "refused",
                                 "actor": "user:x", "subject": "client:y"}, r, idx(),
                                [{"id": "g", "from": "a", "to": "b"}])
        self.assertEqual((fid, basis), ("g", "declared"))

    def test_ambiguous_rules(self):
        flows = [
            {"id": "a", "from": "x", "to": "y", "status": "live",
             "traffic": [{"source": "journal", "producer": "gate"}]},
            {"id": "b", "from": "x", "to": "y", "status": "live",
             "traffic": [{"source": "journal", "verb": "merged"}]},
        ]
        fid, basis, reason = T.route(
            {"source": "journal", "producer": "gate", "verb": "merged",
             "actor": "user:g", "subject": "client:q"}, T.compile_rules(flows), idx(), flows)
        self.assertEqual((fid, basis, reason), (None, "ambiguous", "ambiguous-rules"))

    def test_inferred_direction(self):
        rec = {"source": "journal", "producer": "batch.night", "verb": "ran",
               "actor": "client:night", "subject": "service:api"}
        fid, basis, reason = T.route(rec, rules(), idx(), FLOWS)
        self.assertEqual((fid, basis, reason), ("f1", "inferred", None))
        rev = dict(rec, actor="service:api", subject="client:night")
        fid, basis, reason = T.route(rev, rules(), idx(), FLOWS)
        self.assertEqual((fid, basis), ("f2", "inferred"))

    def test_links_direction_and_producer_head(self):
        r = T.compile_rules([{"id": "g", "from": "x", "to": "y", "status": "live",
                              "traffic": [{"source": "links", "producer": "scheduler",
                                           "verb": "found"}]}])
        rec = {"source": "links", "producer": "scheduler:nightly", "verb": "found",
               "actor": "client:night", "subject": "service:api"}
        # declared wins regardless of direction
        self.assertEqual(T.route(rec, r, idx(), [{"id": "g", "from": "x", "to": "y"}])[1], "declared")
        rec2 = {"source": "links", "producer": "shop-ledger", "verb": "tracks",
                "actor": "client:night", "subject": "service:api"}
        fid, basis, _ = T.route(rec2, rules(), idx(), FLOWS)
        self.assertEqual((fid, basis), ("f1", "inferred"))

    def test_internal(self):
        rec = {"source": "journal", "producer": "batch.night", "verb": "ran",
               "actor": "client:a", "subject": "client:b"}
        self.assertEqual(T.route(rec, rules(), idx(), FLOWS)[1], "internal")

    def test_unmapped(self):
        rec = {"source": "journal", "producer": "hook", "verb": "merged",
               "actor": "order:o1", "subject": "order:o2"}
        self.assertEqual(T.route(rec, rules(), idx(), FLOWS)[1:], ("unrouted", "unmapped"))
        half = {"source": "journal", "producer": "hook", "verb": "merged",
                "actor": "client:a", "subject": "order:o2"}
        self.assertEqual(T.route(half, rules(), idx(), FLOWS)[1:], ("unrouted", "unmapped"))

    def test_no_flow(self):
        rec = {"source": "journal", "producer": "batch.night", "verb": "ran",
               "actor": "client:a", "subject": "service:api"}
        flows = [{"id": "only", "from": "front", "to": "front", "status": "live"}]
        self.assertEqual(T.route(rec, rules(), idx(), flows)[1:], ("unrouted", "no-flow"))

    def test_ambiguous_inference(self):
        rec = {"source": "journal", "producer": "batch.night", "verb": "ran",
               "actor": "client:a", "subject": "service:api"}
        flows = [{"id": "a", "from": "front", "to": "back", "status": "live"},
                 {"id": "b", "from": "front", "to": "back", "status": "live"}]
        self.assertEqual(T.route(rec, rules(), idx(), flows)[1:],
                         ("ambiguous", "ambiguous-inference"))

    def test_events_nodes(self):
        flows = [{"id": "a", "from": "front", "to": "back", "status": "live"}]
        rec = {"source": "events", "producer": "cron nightly", "verb": "ledger.wave",
               "nodes": ["client:a", "service:api"]}
        self.assertEqual(T.route(rec, rules(), idx(), flows)[1], "inferred")
        rec = {"source": "events", "producer": "cron nightly", "verb": "ledger.wave",
               "nodes": ["client:a", "client:b"]}
        self.assertEqual(T.route(rec, rules(), idx(), flows)[1], "internal")
        rec = {"source": "events", "producer": "cron nightly", "verb": "ledger.wave",
               "nodes": ["order:o1"]}
        self.assertEqual(T.route(rec, rules(), idx(), flows)[1:], ("unrouted", "unmapped"))
        r = T.compile_rules([{"id": "a", "from": "x", "to": "y", "status": "live",
                              "traffic": [{"source": "events", "subject": "client:*"}]}])
        rec = {"source": "events", "producer": "cron nightly", "verb": "ledger.wave",
               "nodes": ["order:o1", "client:a"]}
        self.assertEqual(T.route(rec, r, idx(), flows)[1], "declared")

    def test_events_subject_prefix_and_actor_ignored(self):
        r = T.compile_rules([{"id": "a", "from": "x", "to": "y", "status": "live",
                              "traffic": [{"source": "events", "producer": "cron",
                                           "actor": "user:x"}]}])
        rec = {"source": "events", "producer": "cron nightly", "verb": "k", "nodes": []}
        # an actor pattern cannot match an event (no actor): no declared match -> unmapped
        self.assertEqual(T.route(rec, r, idx(), [{"id": "a", "from": "x", "to": "y"}])[1:],
                         ("unrouted", "unmapped"))

    def test_signature_sorted_nodes(self):
        a = {"source": "events", "producer": "cron x", "verb": "k", "nodes": ["service:b", "client:a"]}
        b = {"source": "events", "producer": "cron x", "verb": "k", "nodes": ["client:a", "service:b"]}
        self.assertEqual(T.signature(a), T.signature(b))

    def test_invalid_rules_counted(self):
        r = T.compile_rules([{"id": "a", "from": "x", "to": "y", "status": "live",
                              "traffic": [{"source": "nope"}, "not-a-dict"]}])
        self.assertEqual(r.invalid, 2)


class IndexTests(unittest.TestCase):
    def test_incremental_equals_rebuild(self):
        ti, clock, _, _, _ = make_index([jrow(1, NOW - 100), jrow(2, NOW - 50)])
        clock.t += 61
        v = ti.verify()
        self.assertTrue(v["equal"], v["differences"])
        self.assertEqual(ti.snapshot()["coverage"]["records"], 2)

    def test_two_refreshes_then_verify(self):
        rows = [jrow(1, NOW - 100)]
        ti, clock, jrows, _, _ = make_index(rows)
        ti.refresh()
        jrows.append(jrow(2, NOW - 50))
        clock.t += 16
        ti.refresh()
        self.assertEqual(ti.snapshot()["coverage"]["records"], 2)
        clock.t += 61
        v = ti.verify()
        self.assertTrue(v["equal"], v["differences"])

    def test_new_sha_rebuilds(self):
        ti, clock, jrows, _, _ = make_index([jrow(1, NOW - 100)])
        ti.refresh()
        clock.t += 16
        jrows.append(jrow(2, NOW - 50))
        ti.refresh()
        self.assertEqual(ti.snapshot()["coverage"]["records"], 2)
        # a new sha clears and rebuilds from the same readers (both rows still there)
        clock.t += 16
        ti._atlas_fn = lambda: ("s2", EXPORT)
        ti.refresh()
        self.assertEqual(ti.snapshot()["coverage"]["records"], 2)

    def test_window_expiry(self):
        ti, clock, _, _, _ = make_index([jrow(1, NOW - 100)])
        ti.refresh()
        self.assertEqual(ti.snapshot()["coverage"]["records"], 1)
        clock.t += 169 * H
        ti._last_refresh = clock.t - 16
        ti.refresh()
        self.assertEqual(ti.snapshot()["coverage"]["records"], 0)

    def test_expiry_drops_old_crossings_and_last_at(self):
        # expired hours left their crossings in the ring and in last_at
        ti, clock, _, _, _ = make_index([jrow(1, NOW - 100, actor="client:a", subject="service:api",
                                             producer="batch.night", verb="ran")])
        ti.refresh()
        self.assertEqual(len(ti.snapshot("f1")["records"]), 1)
        self.assertIsNotNone(ti.snapshot()["flows"]["f1"]["last_at"])
        clock.t += 169 * H
        ti._last_refresh = clock.t - 16
        ti.refresh()
        self.assertEqual(ti.snapshot("f1")["records"], [])
        self.assertIsNone(ti.snapshot()["flows"]["f1"]["last_at"])

    def test_partial_expiry_keeps_the_newest_last_at_and_drops_d7(self):
        # one key: its older rows leave the window while its newest stays inside it
        kw = dict(actor="client:a", subject="service:api", producer="batch.night", verb="ran")
        ti, clock, _, _, _ = make_index([jrow(1, NOW - 160 * H, **kw), jrow(2, NOW - 150 * H, **kw),
                                         jrow(3, NOW - 10 * H, **kw)])
        ti.refresh()
        before = ti.snapshot()["flows"]["f1"]
        self.assertEqual((before["d7"], before["last_at"]), (3, NOW - 10 * H))
        clock.t += 20 * H  # the two older rows are now 180 h and 170 h old; the newest is 30 h old
        ti._last_refresh = clock.t - 16
        ti.refresh()
        after = ti.snapshot()["flows"]["f1"]
        self.assertEqual(after["d7"], 1)
        self.assertEqual(after["last_at"], NOW - 10 * H)
        v = ti.verify()
        self.assertTrue(v["equal"], v["differences"])

    def test_verify_is_equal_across_an_hour_boundary(self):
        # the snapshot and the rebuild are compared at one instant, over whole buckets
        rows = [jrow(i, NOW - 168 * H + 30 * 60 + i, actor="client:a", subject="service:api",
                     producer="batch.night", verb="ran") for i in range(1, 6)]
        ti, clock, _, _, _ = make_index(rows)
        ti.refresh()
        clock.t += 45 * 60  # the oldest hour leaves the window between the refresh and verify
        v = ti.verify()
        self.assertTrue(v["equal"], v["differences"])

    def test_undated_link_d7_only(self):
        ti, _, _, _, _ = make_index(
            links=[("shop-ledger", {"from": "client:a", "to": "service:api", "rel": "tracks"})])
        doc = ti.snapshot()
        # the undated link routes inferred f1
        self.assertEqual(doc["flows"]["f1"]["d7"], 1)
        self.assertEqual(doc["flows"]["f1"]["d24"], 0)
        self.assertEqual(doc["sources"]["links"]["undated"], 1)

    def test_dated_link_buckets(self):
        import datetime
        iso = datetime.datetime.fromtimestamp(NOW - 3600, tz=datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        ti, _, _, _, _ = make_index(
            links=[("shop-ledger", {"from": "client:a", "to": "service:api",
                                 "rel": "tracks", "at": iso})])
        doc = ti.snapshot()
        self.assertEqual(doc["flows"]["f1"]["d7"], 1)
        self.assertEqual(doc["flows"]["f1"]["d24"], 1)

    def test_throttle(self):
        ti, clock, jrows, _, _ = make_index([jrow(1, NOW - 100)])
        ti.refresh()
        jrows.append(jrow(2, NOW - 50))
        ti.refresh()  # within 15 s: throttled
        self.assertEqual(ti.snapshot()["coverage"]["records"], 1)
        clock.t += 16
        ti.refresh()
        self.assertEqual(ti.snapshot()["coverage"]["records"], 2)

    def test_failing_reader(self):
        ti, _, _, _, _ = make_index([jrow(1, NOW - 100)],
                                    [{"id": 1, "at": NOW - 100, "kind": "k",
                                      "source": "cron x", "nodes": ["client:a", "service:api"]}],
                                    fail_journal=True)
        doc = ti.snapshot()
        self.assertFalse(doc["sources"]["journal"]["ok"])
        self.assertTrue(doc["sources"]["journal"]["error"])
        self.assertEqual(doc["coverage"]["records"], 1)

    def test_records_filtering_cap_and_unknown(self):
        rows = [jrow(i, NOW - i) for i in range(1, 26)]
        ti, _, _, _, _ = make_index(journal=rows)
        doc = ti.snapshot(flow="f1")
        self.assertLessEqual(len(doc["records"]), 20)
        self.assertEqual(ti.snapshot(flow="nope")["records"], [])
        self.assertNotIn("records", ti.snapshot())
        journal_only = ti.snapshot(flow="f1", source="journal")
        self.assertTrue(all(r["source"] == "journal" for r in journal_only["records"]))
        declared_only = ti.snapshot(flow="f1", basis="declared")
        self.assertEqual(len(declared_only["records"]), 20)
        self.assertTrue(all(r["basis"] == "declared" for r in declared_only["records"]))

    def test_crosschecks(self):
        ti, _, _, _, _ = make_index(
            [jrow(1, NOW - 100, actor="service:api", subject="client:a",
                  producer="batch.night", verb="ran")])
        doc = ti.snapshot()
        self.assertIn("f1", doc["crosschecks"]["silent"])  # live with rules, no crossing
        self.assertEqual(doc["crosschecks"]["off_status"], [{"flow": "f2", "status": "planned", "d7": 1}])
        self.assertEqual(doc["crosschecks"]["rule_gaps"], [])
        self.assertEqual(doc["crosschecks"]["unrouted"], [])

    def test_rule_gaps(self):
        flows = [dict(FLOWS[0]), dict(FLOWS[1])]
        ti = T.TrafficIndex(journal_rows=lambda a, s: [jrow(1, NOW - 100, actor="client:a",
                                                             subject="service:api", producer="batch.night",
                                                             verb="ran")],
                            event_rows=lambda a, s: [], links=lambda: [],
                            atlas=lambda: ("s", {"components": COMPS, "flows": flows}),
                            now=Clock())
        ti.refresh()
        doc = ti.snapshot()
        self.assertEqual(doc["crosschecks"]["rule_gaps"], [{"flow": "f1", "inferred": 1}])

    def test_unrouted_top20(self):
        rows = [jrow(i, NOW - 100, producer="batch.night", verb="ran",
                     actor="order:o1", subject="order:o2") for i in range(1, 25)]
        ti, _, _, _, _ = make_index(journal=rows)
        un = ti.snapshot()["crosschecks"]["unrouted"]
        self.assertLessEqual(len(un), 20)
        self.assertEqual(un[0]["d7"], 24)

    def test_snapshot_shape(self):
        ti, _, _, _, _ = make_index([jrow(1, NOW - 100)])
        doc = ti.snapshot()
        self.assertEqual(doc["schema"], "estate-atlas-traffic@1")
        self.assertEqual(sorted(doc), ["coverage", "crosschecks", "flows", "generated", "schema", "source",
                                       "sources", "timing", "window"])
        self.assertEqual(doc["window"], {"hours": 168, "bucket_s": 3600})
        self.assertEqual(sorted(doc["crosschecks"]), ["off_status", "rule_gaps", "silent", "unrouted"])
        self.assertIn("records", ti.snapshot(flow="f1"))

    def test_verify_caches(self):
        ti, clock, _, _, _ = make_index([jrow(1, NOW - 100)])
        ti.refresh()
        v1 = ti.verify()
        v2 = ti.verify()
        self.assertEqual(v1, v2)
        clock.t += 61
        v3 = ti.verify()
        self.assertTrue(v3["equal"])


class PulseTests(unittest.TestCase):
    """Heartbeats are pulse, not traffic."""

    FLOWS = [
        {"id": "beat", "from": "front", "to": "back", "status": "live",
         "traffic": [{"source": "events", "producer": "scheduler", "verb": "scheduler.tick", "pulse": True},
                     {"source": "events", "producer": "scheduler", "verb": "scheduler.turn"}]},
        {"id": "quiet", "from": "back", "to": "front", "status": "planned",
         "traffic": [{"source": "events", "producer": "cron", "pulse": True}]},
    ]

    def index(self, events):
        clock = Clock()
        ti = T.TrafficIndex(journal_rows=lambda a, s: [],
                            event_rows=lambda a, s: [r for r in events if r["id"] > a and r["at"] >= s],
                            links=lambda: [], atlas=lambda: ("p1", {"components": COMPS, "flows": self.FLOWS}),
                            now=clock)
        ti.refresh()
        return ti, clock

    @staticmethod
    def ev(i, at, source="scheduler night", kind="scheduler.tick"):
        return {"id": i, "at": at, "kind": kind, "source": source, "nodes": ["scheduler:night"]}

    def test_route_gives_pulse_and_a_volume_rule_wins(self):
        rules = T.compile_rules(self.FLOWS)
        idx = T.instance_index(COMPS)
        tick = {"source": "events", "producer": "scheduler night", "verb": "scheduler.tick", "nodes": []}
        self.assertEqual(T.route(tick, rules, idx, self.FLOWS)[:2], ("beat", "pulse"))
        both = [{"id": "both", "from": "front", "to": "back", "status": "live",
                 "traffic": [{"source": "events", "producer": "scheduler", "pulse": True},
                             {"source": "events", "verb": "scheduler.tick"}]}]
        self.assertEqual(T.route(tick, T.compile_rules(both), idx, both)[:2], ("both", "declared"))

    def test_pulse_only_flow(self):
        ti, _ = self.index([self.ev(1, NOW - 100), self.ev(2, NOW - 200)])
        doc = ti.snapshot()
        beat = doc["flows"]["beat"]
        self.assertEqual((beat["d24"], beat["d7"], beat["pulse_d7"], beat["heat"]), (0, 0, 2, "pulse"))
        self.assertEqual(beat["by_basis"], {"declared": 0, "inferred": 0})
        self.assertIsNone(beat["last_at"])
        self.assertEqual(beat["pulse_last_at"], NOW - 100)
        self.assertNotIn("beat", doc["crosschecks"]["silent"])
        self.assertEqual(doc["coverage"]["pulse"], 2)
        self.assertEqual(doc["coverage"]["records"], 2)

    def test_volume_and_pulse_together(self):
        ti, _ = self.index([self.ev(1, NOW - 100), self.ev(2, NOW - 50, kind="scheduler.turn")])
        beat = ti.snapshot()["flows"]["beat"]
        self.assertEqual((beat["d24"], beat["d7"], beat["pulse_d7"], beat["heat"]), (1, 1, 1, "hot"))
        self.assertEqual(beat["last_at"], NOW - 50)

    def test_off_status_counts_pulses(self):
        ti, _ = self.index([self.ev(1, NOW - 100, source="cron", kind="cron")])
        self.assertEqual(ti.snapshot()["crosschecks"]["off_status"], [{"flow": "quiet", "status": "planned", "d7": 1}])

    def test_basis_filter_verify_and_expiry(self):
        ti, clock = self.index([self.ev(1, NOW - 100), self.ev(2, NOW - 50, kind="scheduler.turn")])
        recs = ti.snapshot("beat", basis="pulse")["records"]
        self.assertEqual([r["basis"] for r in recs], ["pulse"])
        self.assertTrue(ti.verify()["equal"])
        clock.t += 169 * H
        ti._last_refresh = clock.t - 16
        ti.refresh()
        beat = ti.snapshot()["flows"]["beat"]
        self.assertEqual((beat["pulse_d7"], beat["pulse_last_at"], beat["last_at"]), (0, None, None))


SHOP_ATLAS = {
    "components": [{"id": "web", "instances": ["service:web"]}, {"id": "api", "instances": ["service:api"]},
                   {"id": "worker", "instances": ["service:worker"]}],
    "flows": [
        {"id": "web-api", "from": "web", "to": "api", "status": "live"},
        {"id": "api-worker", "from": "api", "to": "worker", "status": "live",
         "traffic": [{"source": "journal", "producer": "api", "verb": "enqueue"}]},
        {"id": "beat", "from": "worker", "to": "api", "status": "live",
         "traffic": [{"source": "events", "producer": "worker", "verb": "heartbeat", "pulse": True}]},
        {"id": "quiet", "from": "api", "to": "web", "status": "live",
         "traffic": [{"source": "journal", "producer": "api", "verb": "push"}]},
    ],
}
T0 = 1_790_000_000.0


class LastAtTests(unittest.TestCase):
    """last_at and pulse_last_at are exact per basis, however full the 20-entry ring is of the other basis."""

    LA_FLOWS = [{"id": "fl", "from": "front", "to": "back", "status": "live",
                 "traffic": [{"source": "journal", "producer": "gate", "verb": "merged"},
                             {"source": "journal", "producer": "beat", "pulse": True}]}]
    T_DECLARED = NOW - 100 * H
    PULSE_NEWEST = NOW - 10 * H

    def _index(self):
        clock = Clock()
        rows = [jrow(1, self.T_DECLARED)]
        rows += [jrow(2 + i, NOW - 30 * H + i * H, producer="beat", verb="thump") for i in range(21)]
        self.assertEqual(rows[-1]["at"], self.PULSE_NEWEST)
        export = {"components": COMPS, "flows": self.LA_FLOWS}
        ti = T.TrafficIndex(journal_rows=lambda a, s: [r for r in rows if r["id"] > a and r["at"] >= s],
                            event_rows=lambda a, s: [], links=lambda: [],
                            atlas=lambda: ("s1", export), now=clock)
        ti.refresh()
        return ti, clock

    def _check(self, ti):
        fl = ti.snapshot()["flows"]["fl"]
        self.assertEqual(fl["last_at"], self.T_DECLARED)
        self.assertEqual(fl["pulse_last_at"], self.PULSE_NEWEST)
        self.assertGreater(fl["d7"], 0)
        return fl

    def test_declared_survives_a_ring_full_of_pulses(self):
        ti, _ = self._index()
        self.assertTrue(all(c["basis"] == "pulse" for c in ti.snapshot(flow="fl")["records"]))
        self._check(ti)
        self.assertTrue(ti.verify()["equal"])

    def test_the_strip_and_recount_path_keeps_it(self):
        ti, clock = self._index()
        clock.t += 16
        ti.refresh()
        self._check(ti)
        clock.t += 61
        v = ti.verify()
        self.assertTrue(v["equal"], v)

    def test_expiry_drops_only_the_aged_basis(self):
        ti, clock = self._index()
        clock.t += 80 * H  # the declared row (100 h old) leaves the 168 h window; the pulses stay
        ti._last_refresh = clock.t - 16
        ti.refresh()
        fl = ti.snapshot()["flows"]["fl"]
        self.assertIsNone(fl["last_at"])
        self.assertEqual(fl["pulse_last_at"], self.PULSE_NEWEST)
        v = ti.verify()
        self.assertTrue(v["equal"], v)

    def test_a_snapshot_after_an_hour_boundary_without_refresh_agrees_with_verify(self):
        ti, clock = self._index()
        # The declared row is 100 h old; move the clock so it falls below the window floor
        # without a refresh: the counts drop it at snapshot time, and last_at must too.
        clock.t += 69 * H
        fl = ti.snapshot()["flows"]["fl"]
        self.assertIsNone(fl["last_at"])
        self.assertEqual(fl["pulse_last_at"], self.PULSE_NEWEST)
        v = ti.verify()
        self.assertTrue(v["equal"], v)

    def test_verify_compares_last_at(self):
        ti, _ = self._index()
        ti._flow_newest["fl"][(False, "journal")] = self.T_DECLARED + 5
        v = ti.verify()
        self.assertFalse(v["equal"])
        self.assertTrue(any("last_at" in d for d in v["differences"]), v)


class RouteFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="route-files-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def write(self, name, lines):
        path = self.tmp / name
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def files(self):
        journal = self.write("journal.jsonl", [
            json.dumps({"at": T0 - 600, "producer": "web.edge", "verb": "request",
                        "actor": "service:web", "subject": "service:api"}),
            "",
            json.dumps({"id": 7, "at": T0 - 300, "producer": "api.main", "verb": "enqueue",
                        "actor": "service:api", "subject": "service:worker"}),
            json.dumps({"at": T0 - 100, "producer": "api.main", "verb": "enqueue",
                        "actor": "service:api", "subject": "service:worker"}),
            json.dumps({"at": T0 - 50, "producer": "api.main", "verb": "ran", "actor": "service:api", "subject": "service:api"}),
        ])
        events = self.write("events.jsonl", [
            json.dumps({"at": "2026-09-21T12:00:00Z", "kind": "heartbeat", "source": "worker pool",
                        "nodes": ["service:worker"]}),
        ])
        links = self.write("links.json", [json.dumps([
            {"source": "docs", "from": "service:web", "rel": "calls", "to": "service:api", "at": T0 - 10}])])
        return journal, events, links

    def test_routes_all_three_sources_and_assigns_ids(self):
        journal, events, links = self.files()
        doc = T.route_files(SHOP_ATLAS, journal=journal, events=events, links=links)
        self.assertEqual(doc["schema"], T.SCHEMA)
        self.assertEqual(doc["generated"], T0 - 10)  # the newest record is the clock by default
        flows = doc["flows"]
        self.assertEqual(flows["api-worker"]["d24"], 2)
        self.assertEqual(flows["api-worker"]["heat"], "hot")
        self.assertEqual(flows["web-api"]["by_basis"]["inferred"], 2)  # one journal row, one link
        self.assertEqual(doc["sources"]["links"]["records"], 1)
        self.assertEqual(doc["coverage"]["internal"], 1)
        self.assertEqual(doc["crosschecks"]["silent"], ["quiet"])

    def test_pulse_from_an_iso_time(self):
        _, events, _ = self.files()
        doc = T.route_files(SHOP_ATLAS, events=events, now=1_790_000_000.0)
        beat = doc["flows"]["beat"]
        self.assertEqual((beat["heat"], beat["pulse_d7"], beat["d7"]), ("pulse", 1, 0))

    def test_an_explicit_now_and_the_window(self):
        journal, _, _ = self.files()
        later = T.route_files(SHOP_ATLAS, journal=journal, now=T0 + 3 * 86400)
        self.assertEqual((later["flows"]["api-worker"]["d24"], later["flows"]["api-worker"]["d7"]), (0, 2))
        self.assertEqual(later["flows"]["api-worker"]["heat"], "warm")
        gone = T.route_files(SHOP_ATLAS, journal=journal, now=T0 + 30 * 86400)
        self.assertEqual(gone["coverage"]["records"], 0)

    def test_same_files_same_answer(self):
        journal, events, links = self.files()
        a = T.route_files(SHOP_ATLAS, journal, events, links)
        b = T.route_files(SHOP_ATLAS, journal, events, links)
        a["timing"] = b["timing"] = None
        self.assertEqual(a, b)

    def test_no_timestamped_record_and_no_now_routes_at_epoch_zero(self):
        empty = self.write("empty.jsonl", [])
        a = T.route_files(SHOP_ATLAS, journal=empty)
        b = T.route_files(SHOP_ATLAS, journal=empty)
        self.assertEqual(a["generated"], 0.0)
        a["timing"] = b["timing"] = None
        self.assertEqual(a, b)
        self.assertEqual(T.route_files(SHOP_ATLAS)["generated"], 0.0)

    def test_no_files_is_an_empty_snapshot(self):
        doc = T.route_files(SHOP_ATLAS)
        self.assertEqual(doc["coverage"]["records"], 0)
        self.assertEqual(sorted(doc["flows"]), ["api-worker", "beat", "quiet", "web-api"])

    def test_bad_input_raises_a_value_error_naming_the_line(self):
        bad = self.write("bad.jsonl", ['{"at": 1}', "{nope"])
        with self.assertRaisesRegex(ValueError, r"bad\.jsonl:2"):
            T.route_files(SHOP_ATLAS, journal=bad)
        notobj = self.write("list.jsonl", ["[1, 2]"])
        with self.assertRaisesRegex(ValueError, "JSON object"):
            T.route_files(SHOP_ATLAS, journal=notobj)
        with self.assertRaisesRegex(ValueError, "cannot read"):
            T.route_files(SHOP_ATLAS, journal=self.tmp / "missing.jsonl")
        notlist = self.write("links2.json", ['{"a": 1}'])
        with self.assertRaisesRegex(ValueError, "JSON list"):
            T.route_files(SHOP_ATLAS, links=notlist)

    def test_rows_without_a_usable_time_are_left_out(self):
        rows = self.write("j.jsonl", [json.dumps({"at": "yesterday-ish", "producer": "api.main", "verb": "enqueue",
                                                  "actor": "service:api", "subject": "service:worker"})])
        doc = T.route_files(SHOP_ATLAS, journal=rows, now=T0)
        self.assertEqual(doc["coverage"]["records"], 0)

    def test_unrepresentable_integer_rejects_record_files(self):
        for source in ("journal", "events", "links"):
            for value in (10**1000, -(10**1000)):
                rows = [{"at": T0}, {"at": value}]
                path = self.write(source, [json.dumps(rows)] if source == "links"
                                  else [json.dumps(row) for row in rows])
                for now in (None, T0):
                    with self.subTest(source=source, negative=value < 0, now=now):
                        with self.assertRaisesRegex(ValueError,
                                                    "^timestamp is outside the supported numeric range$"):
                            T.route_files(SHOP_ATLAS, **{source: path}, now=now)


class RouteRecordTests(unittest.TestCase):
    def test_route_record_uses_the_index_rules(self):
        ti, _, _, _, _ = make_index()
        got = ti.route_record({"source": "journal", "producer": "gate", "verb": "merged",
                               "actor": "user:gate", "subject": "order:o1"})
        self.assertEqual(got, ("f1", "declared", None))

    def test_malformed_sources_remain_unrouted_with_a_warm_memo(self):
        ti, _, _, _, _ = make_index()
        record = {"producer": "gate", "verb": "merged",
                  "actor": "client:one", "subject": "service:back"}
        expected = (None, "unrouted", "unmapped")
        for source in (["journal"], {"source": "journal"}, [], {}, 3, True, None,
                       "unknown"):
            with self.subTest(source=source):
                record["source"] = source
                self.assertEqual(T.route(record, rules(), idx(), FLOWS), expected)
                self.assertEqual(ti.route_record(record), expected)
                self.assertEqual(ti.route_record(record), expected)
        record["source"] = "journal"
        self.assertEqual(ti.route_record(record), ("f1", "declared", None))
        del record["source"]
        self.assertEqual(ti.route_record(record), expected)
        for source in ("events", "links"):
            with self.subTest(source=source):
                record.update(source=source, nodes=["client:one", "service:back"])
                expected = ((None, "ambiguous", "ambiguous-inference")
                            if source == "events" else ("f1", "inferred", None))
                self.assertEqual(ti.route_record(record), expected)

    def test_signature_is_hashable_for_malformed_record_fields(self):
        for source in (["journal"], {"source": "journal"}, [], {}, 3, True, None):
            with self.subTest(source=source):
                record = {"source": source, "producer": [], "verb": {},
                          "actor": [], "subject": {}, "nodes": [[], {}, "client:one"]}
                key = T.signature(record)
                self.assertEqual({key: "stored"}[T.signature(record)], "stored")


class ParseAtTests(unittest.TestCase):
    def test_unrepresentable_integer_is_invalid_input(self):
        for value in (10**1000, -(10**1000), 1 << 1024, -(1 << 1024)):
            with self.subTest(negative=value < 0, digits=len(str(abs(value)))):
                with self.assertRaisesRegex(ValueError,
                                            "^timestamp is outside the supported numeric range$"):
                    T.parse_at(value)

    def test_numbers_and_iso_strings(self):
        self.assertEqual(T.parse_at(5), 5.0)
        for value in (1 << 1023, -(1 << 1023), 1.25, -1.25):
            self.assertEqual(T.parse_at(value), float(value))
        self.assertIsNone(T.parse_at(True))
        self.assertIsNone(T.parse_at(float("nan")))
        self.assertIsNone(T.parse_at(float("inf")))
        self.assertIsNone(T.parse_at(float("-inf")))
        self.assertEqual(T.parse_at("1970-01-01T00:00:10Z"), 10.0)
        self.assertEqual(T.parse_at("1970-01-01T00:00:10"), 10.0)
        self.assertEqual(T.parse_at("1970-01-01T01:00:10+01:00"), 10.0)
        self.assertIsNone(T.parse_at("soon"))
        self.assertIsNone(T.parse_at(None))


if __name__ == "__main__":
    unittest.main()
