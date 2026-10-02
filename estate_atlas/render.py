"""Render the estate atlas into an offline HTML page and a Mermaid flowchart.

Standard library only. Rendering is deterministic: same inputs, same bytes.
"""
from __future__ import annotations

import html
import json
import re
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

SCHEMA = "estate-atlas@2"
CHECK_SCHEMA = "estate-atlas-check@2"


def _markdown_text(value: Any) -> str:
    """Literal atlas text in Markdown: no raw HTML, links, or inline code."""
    text = html.escape(str(value), quote=False)
    return re.sub(r"([\\`*_\[\]|])", r"\\\1", text)


def is_atlas_schema(value: Any) -> bool:
    """Accept ``estate-atlas@2`` or any namespace ending in ``/atlas@2``."""
    return isinstance(value, str) and (
        value == SCHEMA or value.endswith("/atlas@2")
    )


def is_check_schema(value: Any) -> bool:
    """Accept ``estate-atlas-check@2`` or any namespace ending likewise."""
    return isinstance(value, str) and (
        value == CHECK_SCHEMA or value.endswith("/atlas-check@2")
    )

# The same sets as model.py, so every atlas that validates also renders.
COMPONENT_STATUSES = ("live", "partial", "planned", "retired")
CONTRACT_STATUSES = ("live", "partial", "planned", "retired")
FLOW_STATUSES = ("live", "partial", "documented", "planned", "absent")
GAP_ORDER = ("absent", "planned", "documented", "partial")
EXPECT_STATUSES = ("planned", "documented", "absent")
INSTANCE_RE = re.compile(r"^[a-z][a-z-]{0,31}:(\*|[A-Za-z0-9._/#@:+-]{1,200})$")
VOCAB_STATUSES = ("ok", "drift", "unresolved")

# Contract ids may carry versions and namespaces; keep the grammar permissive here.
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}")
REPO_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
REMOTE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")
DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


class AtlasError(ValueError):
    """Invalid atlas or check input."""


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AtlasError("duplicate JSON key %r" % (key,))
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise AtlasError("non-finite JSON number %r" % (value,))


def strict_json(text: str) -> Any:
    try:
        return json.loads(
            text, object_pairs_hook=_no_duplicates, parse_constant=_reject_constant
        )
    except json.JSONDecodeError as exc:
        raise AtlasError("invalid JSON: %s at line %s" % (exc.msg, exc.lineno)) from exc


def read_json_file(path: Path) -> Any:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise AtlasError("cannot read %s: %s" % (path, exc.strerror or exc)) from exc
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AtlasError("file is not UTF-8: %s" % (path,)) from exc
    return strict_json(text)


