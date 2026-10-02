#!/usr/bin/env python3
"""Loading and validating an estate-atlas document (schema estate-atlas@2).

The document lists repos, layers, components, contracts, flows and optional
vocabularies. Each claim carries evidence references into versioned checkouts.
``validate`` raises :class:`AtlasError` on the first violation.
"""
from __future__ import annotations

import datetime
import json
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

SCHEMA = "estate-atlas@2"
MAX_ATLAS_BYTES = 512 * 1024

TOP_KEYS = {"schema", "updated", "repos", "layers", "components", "contracts", "flows"}
TOP_OPTIONAL = {"vocabularies"}
LAYER_KEYS = {"id", "title", "summary"}
COMPONENT_KEYS = {"id", "title", "layer", "home", "status", "summary", "surfaces", "owns", "evidence"}
COMPONENT_OPTIONAL = {"instances", "expect"}
SURFACE_KEYS = {"kind", "name", "evidence"}
CONTRACT_KEYS = {"id", "title", "producer", "consumers", "format", "status", "summary", "evidence"}
CONTRACT_OPTIONAL = {"expect"}
FLOW_REQUIRED = {"id", "from", "to", "contract", "trigger", "status", "evidence"}
FLOW_OPTIONAL = {"gap", "expect", "traffic"}
TRAFFIC_SOURCES = {"journal", "links", "events"}
TRAFFIC_KEYS = {"source", "producer", "actor", "subject", "verb", "pulse"}
REF_REQUIRED = {"repo", "path"}
REF_OPTIONAL = {"anchor", "note"}
VOCAB_SOURCE_OPTIONAL = {"anchor", "note", "select", "each"}
EXTRA_REPO_KEYS = {"remote", "default_branch", "paths"}
INSTANCE_PATTERN = re.compile(r"^[a-z][a-z-]{0,31}:(\*|[A-Za-z0-9._/#@:+-]{1,200})$")
EXPECT_STATUSES = {"planned", "documented", "absent"}

ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
CONTRACT_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9._/@-]{0,79}")
DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")
COMPONENT_STATUS = {"live", "partial", "planned", "retired"}
CONTRACT_STATUS = {"live", "partial", "planned", "retired"}
FLOW_STATUS = {"live", "partial", "documented", "planned", "absent"}
SURFACE_KINDS = {"cli", "http", "sse", "mcp", "file", "ui", "scheduled-task", "gh-status", "library", "protocol"}
FORMATS = {"json", "jsonl", "json-schema", "markdown", "yaml", "http", "sqlite", "sse", "mcp", "git"}
EVIDENCED = {"live", "partial"}

REMOTE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
BRANCH_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")
HOST_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,63}")


class AtlasError(ValueError):
    """A bounded, actionable atlas error; the message names where it is."""


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AtlasError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise AtlasError(f"non-finite JSON number {value!r}")


def strict_json(text: str) -> Any:
    """Parse JSON strictly: no duplicate keys, no non-finite numbers."""
    try:
        return json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise AtlasError(f"invalid JSON: {exc.msg} at line {exc.lineno}") from exc


def is_atlas_schema(s: Any) -> bool:
    """True for the current schema or any namespaced variant ending in /atlas@2."""
    return isinstance(s, str) and (s == SCHEMA or s.endswith("/atlas@2"))


def _absolute(value: str) -> bool:
    return PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()


def read_json_file(path: str | Path, *, max_bytes: int | None = None) -> Any:
    """Read strict UTF-8 JSON once, accepting an optional leading UTF-8 BOM."""
    where = Path(path)
    try:
        raw = where.read_bytes()
    except OSError as exc:
        raise AtlasError(f"cannot read {where}: {exc.strerror or type(exc).__name__}") from None
    if max_bytes is not None and len(raw) > max_bytes:
        raise AtlasError(f"{where} is larger than {max_bytes // 1024} KiB")
    try:
        doc = strict_json(raw.decode("utf-8-sig"))
    except UnicodeDecodeError:
        raise AtlasError(f"{where} is not UTF-8") from None
    except AtlasError as exc:
        raise AtlasError(f"{where}: {exc}") from None
    return doc


