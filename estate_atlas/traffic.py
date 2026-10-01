"""Traffic: real records routed onto the atlas flows.

Pure core: no I/O in the engine. The caller injects readers; `route_files` is the file-backed convenience that
the `route` verb uses. The output document is `estate-atlas-traffic@1`.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import math
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = "estate-atlas-traffic@1"
WINDOW_HOURS = 168
BUCKET_S = 3600
WINDOW_S = WINDOW_HOURS * 3600
DAY_S = 24 * 3600
THROTTLE_S = 15.0
VERIFY_TTL_S = 60.0
MAX_MEMO = 50_000
MAX_ROWS = 200_000
PAGE = 5_000
RING = 20
ERROR_MAX = 200
SOURCES = ("journal", "events", "links")
BASES = ("declared", "inferred", "internal", "unrouted", "ambiguous")
REASONS = ("no-flow", "unmapped", "ambiguous-rules", "ambiguous-inference")


def _err(text: Any) -> str:
    return str(text)[:ERROR_MAX]


def parse_at(value: Any) -> float | None:
    """Epoch seconds from a number or an ISO 8601 string (a trailing `Z` is UTC, a naive time is UTC); else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        out = float(value)
        return out if math.isfinite(out) else None
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            when = datetime.datetime.fromisoformat(text)
        except ValueError:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=datetime.timezone.utc)
        return when.timestamp()
    return None


def _valid_ref(ref: Any) -> bool:
    """A node ref is `<kind>:<name>`: a non-empty string with a colon that has text on both sides."""
    if not isinstance(ref, str) or ":" not in ref:
        return False
    head, _, tail = ref.partition(":")
    return bool(head) and bool(tail)


def instance_index(components: Any) -> dict[str, dict[str, str]]:
    """{"exact": {ref: component}, "kind": {kind: component}} from component `instances`; first claim wins."""
    exact: dict[str, str] = {}
    kind: dict[str, str] = {}
    for comp in components if isinstance(components, list) else []:
        if not isinstance(comp, dict) or not isinstance(comp.get("id"), str):
            continue
        instances = comp.get("instances")
        for ref in instances if isinstance(instances, list) else []:
            if not _valid_ref(ref):
                continue
            head, _, tail = ref.partition(":")
            if tail == "*":
                kind.setdefault(head, comp["id"])
            else:
                exact.setdefault(ref, comp["id"])
    return {"exact": exact, "kind": kind}


def component_of(ref: Any, index: dict[str, Any]) -> str | None:
    """The component a node ref belongs to: an exact instance beats a `<kind>:*` instance; else None."""
    if not _valid_ref(ref):
        return None
    exact = index.get("exact", {})
    if ref in exact:
        return exact[ref]
    return index.get("kind", {}).get(ref.split(":", 1)[0])


def match(pattern: str | None, value: str | None) -> bool:
    """None or `*` matches anything (even a None value); a trailing `*` is startswith; else equality."""
    if pattern is None:
        return True
    if not isinstance(pattern, str):
        return True
    if pattern == "*":
        return True
    if value is None or not isinstance(value, str):
        return False
    if pattern.endswith("*"):
        return value.startswith(pattern[:-1])
    return value == pattern


def producer_head(source: Any, producer: Any) -> str:
    """The matchable head: journal up to first `.`, links up to first `:`, events up to first space."""
    if not isinstance(producer, str) or not producer:
        return ""
    if source == "journal":
        return producer.split(".", 1)[0]
    if source == "links":
        return producer.split(":", 1)[0]
    if source == "events":
        return producer.split(" ", 1)[0]
    return producer


def _kind_of(ref: Any) -> str | None:
    if isinstance(ref, str) and ref:
        return ref.split(":")[0] if ":" in ref else ref
    return None


class Rules:
    """Compiled traffic rules indexed by source; malformed rules counted in `invalid`."""

    def __init__(self) -> None:
        self.by_source: dict[str, list[tuple[str, dict[str, Any]]]] = {s: [] for s in SOURCES}
        self.invalid = 0
        self.flow_counts: dict[str, int] = {}


def _valid_field(value: Any, allow_list: bool = False) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return True
    if allow_list and isinstance(value, list):
        return all(isinstance(v, str) for v in value)
    return False