def _need_id(value: Any, where: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise AtlasError("%s must be a short id" % (where,))
    return value


def _need_line(value: Any, where: str, limit: int = 300) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AtlasError("%s must be a non-empty string" % (where,))
    if len(value) > limit or "\n" in value or "\r" in value:
        raise AtlasError("%s must be a single line of at most %d characters" % (where, limit))
    return value


def _need_text(value: Any, where: str, limit: int = 2000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AtlasError("%s must be a non-empty string" % (where,))
    if len(value) > limit:
        raise AtlasError("%s must be at most %d characters" % (where, limit))
    return value


def _valid_date(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise AtlasError("%s must be a YYYY-MM-DD date" % (where,))
    match = DATE_RE.fullmatch(value)
    if not match:
        raise AtlasError("%s must be a YYYY-MM-DD date" % (where,))
    try:
        date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError as exc:
        raise AtlasError("%s is not a calendar date" % (where,)) from exc
    return value


def _valid_ref(ref: Any, where: str) -> dict[str, Any]:
    if not isinstance(ref, dict):
        raise AtlasError("%s must be an object" % (where,))
    allowed = {"repo", "path", "anchor", "note", "url"}
    extra = set(ref) - allowed
    if extra:
        raise AtlasError("%s has unknown keys: %s" % (where, sorted(extra)))
    repo = ref.get("repo")
    if not isinstance(repo, str) or not repo.strip() or len(repo) > 200:
        raise AtlasError("%s.repo must be a non-empty string" % (where,))
    if "\n" in repo or "\r" in repo:
        raise AtlasError("%s.repo must be a single line" % (where,))
    p = ref.get("path")
    if not isinstance(p, str) or not p or len(p) > 500 or "\\" in p:
        raise AtlasError("%s.path must be a repository-relative POSIX path" % (where,))
    parts = p.split("/")
    if p.startswith("/") or p == "" or any(seg in ("", ".", "..") for seg in parts):
        raise AtlasError("%s.path must be a canonical relative path" % (where,))
    out: dict[str, Any] = {"repo": repo, "path": p}
    for key in ("anchor", "note"):
        if key in ref:
            val = ref[key]
            if not isinstance(val, str) or not val or len(val) > 300:
                raise AtlasError("%s.%s must be a short non-empty string" % (where, key))
            out[key] = val
    if "url" in ref:
        url = ref["url"]
        if not isinstance(url, str) or not url.startswith("https://github.com/"):
            raise AtlasError("%s.url must be a github link" % (where,))
        out["url"] = url
    return out


def _ref_list(value: Any, where: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise AtlasError("%s must be a list" % (where,))
    return [_valid_ref(item, "%s[%d]" % (where, i)) for i, item in enumerate(value)]


def _need_instances(value: Any, where: str) -> list[str]:
    if not isinstance(value, list):
        raise AtlasError("%s must be a list" % (where,))
    out: list[str] = []
    for i, entry in enumerate(value):
        if not isinstance(entry, str) or not INSTANCE_RE.fullmatch(entry):
            raise AtlasError("%s[%d] is not a <kind>:* or <kind>:<id> instance" % (where, i))
        tail = entry.split(":", 1)[1]
        if tail != "*" and "*" in tail:
            raise AtlasError("%s[%d] is not a <kind>:* or <kind>:<id> instance" % (where, i))
        out.append(entry)
    return out


def _need_traffic(value: Any, where: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise AtlasError("%s.traffic must be a non-empty list" % (where,))
    allowed = {"source", "producer", "actor", "subject", "verb", "pulse"}
    rules: list[dict[str, Any]] = []
    for i, rule in enumerate(value):
        rwhere = "%s.traffic[%d]" % (where, i)
        if not isinstance(rule, dict):
            raise AtlasError("%s must be an object" % (rwhere,))
        if set(rule) - allowed:
            raise AtlasError("%s has unknown keys" % (rwhere,))
        source = rule.get("source")
        if source not in ("journal", "links", "events"):
            raise AtlasError("%s.source must be journal, links or events" % (rwhere,))
        if source == "events" and "actor" in rule:
            raise AtlasError("%s.actor is not allowed on an events rule" % (rwhere,))
        out: dict[str, Any] = {"source": source}
        for field in ("producer", "actor", "subject"):
            if field in rule:
                val = rule[field]
                if not isinstance(val, str) or not val:
                    raise AtlasError("%s.%s must be a non-empty string" % (rwhere, field))
                out[field] = val
        if "verb" in rule:
            verb = rule["verb"]
            if isinstance(verb, str):
                if not verb:
                    raise AtlasError("%s.verb must be a non-empty string" % (rwhere,))
                out["verb"] = verb
            elif isinstance(verb, list):
                if not verb or any(not isinstance(v, str) or not v for v in verb):
                    raise AtlasError("%s.verb must be a pattern or a non-empty list of patterns"
                                     % (rwhere,))
                out["verb"] = list(verb)
            else:
                raise AtlasError("%s.verb must be a pattern or a non-empty list of patterns"
                                 % (rwhere,))
        if "pulse" in rule:
            if rule["pulse"] is not True and rule["pulse"] is not False:
                raise AtlasError("%s.pulse must be a JSON boolean" % (rwhere,))
            out["pulse"] = rule["pulse"]
        rules.append(out)
    return rules


def _need_expect(value: Any, where: str, status: str) -> list[dict[str, Any]]:
    if status not in EXPECT_STATUSES:
        raise AtlasError("%s.expect: only a planned, documented or absent item may carry expect" % (where,))
    refs = _ref_list(value, where + ".expect")
    if not refs:
        raise AtlasError("%s.expect must be a non-empty list" % (where,))
    return refs


def _parse_vocabularies(raw: Any, comp_ids: set[str]) -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise AtlasError("vocabularies must be a list")
    vocabs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, item in enumerate(raw):
        where = "vocabularies[%d]" % i
        if not isinstance(item, dict):
            raise AtlasError("%s must be an object" % (where,))
        if set(item) - {"id", "title", "owner", "source", "terms"}:
            raise AtlasError("%s has unknown keys" % (where,))
        vid = _need_id(item.get("id"), where + ".id")
        if vid in seen:
            raise AtlasError("duplicate vocabulary id %r" % (vid,))
        seen.add(vid)
        title = _need_line(item.get("title"), where + ".title", 300)
        owner = item.get("owner")
        if owner not in comp_ids:
            raise AtlasError("%s.owner names an unknown component" % (where,))
        source = item.get("source")
        if not isinstance(source, dict):
            raise AtlasError("%s.source must be an object" % (where,))
        if set(source) - {"repo", "path", "anchor", "note", "url", "select", "each"}:
            raise AtlasError("%s.source has unknown keys" % (where,))
        if ("select" in source) == ("each" in source):
            raise AtlasError("%s.source must carry exactly one of select or each" % (where,))
        stripped = {k: v for k, v in source.items() if k not in ("select", "each")}
        ref = _valid_ref(stripped, where + ".source")
        if "select" in source:
            sel = source["select"]
            if not isinstance(sel, str) or not sel or len(sel) > 120 or "\n" in sel or "\r" in sel:
                raise AtlasError("%s.source.select must be 1-120 characters on one line" % (where,))
            ref["select"] = sel
        else:
            if source["each"] is not True:
                raise AtlasError("%s.source.each must be true" % (where,))
            ref["each"] = True
        terms_raw = item.get("terms")
        if not isinstance(terms_raw, list) or not terms_raw:
            raise AtlasError("%s.terms must be a non-empty list" % (where,))
        terms: list[dict[str, str]] = []
        seen_terms: set[str] = set()
        for j, term_item in enumerate(terms_raw):
            twhere = "%s.terms[%d]" % (where, j)
            if not isinstance(term_item, dict) or set(term_item) - {"term", "means"}:
                raise AtlasError("%s must have exactly term, means" % (twhere,))
            term = _need_line(term_item.get("term"), twhere + ".term", 80)
            means = _need_line(term_item.get("means"), twhere + ".means", 200)
            if term in seen_terms:
                raise AtlasError("%s.terms: duplicate term %r" % (where, term))
            seen_terms.add(term)
            terms.append({"term": term, "means": means})
        vocabs.append({"id": vid, "title": title, "owner": owner,
                       "source": ref, "terms": terms})
    return vocabs


def parse_atlas(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise AtlasError("atlas must be a JSON object")
    schema = data.get("schema")
    if not is_atlas_schema(schema):
        raise AtlasError("atlas schema must be estate-atlas@2 or end in /atlas@2")
    allowed = {"schema", "updated", "repos", "layers", "components", "contracts", "flows",
               "vocabularies", "check"}
    extra = set(data) - allowed
    if extra:
        raise AtlasError("atlas has unknown keys: %s" % (sorted(extra),))
    updated = _valid_date(data.get("updated"), "updated")

    repos_raw = data.get("repos", {})
    if not isinstance(repos_raw, dict):
        raise AtlasError("repos must be an object")
    repos: dict[str, dict[str, Any]] = {}
    for rid, entry in repos_raw.items():
        if not isinstance(rid, str) or not REPO_ID_RE.fullmatch(rid):
            raise AtlasError("repos key %r is not a plain repo id" % (rid,))
        if not isinstance(entry, dict):
            raise AtlasError("repos[%s] must be an object" % (rid,))
        if set(entry) - {"remote", "default_branch", "paths"}:
            raise AtlasError("repos[%s] has unknown keys" % (rid,))
        remote = entry.get("remote")
        if not isinstance(remote, str) or not REMOTE_RE.fullmatch(remote):
            raise AtlasError("repos[%s].remote must be owner/name" % (rid,))
        branch = entry.get("default_branch", "main")
        if not isinstance(branch, str) or not BRANCH_RE.fullmatch(branch) or ".." in branch:
            raise AtlasError("repos[%s].default_branch is not a plain branch name" % (rid,))
        paths = entry.get("paths", {})
        if not isinstance(paths, dict):
            raise AtlasError("repos[%s].paths must be an object" % (rid,))
        repos[rid] = {"remote": remote, "default_branch": branch, "paths": dict(paths)}

    layers_raw = data.get("layers", [])
    if not isinstance(layers_raw, list) or not layers_raw:
        raise AtlasError("layers must be a non-empty list")
    layers: list[dict[str, Any]] = []
    layer_ids: set[str] = set()
    for i, item in enumerate(layers_raw):
        where = "layers[%d]" % i
        if not isinstance(item, dict) or set(item) - {"id", "title", "summary"}:
            raise AtlasError("%s must have exactly id, title, summary" % (where,))
        lid = _need_id(item.get("id"), where + ".id")
        if lid in layer_ids:
            raise AtlasError("duplicate layer id %r" % (lid,))
        layer_ids.add(lid)
        layers.append({
            "id": lid,
            "title": _need_line(item.get("title"), where + ".title", 300),
            "summary": _need_text(item.get("summary"), where + ".summary", 2000),
        })

    comps_raw = data.get("components", [])
    if not isinstance(comps_raw, list):
        raise AtlasError("components must be a list")
    components: list[dict[str, Any]] = []
    comp_ids: set[str] = set()
    for i, item in enumerate(comps_raw):
        where = "components[%d]" % i
        if not isinstance(item, dict):
            raise AtlasError("%s must be an object" % (where,))
        if set(item) - {"id", "title", "layer", "home", "status", "summary",
                         "surfaces", "owns", "evidence", "instances", "expect"}:
            raise AtlasError("%s has unknown keys" % (where,))
        cid = _need_id(item.get("id"), where + ".id")
        if cid in comp_ids:
            raise AtlasError("duplicate component id %r" % (cid,))
        comp_ids.add(cid)
        layer = item.get("layer")
        if layer not in layer_ids:
            raise AtlasError("%s.layer names an unknown layer" % (where,))
        home = item.get("home")
        if not isinstance(home, str) or not home:
            raise AtlasError("%s.home must be a repo id or external:<name>" % (where,))
        if home.startswith("external:"):
            if len(home) <= len("external:") or len(home) > 200:
                raise AtlasError("%s.home names an empty external" % (where,))
        elif not REPO_ID_RE.fullmatch(home):
            raise AtlasError("%s.home must be a repo id or external:<name>" % (where,))
        status = item.get("status")
        if status not in COMPONENT_STATUSES:
            raise AtlasError("%s.status must be one of %s" % (where, list(COMPONENT_STATUSES)))
        surfaces_raw = item.get("surfaces", [])
        if not isinstance(surfaces_raw, list):
            raise AtlasError("%s.surfaces must be a list" % (where,))
        surfaces = []
        for j, surf in enumerate(surfaces_raw):
            swhere = "%s.surfaces[%d]" % (where, j)
            if not isinstance(surf, dict) or set(surf) - {"kind", "name", "evidence"}:
                raise AtlasError("%s must have exactly kind, name, evidence" % (swhere,))
            surfaces.append({
                "kind": _need_line(surf.get("kind"), swhere + ".kind", 80),
                "name": _need_line(surf.get("name"), swhere + ".name", 200),
                "evidence": _ref_list(surf.get("evidence", []), swhere + ".evidence"),
            })
        owns = item.get("owns", [])
        if not isinstance(owns, list) or any(not isinstance(x, str) or not x for x in owns):
            raise AtlasError("%s.owns must be a list of strings" % (where,))
        instances = _need_instances(item.get("instances", []), where + ".instances") if "instances" in item else []
        expect = _need_expect(item.get("expect"), where, status) if "expect" in item else []
        components.append({
            "id": cid,
            "title": _need_line(item.get("title"), where + ".title", 300),
            "layer": layer,
            "home": home,
            "status": status,
            "summary": _need_text(item.get("summary"), where + ".summary", 4000),
            "surfaces": surfaces,
            "owns": list(owns),
            "evidence": _ref_list(item.get("evidence", []), where + ".evidence"),
            "instances": instances,
            "expect": expect,
        })

    contracts_raw = data.get("contracts", [])
    if not isinstance(contracts_raw, list):
        raise AtlasError("contracts must be a list")
    contracts: list[dict[str, Any]] = []
    contract_ids: set[str] = set()
    for i, item in enumerate(contracts_raw):
        where = "contracts[%d]" % i
        if not isinstance(item, dict):
            raise AtlasError("%s must be an object" % (where,))
        if set(item) - {"id", "title", "producer", "consumers", "format",
                         "status", "summary", "evidence", "expect"}:
            raise AtlasError("%s has unknown keys" % (where,))
        kid = _need_id(item.get("id"), where + ".id")
        if kid in contract_ids:
            raise AtlasError("duplicate contract id %r" % (kid,))
        contract_ids.add(kid)
        producer = item.get("producer")
        if producer not in comp_ids:
            raise AtlasError("%s.producer names an unknown component" % (where,))
        consumers = item.get("consumers", [])
        if not isinstance(consumers, list):
            raise AtlasError("%s.consumers must be a list" % (where,))
        for consumer in consumers:
            if consumer not in comp_ids:
                raise AtlasError("%s names an unknown consumer component" % (where,))
        cstatus = _need_line(item.get("status"), where + ".status", 40)
        if cstatus not in CONTRACT_STATUSES:
            raise AtlasError("%s.status must be one of %s" % (where, list(CONTRACT_STATUSES)))
        cexpect = _need_expect(item.get("expect"), where, cstatus) if "expect" in item else []
        contracts.append({
            "id": kid,
            "title": _need_line(item.get("title"), where + ".title", 300),
            "producer": producer,
            "consumers": list(consumers),
            "format": _need_line(item.get("format"), where + ".format", 120),
            "status": cstatus,
            "summary": _need_text(item.get("summary"), where + ".summary", 4000),
            "evidence": _ref_list(item.get("evidence", []), where + ".evidence"),
            "expect": cexpect,
        })

    flows_raw = data.get("flows", [])
    if not isinstance(flows_raw, list):
        raise AtlasError("flows must be a list")
    flows: list[dict[str, Any]] = []
    flow_ids: set[str] = set()
    for i, item in enumerate(flows_raw):
        where = "flows[%d]" % i
        if not isinstance(item, dict):
            raise AtlasError("%s must be an object" % (where,))
        if set(item) - {"id", "from", "to", "contract", "trigger",
                         "status", "gap", "evidence", "expect", "traffic"}:
            raise AtlasError("%s has unknown keys" % (where,))
        fid = _need_id(item.get("id"), where + ".id")
        if fid in flow_ids:
            raise AtlasError("duplicate flow id %r" % (fid,))
        flow_ids.add(fid)
        src = item.get("from")
        dst = item.get("to")
        if src not in comp_ids:
            raise AtlasError("%s.from names an unknown component" % (where,))
        if dst not in comp_ids:
            raise AtlasError("%s.to names an unknown component" % (where,))
        contract = item.get("contract")
        if contract is not None and contract not in contract_ids:
            raise AtlasError("%s.contract names an unknown contract" % (where,))
        status = item.get("status")
        if status not in FLOW_STATUSES:
            raise AtlasError("%s.status must be one of %s" % (where, list(FLOW_STATUSES)))
        gap = item.get("gap", "")
        if status == "live":
            if gap not in (None, ""):
                raise AtlasError("%s.gap is for non-live flows only" % (where,))
            gap = ""
        else:
            if not isinstance(gap, str) or not gap.strip():
                raise AtlasError("%s.gap is required for non-live flows" % (where,))
        fexpect = _need_expect(item.get("expect"), where, status) if "expect" in item else []
        traffic = _need_traffic(item.get("traffic"), where) if "traffic" in item else []
        flows.append({
            "id": fid,
            "from": src,
            "to": dst,
            "contract": contract,
            "trigger": _need_line(item.get("trigger"), where + ".trigger", 300),
            "status": status,
            "gap": gap if isinstance(gap, str) else "",
            "evidence": _ref_list(item.get("evidence", []), where + ".evidence"),
            "expect": fexpect,
            "traffic": traffic,
        })

    seen_instances: dict[str, str] = {}
    for comp in components:
        for entry in comp.get("instances", []):
            if entry in seen_instances:
                raise AtlasError("duplicate instance %r" % (entry,))
            seen_instances[entry] = comp["id"]
    vocabularies = _parse_vocabularies(data.get("vocabularies", []), comp_ids)
    out: dict[str, Any] = {"schema": schema, "updated": updated, "repos": repos, "layers": layers,
            "components": components, "contracts": contracts, "flows": flows,
            "vocabularies": vocabularies}
    if "check" in data:
        out["check"] = parse_check(data["check"])
    return out


def _parse_check_vocabularies(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise AtlasError("check.vocabularies must be a list")
    out: list[dict[str, Any]] = []
    for i, item in enumerate(value):
        where = "check.vocabularies[%d]" % i
        if not isinstance(item, dict):
            raise AtlasError("%s must be an object" % (where,))
        vid = item.get("id")
        if not isinstance(vid, str) or not vid:
            raise AtlasError("%s.id must be a non-empty string" % (where,))
        status = item.get("status")
        if status not in VOCAB_STATUSES:
            raise AtlasError("%s.status must be one of %s" % (where, list(VOCAB_STATUSES)))
        for key in ("missing_in_atlas", "missing_in_source"):
            vals = item.get(key, [])
            if not isinstance(vals, list) or any(not isinstance(x, str) for x in vals):
                raise AtlasError("%s.%s must be a list of strings" % (where, key))
        entry: dict[str, Any] = {
            "id": vid,
            "status": status,
            "missing_in_atlas": list(item.get("missing_in_atlas", [])),
            "missing_in_source": list(item.get("missing_in_source", [])),
        }
        if "why" in item:
            if not isinstance(item["why"], str):
                raise AtlasError("%s.why must be a string" % (where,))
            entry["why"] = item["why"]
        out.append(entry)
    return out


def _parse_check_promotable(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise AtlasError("check.promotable must be a list")
    out: list[dict[str, Any]] = []
    for i, item in enumerate(value):
        where = "check.promotable[%d]" % i
        if not isinstance(item, dict):
            raise AtlasError("%s must be an object" % (where,))
        owner = item.get("owner")
        if not isinstance(owner, str) or not owner:
            raise AtlasError("%s.owner must be a non-empty string" % (where,))
        refs = item.get("refs")
        if not isinstance(refs, int) or isinstance(refs, bool) or refs < 0:
            raise AtlasError("%s.refs must be a non-negative integer" % (where,))
        out.append({"owner": owner, "refs": refs})
    return out


def parse_check(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise AtlasError("check must be a JSON object")
    if not is_check_schema(data.get("schema")):
        raise AtlasError("check schema must be estate-atlas-check@2 or end in /atlas-check@2")
    for key in ("host", "status"):
        if not isinstance(data.get(key), str) or not data[key]:
            raise AtlasError("check.%s must be a non-empty string" % (key,))
    for key in ("checked", "ok"):
        if not isinstance(data.get(key), int) or isinstance(data.get(key), bool):
            raise AtlasError("check.%s must be an integer" % (key,))
        if data[key] < 0:
            raise AtlasError("check.%s must not be negative" % (key,))
    for key in ("missing", "unresolved"):
        if not isinstance(data.get(key), list):
            raise AtlasError("check.%s must be a list" % (key,))
    heads = data.get("heads", {})
    if not isinstance(heads, dict):
        raise AtlasError("check.heads must be an object")
    summary = data.get("summary", {})
    if not isinstance(summary, dict):
        raise AtlasError("check.summary must be an object")
    traffic = data.get("traffic", [])
    if not isinstance(traffic, list):
        raise AtlasError("check.traffic must be a list")
    return {"host": data["host"], "checked": data["checked"], "ok": data["ok"],
            "missing": list(data["missing"]), "unresolved": list(data["unresolved"]),
            "heads": dict(heads), "status": data["status"],
            "vocabularies": _parse_check_vocabularies(data.get("vocabularies")),
            "promotable": _parse_check_promotable(data.get("promotable")),
            "summary": dict(summary), "traffic": list(traffic)}


def ref_href(ref: dict[str, Any],
             atlas_repos: dict[str, dict[str, Any]]) -> str | None:
    if ref.get("url"):
        return str(ref["url"])
    repo = str(ref.get("repo"))
    if repo not in atlas_repos:
        return None
    remote = atlas_repos[repo]["remote"]
    branch = atlas_repos[repo]["default_branch"]
    path = str(ref.get("path"))
    href = "https://github.com/%s/blob/%s/%s" % (remote, branch, path)
    if ref.get("anchor"):
        href += "#%s" % (ref["anchor"],)
    return href


def order_cards(atlas: dict[str, Any]) -> dict[str, list[str]]:
    """Barycentre order per layer: two passes, ties by id."""
    by_layer: dict[str, list[str]] = {layer["id"]: [] for layer in atlas["layers"]}
    for comp in atlas["components"]:
        by_layer[comp["layer"]].append(comp["id"])
    for lid in by_layer:
        by_layer[lid] = sorted(by_layer[lid])
        seed = [c["id"] for c in atlas["components"] if c["layer"] == lid]
        if seed:
            pos = {cid: i for i, cid in enumerate(seed)}
            by_layer[lid] = sorted(by_layer[lid], key=lambda c: (pos.get(c, 0), c))
    neighbours: dict[str, set[str]] = {c["id"]: set() for c in atlas["components"]}
    for flow in atlas["flows"]:
        neighbours[flow["from"]].add(flow["to"])
        neighbours[flow["to"]].add(flow["from"])
    layer_index = {layer["id"]: i for i, layer in enumerate(atlas["layers"])}
    comp_layer = {c["id"]: c["layer"] for c in atlas["components"]}
    order = len(atlas["layers"])
    for _pass in range(2):
        seq = range(order) if _pass == 0 else range(order - 1, -1, -1)
        for li in seq:
            lid = atlas["layers"][li]["id"]
            if _pass == 0 and li == 0:
                continue
            if _pass == 1 and li == order - 1:
                continue
            adj = atlas["layers"][li - 1]["id"] if _pass == 0 else atlas["layers"][li + 1]["id"]
            adj_pos = {cid: i for i, cid in enumerate(by_layer[adj])}
            current = {cid: i for i, cid in enumerate(by_layer[lid])}
            def key(cid: str) -> tuple[float, str]:
                vals = sorted(adj_pos[n] for n in neighbours[cid]
                              if comp_layer.get(n) == adj and n in adj_pos)
                if not vals:
                    return (float(current[cid]), cid)
                return (sum(vals) / len(vals), cid)
            by_layer[lid] = sorted(by_layer[lid], key=key)
    _ = layer_index
    return by_layer


def _mmd_id(raw: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_]", "_", raw)
    if not safe or safe[0].isdigit():
        safe = "n_" + safe
    return safe


def _mmd_text(raw: str) -> str:
    table = {"#": "#nbr;", "&": "#amp;", '"': "#quot;", "<": "#lt;",
             ">": "#gt;", "|": "#124;", "{": "#123;", "}": "#125;",
             ";": "#59;", "`": "'", "\n": " "}
    return "".join(table.get(ch, ch) for ch in str(raw)).strip()


def render_mermaid(atlas: dict[str, Any], view: str = "flows") -> str:
    if view not in ("flows", "components"):
        raise AtlasError("view must be flows or components")
    lines = ["flowchart TB"]
    comp_layer = {c["id"]: c["layer"] for c in atlas["components"]}
    node_ids = {_c["id"]: _mmd_id("c_" + _c["id"]) for _c in atlas["components"]}
    for layer in atlas["layers"]:
        lines.append('    subgraph %s ["%s"]' % (_mmd_id("l_" + layer["id"]), _mmd_text(layer["title"])))
        for comp in sorted((c for c in atlas["components"] if c["layer"] == layer["id"]),
                           key=lambda c: c["id"]):
            label = "%s<br/>%s" % (_mmd_text(comp["title"]), _mmd_text(comp["home"]))
            lines.append('        %s["%s"]' % (node_ids[comp["id"]], label))
        lines.append("    end")
    lines.append("    classDef live fill:#dcfce7,stroke:#166534,color:#14532d")
    lines.append("    classDef partial fill:#fef3c7,stroke:#92400e,color:#451a03")
    lines.append("    classDef planned fill:#e0e7ff,stroke:#3730a3,color:#1e1b4b")
    lines.append("    classDef retired fill:#f1f5f9,stroke:#475569,color:#0f172a")
    for comp in sorted(atlas["components"], key=lambda c: c["id"]):
        lines.append("    class %s %s" % (node_ids[comp["id"]], comp["status"]))
    if view == "flows":
        for flow in sorted(atlas["flows"], key=lambda f: f["id"]):
            src = node_ids[flow["from"]]
            dst = node_ids[flow["to"]]
            label = _mmd_text(flow["contract"] if flow["contract"] else flow["id"])
            if flow["status"] in ("live", "partial"):
                lines.append("    %s -->|%s| %s" % (src, label, dst))
            else:
                lines.append("    %s -.->|%s| %s" % (src, label, dst))
        _ = comp_layer
    return "\n".join(lines) + "\n"


CSS_TEXT = """:root{
--bg:#ffffff;--fg:#0f172a;--muted:#334155;--band:#f8fafc;--card:#ffffff;
--line:#cbd5e1;--accent:#1d4ed8;--pill-fg:#ffffff;
--live:#166534;--partial:#92400e;--documented:#334155;--planned:#3730a3;--absent:#991b1b;
--edge-live:#15803d;--edge-partial:#b45309;--edge-slate:#475569;--edge-absent:#b91c1c;
--focus:#1d4ed8;
}
@media (prefers-color-scheme: dark){
:root{
--bg:#0b1220;--fg:#f1f5f9;--muted:#cbd5e1;--band:#111c33;--card:#16223d;
--line:#3b4d6b;--accent:#93c5fd;
--live:#4ade80;--partial:#fbbf24;--documented:#cbd5e1;--planned:#a5b4fc;--absent:#f87171;
--edge-live:#4ade80;--edge-partial:#fbbf24;--edge-slate:#94a3b8;--edge-absent:#f87171;
--focus:#93c5fd;
}
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font-family:system-ui,-apple-system,"Segoe UI",Roboto,Ubuntu,Cantarell,sans-serif;line-height:1.5}
header{padding:16px 20px;border-bottom:2px solid var(--line)}
h1{font-size:1.6rem;margin:0 0 4px}
.meta{color:var(--muted);margin:0 0 8px}
.pills{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}
.pill{display:inline-block;padding:2px 10px;border-radius:999px;font-size:.8rem;
background:var(--muted);color:#fff}
.pill.live{background:var(--live);color:#fff}
.pill.partial{background:var(--partial);color:#fff}
.pill.documented{background:var(--documented);color:#fff}
.pill.planned{background:var(--planned);color:#fff}
.pill.absent{background:var(--absent);color:#fff}
.check{margin:8px 0;font-weight:600}
.check.bad{color:var(--absent)}
.toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:12px 20px;
border-bottom:1px solid var(--line)}
.toolbar input[type="search"]{padding:6px 10px;font-size:1rem;max-width:280px;
background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:6px}
.chip{padding:4px 10px;border-radius:999px;border:1px solid var(--line);cursor:pointer;
background:var(--card);color:var(--fg);font-size:.85rem}
.chip[aria-pressed="true"]{background:var(--accent);color:#fff;border-color:var(--accent)}
.flowtoggle{padding:6px 12px;border:1px solid var(--line);border-radius:6px;cursor:pointer;
background:var(--card);color:var(--fg)}
.flowtoggle[aria-pressed="true"]{background:var(--accent);color:#fff;border-color:var(--accent)}
.viewbtn{padding:6px 12px;border:1px solid var(--line);border-radius:6px;cursor:pointer;
background:var(--card);color:var(--fg)}
.viewbtn[aria-pressed="true"]{background:var(--fg);color:var(--bg)}
#map{position:relative;padding:12px 20px 24px}
.band{background:var(--band);border:1px solid var(--line);border-radius:10px;
margin:12px 0;padding:10px 12px}
.band h2{font-size:1.05rem;margin:0 0 8px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px}
.card{background:var(--card);border:2px solid var(--line);border-radius:8px;padding:8px 10px;
cursor:pointer}
.card h3{font-size:.95rem;margin:0 0 2px;overflow-wrap:anywhere}
.card .home{font-size:.8rem;color:var(--muted)}
.card.dim{opacity:.25}
.card.hl{border-color:var(--accent)}
.dot{display:inline-block;width:.7em;height:.7em;border-radius:50%;margin-right:6px;
border:1px solid var(--line)}
.dot.live{background:var(--live)}.dot.partial{background:var(--partial)}
.dot.planned{background:var(--planned)}.dot.retired{background:var(--documented)}
#edges{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}
#edges #paths path{fill:none;stroke-width:1.8;opacity:.18;transition:opacity .15s,stroke-width .15s}
#map.show-all #edges #paths path{opacity:.8}
.legend{color:var(--muted);font-size:.85rem;margin:4px 0 0}
#edges #paths path.edge-live{stroke:var(--edge-live)}
#edges #paths path.edge-partial{stroke:var(--edge-partial)}
#edges #paths path.edge-documented,#edges #paths path.edge-planned{stroke:var(--edge-slate);stroke-dasharray:6 4}
#edges #paths path.edge-absent{stroke:var(--edge-absent);stroke-dasharray:6 4}
#edges #paths path.dim,#map.show-all #edges #paths path.dim{opacity:.04}
#edges #paths path.hl,#map.show-all #edges #paths path.hl{opacity:1;stroke-width:3.2}
#tip{position:fixed;display:none;max-width:280px;background:var(--fg);color:var(--bg);
padding:8px 10px;border-radius:6px;font-size:.8rem;z-index:40}
#panel{position:fixed;top:0;right:0;width:min(420px,100%);height:100%;overflow:auto;
background:var(--card);color:var(--fg);border-left:2px solid var(--line);padding:16px 18px;
z-index:30}
#panel[hidden]{display:none}
#panel a{color:var(--accent)}
.gapgroup{margin:12px 20px}
.gapgroup h3{text-transform:capitalize}
table{border-collapse:collapse;margin:12px 20px;max-width:calc(100% - 40px)}
th,td{border:1px solid var(--line);padding:6px 10px;text-align:left;font-size:.9rem}
a{color:var(--accent)}
:focus-visible{outline:3px solid var(--focus);outline-offset:2px}
@media (prefers-reduced-motion: reduce){*{transition:none !important;animation:none !important}}
@media (max-width: 700px){
#edges{display:none}
.cards{grid-template-columns:1fr}
#panel{left:0;right:0;top:auto;bottom:0;width:100%;max-height:80%;
border-left:none;border-top:2px solid var(--line)}
}"""

JS_TEXT = """(function(){
"use strict";
var dataEl=document.getElementById("atlas-data");
var data=JSON.parse(dataEl.textContent);
var byId={};
data.components.forEach(function(c){byId[c.id]=c;});
var flows=data.flows;
var cards=Array.prototype.slice.call(document.querySelectorAll(".card"));
var edgeSvg=document.getElementById("edges");
var edgePaths=document.getElementById("paths");
var tip=document.getElementById("tip");
var panel=document.getElementById("panel");
var lastCard=null;
var searchMatches=null;
function adjOf(id){
var s={};
flows.forEach(function(f){
if(f.from===id){s[f.to]=1;}
if(f.to===id){s[f.from]=1;}
});
return s;
}
function attr(s){
return String(s).replace(/&/g,"&amp;").replace(/"/g,"&quot;").replace(/</g,"&lt;");
}
function paintEdges(){
var map=document.getElementById("map");
var r0=map.getBoundingClientRect();
var out="";
flows.forEach(function(f){
var a=document.querySelector('.card[data-id="'+CSS.escape(f.from)+'"]');
var b=document.querySelector('.card[data-id="'+CSS.escape(f.to)+'"]');
if(!a||!b){return;}
var ra=a.getBoundingClientRect(),rb=b.getBoundingClientRect();
var x1=ra.left+ra.width/2-r0.left;
var x2=rb.left+rb.width/2-r0.left;
var same=a.closest(".band")===b.closest(".band");
var d;
if(same){
// within a band: an arc above both cards
var y1=ra.top-r0.top-4,p2=rb.top-r0.top-4;
var lift=Math.max(24,Math.abs(x2-x1)/3);
d="M"+x1+","+y1+" C"+x1+","+(y1-lift)+" "+x2+","+(p2-lift)+" "+x2+","+p2;
}else{
// across bands: leave the facing edge of the source, enter the facing edge of the target
var down=rb.top>ra.top;
var sy=(down?ra.bottom:ra.top)-r0.top;
var ty=(down?rb.top:rb.bottom)-r0.top;
var bend=(ty-sy)/2;
d="M"+x1+","+sy+" C"+x1+","+(sy+bend)+" "+x2+","+(ty-bend)+" "+x2+","+ty;
}
var label=(f.contract||f.id)+" | "+f.trigger+(f.gap?" | "+f.gap:"");
out+='<path d="'+attr(d)+'" class="edge-'+attr(f.status)+'" data-from="'+
attr(f.from)+'" data-to="'+attr(f.to)+'" data-id="'+attr(f.id)+
'" marker-end="url(#arr-'+attr(f.status)+')"><title>'+attr(label)+
"</title></path>";
});
edgePaths.innerHTML=out;
Array.prototype.forEach.call(edgePaths.querySelectorAll("path"),function(p){
for(var s in hiddenStatus){
if(hiddenStatus[s]&&p.getAttribute("class").indexOf("edge-"+s)>=0){p.style.display="none";}
}
});
Array.prototype.forEach.call(edgePaths.querySelectorAll("path"),function(p){
p.style.pointerEvents="stroke";
p.addEventListener("mouseenter",function(ev){
var t=p.querySelector("title");
tip.textContent=t?t.textContent:"";
tip.style.display="block";
tip.style.left=(ev.clientX+12)+"px";
tip.style.top=(ev.clientY+12)+"px";
});
p.addEventListener("mouseleave",function(){tip.style.display="none";});
});
}
function resetDim(){
cards.forEach(function(c){c.classList.remove("dim");c.classList.remove("hl");});
Array.prototype.forEach.call(edgePaths.querySelectorAll("path"),function(p){
p.classList.remove("dim");p.classList.remove("hl");});
if(searchMatches){
cards.forEach(function(c){
if(searchMatches.indexOf(c.getAttribute("data-id"))<0){c.classList.add("dim");}
});
}
}
function clearDim(){
resetDim();
// while a component's panel is open its flows stay lit
if(!panel.hidden&&lastCard){focusCard(lastCard);}
}
function focusCard(card){
resetDim();
var adj=adjOf(card.getAttribute("data-id"));
adj[card.getAttribute("data-id")]=1;
cards.forEach(function(c){
if(!adj[c.getAttribute("data-id")]){c.classList.add("dim");}
else{c.classList.add("hl");}
});
Array.prototype.forEach.call(edgePaths.querySelectorAll("path"),function(p){
var hit=(p.getAttribute("data-from")===card.getAttribute("data-id")||
p.getAttribute("data-to")===card.getAttribute("data-id"));
if(hit){p.classList.add("hl");}else{p.classList.add("dim");}
});
}
cards.forEach(function(card){
card.addEventListener("mouseenter",function(){focusCard(card);});
card.addEventListener("focus",function(){focusCard(card);});
card.addEventListener("mouseleave",clearDim);
card.addEventListener("blur",clearDim);
card.addEventListener("click",function(){openPanel(card);});
card.addEventListener("keydown",function(ev){
if(ev.key==="Enter"||ev.key===" "){ev.preventDefault();openPanel(card);}
if(ev.key==="ArrowRight"||ev.key==="ArrowLeft"){
ev.preventDefault();
var sibs=Array.prototype.slice.call(
card.closest(".cards").querySelectorAll(".card"));
var i=sibs.indexOf(card);
var n=ev.key==="ArrowRight"?sibs[i+1]:sibs[i-1];
if(n){n.focus();}
}
});
});
function esc(s){
return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;")
.replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}
function linkFor(r){
if(r.href){
var label=r.path+(r.anchor?"#"+r.anchor:"")+(r.note?" ("+r.note+")":"");
return '<a href="'+esc(r.href)+'" rel="noopener">'+esc(label)+"</a>";
}
return esc(r.repo+" "+r.path);
}
function openPanel(card){
lastCard=card;
var c=byId[card.getAttribute("data-id")];
var produced=data.contracts.filter(function(k){return k.producer===c.id;});
var consumed=data.contracts.filter(function(k){
return k.consumers.indexOf(c.id)>=0;});
var fin=flows.filter(function(f){return f.to===c.id;});
var fout=flows.filter(function(f){return f.from===c.id;});
var h="<h2>"+esc(c.title)+"</h2>";
h+="<p>"+esc(c.summary)+"</p>";
h+="<p>Status: "+esc(c.status)+" | Home: "+esc(c.home)+"</p>";
h+="<h3>Surfaces</h3><ul>";
c.surfaces.forEach(function(s){
var ev=s.evidence.map(linkFor).join(", ");
h+="<li>"+esc(s.kind)+": "+esc(s.name)+(ev?" ("+ev+")":"")+"</li>";
});
h+="</ul>";
h+="<h3>Owns</h3><p>"+(c.owns.length?esc(c.owns.join(", ")):"none")+"</p>";
if(c.instances&&c.instances.length){
h+="<h3>Runs as</h3><ul>";
c.instances.forEach(function(inst){h+="<li><code>"+esc(inst)+"</code></li>";});
h+="</ul>";
}
h+="<h3>Produces</h3><p>"+(produced.length?
esc(produced.map(function(k){return k.id;}).join(", ")):"none")+"</p>";
h+="<h3>Consumes</h3><p>"+(consumed.length?
esc(consumed.map(function(k){return k.id;}).join(", ")):"none")+"</p>";
h+="<h3>Flows in</h3><ul>";
fin.forEach(function(f){
h+="<li>"+esc(f.id)+" ("+esc(f.status)+") from "+esc(f.from)+
(f.gap?" gap: "+esc(f.gap):"")+"</li>";});
h+="</ul><h3>Flows out</h3><ul>";
fout.forEach(function(f){
h+="<li>"+esc(f.id)+" ("+esc(f.status)+") to "+esc(f.to)+
(f.gap?" gap: "+esc(f.gap):"")+"</li>";});
h+="</ul><h3>Evidence</h3><ul>";
c.evidence.forEach(function(r){h+="<li>"+linkFor(r)+"</li>";});
h+="</ul>";
h+='<button id="closep">Close (Esc)</button>';
panel.innerHTML=h;
panel.hidden=false;
var btn=document.getElementById("closep");
if(btn){btn.addEventListener("click",closePanel);btn.focus();}
}
function closePanel(){
panel.hidden=true;
panel.innerHTML="";
if(lastCard){lastCard.focus();}
}
document.addEventListener("keydown",function(ev){
if(ev.key==="Escape"&&!panel.hidden){closePanel();}
var tag=(document.activeElement&&document.activeElement.tagName)||"";
if(ev.key==="/"&&tag!=="INPUT"&&tag!=="TEXTAREA"){
ev.preventDefault();
var q=document.getElementById("q");
if(q){q.focus();}
}
});
var q=document.getElementById("q");
if(q){q.addEventListener("input",function(){
var needle=q.value.toLowerCase();
if(!needle){searchMatches=null;}
else{searchMatches=[];}
cards.forEach(function(card){
var c=byId[card.getAttribute("data-id")];
var hay=(c.id+" "+c.title+" "+c.summary).toLowerCase();
if(!needle||hay.indexOf(needle)>=0){card.classList.remove("dim");
if(searchMatches){searchMatches.push(card.getAttribute("data-id"));}}
else{card.classList.add("dim");}
});
});}
var activeViews={map:1};
document.querySelectorAll(".viewbtn").forEach(function(b){
b.addEventListener("click",function(){
var v=b.getAttribute("data-view");
if(!panel.hidden){panel.hidden=true;panel.innerHTML="";lastCard=null;resetDim();}
["map","gaps","contracts","flows","vocabularies"].forEach(function(name){
var el=document.getElementById("view-"+name);
if(el){el.hidden=(name!==v);}
});
document.querySelectorAll(".viewbtn").forEach(function(o){
o.setAttribute("aria-pressed",o===b?"true":"false");});
});
});
var hiddenStatus={};
document.querySelectorAll(".chip").forEach(function(ch){
ch.addEventListener("click",function(){
var s=ch.getAttribute("data-status");
var on=ch.getAttribute("aria-pressed")==="true";
ch.setAttribute("aria-pressed",on?"false":"true");
hiddenStatus[s]=on?false:true;
Array.prototype.forEach.call(edgePaths.querySelectorAll("path"),function(p){
if(p.getAttribute("class").indexOf("edge-"+s)>=0){
p.style.display=hiddenStatus[s]?"none":"";
}
});
});
});
window.addEventListener("resize",paintEdges);
var allFlows=document.getElementById("allflows");
if(allFlows){allFlows.addEventListener("click",function(){
var on=allFlows.getAttribute("aria-pressed")!=="true";
allFlows.setAttribute("aria-pressed",on?"true":"false");
document.getElementById("map").classList.toggle("show-all",on);
});}
paintEdges();
setTimeout(paintEdges,50);
})();"""


def _link(url: str, label: str) -> str:
    return '<a href="%s" rel="noopener">%s</a>' % (html.escape(url, quote=True), html.escape(label))


def _ref_label(ref: dict[str, Any]) -> str:
    label = ref["path"]
    if ref.get("anchor"):
        label += "#" + ref["anchor"]
    if ref.get("note"):
        label += " (" + ref["note"] + ")"
    return label


def _vocab_overall_status(results: list[dict[str, Any]]) -> str | None:
    if not results:
        return None
    if any(r.get("status") == "drift" for r in results):
        return "drift"
    if any(r.get("status") == "unresolved" for r in results):
        return "unresolved"
    return "ok"


def _traffic_rule_line(rule: dict[str, Any]) -> str:
    verb = rule.get("verb", "*")
    if isinstance(verb, list):
        verb_text = ", ".join(str(v) for v in verb)
    else:
        verb_text = str(verb)
    line = "%s · %s · %s · %s → %s" % (
        html.escape(str(rule.get("source", "*"))),
        html.escape(str(rule.get("producer", "*"))),
        html.escape(verb_text),
        html.escape(str(rule.get("actor", "*"))),
        html.escape(str(rule.get("subject", "*"))),
    )
    if rule.get("pulse") is True:
        line += " (pulse)"
    return line


def _expect_links(refs: list[dict[str, Any]], resolved: Any) -> str:
    parts: list[str] = []
    for ref in refs:
        item = resolved(ref)
        href = item.get("href")
        label = _ref_label(ref)
        if href is not None:
            parts.append(_link(str(href), label))
        else:
            parts.append(html.escape("%s/%s" % (ref.get("repo", ""), label)))
    return ", ".join(parts)


def render_html(atlas: dict[str, Any],
                check: dict[str, Any] | None = None) -> str:
    atlas_repos = atlas["repos"]
    ordered = order_cards(atlas)
    comp_by_id = {c["id"]: c for c in atlas["components"]}
    effective = check if check is not None else atlas.get("check")

    def resolved(ref: dict[str, Any]) -> dict[str, Any]:
        out = dict(ref)
        out["href"] = ref_href(ref, atlas_repos)
        return out

    flow_count: dict[str, int] = {s: 0 for s in FLOW_STATUSES}
    for flow in atlas["flows"]:
        flow_count[flow["status"]] += 1

    pills = "".join(
        '<span class="pill %s">%s: %d</span>' % (s, s, flow_count[s]) for s in FLOW_STATUSES
    )
    check_html = ""
    promotable_owners: set[str] = set()
    vocab_results: list[dict[str, Any]] = []
    if effective is not None:
        bad = bool(effective["missing"]) or bool(effective["unresolved"]) or effective["ok"] < effective["checked"]
        cls = "check bad" if bad else "check"
        promotable_owners = {str(p["owner"]) for p in effective.get("promotable", [])}
        vocab_results = list(effective.get("vocabularies", []))
        suffix = ""
        if promotable_owners:
            suffix += ", %d promotable" % (len(promotable_owners),)
        vocab_status = _vocab_overall_status(vocab_results)
        if vocab_status is not None:
            suffix += ", vocabularies: %s" % (vocab_status,)
        summary = effective.get("summary", {})
        if isinstance(summary, dict) and "traffic_overlaps" in summary:
            suffix += ", traffic_overlaps: %d" % (summary["traffic_overlaps"],)
        elif "traffic" in effective:
            suffix += ", traffic_overlaps: %d" % (len(effective["traffic"]),)
        check_html = (
            '<p class="%s">Checked against origin/main on %s: %d/%d references present'
            " (missing: %d, unresolved: %d%s)</p>"
            % (cls, html.escape(effective["host"]), effective["ok"], effective["checked"],
               len(effective["missing"]), len(effective["unresolved"]), suffix)
        )

    bands: list[str] = []
    for layer in atlas["layers"]:
        cards = []
        for cid in ordered[layer["id"]]:
            comp = comp_by_id[cid]
            home = comp["home"]
            display_home = home[len("external:"):] if home.startswith("external:") else home
            label = "%s, %s, %s" % (comp["title"], display_home, comp["status"])
            cards.append(
                '<article class="card" tabindex="0" role="button" data-id="%s" '
                'aria-label="%s"><h3><span class="dot %s" aria-hidden="true"></span>%s</h3>'
                '<div class="home">%s</div></article>'
                % (html.escape(cid, quote=True), html.escape(label, quote=True),
                   comp["status"], html.escape(comp["title"]), html.escape(display_home))
            )
        bands.append(
            '<section class="band" data-layer="%s"><h2 title="%s">%s</h2>'
            '<div class="cards">%s</div></section>'
            % (html.escape(layer["id"], quote=True), html.escape(layer["summary"], quote=True),
               html.escape(layer["title"]), "".join(cards))
        )

    def _promotable_pill(owner: str) -> str:
        if owner in promotable_owners:
            return ' <span class="pill planned">built: update the atlas</span>'
        return ""

    def _expect_line(owner: str, refs: list[dict[str, Any]]) -> str:
        if not refs:
            return ""
        return " Expected evidence: %s.%s" % (_expect_links(refs, resolved), _promotable_pill(owner))

    gaps: list[str] = []
    for status in GAP_ORDER:
        items = sorted((f for f in atlas["flows"] if f["status"] == status),
                       key=lambda f: f["id"])
        if not items:
            continue
        rows = []
        for flow in items:
            label = flow["contract"] if flow["contract"] else flow["id"]
            owner = "flows." + flow["id"]
            rows.append(
                "<li><strong>%s</strong> %s to %s: %s%s</li>"
                % (html.escape(label), html.escape(flow["from"]),
                   html.escape(flow["to"]), html.escape(flow["gap"]),
                   _expect_line(owner, flow.get("expect", [])) + (_promotable_pill(owner) if not flow.get("expect", []) else ""))
            )
        gaps.append('<div class="gapgroup"><h3>%s</h3><ul>%s</ul></div>' % (status, "".join(rows)))
    planned_comps = sorted((c for c in atlas["components"] if c["status"] == "planned" and c.get("expect")),
                           key=lambda c: c["id"])
    if planned_comps:
        comp_rows = []
        for comp in planned_comps:
            owner = "components." + comp["id"]
            comp_rows.append(
                "<li><strong>%s</strong> (%s): %s%s</li>"
                % (html.escape(comp["title"]), html.escape(comp["id"]),
                   html.escape(comp["summary"]),
                   _expect_line(owner, comp.get("expect", [])) + (_promotable_pill(owner) if not comp.get("expect", []) else ""))
            )
        gaps.append('<div class="gapgroup"><h3>planned components</h3><ul>%s</ul></div>' % ("".join(comp_rows),))
    gaps_html = "".join(gaps) if gaps else "<p>No gaps: every flow is live.</p>"

    vocabularies = list(atlas.get("vocabularies", []))
    vocab_by_check = {v.get("id"): v for v in vocab_results if isinstance(v, dict)}
    vocab_sections: list[str] = []
    for vocab in vocabularies:
        owner_comp = comp_by_id.get(vocab["owner"], {})
        owner_title = str(owner_comp.get("title", vocab["owner"]))
        src = vocab["source"]
        src_item = resolved(src)
        src_label = "%s/%s" % (src.get("repo", ""), src.get("path", ""))
        if src_item.get("href") is not None:
            src_html = _link(str(src_item["href"]), src_label)
        else:
            src_html = html.escape(src_label)
        term_rows = "".join(
            "<tr><td>%s</td><td>%s</td></tr>" % (html.escape(t["term"]), html.escape(t["means"]))
            for t in vocab["terms"]
        )
        result = vocab_by_check.get(vocab["id"])
        pill_html = ""
        drift_html = ""
        if result is not None:
            status = str(result.get("status"))
            pill_html = ' <span class="pill %s">%s</span>' % (html.escape(status), html.escape(status))
            if status == "drift":
                missing_atlas = list(result.get("missing_in_atlas", []))
                missing_source = list(result.get("missing_in_source", []))
                if missing_atlas:
                    drift_html += "<p>Missing in atlas: %s</p>" % (html.escape(", ".join(missing_atlas)),)
                if missing_source:
                    drift_html += "<p>Missing in source: %s</p>" % (html.escape(", ".join(missing_source)),)
                why = result.get("why")
                if why and not missing_atlas and not missing_source:
                    drift_html += "<p>%s</p>" % (html.escape(str(why)),)
        vocab_sections.append(
            '<section class="vocab"><h3>%s%s</h3><p>Owner: %s | Source: %s</p>'
            '<table><thead><tr><th>Term</th><th>Meaning</th></tr></thead>'
            '<tbody>%s</tbody></table>%s</section>'
            % (html.escape(vocab["title"]), pill_html,
               html.escape(owner_title), src_html, term_rows, drift_html)
        )
    vocab_html = "".join(vocab_sections)

    contract_rows = []
    for contract in sorted(atlas["contracts"], key=lambda k: k["id"]):
        ev = ", ".join(
            _link(h, _ref_label(r)) for r in (resolved(x) for x in contract["evidence"])
            if (h := r["href"]) is not None
        ) or "none"
        consumers = ", ".join(sorted(contract["consumers"]))
        owner = "contracts." + contract["id"]
        expect_refs = contract.get("expect", [])
        ev += _expect_line(owner, expect_refs) + (_promotable_pill(owner) if not expect_refs else "")
        contract_rows.append(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (html.escape(contract["id"]), html.escape(contract["format"]),
               html.escape(contract["status"]), html.escape(contract["producer"]),
               html.escape(consumers), ev)
        )
    contracts_html = (
        "<table><thead><tr><th>Contract</th><th>Format</th><th>Status</th>"
        "<th>Producer</th><th>Consumers</th><th>Evidence</th></tr></thead>"
        "<tbody>%s</tbody></table>" % "".join(contract_rows)
    )

    flow_rows = []
    for flow in sorted(atlas["flows"], key=lambda f: f["id"]):
        if not flow.get("traffic"):
            continue
        label = flow["contract"] if flow["contract"] else flow["id"]
        detail = ("<li><strong>%s</strong> %s → %s (%s): %s"
                  "<br>Traffic rules:<ul>%s</ul></li>"
                  % (html.escape(label), html.escape(flow["from"]),
                     html.escape(flow["to"]), html.escape(flow["status"]),
                     html.escape(flow["trigger"]),
                     "".join("<li>%s</li>" % (_traffic_rule_line(rule),)
                             for rule in flow["traffic"])))
        flow_rows.append(detail)
    flows_html = "<ul>%s</ul>" % ("".join(flow_rows),)

    chips = "".join(
        '<button class="chip" data-status="%s" aria-pressed="false">%s</button>'
        % (s, s) for s in FLOW_STATUSES
    )

    embedded = {
        "updated": atlas["updated"],
        "layers": atlas["layers"],
        "components": [
            {
                "id": c["id"], "title": c["title"], "layer": c["layer"], "home": c["home"],
                "status": c["status"], "summary": c["summary"], "owns": sorted(c["owns"]),
                "instances": list(c.get("instances", [])),
                "surfaces": [
                    {"kind": s["kind"], "name": s["name"],
                     "evidence": [resolved(r) for r in s["evidence"]]}
                    for s in sorted(c["surfaces"], key=lambda s: (s["kind"], s["name"]))
                ],
                "evidence": [resolved(r) for r in c["evidence"]],
            }
            for c in sorted(atlas["components"], key=lambda c: c["id"])
        ],
        "contracts": [
            {"id": k["id"], "producer": k["producer"],
             "consumers": sorted(k["consumers"]), "format": k["format"]}
            for k in sorted(atlas["contracts"], key=lambda k: k["id"])
        ],
        "flows": [
            {"id": f["id"], "from": f["from"], "to": f["to"],
             "contract": f["contract"], "trigger": f["trigger"],
             "status": f["status"], "gap": f["gap"]}
            for f in sorted(atlas["flows"], key=lambda f: f["id"])
        ],
    }
    if effective is not None:
        embedded["check"] = {"host": effective["host"], "checked": effective["checked"],
                             "ok": effective["ok"]}
    json_text = json.dumps(embedded, sort_keys=True, ensure_ascii=False)
    json_text = json_text.replace("<", "\\u003c")

    markers = "".join(
        '<marker id="arr-%s" viewBox="0 0 10 10" refX="8" refY="5" '
        'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        '<path d="M0,0 L10,5 L0,10 z" class="m-%s"></path></marker>' % (s, s)
        for s in FLOW_STATUSES
    )

    vocab_btn = ('<button class="viewbtn" data-view="vocabularies" aria-pressed="false">Vocabularies</button>'
                 if vocabularies else "")
    vocab_main = ('<main id="view-vocabularies" hidden><h2 style="margin:12px 20px">Vocabularies</h2>'
                  + vocab_html + "</main>") if vocabularies else ""
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>Estate Atlas</title>",
        "<style>" + CSS_TEXT + "</style>",
        "</head>",
        "<body>",
        "<header><h1>Estate Atlas</h1>",
        '<p class="meta">Updated %s | %d components | %d contracts | %d flows</p>'
        % (html.escape(atlas["updated"]), len(atlas["components"]),
           len(atlas["contracts"]), len(atlas["flows"])),
        '<div class="pills">' + pills + "</div>",
        '<p class="legend">Dots are component status. Lines are flows: solid when live or partial, dashed when '
        "documented, planned or absent. Hover or select a component to light its flows; open Gaps for what "
        "is missing.</p>",
        check_html,
        "</header>",
        '<div class="toolbar" role="toolbar" aria-label="Atlas controls">',
        '<label for="q">Search</label>',
        '<input type="search" id="q" aria-label="Search components" placeholder="Search ( / )">',
        chips,
        '<button class="flowtoggle" id="allflows" aria-pressed="false">All flows</button>',
        '<button class="viewbtn" data-view="map" aria-pressed="true">Map</button>',
        '<button class="viewbtn" data-view="gaps" aria-pressed="false">Gaps</button>',
        '<button class="viewbtn" data-view="contracts" aria-pressed="false">Contracts</button>',
        '<button class="viewbtn" data-view="flows" aria-pressed="false">Flows</button>',
        vocab_btn,
        "</div>",
        '<main id="view-map"><div id="map">',
        '<svg id="edges" aria-hidden="true"><defs>' + markers + '</defs><g id="paths"></g></svg>',
        "".join(bands),
        "</div></main>",
        '<main id="view-gaps" hidden><h2 style="margin:12px 20px">Gaps</h2>' + gaps_html + "</main>",
        '<main id="view-contracts" hidden><h2 style="margin:12px 20px">Contracts</h2>'
        + contracts_html + "</main>",
        '<main id="view-flows" hidden><h2 style="margin:12px 20px">Flows</h2>'
        + flows_html + "</main>",
        vocab_main,
        '<div id="tip" role="status"></div>',
        '<aside id="panel" role="dialog" aria-label="Component detail" hidden></aside>',
        '<script type="application/json" id="atlas-data">' + json_text + "</script>",
        "<script>" + JS_TEXT + "</script>",
        "</body>",
        "</html>",
        "",
    ]
    return "\n".join(parts)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, temp = tempfile.mkstemp(dir=str(path.parent),
                                       prefix="." + path.name + ".", suffix=".tmp")
    try:
        with open(handle_fd, "wb") as stream:
            stream.write(text.encode("utf-8"))
        Path(temp).replace(path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise




