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
import html
import json
import re
import unicodedata
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
    gone; ``home`` is the public label and a contract's ``format`` is dropped. It does not pass ``model.validate``.
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
        # ``format`` is deliberately dropped: the tour never prints it, and copying it unchecked would let an
        # unvalidated string through. Everything in the output is overlay text, a validated id or a status.
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
    is any flow the overlay does not name (its id would be private). A non-string ``flow`` in ``off_status``
    raises ``AtlasError``.
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
    for item in off if isinstance(off, list) else []:
        if isinstance(item, dict) and "flow" in item and not isinstance(item["flow"], str):
            raise AtlasError("traffic: crosschecks.off_status[].flow must be a string")
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

MIN_TEXT_TERM = 12  # an original summary, trigger or gap shorter than this is not used as a term: too much noise
MIN_TITLE_SUBSTRING = 4  # an original title shorter than this (folded) is matched as a word, like an id, not a substring


def _published_unchanged(doc: dict[str, Any], overlay: dict[str, Any]) -> set[str]:
    """Folded texts the overlay publishes unchanged: a title, summary, trigger, gap or home label equal to the original."""
    same: set[str] = set()
    for kind, names in (("layers", ("title", "summary")), ("components", ("title", "summary", "home")),
                        ("contracts", ("title", "summary")), ("flows", ("trigger", "gap"))):
        for item in doc.get(kind, []):
            entry = overlay[kind].get(item.get("id")) if isinstance(item, dict) else None
            if not isinstance(entry, dict):
                continue
            for name in names:
                original, public = item.get(name), entry.get(name)
                # raw equality, not folded: a case-, width- or whitespace-only "rename" is still a rename and
                # must not exempt the private text it copies (review of #19)
                if isinstance(original, str) and isinstance(public, str) and original == public:
                    same.add(fold(public))
    return same


def leak_terms(doc: dict[str, Any], overlay: dict[str, Any]) -> tuple[list[str], list[str]]:
    """``(substring terms, word terms)`` that the overlaid output must not contain.

    Substring terms (matched anywhere): the overlay's own ``denylist``, every original title that differs from its
    public title and is at least ``MIN_TITLE_SUBSTRING`` characters, every original summary, flow trigger and flow
    gap that differs from its public text (only when it is at least ``MIN_TEXT_TERM`` characters, so a short common
    phrase does not flood the check), and every repo ``remote``. Word terms (matched whole): every original id that
    differs from its public id, every original title shorter than ``MIN_TITLE_SUBSTRING`` characters (so a private
    ``DB`` flags the word ``db`` but not ``feedback``), every repo key and every original ``home`` that differs from
    its public label. A word term that is itself a public id or label of the overlay is public already and is left
    out. Matching is described at ``leaks``.

    A derived title or text term (not the ``denylist``, not a ``remote``) is also left out when, folded, it equals a
    whole text that the overlay publishes unchanged: some entry's public title, summary, trigger, gap or home label
    that is the same as that entry's original. A private layer titled ``Core`` is then not refused because a
    component's public title is still ``Core``; the author is publishing that exact string in the atlas's own
    wording. This cannot whitelist a private term the overlay does not publish verbatim: it needs equality with a
    whole published text, never a substring of one, so ``Core`` does not excuse ``Core services``; and the entry
    must publish its own original wording, so a private text that was merely copied into another entry's public text
    (the entry's original differs) is still a leak. An explicit ``denylist`` term is the author's own instruction
    and never takes this exemption.
    """
    subs: set[str] = {t for t in overlay.get("denylist", []) if isinstance(t, str)}
    words: set[str] = set()
    published: set[str] = set()
    unchanged = _published_unchanged(doc, overlay)
    for kind in KINDS:
        for original, entry in overlay[kind].items():
            published.add(entry["id"])
            if entry["id"] != original:
                words.add(original)
    for kind, names in (("layers", ("title", "summary")), ("components", ("title", "summary")),
                        ("contracts", ("title", "summary")), ("flows", ("trigger", "gap"))):
        for item in doc.get(kind, []):
            entry = overlay[kind].get(item.get("id")) if isinstance(item, dict) else None
            if entry is None:
                continue
            for name in names:
                original = item.get(name)
                if not isinstance(original, str) or original == entry.get(name):
                    continue
                folded = fold(original)
                if folded in unchanged:
                    continue
                if name == "title":
                    (subs if len(folded) >= MIN_TITLE_SUBSTRING else words).add(original)
                elif len(folded) >= MIN_TEXT_TERM:
                    subs.add(original)
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