def compile_rules(flows: Any) -> Rules:
    out = Rules()
    if not isinstance(flows, list):
        return out
    for flow in flows:
        if not isinstance(flow, dict):
            continue
        fid = flow.get("id")
        if not isinstance(fid, str) or not fid:
            continue
        traffic = flow.get("traffic")
        if traffic is None:
            continue
        if not isinstance(traffic, list):
            continue
        for rule in traffic:
            if not isinstance(rule, dict):
                out.invalid += 1
                continue
            src = rule.get("source")
            if src not in SOURCES:
                out.invalid += 1
                continue
            if not _valid_field(rule.get("producer")):
                out.invalid += 1
                continue
            if not _valid_field(rule.get("verb"), allow_list=True):
                out.invalid += 1
                continue
            if not _valid_field(rule.get("actor")):
                out.invalid += 1
                continue
            if not _valid_field(rule.get("subject")):
                out.invalid += 1
                continue
            norm = {"producer": rule.get("producer"), "verb": rule.get("verb"),
                    "actor": rule.get("actor"), "subject": rule.get("subject"),
                    "pulse": rule.get("pulse") is True}
            out.by_source[src].append((fid, norm))
            out.flow_counts[fid] = out.flow_counts.get(fid, 0) + 1
    return out


def _verb_matches(pattern: Any, value: Any) -> bool:
    if isinstance(pattern, list):
        return any(match(p, value) for p in pattern)
    return match(pattern, value)


def _rule_matches(rule: dict[str, Any], source: Any, phead: str, verb: Any,
                  actor: Any, subject: Any, nodes: list[Any]) -> bool:
    if not match(rule.get("producer"), phead):
        return False
    if not _verb_matches(rule.get("verb"), verb if isinstance(verb, str) else None):
        return False
    if source == "events":
        if not match(rule.get("actor"), None):
            return False
        pat = rule.get("subject")
        if pat is None or pat == "*":
            return True
        if not isinstance(pat, str):
            return True
        return any(match(pat, n) if isinstance(n, str) else False for n in nodes)
    return match(rule.get("actor"), actor) and match(rule.get("subject"), subject)


def route(record: dict[str, Any], rules: Rules, index: dict[str, Any],
          flows: list[dict[str, Any]]) -> tuple[str | None, str, str | None]:
    """(flow_id | None, basis, reason | None) for one record, exactly once."""
    source = record.get("source")
    phead = producer_head(source, record.get("producer"))
    verb = record.get("verb")
    actor = record.get("actor")
    subject = record.get("subject")
    nodes = record.get("nodes") if isinstance(record.get("nodes"), list) else []
    declared: list[str] = []
    has_volume: dict[str, bool] = {}
    for fid, rule in rules.by_source.get(source, []) if isinstance(source, str) else []:
        if _rule_matches(rule, source, phead, verb, actor, subject, nodes):
            if fid not in has_volume:
                declared.append(fid)
                has_volume[fid] = rule.get("pulse") is not True
            elif rule.get("pulse") is not True:
                has_volume[fid] = True
    if len(declared) == 1:
        fid = declared[0]
        if has_volume.get(fid):
            return fid, "declared", None
        return fid, "pulse", None
    if len(declared) > 1:
        return None, "ambiguous", "ambiguous-rules"
    if source in ("journal", "links"):
        ca = component_of(actor, index)
        cs = component_of(subject, index)
        if ca is None and cs is None:
            return None, "unrouted", "unmapped"
        if ca is not None and cs is not None:
            if ca == cs:
                return None, "internal", None
            cands = [f.get("id") for f in flows
                     if isinstance(f, dict) and f.get("from") == ca and f.get("to") == cs]
            if len(cands) == 1:
                return cands[0], "inferred", None
            if not cands:
                return None, "unrouted", "no-flow"
            return None, "ambiguous", "ambiguous-inference"
        return None, "unrouted", "unmapped"
    if source == "events":
        comps = {component_of(n, index) for n in nodes if isinstance(n, str)}
        comps.discard(None)
        if not comps:
            return None, "unrouted", "unmapped"
        if len(comps) == 1:
            return None, "internal", None
        cands = [f.get("id") for f in flows
                 if isinstance(f, dict) and f.get("from") in comps
                 and f.get("to") in comps and f.get("from") != f.get("to")]
        if len(cands) == 1:
            return cands[0], "inferred", None
        if not cands:
            return None, "unrouted", "no-flow"
        return None, "ambiguous", "ambiguous-inference"
    return None, "unrouted", "unmapped"


def signature(record: dict[str, Any]) -> tuple:
    """A hashable key over (source, producer head, verb, actor, subject, sorted nodes)."""
    source = record.get("source")
    phead = producer_head(source, record.get("producer"))
    verb = record.get("verb")
    actor = record.get("actor")
    subject = record.get("subject")
    nodes = record.get("nodes") if isinstance(record.get("nodes"), list) else []
    clean = tuple(sorted(n for n in nodes if isinstance(n, str)))
    return (source, phead, verb if isinstance(verb, str) else None,
            actor if isinstance(actor, str) else None,
            subject if isinstance(subject, str) else None, clean)


def _bucket(at: float) -> int:
    return math.floor(float(at) / BUCKET_S) * BUCKET_S


