"""A public overlay for a private atlas.

A project can keep its atlas private and still publish a tour of it. The overlay supplies public wording for every
layer, component, contract and flow, and public ids to go with it. ``apply_overlay`` rewrites the atlas with that
wording and drops everything a public tour must not carry. ``leaks`` proves nothing private slipped through.

Format (``estate-atlas-overlay@1``)::

    {"schema": "estate-atlas-overlay@1",
     "title": "optional public name of the system",
     "denylist": ["extra term", ...],
     "layers":     {"<layer id>":     {"id": "<public id>", "title": "...", "summary": "..."}},
     "components": {"<component id>": {"id": "<public id>", "title": "...", "summary": "...", "home": "<label>"}},
     "contracts":  {"<contract id>":  {"id": "<public id>", "title": "...", "summary": "..."}},
     "flows":      {"<flow id>":      {"id": "<public id>", "trigger": "...", "gap": "..."}}}

Coverage is total: every layer, component, contract and flow of the atlas has exactly one entry, and no entry names
an id the atlas lacks. A flow entry carries ``gap`` when, and only when, the flow has one.

The overlaid document is the minimum the tour and ``explain`` read. It is not a valid atlas (it has no repos,
evidence or traffic rules), so it does not need to pass ``model.validate``. Do not validate it, and do not feed it to
``check``.

Standard library only. Output is deterministic.
"""
from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

from .model import (CONTRACT_ID_PATTERN, ID_PATTERN, MAX_ATLAS_BYTES, AtlasError, _text, strict_json)

OVERLAY_SCHEMA = "estate-atlas-overlay@1"
TRAFFIC_SCHEMA = "estate-atlas-traffic@1"

TOP_REQUIRED = {"schema", "layers", "components", "contracts", "flows"}
TOP_OPTIONAL = {"title", "denylist"}
MAX_DENYLIST = 500

# What each kind of entry carries: (public id pattern, text fields with their length limits).
KINDS: dict[str, tuple[Any, dict[str, tuple[int, int]]]] = {
    "layers": (ID_PATTERN, {"title": (1, 300), "summary": (1, 300)}),
    "components": (ID_PATTERN, {"title": (1, 300), "summary": (1, 300)}),
    "contracts": (CONTRACT_ID_PATTERN, {"title": (1, 300), "summary": (1, 300)}),
    "flows": (ID_PATTERN, {"trigger": (1, 200)}),
}
GAP_LIMITS = (1, 300)


# ------------------------------------------------------------------------------------------ loading

def load_overlay(path: str | Path) -> dict[str, Any]:
    """Read and parse an overlay file strictly, then check its shape (not its fit to any atlas)."""
    where = Path(path)
    try:
        raw = where.read_bytes()
    except OSError as exc:
        raise AtlasError(f"cannot read {where}: {exc.strerror or type(exc).__name__}") from None
    if len(raw) > MAX_ATLAS_BYTES:
        raise AtlasError(f"{where} is larger than {MAX_ATLAS_BYTES // 1024} KiB")
    try:
        overlay = strict_json(raw.decode("utf-8-sig"))
    except UnicodeDecodeError:
        raise AtlasError(f"{where} is not UTF-8") from None
    except AtlasError as exc:
        raise AtlasError(f"{where}: {exc}") from None
    _shape(overlay)
    return overlay


def _shape(overlay: Any) -> None:
    if not isinstance(overlay, dict):
        raise AtlasError("overlay: the top level must be an object")
    unknown = set(overlay) - TOP_REQUIRED - TOP_OPTIONAL
    missing = TOP_REQUIRED - set(overlay)
    if unknown:
        raise AtlasError(f"overlay: unknown key(s) {sorted(unknown)}")
    if missing:
        raise AtlasError(f"overlay: missing key(s) {sorted(missing)}")
    if overlay["schema"] != OVERLAY_SCHEMA:
        raise AtlasError(f"overlay.schema: must be {OVERLAY_SCHEMA}")
    for kind in KINDS:
        if not isinstance(overlay[kind], dict):
            raise AtlasError(f"overlay.{kind}: must be an object keyed by the original id")


# ------------------------------------------------------------------------------------------ validating

def _doc_ids(doc: dict[str, Any], kind: str) -> list[str]:
    items = doc.get(kind)
    return [item["id"] for item in items if isinstance(item, dict) and isinstance(item.get("id"), str)] \
        if isinstance(items, list) else []


def _home_label(value: Any, where: str) -> None:
    label = value[len("external:"):] if isinstance(value, str) and value.startswith("external:") else value
    if not isinstance(label, str) or not ID_PATTERN.fullmatch(label):
        raise AtlasError(f"{where}: must be a label like a component id, or external:<label>")