def fold(text: str) -> str:
    """NFKC-normalised, case-folded text with every run of whitespace collapsed to one space."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _word(folded_term: str) -> re.Pattern[str]:
    # Boundaries are ASCII letters and digits only, so "-" and "_" end a token: payments-gateway-v2 holds payments-gateway.
    # A single trailing letter or digit still matches, so plurals and numbered forms (vvms, vvm2) of a short
    # private name are caught; two or more continue a different word (heal / healthy) (review of #19).
    return re.compile(r"(?<![a-z0-9])" + re.escape(folded_term) + r"(?![a-z0-9]{2})")


def leaks(text: str, denylist: Any, words: Any = ()) -> list[str]:
    """Each denylisted term found in ``text``, sorted.

    Both the text and the terms are folded first (NFKC, ``casefold``, whitespace collapsed), so case, width,
    compatibility forms and spacing do not hide a term. ``denylist`` terms match as substrings; ``words`` terms
    (optional) match only between ASCII-alphanumeric boundaries, so a hyphen or underscore ends a word. A term is
    returned as it was given.
    """
    folded = fold(text)
    found = set()
    for term in denylist:
        key = fold(term) if isinstance(term, str) else ""
        if key and key in folded:
            found.add(term)
    for term in words:
        key = fold(term) if isinstance(term, str) else ""
        if key and _word(key).search(folded):
            found.add(term)
    return sorted(found)


def _walk(value: Any, where: str):
    """Every string in a JSON-like value, keys included, with the path that leads to it."""
    if isinstance(value, str):
        yield where, value
    elif isinstance(value, dict):
        for key, item in value.items():
            here = f"{where}.{key}" if where else str(key)
            yield f"{here} (key)", str(key)
            yield from _walk(item, here)
    elif isinstance(value, list):
        for k, item in enumerate(value):
            label = item["id"] if isinstance(item, dict) and isinstance(item.get("id"), str) else k
            yield from _walk(item, f"{where}[{label}]")


def public_fields(public: dict[str, Any], overlay: dict[str, Any]) -> list[tuple[str, str]]:
    """Every string of the overlaid document, and the overlay title, as ``(field, text)``."""
    fields: list[tuple[str, str]] = []
    if isinstance(overlay.get("title"), str):
        fields.append(("overlay.title", overlay["title"]))
    for kind in KINDS:
        fields.extend(_walk(public.get(kind, []), kind))
    return fields


def find_leaks(atlas: dict[str, Any], overlay: dict[str, Any], public: dict[str, Any], output: str,
               *, markdown: bool) -> list[tuple[str, str]]:
    """``(term, field)`` for every private term in the public tour; empty when it is clean.

    The renderers escape (``|`` and ``<`` in Markdown, ``"`` and ``\\`` in JSON), and escaping can hide a term that
    contains those characters, so the check does not trust the finished output alone. It runs on every string of
    the overlaid document and the overlay title (before any escaping); on the rendered text as it is; on the
    Markdown text with ``\\|`` and HTML entities undone; and, for JSON, on every string and key of the parsed output.
    """
    subs, words = leak_terms(atlas, overlay)
    hits: set[tuple[str, str]] = set()

    def check(text: str, field: str) -> None:
        hits.update((term, field) for term in leaks(text, subs, words))

    for field, text in public_fields(public, overlay):
        check(text, field)
    check(output, "the rendered tour")
    if markdown:
        check(html.unescape(output.replace("\\|", "|")), "the rendered tour (unescaped)")
    else:
        try:
            parsed = json.loads(output)
        except ValueError:
            raise AtlasError("the JSON tour does not parse; refusing to publish it") from None
        for field, text in _walk(parsed, "tour"):
            check(text, field)
    return sorted(hits)
