"""Traffic history: daily rollups of flow crossings in a SQLite store, weekly series, fading and surging flags.

The store path is a parameter (`TrafficHistory(path)`). Only this module touches the store file. `weekly` and `flags` are pure.
`rollup` reads through the same `(after_id, since)` readers the traffic
index uses and routes every row with the index's current rules, so the
rollups agree with what the live index would have said.
"""
from __future__ import annotations

import datetime
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .traffic import parse_at

DAY_S = 24 * 3600
RETENTION_DAYS = 400
BACKFILL_DAYS = 90
READ_PAGE = 5_000
READ_MAX = 1_000_000
FLAG_MIN_DAYS = 35
FLAG_WEEKS = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS flow_day(day TEXT NOT NULL, flow TEXT NOT NULL, crossings INTEGER NOT NULL,
  pulse INTEGER NOT NULL, declared INTEGER NOT NULL, inferred INTEGER NOT NULL, journal INTEGER NOT NULL,
  events INTEGER NOT NULL, links INTEGER NOT NULL, PRIMARY KEY(day, flow));
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


def _day_of(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%d")


def _day_start(day: str) -> float:
    return datetime.datetime.strptime(day, "%Y-%m-%d").replace(
        tzinfo=datetime.timezone.utc).timestamp()


def _yesterday(now_f: float) -> str:
    return _day_of(float(int(now_f // DAY_S) * DAY_S) - 1)


class TrafficHistory:
    """Daily per-flow rollups in a SQLite file. Thread-safe (one lock)."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._con: sqlite3.Connection | None = None
        self._open()

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self.path), timeout=5.0, isolation_level=None,
                              check_same_thread=False)
        try:
            con.execute("PRAGMA journal_mode = WAL")
            con.executescript(SCHEMA)
        except BaseException:
            con.close()
            raise
        self._con = con

    def close(self) -> None:
        with self._lock:
            if self._con is not None:
                try:
                    self._con.close()
                except Exception:
                    pass
                self._con = None

    def _meta(self, key: str) -> str | None:
        assert self._con is not None
        row = self._con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def last_day(self) -> str | None:
        """The newest complete UTC day covered by the rollups (None when empty)."""
        with self._lock:
            return self._meta("last_day")

    def days_stored(self) -> int:
        """Days the rollups cover, first to last complete day (quiet days count: a day with no traffic is still
        a day of history)."""
        with self._lock:
            first, last = self._meta("first_day"), self._meta("last_day")
        if not first or not last or last < first:
            return 0
        return int((_day_start(last) - _day_start(first)) // DAY_S) + 1

    def first_seen(self) -> dict[str, str]:
        """Each flow's earliest stored day: a flow is judged only on its own history (the event store keeps 14
        days, so a backfill gives event-fed flows a short past that must not read as a surge)."""
        with self._lock:
            assert self._con is not None
            return {r[0]: r[1] for r in self._con.execute("SELECT flow, MIN(day) FROM flow_day GROUP BY flow")}

    def read_all(self) -> list[dict[str, Any]]:
        """Every stored (day, flow) row as {day, flow, crossings, pulse}."""
        with self._lock:
            assert self._con is not None
            return [{"day": r[0], "flow": r[1], "crossings": r[2], "pulse": r[3]}
                    for r in self._con.execute(
                        "SELECT day, flow, crossings, pulse FROM flow_day").fetchall()]

    def rollup(self, index: Any, journal_rows: Any, event_rows: Any,
               links: Any, now: Any) -> dict[str, Any]:
        """Roll up the missing complete UTC days, up to yesterday.

        The first run backfills at most 90 days; later runs only read the
        days after `meta.last_day`. Idempotent: a day already present is
        never rewritten. Returns {"days_written", "ms"}, plus "skipped" when
        the index has no rules to route with (it is not built, or the atlas has
        no flows): nothing is written and `last_day` does not advance, so the
        days stay missing until a later run can route them.
        """
        t0 = time.perf_counter()
        if not index.built or index.flow_count == 0:
            return {"days_written": 0, "ms": (time.perf_counter() - t0) * 1000.0,
                    "skipped": "index not built"}
        now_f = float(now() if callable(now) else now)
        today_start = float(int(now_f // DAY_S) * DAY_S)
        yesterday = _day_of(today_start - 1)
        with self._lock:
            assert self._con is not None
            last = self._meta("last_day")
            if last is not None and last >= yesterday:
                return {"days_written": 0, "ms": (time.perf_counter() - t0) * 1000.0}
            if last is None:
                first = _day_of(today_start - BACKFILL_DAYS * DAY_S)
            else:
                first = _day_of(_day_start(last) + DAY_S)
            missing = []
            cur = first
            while cur <= yesterday:
                missing.append(cur)
                cur = _day_of(_day_start(cur) + DAY_S)
            present = {r[0] for r in self._con.execute(
                "SELECT DISTINCT day FROM flow_day WHERE day >= ? AND day <= ?",
                (first, yesterday)).fetchall()} if missing else set()
            missing = [d for d in missing if d not in present]
            if not missing:
                # nothing to route: the whole days are already stored, so seal them in one transaction
                self._con.execute("BEGIN IMMEDIATE")
                try:
                    self._seal(first, yesterday)
                    self._con.execute("COMMIT")
                except BaseException:
                    self._abort()
                    raise
                return {"days_written": 0, "ms": (time.perf_counter() - t0) * 1000.0}
            wanted = set(missing)
            since = _day_start(missing[0])
            acc: dict[tuple[str, str], dict[str, int]] = {}

            def add(day: str, fid: str, basis: str, source: str) -> None:
                if day not in wanted or fid is None:
                    return
                cell = acc.setdefault((day, fid), {"crossings": 0, "pulse": 0, "declared": 0,
                                                   "inferred": 0, "journal": 0, "events": 0,
                                                   "links": 0})
                if basis == "pulse":
                    cell["pulse"] += 1
                elif basis in ("declared", "inferred"):
                    cell["crossings"] += 1
                    cell[basis] += 1
                else:
                    return
                if source in ("journal", "events", "links"):
                    cell[source] += 1

            self._roll_journal(index, journal_rows, since, today_start, add)
            self._roll_events(index, event_rows, since, today_start, add)
            self._roll_links(index, links, since, today_start, add)
            written = 0
            self._con.execute("BEGIN IMMEDIATE")
            try:
                if not index.built or index.flow_count == 0:
                    # another thread's refresh dropped the rules while this one was routing: the counts above
                    # may be partial, so seal nothing and let a later run roll the days up
                    self._con.execute("ROLLBACK")
                    return {"days_written": 0, "rows_written": 0,
                            "ms": (time.perf_counter() - t0) * 1000.0,
                            "skipped": "index changed during rollup"}
                written = self._write_days(acc, first, yesterday, today_start)
                self._con.execute("COMMIT")
            except BaseException:
                self._abort()
                raise
            return {"days_written": len(missing), "rows_written": written,
                    "ms": (time.perf_counter() - t0) * 1000.0}

    def _abort(self) -> None:
        """Roll back whatever is open, so a failed COMMIT never wedges later rollups; the caller re-raises the
        original error, which a failing ROLLBACK must not replace."""
        try:
            if self._con is not None and self._con.in_transaction:
                self._con.execute("ROLLBACK")
        except Exception:  # noqa: BLE001 - the original exception is the one worth raising
            pass

    def _seal(self, first: str, yesterday: str) -> None:
        """`first_day` (kept once set) and `last_day`, inside the caller's transaction: a rollup that is skipped
        or fails leaves neither, so `days_stored()` never counts days that were not sealed."""
        assert self._con is not None
        self._con.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('first_day', ?)", (first,))
        self._con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('last_day', ?)", (yesterday,))

    def _write_days(self, acc: dict[tuple[str, str], dict[str, int]], first: str, yesterday: str,
                    today_start: float) -> int:
        """The rollup's rows, `first_day`, `last_day` and retention, inside the caller's transaction."""
        assert self._con is not None
        written = 0
        for (day, fid), cell in sorted(acc.items()):
            if cell["crossings"] <= 0 and cell["pulse"] <= 0:
                continue
            cur = self._con.execute(
                "INSERT OR IGNORE INTO flow_day(day, flow, crossings, pulse, declared,"
                " inferred, journal, events, links) VALUES(?,?,?,?,?,?,?,?,?)",
                (day, fid, cell["crossings"], cell["pulse"], cell["declared"],
                 cell["inferred"], cell["journal"], cell["events"], cell["links"]))
            if cur.rowcount > 0:
                written += 1  # rows; the answer below counts days
        self._seal(first, yesterday)
        cutoff = _day_of(today_start - RETENTION_DAYS * DAY_S)
        self._con.execute("DELETE FROM flow_day WHERE day < ?", (cutoff,))
        return written

    def _paged(self, reader: Any, since: float) -> list[dict[str, Any]]:
        """Rows past the cursor in id order, at most 1M (like TrafficIndex._drain)."""
        out: list[dict[str, Any]] = []
        after = 0
        while len(out) < READ_MAX:
            rows = reader(after, since)
            if not isinstance(rows, list) or not rows:
                break
            fresh = sorted((r for r in rows if isinstance(r, dict)
                            and isinstance(r.get("id"), int)
                            and not isinstance(r.get("id"), bool)
                            and r["id"] > after), key=lambda r: r["id"])
            if not fresh:
                break
            out.extend(fresh[:READ_MAX - len(out)])
            after = fresh[-1]["id"]
            if len(rows) < READ_PAGE:
                break
        return out

    @staticmethod
    def _at_of(row: dict[str, Any]) -> float | None:
        at = row.get("at")
        if isinstance(at, (int, float)) and not isinstance(at, bool):
            return float(at)
        return None

    def _roll_journal(self, index: Any, reader: Any, since: float,
                      today_start: float, add: Any) -> None:
        for r in self._paged(reader, since):
            at = self._at_of(r)
            if at is None or at < since or at >= today_start:
                continue
            rec = {"source": "journal", "producer": r.get("producer"), "verb": r.get("verb"),
                   "actor": r.get("actor"), "subject": r.get("subject")}
            fid, basis, _reason = index.route_record(rec)
            if fid is not None and basis in ("declared", "inferred", "pulse"):
                add(_day_of(at), fid, basis, "journal")

    def _roll_events(self, index: Any, reader: Any, since: float,
                     today_start: float, add: Any) -> None:
        for r in self._paged(reader, since):
            at = self._at_of(r)
            if at is None or at < since or at >= today_start:
                continue
            nodes = r.get("nodes") if isinstance(r.get("nodes"), list) else []
            rec = {"source": "events", "producer": r.get("source"), "verb": r.get("kind"),
                   "nodes": [n for n in nodes if isinstance(n, str)]}
            fid, basis, _reason = index.route_record(rec)
            if fid is not None and basis in ("declared", "inferred", "pulse"):
                add(_day_of(at), fid, basis, "events")

    def _roll_links(self, index: Any, links_fn: Any, since: float,
                    today_start: float, add: Any) -> None:
        """Dated links only, best effort: a links producer that fails leaves that day without link counts (the
        journal and events for the day still roll up). Links are the smallest source and are recounted live."""
        try:
            pairs = links_fn()
        except Exception:
            return
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
            if isinstance(raw_at, (int, float)) and not isinstance(raw_at, bool):
                at_f = float(raw_at)
            elif isinstance(raw_at, str) and raw_at:
                try:
                    at_f = parse_at(raw_at)
                except Exception:
                    at_f = None
            if at_f is None or at_f < since or at_f >= today_start:
                continue  # only dated links count
            rec = {"source": "links", "producer": doc_source, "verb": link.get("rel"),
                   "actor": link.get("from"), "subject": link.get("to")}
            fid, basis, _reason = index.route_record(rec)
            if fid is not None and basis in ("declared", "inferred", "pulse"):
                add(_day_of(at_f), fid, basis, "links")


def weekly(rows: Any, last_day: str, weeks: int,
           flows: Any = ()) -> dict[str, list[dict[str, Any]]]:
    """Weekly totals per flow: `weeks` consecutive 7-day windows ending at
    `last_day`, oldest first. A flow in `flows` with no rows still appears
    with zeros. Pure."""
    end = datetime.datetime.strptime(last_day, "%Y-%m-%d").replace(
        tzinfo=datetime.timezone.utc).date()
    ends = [(end - datetime.timedelta(days=7 * (weeks - 1 - i))).strftime("%Y-%m-%d")
            for i in range(max(0, int(weeks)))]
    starts = [(end - datetime.timedelta(days=7 * (weeks - 1 - i) + 6)).strftime("%Y-%m-%d")
              for i in range(max(0, int(weeks)))]
    names: set[str] = set()
    if isinstance(flows, (list, tuple, set)):
        names.update(f for f in flows if isinstance(f, str))
    for r in rows or []:
        if isinstance(r, dict) and isinstance(r.get("flow"), str):
            names.add(r["flow"])
    out: dict[str, list[dict[str, Any]]] = {
        fid: [{"week_end": e, "crossings": 0, "pulse": 0} for e in ends] for fid in names}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        fid, day = r.get("flow"), r.get("day")
        if not isinstance(fid, str) or not isinstance(day, str) or fid not in out:
            continue
        for i, (start, stop) in enumerate(zip(starts, ends)):
            if start <= day <= stop:
                try:
                    out[fid][i]["crossings"] += int(r.get("crossings") or 0)
                except (TypeError, ValueError):
                    pass
                try:
                    out[fid][i]["pulse"] += int(r.get("pulse") or 0)
                except (TypeError, ValueError):
                    pass
                break
    return out


def flags(series: Any, days_stored: int | None = None,
          first_seen: dict[str, str] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(fading, surging) over each flow's last 5 weeks.

    avg4 is the mean of weeks -5..-2, last is week -1; the metric is
    crossings, or pulse when the flow has no crossings in those 5 weeks.
    Fading: avg4 >= 10 and last < 0.4 * avg4. Surging: last >= 20 and
    last > 3 * avg4. Sorted by flow as {"flow", "last_week", "avg4"}
    (avg4 rounded to 1 decimal). No flags when fewer than 35 days of
    history are stored, or when a flow has fewer than 5 weeks. Pure.
    """
    fading: list[dict[str, Any]] = []
    surging: list[dict[str, Any]] = []
    if days_stored is not None and days_stored < FLAG_MIN_DAYS:
        return fading, surging
    if not isinstance(series, dict):
        return fading, surging
    for fid in sorted(series):
        weeks5 = series[fid]
        if not isinstance(weeks5, list) or len(weeks5) < FLAG_WEEKS:
            continue
        tail = weeks5[-FLAG_WEEKS:]
        if first_seen is not None:
            seen = first_seen.get(fid)
            end = tail[-1].get("week_end") if isinstance(tail[-1], dict) else None
            if not isinstance(seen, str) or not isinstance(end, str) \
                    or _day_start(seen) > _day_start(end) - (FLAG_MIN_DAYS - 1) * DAY_S:
                continue  # fewer than 35 days of this flow's own history
        try:
            cross = [int(w.get("crossings") or 0) for w in tail]
            pulse = [int(w.get("pulse") or 0) for w in tail]
        except (TypeError, ValueError, AttributeError):
            continue
        metric = cross if sum(cross) > 0 else pulse
        avg4 = sum(metric[:4]) / 4.0
        last = metric[4]
        if avg4 >= 10 and last < 0.4 * avg4:
            fading.append({"flow": fid, "last_week": last, "avg4": round(avg4, 1)})
        if last >= 20 and last > 3 * avg4:
            surging.append({"flow": fid, "last_week": last, "avg4": round(avg4, 1)})
    return fading, surging


__all__ = ["TrafficHistory", "weekly", "flags", "DAY_S", "RETENTION_DAYS", "BACKFILL_DAYS",
           "FLAG_MIN_DAYS", "FLAG_WEEKS"]