def validate_overlay(overlay: dict[str, Any], doc: dict[str, Any]) -> None:
    """Raise AtlasError, listing every problem found, unless the overlay fits ``doc`` exactly."""
    _shape(overlay)
    problems: list[str] = []

    if "title" in overlay:
        try:
            _text(overlay["title"], "overlay.title", 1, 80)
        except AtlasError as exc:
            problems.append(str(exc))
    if "denylist" in overlay:
        terms = overlay["denylist"]
        if not isinstance(terms, list) or len(terms) > MAX_DENYLIST:
            problems.append(f"overlay.denylist: must be a list of at most {MAX_DENYLIST} terms")
        else:
            for k, term in enumerate(terms):
                try:
                    _text(term, f"overlay.denylist[{k}]", 1, 120)
                except AtlasError as exc:
                    problems.append(str(exc))

    flow_gaps = {flow["id"]: bool(flow.get("gap")) for flow in doc.get("flows", [])
                 if isinstance(flow, dict) and isinstance(flow.get("id"), str)}
    for kind, (id_pattern, fields) in KINDS.items():
        have = _doc_ids(doc, kind)
        entries = overlay[kind]
        missing = [i for i in have if i not in entries]
        unknown = sorted(i for i in entries if i not in set(have))
        if missing:
            problems.append(f"overlay.{kind}: no entry for {missing}")
        if unknown:
            problems.append(f"overlay.{kind}: entry for unknown id(s) {unknown}")
        public_ids: dict[str, str] = {}
        for original in have:
            entry = entries.get(original)
            if entry is None:
                continue
            where = f"overlay.{kind}.{original}"
            allowed = {"id"} | set(fields) | ({"home"} if kind == "components" else set())
            required = set(allowed)
            if kind == "flows" and flow_gaps.get(original):
                allowed.add("gap")
                required.add("gap")
            if not isinstance(entry, dict):
                problems.append(f"{where}: must be an object")
                continue
            extra = sorted(set(entry) - allowed)
            absent = sorted(required - set(entry))
            if extra:
                problems.append(f"{where}: unknown key(s) {extra}" + (
                    " (the flow has no gap)" if extra == ["gap"] and kind == "flows" else ""))
            if absent:
                problems.append(f"{where}: missing key(s) {absent}")
            public = entry.get("id")
            if isinstance(public, str) and id_pattern.fullmatch(public):
                if public in public_ids:
                    problems.append(f"{where}.id: {public!r} is already the public id of {public_ids[public]!r}")
                public_ids[public] = original
            elif "id" in entry:
                problems.append(f"{where}.id: invalid public id")
            limits = dict(fields)
            if kind == "flows" and "gap" in entry:
                limits["gap"] = GAP_LIMITS
            for name, (low, high) in limits.items():
                if name in entry:
                    try:
                        _text(entry[name], f"{where}.{name}", low, high)
                    except AtlasError as exc:
                        problems.append(str(exc))
            if kind == "components" and "home" in entry:
                try:
                    _home_label(entry["home"], f"{where}.home")
                except AtlasError as exc:
                    problems.append(str(exc))
    if problems:
        raise AtlasError("; ".join(problems))


# ------------------------------------------------------------------------------------------ rewriting

def _rename(table: dict[str, dict[str, Any]], original: Any) -> Any:
    """The public id for an original id; anything else (such as a null contract) stays as it is."""
    entry = table.get(original) if isinstance(original, str) else None
    return entry["id"] if entry is not None else original