def _crossing(source: str, basis: str, at: float | None, phead: str, verb: Any,
              actor: Any, subject: Any, nodes: list[str], eid: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"source": source, "basis": basis, "at": at,
                           "producer": phead, "verb": verb if isinstance(verb, str) else ""}
    if eid is not None:
        out["id"] = eid
    out["actor"] = actor if isinstance(actor, str) else None
    out["subject"] = subject if isinstance(subject, str) else None
    if source == "events":
        out["nodes"] = list(nodes)
    return out


def _row_id(row: dict[str, Any]) -> int | None:
    rid = row.get("id")
    return rid if isinstance(rid, int) and not isinstance(rid, bool) else None


class TrafficIndex:
    """The traffic cache over 7 days of the ledgers. Thread-safe; throttled; bounded."""

    def __init__(self, *, journal_rows: Any, event_rows: Any, links: Any,
                 atlas: Any, now: Any = time.time) -> None:
        self._journal_rows = journal_rows
        self._event_rows = event_rows
        self._links_fn = links
        self._atlas_fn = atlas
        self._now = now
        self._lock = threading.Lock()
        self._last_refresh: float | None = None
        self._sha: str | None = None
        self._atlas_ok = False
        self._atlas_error: str | None = None
        self._built = False
        self._rules = Rules()
        self._index: dict[str, Any] = {"exact": {}, "kind": {}}
        self._flows_list: list[dict[str, Any]] = []
        self._flows_meta: dict[str, dict[str, Any]] = {}
        self._journal_cursor = 0
        self._event_cursor = 0
        self._flow_buckets: dict[str, dict[int, dict[tuple, int]]] = {}
        self._flow_undated: dict[str, dict[tuple, int]] = {}
        # newest dated `at` per flow and (is_pulse, source): exact, independent of the ring's 20 entries
        self._flow_newest: dict[str, dict[tuple[bool, str], float]] = {}
        self._flow_rings: dict[str, list[dict[str, Any]]] = {}
        self._cov_buckets: dict[int, dict[tuple, int]] = {}
        self._cov_undated: dict[tuple, int] = {}
        self._unrouted: dict[tuple, dict[str, Any]] = {}
        self._unrouted_undated: dict[tuple, dict[str, Any]] = {}
        self._memo: dict[tuple, tuple] = {}
        self._src: dict[str, dict[str, Any]] = {
            "journal": {"ok": True, "records": 0, "error": None},
            "events": {"ok": True, "records": 0, "error": None},
            "links": {"ok": True, "records": 0, "undated": 0, "error": None},
        }
        self._refresh_ms = 0.0
        self._rebuild_ms: float | None = None
        self._rebuilt_at: float | None = None
        self._verify_at: float | None = None
        self._verify_cached: dict[str, Any] | None = None

    @property
    def built(self) -> bool:
        """True once a rebuild read an atlas; False when the atlas was never read or could not be."""
        return self._built

    @property
    def flow_count(self) -> int:
        return len(self._flows_list)

    # -- refresh ------------------------------------------------------------------
    def refresh(self) -> dict[str, Any]:
        t0 = float(self._now())
        with self._lock:
            if self._last_refresh is not None and t0 - self._last_refresh < THROTTLE_S:
                return {"throttled": True}
            start = time.perf_counter()
            window_start = float(_bucket(t0 - WINDOW_S))  # whole buckets: rebuild and refresh agree
            try:
                sha, doc = self._atlas_fn()
            except Exception as exc:  # noqa: BLE001 - no atlas: empty flows, zeroed counts
                self._atlas_ok = False
                self._atlas_error = _err(f"{type(exc).__name__}: {exc}")
                self._sha = None
                self._clear_all()
                self._refresh_ms = (time.perf_counter() - start) * 1000.0
                self._last_refresh = t0
                return {"throttled": False}
            if not isinstance(doc, dict):
                self._atlas_ok = False
                self._atlas_error = "atlas unavailable"
                self._sha = None
                self._clear_all()
                self._refresh_ms = (time.perf_counter() - start) * 1000.0
                self._last_refresh = t0
                return {"throttled": False}
            sha_s = sha if isinstance(sha, str) else None
            if not self._built or sha_s != self._sha:
                self._rebuild(doc, sha_s, t0, window_start, start)
            else:
                self._expire(window_start)
                self._read_journal(window_start, incremental=True)
                self._read_events(window_start, incremental=True)
                self._recount_links(window_start)
                self._atlas_ok = True
                self._atlas_error = None
                self._refresh_ms = (time.perf_counter() - start) * 1000.0
            self._last_refresh = t0
            return {"throttled": False}

    def _clear_all(self) -> None:
        self._rules = Rules()
        self._index = {"exact": {}, "kind": {}}
        self._flows_list = []
        self._flows_meta = {}
        self._journal_cursor = 0
        self._event_cursor = 0
        self._flow_buckets = {}
        self._flow_undated = {}
        self._flow_newest = {}
        self._flow_rings = {}
        self._cov_buckets = {}
        self._cov_undated = {}
        self._unrouted = {}
        self._unrouted_undated = {}
        self._memo = {}
        self._built = False

    def _rebuild(self, doc: dict[str, Any], sha: str | None, t0: float,
                 window_start: float, start: float) -> None:
        self._clear_all()
        comps = doc.get("components") if isinstance(doc.get("components"), list) else []
        flows = doc.get("flows") if isinstance(doc.get("flows"), list) else []
        self._index = instance_index(comps)
        self._rules = compile_rules(flows)
        self._flows_list = [f for f in flows if isinstance(f, dict) and isinstance(f.get("id"), str)]
        for f in self._flows_list:
            fid = f["id"]
            self._flows_meta[fid] = {"from": f.get("from"), "to": f.get("to"),
                                     "status": f.get("status") if isinstance(f.get("status"), str) else "unknown",
                                     "rules": self._rules.flow_counts.get(fid, 0)}
            self._flow_buckets[fid] = {}
            self._flow_undated[fid] = {}
            self._flow_newest[fid] = {}
            self._flow_rings[fid] = []
        self._sha = sha
        self._atlas_ok = True
        self._atlas_error = None
        self._read_journal(window_start, incremental=False)
        self._read_events(window_start, incremental=False)
        self._recount_links(window_start)
        self._built = True
        self._rebuild_ms = (time.perf_counter() - start) * 1000.0
        self._rebuilt_at = t0
        self._refresh_ms = self._rebuild_ms

    def _expire(self, window_start: float) -> None:
        floor_start = _bucket(window_start)
        for fid, ring in self._flow_rings.items():
            kept = [c for c in ring if not isinstance(c.get("at"), (int, float)) or c["at"] >= floor_start]
            if len(kept) != len(ring):
                self._flow_rings[fid] = kept
        for newest in self._flow_newest.values():
            # a key whose newest row is older than the window has only rows older than the window
            for k in [k for k, v in newest.items() if v < floor_start]:
                del newest[k]
        for fid, buckets in self._flow_buckets.items():
            for b in [k for k in buckets if k < floor_start]:
                del buckets[b]
        for b in [k for k in self._cov_buckets if k < floor_start]:
            del self._cov_buckets[b]
        for k in [k for k in self._unrouted if k[0] < floor_start]:
            del self._unrouted[k]

    def _drain(self, reader: Any, since: float, cursor: int) -> tuple[list[dict[str, Any]], int]:
        """Pages of up to 5,000 rows past the cursor, at most 200,000 per refresh; returns (rows, new cursor).

        Only rows with an integer id strictly past the cursor count, so a reader that repeats a row can never
        count it twice; a short page ends the read; the cursor is the last row kept, so rows past the cap are
        read at the next refresh rather than skipped."""
        out: list[dict[str, Any]] = []
        after = cursor
        while len(out) < MAX_ROWS:
            rows = reader(after, since)
            if not isinstance(rows, list) or not rows:
                break
            fresh = sorted((r for r in rows if isinstance(r, dict) and _row_id(r) is not None and _row_id(r) > after),
                           key=_row_id)
            if not fresh:
                break  # the cursor cannot advance: stop rather than loop forever
            out.extend(fresh[:MAX_ROWS - len(out)])
            after = _row_id(out[-1])
            if len(rows) < PAGE:
                break
        return out, after

    def _read_journal(self, window_start: float, incremental: bool) -> None:
        since = window_start
        after = self._journal_cursor if incremental else 0
        try:
            rows, top = self._drain(self._journal_rows, since, after)
        except Exception as exc:  # noqa: BLE001 - one bad source never breaks the others
            self._src["journal"] = {"ok": False, "records": 0, "error": _err(f"{type(exc).__name__}: {exc}")}
            return
        self._src["journal"] = {"ok": True, "records": 0, "error": None}
        for r in rows:
            at = r.get("at")
            at_f = float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else None
            if at_f is None or at_f < window_start:
                continue
            rec = {"source": "journal", "producer": r.get("producer"), "verb": r.get("verb"),
                   "actor": r.get("actor"), "subject": r.get("subject")}
            self._ingest(rec, at_f, r.get("id"))
        self._journal_cursor = top

    def _read_events(self, window_start: float, incremental: bool) -> None:
        since = window_start
        after = self._event_cursor if incremental else 0
        try:
            rows, top = self._drain(self._event_rows, since, after)
        except Exception as exc:  # noqa: BLE001
            self._src["events"] = {"ok": False, "records": 0, "error": _err(f"{type(exc).__name__}: {exc}")}
            return
        self._src["events"] = {"ok": True, "records": 0, "error": None}
        for r in rows:
            at = r.get("at")
            at_f = float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else None
            if at_f is None or at_f < window_start:
                continue
            nodes = r.get("nodes") if isinstance(r.get("nodes"), list) else []
            rec = {"source": "events", "producer": r.get("source"), "verb": r.get("kind"),
                   "nodes": [n for n in nodes if isinstance(n, str)]}
            self._ingest(rec, at_f, r.get("id"))
        self._event_cursor = top

    def _strip_links(self) -> None:
        for fid, buckets in self._flow_buckets.items():
            for b in list(buckets):
                cell = buckets[b]
                for k in [k for k in cell if k[1] == "links"]:
                    del cell[k]
                if not cell:
                    del buckets[b]
        for b in list(self._cov_buckets):
            cell = self._cov_buckets[b]
            for k in [k for k in cell if k[1] == "links"]:
                del cell[k]
            if not cell:
                del self._cov_buckets[b]
        for k in [k for k in self._cov_undated if k[1] == "links"]:
            del self._cov_undated[k]
        for fid in self._flow_undated:
            cell = self._flow_undated[fid]
            for k in [k for k in cell if k[1] == "links"]:
                del cell[k]
        for fid, ring in self._flow_rings.items():
            kept = [c for c in ring if c.get("source") != "links"]
            if len(kept) != len(ring):
                self._flow_rings[fid] = kept
        for k in [k for k in self._unrouted if k[1] == "links"]:
            del self._unrouted[k]
        for k in [k for k in self._unrouted_undated if k[0] == "links"]:
            del self._unrouted_undated[k]
        for newest in self._flow_newest.values():
            for k in [k for k in newest if k[1] == "links"]:
                del newest[k]

    def _recount_links(self, window_start: float) -> None:
        self._strip_links()
        try:
            pairs = self._links_fn()
        except Exception as exc:  # noqa: BLE001
            self._src["links"] = {"ok": False, "records": 0, "undated": 0,
                                  "error": _err(f"{type(exc).__name__}: {exc}")}
            return
        self._src["links"] = {"ok": True, "records": 0, "undated": 0, "error": None}
        if not isinstance(pairs, list):
            return
        for item in pairs:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                doc_source, link = item
            elif isinstance(item, dict) and isinstance(item.get("link"), dict):
                doc_source, link = item.get("doc_source"), item.get("link")
            else:
                continue
            if not isinstance(link, dict):
                continue
            raw_at = link.get("at")
            at_f: float | None = None
            if raw_at is not None:
                if isinstance(raw_at, (int, float)) and not isinstance(raw_at, bool):
                    at_f = float(raw_at)
                elif isinstance(raw_at, str) and raw_at:
                    at_f = parse_at(raw_at)
            if at_f is not None and at_f < window_start:
                continue
            rec = {"source": "links", "producer": doc_source, "verb": link.get("rel"),
                   "actor": link.get("from"), "subject": link.get("to")}
            self._ingest(rec, at_f, None)

    def _route_memo(self, rec: dict[str, Any]) -> tuple[str | None, str, str | None]:
        if len(self._memo) >= MAX_MEMO:
            self._memo = {}
        sig = signature(rec)
        hit = self._memo.get(sig)
        if hit is not None:
            return hit  # type: ignore[return-value]
        res = route(rec, self._rules, self._index, self._flows_list)
        self._memo[sig] = res
        return res

    def route_record(self, record: dict[str, Any]) -> tuple[str | None, str, str | None]:
        """(flow id | None, basis, reason | None) for one record with the rules of the current build."""
        with self._lock:
            return self._route_memo(record)

    def _ingest(self, rec: dict[str, Any], at: float | None, eid: Any) -> None:
        fid, basis, reason = self._route_memo(rec)
        source = rec.get("source")
        phead = producer_head(source, rec.get("producer"))
        verb = rec.get("verb") if isinstance(rec.get("verb"), str) else ""
        actor = rec.get("actor")
        subject = rec.get("subject")
        nodes = rec.get("nodes") if isinstance(rec.get("nodes"), list) else []
        nodes = [n for n in nodes if isinstance(n, str)]
        crossing = _crossing(source, basis, at, phead, verb, actor, subject, nodes,
                             eid if isinstance(eid, int) and not isinstance(eid, bool) else None)
        if at is None:
            self._cov_undated[(basis, source)] = self._cov_undated.get((basis, source), 0) + 1
        else:
            b = _bucket(at)
            cell = self._cov_buckets.setdefault(b, {})
            cell[(basis, source)] = cell.get((basis, source), 0) + 1
        if basis in ("declared", "inferred", "pulse") and fid is not None and fid in self._flows_meta:
            if at is None:
                cell = self._flow_undated[fid]
                cell[(basis, source)] = cell.get((basis, source), 0) + 1
            else:
                b = _bucket(at)
                cell = self._flow_buckets[fid].setdefault(b, {})
                cell[(basis, source)] = cell.get((basis, source), 0) + 1
                newest = self._flow_newest[fid]
                nk = (basis == "pulse", source)
                cur = newest.get(nk)
                if cur is None or at > cur:
                    newest[nk] = at
            ring = self._flow_rings[fid]
            ring.append(crossing)
            ring.sort(key=lambda c: (c.get("at") is not None,
                                     c.get("at") or 0,
                                     c.get("id") if isinstance(c.get("id"), int) else -1),
                      reverse=True)
            del ring[RING:]
        elif basis in ("unrouted", "ambiguous"):
            if source == "events":
                kinds = sorted({_kind_of(n) for n in nodes if _kind_of(n)})
                skind = "+".join(kinds) if kinds else None
                akind = None
            else:
                akind = _kind_of(actor)
                skind = _kind_of(subject)
            if at is None:
                key = (source, phead, verb, akind, skind, reason)
                slot = self._unrouted_undated.get(key)
                if slot is None:
                    self._unrouted_undated[key] = {"count": 1, "example": crossing}
                else:
                    slot["count"] += 1
            else:
                key = (_bucket(at), source, phead, verb, akind, skind, reason)
                slot = self._unrouted.get(key)
                if slot is None:
                    self._unrouted[key] = {"count": 1, "example": crossing}
                else:
                    slot["count"] += 1
                    old = slot["example"]
                    oa = old.get("at") or 0
                    if at > oa:
                        slot["example"] = crossing

    def _last_at(self, fid: str, pulse: bool) -> float | None:
        vals = [v for (is_pulse, _src), v in self._flow_newest.get(fid, {}).items() if is_pulse == pulse]
        return max(vals) if vals else None

    # -- snapshot -----------------------------------------------------------------
    def snapshot(self, flow: str | None = None, source: str | None = None,
                 basis: str | None = None) -> dict[str, Any]:
        with self._lock:
            t = float(self._now())
            return self._snapshot_locked(t, flow, source, basis)

    def _flow_totals(self, fid: str, d24_start: float, floor: int) -> dict[str, Any]:
        buckets = self._flow_buckets.get(fid, {})
        und = self._flow_undated.get(fid, {})
        d24 = d7 = pulse_d7 = 0
        by_basis = {"declared": 0, "inferred": 0}
        by_source = {"journal": 0, "events": 0, "links": 0}
        for b, cell in buckets.items():
            if b < floor:
                continue
            for (bs, src), n in cell.items():
                if bs == "pulse":
                    pulse_d7 += n
                    continue
                d7 += n
                if b >= d24_start:
                    d24 += n
                if bs in by_basis and src in by_source:
                    by_basis[bs] += n
                    by_source[src] += n
        for (bs, src), n in und.items():
            if bs == "pulse":
                pulse_d7 += n
                continue
            d7 += n
            if bs in by_basis and src in by_source:
                by_basis[bs] += n
                by_source[src] += n
        return {"d24": d24, "d7": d7, "pulse_d7": pulse_d7,
                "by_basis": by_basis, "by_source": by_source}

    def _snapshot_locked(self, t: float, flow: str | None, source: str | None,
                         basis: str | None) -> dict[str, Any]:
        d24_start = t - DAY_S
        floor = _bucket(t - WINDOW_S)
        flows: dict[str, Any] = {}
        for fid, meta in self._flows_meta.items():
            tot = self._flow_totals(fid, d24_start, floor)
            if tot["d24"] > 0:
                heat = "hot"
            elif tot["d7"] > 0:
                heat = "warm"
            elif tot["pulse_d7"] > 0:
                heat = "pulse"
            else:
                heat = "silent"
            flows[fid] = {"status": meta["status"], "rules": meta["rules"],
                          "d24": tot["d24"], "d7": tot["d7"],
                          "last_at": self._last_at(fid, False),
                          "heat": heat, "by_basis": tot["by_basis"],
                          "by_source": tot["by_source"],
                          "pulse_d7": tot["pulse_d7"],
                          "pulse_last_at": self._last_at(fid, True)}
        coverage = {"records": 0, "declared": 0, "inferred": 0, "internal": 0,
                    "unrouted": 0, "ambiguous": 0, "pulse": 0}
        src_counts = {"journal": 0, "events": 0, "links": 0}
        for b, cell in self._cov_buckets.items():
            if b < floor:
                continue
            for (bs, src), n in cell.items():
                if bs in coverage:
                    coverage[bs] += n
                    coverage["records"] += n
                if src in src_counts:
                    src_counts[src] += n
        for (bs, src), n in self._cov_undated.items():
            if bs in coverage:
                coverage[bs] += n
                coverage["records"] += n
            if src in src_counts:
                src_counts[src] += n
        link_undated = sum(n for (bs, src), n in self._cov_undated.items() if src == "links")
        sources = {
            "journal": dict(self._src["journal"], records=src_counts["journal"]),
            "events": dict(self._src["events"], records=src_counts["events"]),
            "links": {"ok": self._src["links"]["ok"], "records": src_counts["links"],
                      "undated": link_undated, "error": self._src["links"]["error"]},
        }
        silent = sorted(fid for fid, meta in self._flows_meta.items()
                        if meta["status"] == "live" and meta["rules"] > 0
                        and flows[fid]["d7"] == 0 and flows[fid]["pulse_d7"] == 0)
        off_status = sorted(
            ({"flow": fid, "status": meta["status"],
              "d7": flows[fid]["d7"] + flows[fid]["pulse_d7"]}
             for fid, meta in self._flows_meta.items()
             if meta["status"] != "live" and flows[fid]["d7"] + flows[fid]["pulse_d7"] > 0),
            key=lambda d: d["flow"])
        rule_gaps = sorted(
            ({"flow": fid, "inferred": flows[fid]["by_basis"]["inferred"]}
             for fid, meta in self._flows_meta.items()
             if meta["rules"] > 0 and flows[fid]["by_basis"]["inferred"] > 0),
            key=lambda d: d["flow"])
        agg: dict[tuple, dict[str, Any]] = {}
        for (hour, src, prod, verb, ak, sk, reason), slot in self._unrouted.items():
            if hour < floor:
                continue
            key = (src, prod, verb, ak, sk, reason)
            cur = agg.get(key)
            if cur is None:
                agg[key] = {"d7": slot["count"], "example": slot["example"]}
            else:
                cur["d7"] += slot["count"]
                ea = cur["example"].get("at") or 0
                na = slot["example"].get("at") or 0
                if na > ea:
                    cur["example"] = slot["example"]
        for (src, prod, verb, ak, sk, reason), slot in self._unrouted_undated.items():
            key = (src, prod, verb, ak, sk, reason)
            cur = agg.get(key)
            if cur is None:
                agg[key] = {"d7": slot["count"], "example": slot["example"]}
            else:
                cur["d7"] += slot["count"]
        unrouted = sorted(
            ({"source": k[0], "producer": k[1], "verb": k[2], "actor_kind": k[3],
              "subject_kind": k[4], "reason": k[5], "d7": v["d7"], "example": v["example"]}
             for k, v in agg.items()),
            key=lambda d: (-d["d7"], d["source"], d["producer"], d["verb"],
                           d["actor_kind"] or "", d["subject_kind"] or "", d["reason"]))
        doc: dict[str, Any] = {
            "schema": SCHEMA, "generated": t,
            "source": {"atlas_sha": self._sha, "ok": bool(self._atlas_ok),
                       "error": self._atlas_error},
            "window": {"hours": WINDOW_HOURS, "bucket_s": BUCKET_S},
            "timing": {"refresh_ms": self._refresh_ms, "rebuild_ms": self._rebuild_ms,
                       "rebuilt_at": self._rebuilt_at},
            "sources": sources, "coverage": coverage, "flows": flows,
            "crosschecks": {"silent": silent, "off_status": off_status,
                            "rule_gaps": rule_gaps, "unrouted": unrouted[:20]},
        }
        if flow is not None:
            ring = self._flow_rings.get(flow, [])
            recs = [c for c in ring
                    if (source is None or c.get("source") == source)
                    and (basis is None or c.get("basis") == basis)][:RING]
            doc["records"] = recs
        return doc

    # -- verify -------------------------------------------------------------------
    def verify(self) -> dict[str, Any]:
        t = float(self._now())
        with self._lock:
            if self._verify_at is not None and t - self._verify_at < VERIFY_TTL_S \
                    and self._verify_cached is not None:
                return dict(self._verify_cached)
        fresh = TrafficIndex(journal_rows=self._journal_rows, event_rows=self._event_rows,
                             links=self._links_fn, atlas=self._atlas_fn, now=lambda: t)
        with fresh._lock:
            start = time.perf_counter()
            window_start = float(_bucket(t - WINDOW_S))
            try:
                sha, doc = self._atlas_fn()
            except Exception as exc:  # noqa: BLE001
                res = {"equal": False, "rebuild_ms": 0.0,
                       "differences": [_err(f"atlas: {type(exc).__name__}: {exc}")][:20]}
                with self._lock:
                    self._verify_at = t
                    self._verify_cached = dict(res)
                return dict(res)
            if not isinstance(doc, dict):
                res = {"equal": False, "rebuild_ms": 0.0, "differences": ["atlas unavailable"][:20]}
                with self._lock:
                    self._verify_at = t
                    self._verify_cached = dict(res)
                return dict(res)
            sha_s = sha if isinstance(sha, str) else None
            fresh._rebuild(doc, sha_s, t, window_start, start)
            rebuild_ms = fresh._rebuild_ms or 0.0
        with self._lock:
            mine = self._snapshot_locked(t, None, None, None)
        with fresh._lock:
            his = fresh._snapshot_locked(t, None, None, None)  # both snapshots at the same instant
        diffs: list[str] = []
        if mine["coverage"] != his["coverage"]:
            diffs.append(f"coverage {mine['coverage']} != {his['coverage']}")
        for fid in sorted(set(mine["flows"]) | set(his["flows"])):
            a, b = mine["flows"].get(fid), his["flows"].get(fid)
            if a is None or b is None:
                diffs.append(f"flow {fid}: present in only one snapshot")
                continue
            for key in ("d24", "d7", "pulse_d7", "by_basis", "by_source", "last_at", "pulse_last_at"):
                if a[key] != b[key]:
                    diffs.append(f"flow {fid} {key}: {a[key]} != {b[key]}")
        res = {"equal": not diffs, "rebuild_ms": rebuild_ms, "differences": diffs[:20]}
        with self._lock:
            self._verify_at = t
            self._verify_cached = dict(res)
        return dict(res)