def load(path: str | Path) -> dict[str, Any]:
    """Read and parse an atlas file strictly; return the document as a dict."""
    doc = read_json_file(path, max_bytes=MAX_ATLAS_BYTES)
    if not isinstance(doc, dict):
        raise AtlasError("atlas: the top level must be an object")
    return doc


def _keys(obj: Any, where: str, required: set[str], optional: set[str] = frozenset()) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise AtlasError(f"{where}: must be an object")
    unknown = set(obj) - required - set(optional)
    if unknown:
        raise AtlasError(f"{where}: unknown key(s) {sorted(unknown)}")
    missing = required - set(obj)
    if missing:
        raise AtlasError(f"{where}: missing key(s) {sorted(missing)}")
    return obj


def _text(value: Any, where: str, low: int = 1, high: int = 300) -> str:
    if not isinstance(value, str) or "\n" in value or "\r" in value or not low <= len(value.strip()) <= high:
        raise AtlasError(f"{where}: must be a single line of {low}-{high} characters")
    return value


def _list(value: Any, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise AtlasError(f"{where}: must be a list")
    return value


def _is(value: Any, allowed: Any) -> bool:
    """Membership that treats a wrong type (a list, an object) as absent, not a crash."""
    return isinstance(value, str) and value in allowed


def _unique(ids: list[str], where: str) -> None:
    seen: set[str] = set()
    for value in ids:
        if value in seen:
            raise AtlasError(f"{where}: duplicate id {value!r}")
        seen.add(value)


def _ref(ref: Any, where: str, repo_ids: set[str]) -> None:
    _keys(ref, where, REF_REQUIRED, REF_OPTIONAL)
    if not _is(ref["repo"], repo_ids):
        raise AtlasError(f"{where}.repo: {ref['repo']!r} is not a declared repo")
    path = ref["path"]
    if (not isinstance(path, str) or not path or "\\" in path or path.startswith("/")
            or ".." in PurePosixPath(path).parts or ":" in path or len(path) > 300):
        raise AtlasError(f"{where}.path: must be a repository-relative path with forward slashes")
    if any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise AtlasError(f"{where}.path: must not contain control characters")
    if any(segment in ("", ".") for segment in path.split("/")):
        raise AtlasError(f"{where}.path: must not have an empty or '.' segment")
    if "anchor" in ref:
        anchor = ref["anchor"]
        if not isinstance(anchor, str) or not 1 <= len(anchor) <= 200 or "\n" in anchor:
            raise AtlasError(f"{where}.anchor: must be 1-200 characters on one line")
    if "note" in ref:
        _text(ref["note"], f"{where}.note")


def _home(value: Any, where: str, repo_ids: set[str]) -> None:
    if isinstance(value, str) and value.startswith("external:"):
        if not ID_PATTERN.fullmatch(value[len("external:"):]):
            raise AtlasError(f"{where}: external:<name> needs a name like the component id pattern")
        return
    if not _is(value, repo_ids):
        raise AtlasError(f"{where}: {value!r} is not a declared repo or external:<name>")


def instance_index(doc: dict[str, Any]) -> dict[str, dict[str, str]]:
    """{"exact": {ref: comp}, "kind": {kind: comp}} from component instances; first claim wins."""
    exact: dict[str, str] = {}
    kind: dict[str, str] = {}
    for comp in doc.get("components", []) or []:
        if not isinstance(comp, dict):
            continue
        cid = comp.get("id")
        if not isinstance(cid, str):
            continue
        for ref in comp.get("instances", []) or []:
            if not isinstance(ref, str) or ":" not in ref:
                continue
            head, tail = ref.split(":", 1)
            if tail == "*":
                kind.setdefault(head, cid)
            else:
                exact.setdefault(ref, cid)
    return {"exact": exact, "kind": kind}


def match_instance(ref: str, index: dict[str, dict[str, str]]) -> str | None:
    """The component id for a node ref: the exact id wins over <kind>:*, else None."""
    exact = index.get("exact", {})
    if ref in exact:
        return exact[ref]
    head = ref.split(":", 1)[0] if ":" in ref else ""
    return index.get("kind", {}).get(head)


def _expect_refs(value: Any, where: str, repo_ids: set[str], status: str) -> list[Any]:
    if status not in EXPECT_STATUSES:
        raise AtlasError(f"{where}.expect: only a planned, documented or absent item may carry expect")
    refs = _list(value, f"{where}.expect")
    if not refs:
        raise AtlasError(f"{where}.expect: must be a non-empty list")
    for k, ref in enumerate(refs):
        _ref(ref, f"{where}.expect[{k}]", repo_ids)
    return refs


def _vocabulary(vocab: Any, where: str, repo_ids: set[str], component_ids: set[str]) -> dict[str, Any]:
    _keys(vocab, where, {"id", "title", "owner", "source", "terms"})
    if not isinstance(vocab["id"], str) or not ID_PATTERN.fullmatch(vocab["id"]):
        raise AtlasError(f"{where}.id: invalid id")
    _text(vocab["title"], f"{where}.title")
    if not _is(vocab["owner"], component_ids):
        raise AtlasError(f"{where}.owner: {vocab['owner']!r} is not a component")
    source = vocab["source"]
    _keys(source, f"{where}.source", REF_REQUIRED, VOCAB_SOURCE_OPTIONAL)
    if ("select" in source) == ("each" in source):
        raise AtlasError(f"{where}.source: must carry exactly one of select or each")
    stripped = {k: v for k, v in source.items() if k not in ("select", "each")}
    _ref(stripped, f"{where}.source", repo_ids)
    if "select" in source:
        select = source["select"]
        if not isinstance(select, str) or not 1 <= len(select) <= 120:
            raise AtlasError(f"{where}.source.select: must be 1-120 characters on one line")
        if "\n" in select or "\r" in select:
            raise AtlasError(f"{where}.source.select: must be 1-120 characters on one line")
        for segment in select.split("/"):
            if segment == "*":
                continue
            if not 1 <= len(segment) <= 64 or "\n" in segment or "\r" in segment:
                raise AtlasError(f"{where}.source.select: invalid segment {segment!r}")
    else:
        if source["each"] is not True:
            raise AtlasError(f"{where}.source.each: must be true")
    terms = _list(vocab["terms"], f"{where}.terms")
    if not terms or len(terms) > 500:
        raise AtlasError(f"{where}.terms: must be a non-empty list of at most 500 terms")
    seen: set[str] = set()
    for k, item in enumerate(terms):
        tw = f"{where}.terms[{k}]"
        _keys(item, tw, {"term", "means"})
        _text(item["term"], f"{tw}.term", 1, 80)
        _text(item["means"], f"{tw}.means", 1, 200)
        if item["term"] in seen:
            raise AtlasError(f"{where}.terms: duplicate term {item['term']!r}")
        seen.add(item["term"])
    return vocab


def _traffic_pattern(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 160:
        raise AtlasError(f"{where}: must be a non-empty pattern of at most 160 characters")
    if any(ch.isspace() for ch in value):
        raise AtlasError(f"{where}: must contain no whitespace")
    if value.count("*") > 1:
        raise AtlasError(f"{where}: must hold at most one '*'")
    if "*" in value and not value.endswith("*"):
        raise AtlasError(f"{where}: '*' must be the last character")
    return value


def _traffic_verbs(value: Any, where: str) -> list[str]:
    if isinstance(value, str):
        patterns = [value]
    elif isinstance(value, list):
        if not value:
            raise AtlasError(f"{where}: must be a non-empty list")
        patterns = value
    else:
        raise AtlasError(f"{where}: must be a pattern or a non-empty list of patterns")
    for i, pattern in enumerate(patterns):
        _traffic_pattern(pattern, f"{where}[{i}]" if isinstance(value, list) else where)
    return [pattern for pattern in patterns if isinstance(pattern, str)]


def _traffic_rule(rule: Any, where: str, vocab_terms: dict[str, set[str]]) -> None:
    _keys(rule, where, {"source"}, {"producer", "actor", "subject", "verb", "pulse"})
    if "pulse" in rule and rule["pulse"] is not True and rule["pulse"] is not False:
        raise AtlasError(f"{where}.pulse: must be a JSON boolean")
    source = rule["source"]
    if not isinstance(source, str) or source not in TRAFFIC_SOURCES:
        raise AtlasError(f"{where}.source: must be one of {sorted(TRAFFIC_SOURCES)}")
    if source == "events" and "actor" in rule:
        raise AtlasError(f"{where}.actor: not allowed on an events rule")
    for field in ("producer", "actor", "subject"):
        if field in rule:
            _traffic_pattern(rule[field], f"{where}.{field}")
    if "verb" in rule:
        patterns = _traffic_verbs(rule["verb"], f"{where}.verb")
        vocab_id = {"journal": "journal-verbs", "links": "link-relations"}.get(source)
        terms = vocab_terms.get(vocab_id) if vocab_id else None
        if terms is not None:
            for pattern in patterns:
                if pattern.endswith("*") and pattern != "*":
                    prefix = pattern[:-1]
                    if not any(term.startswith(prefix) for term in terms):
                        raise AtlasError(f"{where}.verb: {pattern!r} is not a prefix of the {vocab_id} vocabulary")
                elif pattern != "*":
                    if pattern not in terms:
                        raise AtlasError(f"{where}.verb: {pattern!r} is not in the {vocab_id} vocabulary")


def validate(doc: dict[str, Any]) -> None:
    """Raise AtlasError on the first violation; return None when valid."""
    _keys(doc, "atlas", TOP_KEYS, TOP_OPTIONAL)
    if not is_atlas_schema(doc.get("schema")):
        raise AtlasError(f"atlas.schema: must be {SCHEMA}")
    if not isinstance(doc["updated"], str) or not DATE_PATTERN.fullmatch(doc["updated"]):
        raise AtlasError("atlas.updated: must be YYYY-MM-DD")
    try:
        datetime.date.fromisoformat(doc["updated"])
    except ValueError:
        raise AtlasError("atlas.updated: not a real date") from None
    repos = doc["repos"]
    if not isinstance(repos, dict):
        raise AtlasError("atlas.repos: must be an object")
    for key, extra in repos.items():
        where = f"atlas.repos.{key}"
        if not ID_PATTERN.fullmatch(key):
            raise AtlasError(f"{where}: key must match the component id pattern")
        _keys(extra, where, EXTRA_REPO_KEYS)
        if not isinstance(extra["remote"], str) or not REMOTE_PATTERN.fullmatch(extra["remote"]):
            raise AtlasError(f"{where}.remote: must be owner/name")
        if not isinstance(extra["default_branch"], str) or not BRANCH_PATTERN.fullmatch(extra["default_branch"]):
            raise AtlasError(f"{where}.default_branch: invalid branch name")
        paths = extra["paths"]
        if not isinstance(paths, dict):
            raise AtlasError(f"{where}.paths: must map host names to absolute paths")
        for host, value in paths.items():
            if not HOST_PATTERN.fullmatch(host) or not isinstance(value, str) or not _absolute(value):
                raise AtlasError(f"{where}.paths.{host}: must map a host name to an absolute path")
    repo_ids = set(repos)

    layers = _list(doc["layers"], "atlas.layers")
    for i, layer in enumerate(layers):
        where = f"atlas.layers[{i}]"
        _keys(layer, where, LAYER_KEYS)
        if not isinstance(layer["id"], str) or not ID_PATTERN.fullmatch(layer["id"]):
            raise AtlasError(f"{where}.id: invalid id")
        _text(layer["title"], f"{where}.title")
        _text(layer["summary"], f"{where}.summary")
    _unique([layer["id"] for layer in layers], "atlas.layers")
    layer_ids = {layer["id"] for layer in layers}

    components = _list(doc["components"], "atlas.components")
    instance_owner: dict[str, str] = {}
    for i, comp in enumerate(components):
        where = f"atlas.components[{i}]"
        _keys(comp, where, COMPONENT_KEYS, COMPONENT_OPTIONAL)
        if not isinstance(comp["id"], str) or not ID_PATTERN.fullmatch(comp["id"]):
            raise AtlasError(f"{where}.id: invalid id")
        where = f"atlas.components.{comp['id']}"
        _text(comp["title"], f"{where}.title")
        _text(comp["summary"], f"{where}.summary")
        if not _is(comp["layer"], layer_ids):
            raise AtlasError(f"{where}.layer: {comp['layer']!r} is not a declared layer")
        _home(comp["home"], f"{where}.home", repo_ids)
        if not _is(comp["status"], COMPONENT_STATUS):
            raise AtlasError(f"{where}.status: must be one of {sorted(COMPONENT_STATUS)}")
        for j, owned in enumerate(_list(comp["owns"], f"{where}.owns")):
            _text(owned, f"{where}.owns[{j}]", 1, 120)
        evidence = 0
        for j, surface in enumerate(_list(comp["surfaces"], f"{where}.surfaces")):
            sw = f"{where}.surfaces[{j}]"
            _keys(surface, sw, SURFACE_KEYS)
            if not _is(surface["kind"], SURFACE_KINDS):
                raise AtlasError(f"{sw}.kind: must be one of {sorted(SURFACE_KINDS)}")
            _text(surface["name"], f"{sw}.name", 1, 160)
            for k, ref in enumerate(_list(surface["evidence"], f"{sw}.evidence")):
                _ref(ref, f"{sw}.evidence[{k}]", repo_ids)
                evidence += 1
        for k, ref in enumerate(_list(comp["evidence"], f"{where}.evidence")):
            _ref(ref, f"{where}.evidence[{k}]", repo_ids)
            evidence += 1
        if "instances" in comp:
            for j, entry in enumerate(_list(comp["instances"], f"{where}.instances")):
                if not isinstance(entry, str) or not INSTANCE_PATTERN.fullmatch(entry):
                    raise AtlasError(f"{where}.instances[{j}]: invalid instance {entry!r}")
                kind, _, tail = entry.partition(":")
                if tail != "*" and "*" in tail:
                    raise AtlasError(f"{where}.instances[{j}]: invalid instance {entry!r}")
                if entry in instance_owner:
                    raise AtlasError(f"{where}.instances[{j}]: duplicate instance {entry!r} "
                                     f"also declared by {instance_owner[entry]!r}")
                instance_owner[entry] = comp["id"]
        if "expect" in comp:
            _expect_refs(comp["expect"], where, repo_ids, comp["status"])
        if comp["status"] in EVIDENCED and evidence == 0 and not str(comp["home"]).startswith("external:"):
            raise AtlasError(f"{where}: a {comp['status']} component needs at least one evidence reference")
    _unique([comp["id"] for comp in components], "atlas.components")
    component_ids = {comp["id"] for comp in components}

    contracts = _list(doc["contracts"], "atlas.contracts")
    for i, contract in enumerate(contracts):
        where = f"atlas.contracts[{i}]"
        _keys(contract, where, CONTRACT_KEYS, CONTRACT_OPTIONAL)
        if not isinstance(contract["id"], str) or not CONTRACT_ID_PATTERN.fullmatch(contract["id"]):
            raise AtlasError(f"{where}.id: invalid contract id")
        where = f"atlas.contracts.{contract['id']}"
        _text(contract["title"], f"{where}.title")
        _text(contract["summary"], f"{where}.summary")
        if not _is(contract["producer"], component_ids):
            raise AtlasError(f"{where}.producer: {contract['producer']!r} is not a component")
        for consumer in _list(contract["consumers"], f"{where}.consumers"):
            if not _is(consumer, component_ids):
                raise AtlasError(f"{where}.consumers: {consumer!r} is not a component")
        if not _is(contract["format"], FORMATS):
            raise AtlasError(f"{where}.format: must be one of {sorted(FORMATS)}")
        if not _is(contract["status"], CONTRACT_STATUS):
            raise AtlasError(f"{where}.status: must be one of {sorted(CONTRACT_STATUS)}")
        refs = _list(contract["evidence"], f"{where}.evidence")
        for k, ref in enumerate(refs):
            _ref(ref, f"{where}.evidence[{k}]", repo_ids)
        if "expect" in contract:
            _expect_refs(contract["expect"], where, repo_ids, contract["status"])
        if contract["status"] in EVIDENCED and not refs:
            raise AtlasError(f"{where}: a {contract['status']} contract needs at least one evidence reference")
    _unique([contract["id"] for contract in contracts], "atlas.contracts")
    contract_ids = {contract["id"] for contract in contracts}

    flows = _list(doc["flows"], "atlas.flows")
    for i, flow in enumerate(flows):
        where = f"atlas.flows[{i}]"
        _keys(flow, where, FLOW_REQUIRED, FLOW_OPTIONAL)
        if not isinstance(flow["id"], str) or not ID_PATTERN.fullmatch(flow["id"]):
            raise AtlasError(f"{where}.id: invalid id")
        where = f"atlas.flows.{flow['id']}"
        for end in ("from", "to"):
            if not _is(flow[end], component_ids):
                raise AtlasError(f"{where}.{end}: {flow[end]!r} is not a component")
        if flow["contract"] is not None and not _is(flow["contract"], contract_ids):
            raise AtlasError(f"{where}.contract: {flow['contract']!r} is not a contract")
        _text(flow["trigger"], f"{where}.trigger", 1, 200)
        if not _is(flow["status"], FLOW_STATUS):
            raise AtlasError(f"{where}.status: must be one of {sorted(FLOW_STATUS)}")
        if flow["status"] == "live" and "gap" in flow:
            raise AtlasError(f"{where}.gap: a live flow has no gap")
        if flow["status"] != "live":
            if "gap" not in flow:
                raise AtlasError(f"{where}.gap: required when the status is not live")
            _text(flow["gap"], f"{where}.gap")
        refs = _list(flow["evidence"], f"{where}.evidence")
        for k, ref in enumerate(refs):
            _ref(ref, f"{where}.evidence[{k}]", repo_ids)
        if "expect" in flow:
            _expect_refs(flow["expect"], where, repo_ids, flow["status"])
        if flow["status"] in EVIDENCED and not refs:
            raise AtlasError(f"{where}: a {flow['status']} flow needs at least one evidence reference")
    _unique([flow["id"] for flow in flows], "atlas.flows")

    vocabs: list[Any] = _list(doc["vocabularies"], "atlas.vocabularies") if "vocabularies" in doc else []
    for i, vocab in enumerate(vocabs):
        _vocabulary(vocab, f"atlas.vocabularies[{i}]", repo_ids, component_ids)
    _unique([v["id"] for v in vocabs if isinstance(v, dict) and isinstance(v.get("id"), str)],
            "atlas.vocabularies")
    for vocab in vocabs:
        if isinstance(vocab, dict) and vocab.get("id") == "node-kinds":
            claimed: set[str] = set()
            for comp in components:
                for entry in comp.get("instances", []) or []:
                    if isinstance(entry, str) and ":" in entry:
                        claimed.add(entry.split(":", 1)[0])
            unclaimed = sorted(t["term"] for t in vocab["terms"] if t["term"] not in claimed)
            if unclaimed:
                raise AtlasError(f"atlas.vocabularies.node-kinds: unclaimed kinds {unclaimed}")
    vocab_terms: dict[str, set[str]] = {}
    for vocab in vocabs:
        if isinstance(vocab, dict) and isinstance(vocab.get("id"), str):
            vocab_terms[vocab["id"]] = {item["term"] for item in vocab["terms"]
                                        if isinstance(item, dict) and isinstance(item.get("term"), str)}
    for flow in flows:
        if not isinstance(flow, dict) or "traffic" not in flow:
            continue
        where = f"atlas.flows.{flow['id']}"
        rules = _list(flow["traffic"], f"{where}.traffic")
        if not rules:
            raise AtlasError(f"{where}.traffic: must be a non-empty list")
        for k, rule in enumerate(rules):
            _traffic_rule(rule, f"{where}.traffic[{k}]", vocab_terms)
    return None