def apply_overlay(doc: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """A deep copy of ``doc`` with public text and public ids, and nothing a public tour must not carry.

    Call ``validate_overlay`` first. The result keeps only what the tour and ``explain`` read: ``repos`` is empty;
    ``evidence``, ``expect``, ``surfaces``, ``owns``, ``instances``, ``vocabularies`` and flow ``traffic`` rules are
    gone; ``home`` is the public label. It does not pass ``model.validate``.
    """
    doc = copy.deepcopy(doc)
    layers, comps, contracts, flows = (overlay[k] for k in ("layers", "components", "contracts", "flows"))
    out: dict[str, Any] = {
        "schema": doc.get("schema"),
        "updated": doc.get("updated"),
        "repos": {},
        "layers": [], "components": [], "contracts": [], "flows": [],
    }
    for layer in doc.get("layers", []):
        entry = layers[layer["id"]]
        out["layers"].append({"id": entry["id"], "title": entry["title"], "summary": entry["summary"]})
    for comp in doc.get("components", []):
        entry = comps[comp["id"]]
        out["components"].append({
            "id": entry["id"], "title": entry["title"], "summary": entry["summary"],
            "layer": _rename(layers, comp["layer"]), "home": entry["home"], "status": comp["status"]})
    for contract in doc.get("contracts", []):
        entry = contracts[contract["id"]]
        item = {"id": entry["id"], "title": entry["title"], "summary": entry["summary"],
                "producer": _rename(comps, contract["producer"]),
                "consumers": [_rename(comps, c) for c in contract["consumers"]],
                "status": contract["status"]}
        if "format" in contract:
            item["format"] = contract["format"]
        out["contracts"].append(item)
    for flow in doc.get("flows", []):
        entry = flows[flow["id"]]
        item = {"id": entry["id"], "from": _rename(comps, flow["from"]), "to": _rename(comps, flow["to"]),
                "contract": _rename(contracts, flow["contract"]),
                "trigger": entry["trigger"], "status": flow["status"]}
        if bool(flow.get("gap")):
            item["gap"] = entry["gap"]
        out["flows"].append(item)
    return out


def apply_overlay_traffic(traffic_doc: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """The traffic document with public flow ids, so the tour's "this week" numbers can still be shown.

    Only what the tour reads is kept: ``schema``, ``generated``, the per-flow counts, and the ``silent`` and
    ``off_status`` crosschecks. The ``unrouted`` examples, ``rule_gaps``, sources and coverage are dropped, and so
    is any flow the overlay does not name (its id would be private).
    """
    if not isinstance(traffic_doc, dict) or traffic_doc.get("schema") != TRAFFIC_SCHEMA:
        raise AtlasError(f"traffic: not an {TRAFFIC_SCHEMA} document")
    flows = overlay["flows"]
    public = {fid: entry["id"] for fid, entry in flows.items()}
    out: dict[str, Any] = {"schema": traffic_doc["schema"]}
    if "generated" in traffic_doc:
        out["generated"] = traffic_doc["generated"]
    src = traffic_doc.get("flows")
    out["flows"] = {public[fid]: copy.deepcopy(value) for fid, value in sorted(src.items()) if fid in public} \
        if isinstance(src, dict) else {}
    cross = traffic_doc.get("crosschecks")
    cross = cross if isinstance(cross, dict) else {}
    silent = cross.get("silent")
    off = cross.get("off_status")
    out["crosschecks"] = {
        "silent": sorted(public[f] for f in silent if isinstance(f, str) and f in public)
        if isinstance(silent, list) else [],
        "off_status": sorted(
            ({**{k: v for k, v in item.items() if k != "flow"}, "flow": public[item["flow"]]}
             for item in off if isinstance(item, dict) and item.get("flow") in public),
            key=lambda item: item["flow"]) if isinstance(off, list) else [],
    }
    return out


# ------------------------------------------------------------------------------------------ leak check

def leak_terms(doc: dict[str, Any], overlay: dict[str, Any]) -> tuple[list[str], list[str]]:
    """``(substring terms, word terms)`` that the overlaid output must not contain.

    Substring terms (matched case-insensitively anywhere): the overlay's own ``denylist``, every original title
    that differs from its public title, and every repo ``remote``. Word terms (matched whole, case-insensitively,
    where a hyphen or underscore counts as part of the word): every original id that differs from its public id,
    every repo key, and every original ``home`` that differs from its public label. A word term that is itself a
    public id or label of the overlay is public already and is left out.
    """
    subs: set[str] = {t for t in overlay.get("denylist", []) if isinstance(t, str)}
    words: set[str] = set()
    published: set[str] = set()
    for kind in KINDS:
        for original, entry in overlay[kind].items():
            published.add(entry["id"])
            if entry["id"] != original:
                words.add(original)
    for kind in ("layers", "components", "contracts"):
        for item in doc.get(kind, []):
            entry = overlay[kind].get(item.get("id"))
            if entry is not None and isinstance(item.get("title"), str) and item["title"] != entry["title"]:
                subs.add(item["title"])
    for comp in doc.get("components", []):
        entry = overlay["components"].get(comp.get("id"))
        if entry is not None:
            published.add(entry["home"])
            if isinstance(comp.get("home"), str) and comp["home"] != entry["home"]:
                words.add(comp["home"])
    for key, repo in doc.get("repos", {}).items():
        words.add(key)
        if isinstance(repo, dict) and isinstance(repo.get("remote"), str):
            subs.add(repo["remote"])
    return sorted(subs), sorted(w for w in words if w not in published)


def _word(term: str) -> re.Pattern[str]:
    return re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(term) + r"(?![A-Za-z0-9_-])", re.IGNORECASE)


def leaks(text: str, denylist: Any, words: Any = ()) -> list[str]:
    """Each denylisted term found in ``text``, case-insensitive, sorted.

    ``denylist`` terms match as substrings; ``words`` terms (optional) match only as whole words.
    """
    lowered = text.lower()
    found = {t for t in denylist if t and t.lower() in lowered}
    found.update(t for t in words if t and _word(t).search(text))
    return sorted(found)
