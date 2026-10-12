"""Events with several participants must not invent a single observed crossing."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from estate_atlas import rollup, traffic

NOW = 1_800_000_000.0
COMPONENTS = [
    {"id": "front", "instances": ["client:*"]},
    {"id": "back", "instances": ["service:*"]},
    {"id": "worker", "instances": ["agent:*"]},
]
FLOW = {"id": "request", "from": "front", "to": "back", "status": "live"}
NODES = ["client:a", "service:b", "agent:c"]


def event(nodes=NODES):
    return {"source": "events", "producer": "observer daily", "verb": "updated", "nodes": list(nodes)}


def route(record, flows):
    return traffic.route(record, traffic.compile_rules(flows), traffic.instance_index(COMPONENTS), flows)


class EventRoutingTests(unittest.TestCase):
    def test_three_components_are_ambiguous_regardless_of_available_edges(self):
        for flows in ([], [FLOW], [FLOW, {"id": "work", "from": "back", "to": "worker"}]):
            with self.subTest(flows=flows):
                self.assertEqual(route(event(), flows), (None, "ambiguous", "ambiguous-inference"))

    def test_multiple_nodes_in_two_components_still_infer_one_flow(self):
        for nodes in (["client:a", "service:b"], ["client:a", "client:b", "service:b"],
                      ["client:a", "service:b", "client:a"]):
            with self.subTest(nodes=nodes):
                self.assertEqual(route(event(nodes), [FLOW]), ("request", "inferred", None))

    def test_explicit_volume_or_pulse_rule_takes_precedence(self):
        for pulse, expected in ((False, "declared"), (True, "pulse")):
            with self.subTest(pulse=pulse):
                flow = dict(FLOW, traffic=[{"source": "events", "producer": "observer", "pulse": pulse}])
                self.assertEqual(route(event(), [flow]), ("request", expected, None))

    def test_conflicting_explicit_rules_remain_ambiguous_rules(self):
        declaration = [{"source": "events", "producer": "observer"}]
        flows = [dict(FLOW, traffic=declaration), dict(FLOW, id="other", traffic=declaration)]
        self.assertEqual(route(event(), flows), (None, "ambiguous", "ambiguous-rules"))

    def make_index(self):
        row = {"id": 1, "at": NOW - 86400, "source": "observer daily", "kind": "updated", "nodes": NODES}
        reader = lambda after, since: [row] if after < row["id"] and since <= row["at"] else []
        index = traffic.TrafficIndex(
            journal_rows=lambda after, since: [], event_rows=reader, links=lambda: [],
            atlas=lambda: ("revision", {"components": COMPONENTS, "flows": [FLOW]}), now=lambda: NOW)
        index.refresh()
        return index, reader

    def test_index_keeps_ambiguous_event_out_of_flow_volume(self):
        index, _ = self.make_index()
        snapshot = index.snapshot()
        self.assertEqual(snapshot["coverage"]["records"], 1)
        self.assertEqual(snapshot["coverage"]["ambiguous"], 1)
        self.assertEqual(snapshot["coverage"]["inferred"], 0)
        self.assertEqual(snapshot["flows"]["request"]["d7"], 0)
        self.assertEqual(index.route_record(event()), (None, "ambiguous", "ambiguous-inference"))
        self.assertTrue(index.verify()["equal"])

    def test_daily_history_does_not_seal_an_invented_crossing(self):
        index, reader = self.make_index()
        with tempfile.TemporaryDirectory() as directory:
            history = rollup.TrafficHistory(Path(directory) / "history.db")
            try:
                result = history.rollup(index, lambda after, since: [], reader, lambda: [], NOW)
                self.assertGreater(result["days_written"], 0)
                self.assertEqual(result["rows_written"], 0)
                self.assertEqual(history.read_all(), [])
            finally:
                history.close()


if __name__ == "__main__":
    unittest.main()