# -- files ------------------------------------------------------------------------
def _read_jsonl(path: Any) -> list[dict[str, Any]]:
    """The object rows of a JSONL file; blank lines are skipped, a bad line raises ValueError naming file:line."""
    rows: list[dict[str, Any]] = []
    where = Path(path)
    try:
        text = where.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise ValueError(f"cannot read {where}: {exc.strerror or type(exc).__name__}") from None
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            raise ValueError(f"{where}:{number}: not valid JSON") from None
        if not isinstance(row, dict):
            raise ValueError(f"{where}:{number}: each line must be a JSON object")
        rows.append(row)
    return rows


def _with_ids(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copies of the rows with an integer `id` and a numeric `at`.

    A row without an integer id gets the next id after the largest one in the file, in file order. An `at` that
    is not a number or an ISO 8601 time becomes None, and the index then leaves the row out."""
    top = max((r["id"] for r in rows if _row_id(r) is not None), default=0)
    out: list[dict[str, Any]] = []
    for row in rows:
        row = dict(row)
        if _row_id(row) is None:
            top += 1
            row["id"] = top
        row["at"] = parse_at(row.get("at"))
        out.append(row)
    return sorted(out, key=lambda r: r["id"])


def _reader(rows: list[dict[str, Any]]) -> Any:
    def read(after_id: int, since: float) -> list[dict[str, Any]]:
        return [r for r in rows if r["id"] > after_id
                and isinstance(r["at"], float) and r["at"] >= since][:PAGE]
    return read


def route_files(doc: dict[str, Any], journal: Any = None, events: Any = None, links: Any = None,
                now: float | None = None) -> dict[str, Any]:
    """Route record files onto the flows of `doc` and return the `estate-atlas-traffic@1` snapshot.

    `journal` is a JSONL file of `{id?, at, producer, verb, actor, subject}`, `events` a JSONL file of
    `{id?, at, kind, source, nodes}`, `links` a JSON list of `{source, from, rel, to, at?}`. Missing ids are
    assigned. With `now=None` the snapshot is taken at the newest record's time, so routing a captured file
    gives the same answer every time; pass `time.time()` to route against the wall clock.
    """
    jrows = _with_ids(_read_jsonl(journal)) if journal is not None else []
    erows = _with_ids(_read_jsonl(events)) if events is not None else []
    pairs: list[tuple[Any, dict[str, Any]]] = []
    if links is not None:
        try:
            raw = json.loads(Path(links).read_text(encoding="utf-8-sig"))
        except OSError as exc:
            raise ValueError(f"cannot read {links}: {exc.strerror or type(exc).__name__}") from None
        except ValueError:
            raise ValueError(f"{links}: not valid JSON") from None
        if not isinstance(raw, list):
            raise ValueError(f"{links}: must hold a JSON list of links")
        for item in raw:
            if isinstance(item, dict):
                pairs.append((item.get("source"), item))
    if now is None:
        stamps = [r["at"] for r in jrows + erows if isinstance(r["at"], float)]
        stamps += [t for t in (parse_at(link.get("at")) for _, link in pairs) if t is not None]
        clock = max(stamps) if stamps else time.time()
    else:
        clock = float(now)
    sha = hashlib.sha256(json.dumps({"components": doc.get("components"), "flows": doc.get("flows")},
                                    sort_keys=True).encode("utf-8")).hexdigest()[:12]
    index = TrafficIndex(journal_rows=_reader(jrows), event_rows=_reader(erows),
                         links=lambda: list(pairs), atlas=lambda: (sha, doc), now=lambda: clock)
    index.refresh()
    return index.snapshot()


__all__ = ["TrafficIndex", "Rules", "compile_rules", "match", "route", "route_files", "signature",
           "instance_index", "component_of", "parse_at",
           "SCHEMA", "WINDOW_HOURS", "BUCKET_S"]
