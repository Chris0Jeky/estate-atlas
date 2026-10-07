#!/usr/bin/env python3
"""Evidence checks for an estate-atlas document.

``check`` verifies every evidence reference against each repository's
``origin/<default branch>`` in the local checkout (the file exists at that
commit and contains the reference's anchor text), and every proof block's
claimed level against its receipts (each level up to the claim has a passed
receipt, and every receipt revision is a commit on that branch). It reads
through ``git show`` and related commands, never fetches, and never changes a
checkout. ``export`` adds hosted links on every evidence, vocabulary source,
expect reference and receipt revision.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
import re
from urllib.parse import urlsplit
from typing import Any

from .model import (AtlasError, REMOTE_PATTERN, instance_index, match_instance, proof_entries, proof_ladder,
                    receipted_level)

CHECK_SCHEMA = "estate-atlas-check@3"
EXPORT_SCHEMA = "estate-atlas-export@3"
PROOF_STATUSES = ("ok", "over-claim", "unverified", "unresolved")
MAX_EVIDENCE_BYTES = 8 * 1024 * 1024

__all__ = [
    "AtlasError",
    "CHECK_SCHEMA",
    "EXPORT_SCHEMA",
    "check",
    "evidence_refs",
    "expect_refs",
    "export",
    "instance_index",
    "match_instance",
    "patterns_overlap",
    "repositories",
    "rules_overlap",
]

# --------------------------------------------------------------------------- git access (read-only)

_GIT_TIMEOUT_S = 30
_GIT_LOCAL_ENV = frozenset({
    "GIT_DIR", "GIT_COMMON_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE", "GIT_PREFIX",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_INDEX_FILE",
    "GIT_GRAFT_FILE", "GIT_SHALLOW_FILE", "GIT_REPLACE_REF_BASE", "GIT_NO_REPLACE_OBJECTS",
    "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
})


def _git(path: str | Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """Read the selected checkout, without inherited repository-local overrides."""
    env = {name: value for name, value in os.environ.items()
           if name.upper() not in _GIT_LOCAL_ENV
           and not name.upper().startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_", "GIT_TRACE"))}
    # Git initializes trace2 from owner config before command-line overrides. A partial clone would fetch a
    # missing object on demand: GIT_NO_LAZY_FETCH keeps every read local.
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TRACE2="0", GIT_TRACE2_EVENT="0", GIT_TRACE2_PERF="0",
               GIT_NO_LAZY_FETCH="1")
    return subprocess.run(["git", "-C", str(path), *args], env=env,
                          capture_output=True, timeout=_GIT_TIMEOUT_S)


def _parse_remote_id(url: str) -> str | None:
    """Resolve supported GitHub origins, never just a matching owner/name suffix."""
    text = url.strip()
    # An empty query or fragment parses as "" but is still a query/fragment URL: refuse the delimiters.
    if any(ord(char) < 33 or ord(char) == 127 for char in text) or "\\" in text or "?" in text or "#" in text:
        return None
    scp = re.fullmatch(r"git@github\.com:(.+)", text, re.IGNORECASE)
    if scp:
        path = scp.group(1)
    else:
        try:
            parsed = urlsplit(text)
            default_port = {"https": 443, "ssh": 22}.get(parsed.scheme)
            if (default_port is None or parsed.hostname != "github.com"
                    or parsed.port not in (None, default_port) or parsed.query or parsed.fragment):
                return None
            if parsed.scheme == "ssh" and (parsed.username != "git" or parsed.password is not None):
                return None
            path = parsed.path.removeprefix("/")
        except ValueError:
            return None
    path = path.removesuffix("/").removesuffix(".git")
    return path if REMOTE_PATTERN.fullmatch(path) else None


def checkout_identity(path: str | Path, expected_remote: str) -> tuple[str | None, str]:
    """Check that a checkout exists and its origin matches; read-only."""
    checkout = Path(path)
    if not checkout.is_dir():
        return None, f"checkout is not available at {path}"
    try:
        completed = _git(checkout, "remote", "get-url", "origin")
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"git failed: {type(exc).__name__}"
    if completed.returncode != 0:
        return None, f"origin has no remote URL at {path}"
    actual = _parse_remote_id(completed.stdout.decode("utf-8", "replace"))
    if actual is None or actual.lower() != expected_remote.lower():
        found = actual if actual is not None else "unsupported origin URL"
        return None, f"origin is not {expected_remote} (found {found})"
    return expected_remote, ""


def partial_clone(path: str | Path) -> bool:
    """True when the checkout is a partial clone (it has a promisor remote); read-only.

    Git fetches a missing object of a partial clone on demand. GIT_NO_LAZY_FETCH stops that only on Git versions
    that know it, so `check` does not read a partial clone at all: its repository stays unresolved. Any doubt
    (an unreadable configuration, a promisor value Git cannot read as a boolean) counts as a partial clone."""
    try:
        # --name-only prints keys alone: a remote name may hold a space, so a key cannot be cut at the first one.
        found = _git(path, "config", "--name-only", "--get-regexp",
                     r"^(extensions\.partialclone|remote\..*\.(promisor|partialclonefilter))$")
        if found.returncode not in (0, 1):  # 1: no such key
            return True
        keys = found.stdout.decode("utf-8", "replace").splitlines()
        # Git makes a promisor remote of extensions.partialClone and of any remote with a partial-clone filter.
        if any(not key.endswith(".promisor") for key in keys):
            return True
        if not keys:
            return False
        # Let Git read its own boolean spellings (a bare key, yes, on, 2, -1, 01 ...).
        booleans = _git(path, "config", "--bool", "--get-regexp", r"^remote\..*\.promisor$")
    except (OSError, subprocess.TimeoutExpired):
        return True
    if booleans.returncode != 0:
        return True
    # The normalized value is the last token; the key before it may itself contain spaces.
    return any(line.rpartition(" ")[2].strip() != "false"
               for line in booleans.stdout.decode("utf-8", "replace").splitlines())


def remote_head(path: str | Path, default_branch: str) -> str | None:
    """The commit of `origin/<default_branch>` in a local checkout, or None."""
    try:
        completed = _git(path, "rev-parse", "--verify", f"refs/remotes/origin/{default_branch}")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if completed.returncode != 0:
        return None
    head = completed.stdout.decode("utf-8", "replace").strip()
    return head or None


# --------------------------------------------------------------------------- traffic overlap


def patterns_overlap(a: str, b: str) -> bool:
    """True when some string matches both patterns (exact or prefix ending in `*`)."""
    if a == "*" or b == "*":
        return True
    a_prefix = a[:-1] if a.endswith("*") else None
    b_prefix = b[:-1] if b.endswith("*") else None
    if a_prefix is None and b_prefix is None:
        return a == b
    if a_prefix is not None and b_prefix is None:
        return b.startswith(a_prefix)
    if a_prefix is None and b_prefix is not None:
        return a.startswith(b_prefix)
    return a_prefix.startswith(b_prefix) or b_prefix.startswith(a_prefix)


def _rule_verbs(rule: dict[str, Any]) -> list[str]:
    value = rule.get("verb", "*")
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def rules_overlap(r1: dict, r2: dict) -> bool:
    """True when the two rules share a source and every field pair can match the same value.
    `pulse` is ignored: a pulse rule is still a rule for overlap purposes."""
    if r1.get("source") != r2.get("source"):
        return False
    for field in ("producer", "actor", "subject"):
        if field == "subject" and r1.get("source") == "events":
            continue  # an events subject matches ANY of the event's nodes: two subject patterns can both hold
        first = r1.get(field, "*")
        second = r2.get(field, "*")
        if not isinstance(first, str) or not isinstance(second, str):
            return False
        if not patterns_overlap(first, second):
            return False
    return any(patterns_overlap(first, second)
               for first in _rule_verbs(r1) for second in _rule_verbs(r2))


# Validation lives in .model (load/validate); check starts from a valid document.

# --------------------------------------------------------------------------- repositories and evidence

def _normalise_overrides(overrides: Any) -> dict[str, str]:
    """Accept a name->path mapping (or a list of `name=path` strings); else empty."""
    if not overrides:
        return {}
    if isinstance(overrides, dict):
        return {str(name): str(value) for name, value in overrides.items()}
    parsed: dict[str, str] = {}
    for entry in overrides:
        text = str(entry)
        if "=" in text:
            name, _, value = text.partition("=")
            parsed[name] = value
    return parsed


def repositories(
    doc: dict[str, Any],
    overrides: dict[str, str | Path] | list[str] | tuple[str, ...] | None = None,
    host: str = "",
    worktree: bool = False,
) -> dict[str, dict[str, Any]]:
    """Map each repo id in the atlas to {remote, default_branch, path or None, mode}.

    An override path wins, then `repos[name].paths[host]` (matched
    case-insensitively), otherwise the repo stays unresolved with path None.
    `worktree=True` reads evidence from the checkout's working files instead of
    `origin/<default branch>`: for an atlas checked beside the files it
    describes, before anything is pushed.
    """
    won = _normalise_overrides(overrides)
    out: dict[str, dict[str, Any]] = {}
    for key, extra in (doc.get("repos") or {}).items():
        if not isinstance(extra, dict):
            continue
        if key in won:
            path = won[key]
        else:
            paths = extra.get("paths") or {}
            path = None
            if isinstance(paths, dict):
                for name, value in paths.items():
                    if isinstance(name, str) and name.lower() == str(host).lower():
                        path = value
                        break
        out[key] = {
            "remote": extra.get("remote"),
            "default_branch": extra.get("default_branch"),
            "path": str(path) if path else None,
            "mode": "worktree" if worktree else "origin",
        }
    return out


def evidence_refs(doc: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """(owner label, REF) for every evidence reference, in document order (expect excluded)."""
    refs: list[tuple[str, dict[str, Any]]] = []
    for comp in doc["components"]:
        owner = f"components.{comp['id']}"
        for surface in comp["surfaces"]:
            refs.extend((owner, ref) for ref in surface["evidence"])
        refs.extend((owner, ref) for ref in comp["evidence"])
    for contract in doc["contracts"]:
        refs.extend((f"contracts.{contract['id']}", ref) for ref in contract["evidence"])
    for flow in doc["flows"]:
        refs.extend((f"flows.{flow['id']}", ref) for ref in flow["evidence"])
    return refs


def expect_refs(doc: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """(owner label, REF) for every expect reference, in document order."""
    refs: list[tuple[str, dict[str, Any]]] = []
    for comp in doc.get("components", []) or []:
        if isinstance(comp, dict) and comp.get("expect"):
            refs.extend((f"components.{comp['id']}", ref) for ref in comp["expect"])
    for contract in doc.get("contracts", []) or []:
        if isinstance(contract, dict) and contract.get("expect"):
            refs.extend((f"contracts.{contract['id']}", ref) for ref in contract["expect"])
    for flow in doc.get("flows", []) or []:
        if isinstance(flow, dict) and flow.get("expect"):
            refs.extend((f"flows.{flow['id']}", ref) for ref in flow["expect"])
    return refs


def _resolve(repo_id: str, repos: dict[str, dict[str, Any]], host: str,
             heads: dict[str, str], unresolved: dict[str, str]) -> str | None:
    if repo_id in heads or repo_id in unresolved:
        return heads.get(repo_id)
    repo = repos[repo_id]
    path = repo["path"]
    if not path:
        unresolved[repo_id] = f"no checkout on {host}"
    elif repo.get("mode") == "worktree":
        if Path(path).is_dir():
            heads[repo_id] = "worktree"
        else:
            unresolved[repo_id] = f"checkout is not available at {path}"
    else:
        identity, why = checkout_identity(path, repo["remote"])
        if not identity:
            unresolved[repo_id] = why
        elif partial_clone(path):
            unresolved[repo_id] = "checkout is a partial clone (a read could fetch a missing object)"
        else:
            head = remote_head(path, repo["default_branch"])
            if head is None:
                unresolved[repo_id] = f"origin/{repo['default_branch']} is not available locally (fetch first)"
            else:
                heads[repo_id] = head
    return heads.get(repo_id)


def _read(repo_id: str, rel: str, repos: dict[str, dict[str, Any]],
          heads: dict[str, str], blobs: dict[tuple[str, str], tuple[bytes | None, str]]
          ) -> tuple[bytes | None, str]:
    key = (repo_id, rel)
    if key not in blobs:
        repo = repos[repo_id]
        blobs[key] = (_worktree_blob(repo["path"], rel) if repo.get("mode") == "worktree"
                      else _blob(repo["path"], heads[repo_id], rel))
    return blobs[key]


def _select_values(parsed: Any, segments: list[str]) -> list[str]:
    current: list[Any] = [parsed]
    for segment in segments:
        nxt: list[Any] = []
        for node in current:
            if segment == "*":
                if isinstance(node, list):
                    nxt.extend(node)
                elif isinstance(node, dict):
                    nxt.extend(node.values())
            elif isinstance(node, dict):
                if segment in node:
                    nxt.append(node[segment])
            elif isinstance(node, list) and segment.isdigit():
                try:
                    index = int(segment)
                except ValueError:
                    continue
                if 0 <= index < len(node):
                    nxt.append(node[index])
        current = nxt
    return sorted({value for value in current if isinstance(value, str)})


def _ref_resolves(ref: dict[str, Any], repos: dict[str, dict[str, Any]], host: str,
                  heads: dict[str, str], unresolved: dict[str, str],
                  blobs: dict[tuple[str, str], tuple[bytes | None, str]],
                  counts: dict[str, int] | None = None) -> bool:
    repo_id = ref["repo"]
    _resolve(repo_id, repos, host, heads, unresolved)
    if repo_id in unresolved:
        if counts is not None:
            counts[repo_id] = counts.get(repo_id, 0) + 1
        return False
    data, _ = _read(repo_id, ref["path"], repos, heads, blobs)
    if data is None:
        return False
    anchor = ref.get("anchor")
    if anchor is not None and anchor not in data.decode("utf-8", "replace"):
        return False
    return True


def _worktree_blob(path: str, relative: str) -> tuple[bytes | None, str]:
    """The working file's bytes, or (None, why); never outside the checkout."""
    root = Path(path).resolve()
    target = (root / relative).resolve()
    if root != target and root not in target.parents:
        return None, "path leaves the checkout"
    if not target.is_file():
        return None, "file is absent in the working tree"
    if target.stat().st_size > MAX_EVIDENCE_BYTES:
        return None, f"file is larger than {MAX_EVIDENCE_BYTES // (1024 * 1024)} MiB"
    try:
        return target.read_bytes(), ""
    except OSError as exc:
        return None, f"cannot read: {exc.strerror or type(exc).__name__}"


def _blob(path: str, commit: str, relative: str) -> tuple[bytes | None, str]:
    """The file's bytes at `commit`, or (None, why). Existence comes from ls-tree, never error text."""
    try:
        # -z: names come back verbatim, not C-quoted, so non-ASCII paths compare exactly
        listed = _git(path, "ls-tree", "-z", "--name-only", commit, "--", relative)
        if listed.returncode != 0:
            return None, "git ls-tree failed"
        if listed.stdout.split(bytes([0]))[0].decode("utf-8", "replace") != relative:
            return None, "file is absent at origin"
        size = _git(path, "cat-file", "-s", f"{commit}:{relative}")
        if size.returncode != 0:
            return None, "not a file at origin"
        if int(size.stdout.strip() or b"0") > MAX_EVIDENCE_BYTES:
            return None, f"file is larger than {MAX_EVIDENCE_BYTES // (1024 * 1024)} MiB"
        shown = _git(path, "cat-file", "blob", f"{commit}:{relative}")
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        return None, f"git failed: {type(exc).__name__}"
    if shown.returncode != 0:
        return None, "git cat-file failed"
    return shown.stdout, ""


def _commit_state(repo: dict[str, Any], head: str, commit: str) -> tuple[str, str]:
    """("ok", "") when `commit` is a commit on origin/<default branch> (in worktree mode: in the repository),
    ("unresolved", why) when a worktree checkout is not a git repository, else ("missing", why). Read-only."""
    path = repo["path"]
    try:
        if repo.get("mode") == "worktree":
            if _git(path, "rev-parse", "--git-dir").returncode != 0:
                return "unresolved", "checkout is not a git repository"
            if partial_clone(path):
                return "unresolved", "checkout is a partial clone (a read could fetch a missing object)"
        # The exact id must come back: Git also resolves a longer hex string (a SHA-1 id padded to 64 digits) or a
        # ref that happens to look like one to some other object.
        resolved = _git(path, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{commit}^{{commit}}")
        if resolved.returncode != 0 or resolved.stdout.decode("ascii", "replace").strip() != commit:
            return "missing", "commit is not in the repository"
        if repo.get("mode") == "worktree":
            return "ok", ""
        ancestor = _git(path, "merge-base", "--is-ancestor", commit, head)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "missing", f"git failed: {type(exc).__name__}"
    if ancestor.returncode == 0:
        return "ok", ""
    if ancestor.returncode == 1:
        return "missing", f"commit is not on origin/{repo['default_branch']}"
    return "missing", "git merge-base failed"


def _proof(owner: str, block: dict[str, Any], ladder: tuple[str, ...], revision: Any) -> dict[str, Any]:
    """One proof result. `revision(repo, commit)` returns ("ok" | "unresolved" | "missing", why)."""
    receipts = list(block["receipts"])
    states: dict[str, str] = {}
    problems: list[dict[str, Any]] = []
    for receipt in receipts:
        worst = "ok"
        for rev in receipt["revisions"]:
            state, why = revision(rev["repo"], rev["commit"])
            if state == "missing":
                worst = "missing"
                problems.append({"receipt": receipt["id"], "repo": rev["repo"], "commit": rev["commit"], "why": why})
            elif state == "unresolved" and worst == "ok":
                worst = "unresolved"
        states[receipt["id"]] = worst
    position = {level: index for index, level in enumerate(ladder)}
    claimed = position[block["claim"]]
    top = max([claimed] + [position[r["level"]] for r in receipts])
    levels: list[dict[str, Any]] = []
    for index in range(top + 1):
        at = [r for r in receipts if r["level"] == ladder[index]]
        passed = [states[r["id"]] for r in at if r["outcome"] == "passed"]
        if "ok" in passed:
            state = "proven"
        elif "unresolved" in passed:
            state = "unresolved"
        elif passed:
            state = "unverified"
        elif at:
            state = "not-passed"
        else:
            state = "none"
        levels.append({"level": ladder[index], "state": state, "receipts": len(at)})
    proven = -1
    for item in levels:
        if item["state"] != "proven":
            break
        proven += 1
    receipted = receipted_level(block, ladder)
    if problems:
        status = "unverified"
    elif receipted < claimed:
        status = "over-claim"
    elif proven < claimed:
        status = "unresolved"
    else:
        status = "ok"
    problems.sort(key=lambda item: (item["receipt"], item["repo"], item["commit"]))
    return {"owner": owner, "claimed": block["claim"],
            "receipted": ladder[receipted] if receipted >= 0 else None,
            "proven": ladder[proven] if proven >= 0 else None,
            "status": status, "levels": levels, "revisions": problems}


def check(doc: dict[str, Any], repos: dict[str, dict[str, Any]], host: str) -> dict[str, Any]:
    """Verify every evidence reference at each repository's origin/<default branch>; read-only."""
    heads: dict[str, str] = {}
    unresolved: dict[str, str] = {}
    blobs: dict[tuple[str, str], tuple[bytes | None, str]] = {}
    missing: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    checked = ok = 0
    for owner, ref in evidence_refs(doc):
        repo_id = ref["repo"]
        _resolve(repo_id, repos, host, heads, unresolved)
        if repo_id in unresolved:
            counts[repo_id] = counts.get(repo_id, 0) + 1
            continue
        checked += 1
        data, why = _read(repo_id, ref["path"], repos, heads, blobs)
        if data is None:
            missing.append({"owner": owner, "repo": repo_id, "path": ref["path"], "anchor": ref.get("anchor"), "why": why})
            continue
        anchor = ref.get("anchor")
        if anchor is not None and anchor not in data.decode("utf-8", "replace"):
            missing.append({"owner": owner, "repo": repo_id, "path": ref["path"], "anchor": anchor,
                            "why": "anchor text not found at origin"})
            continue
        ok += 1

    vocabularies: list[dict[str, Any]] = []
    for vocab in doc.get("vocabularies", []) or []:
        vid = vocab["id"]
        source = vocab["source"]
        repo_id = source["repo"]
        _resolve(repo_id, repos, host, heads, unresolved)
        if repo_id in unresolved:
            counts[repo_id] = counts.get(repo_id, 0) + 1
            vocabularies.append({"id": vid, "status": "unresolved", "missing_in_atlas": [],
                                "missing_in_source": [], "why": unresolved[repo_id]})
            continue
        data, why = _read(repo_id, source["path"], repos, heads, blobs)
        if data is None:
            vocabularies.append({"id": vid, "status": "drift", "missing_in_atlas": [],
                                "missing_in_source": [], "why": why})
            continue
        text = data.decode("utf-8", "replace")
        anchor = source.get("anchor")
        if anchor is not None and anchor not in text:
            vocabularies.append({"id": vid, "status": "drift", "missing_in_atlas": [],
                                "missing_in_source": [], "why": "anchor text not found at origin"})
            continue
        terms = [item["term"] for item in vocab["terms"]]
        if "select" in source:
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                vocabularies.append({"id": vid, "status": "drift", "missing_in_atlas": [],
                                    "missing_in_source": [], "why": "source is not JSON"})
                continue
            values = _select_values(parsed, source["select"].split("/"))
            missing_in_atlas = sorted(set(values) - set(terms))[:50]
            missing_in_source = sorted(set(terms) - set(values))[:50]
            status = "ok" if not missing_in_atlas and not missing_in_source else "drift"
            vocabularies.append({"id": vid, "status": status, "missing_in_atlas": missing_in_atlas,
                                "missing_in_source": missing_in_source})
        else:
            missing_in_source = sorted(term for term in terms if f'"{term}"' not in text)[:50]
            status = "ok" if not missing_in_source else "drift"
            vocabularies.append({"id": vid, "status": status, "missing_in_atlas": [],
                                "missing_in_source": missing_in_source})

    grouped: dict[str, list[dict[str, Any]]] = {}
    order: list[str] = []
    for owner, ref in expect_refs(doc):
        if owner not in grouped:
            grouped[owner] = []
            order.append(owner)
        grouped[owner].append(ref)
    promotable: list[dict[str, Any]] = []
    # An expected ref names something not built yet: a repository it alone cannot
    # resolve says nothing about the atlas evidence, so expect uses its own
    # scratch map and never turns the check partial.
    expect_unresolved = dict(unresolved)
    for owner in order:
        refs = grouped[owner]
        if all(_ref_resolves(ref, repos, host, heads, expect_unresolved, blobs) for ref in refs):
            promotable.append({"owner": owner, "refs": len(refs)})

    commits: dict[tuple[str, str], tuple[str, str]] = {}

    def revision(repo_id: str, commit: str) -> tuple[str, str]:
        _resolve(repo_id, repos, host, heads, unresolved)
        if repo_id in unresolved:  # not counted in `unresolved[].refs`, which counts evidence references
            return "unresolved", unresolved[repo_id]
        if (repo_id, commit) not in commits:
            commits[(repo_id, commit)] = _commit_state(repos[repo_id], heads[repo_id], commit)
        return commits[(repo_id, commit)]

    proof = [_proof(owner, block, proof_ladder(doc), revision) for owner, block in proof_entries(doc)]

    indexed: list[tuple[str, list[dict[str, Any]]]] = []
    for flow in doc.get("flows", []) or []:
        if isinstance(flow, dict) and isinstance(flow.get("id"), str):
            rules = flow.get("traffic", [])
            indexed.append((flow["id"], [rule for rule in rules if isinstance(rule, dict)]
                            if isinstance(rules, list) else []))
    traffic: list[dict[str, Any]] = []
    for x in range(len(indexed)):
        for y in range(x + 1, len(indexed)):
            first_id, first_rules = indexed[x]
            second_id, second_rules = indexed[y]
            if first_id == second_id:
                continue
            for ia, first in enumerate(first_rules):
                for ib, second in enumerate(second_rules):
                    if rules_overlap(first, second):
                        if first_id < second_id:
                            traffic.append({"kind": "traffic-overlap", "flows": [first_id, second_id],
                                            "rules": [ia, ib]})
                        else:
                            traffic.append({"kind": "traffic-overlap", "flows": [second_id, first_id],
                                            "rules": [ib, ia]})
    traffic.sort(key=lambda item: (item["flows"], item["rules"]))

    summary = {"components": len(doc.get("components", []) or []),
               "contracts": len(doc.get("contracts", []) or []),
               "flows": len(doc.get("flows", []) or []),
               "gaps": sum(1 for flow in doc.get("flows", []) or []
                           if isinstance(flow, dict) and flow.get("status") != "live"),
               "traffic_rules": sum(len(rules) for _, rules in indexed),
               "traffic_overlaps": len(traffic),
               "proof_claims": len(proof),
               "proof_over_claims": sum(1 for item in proof if item["status"] == "over-claim"),
               "proof_unverified": sum(1 for item in proof if item["status"] == "unverified"),
               "proof_unresolved": sum(1 for item in proof if item["status"] == "unresolved")}
    if (missing or any(item["status"] == "drift" for item in vocabularies) or traffic
            or any(item["status"] in ("over-claim", "unverified") for item in proof)):
        status = "drift"
    elif (unresolved or any(item["status"] == "unresolved" for item in vocabularies)
          or any(item["status"] == "unresolved" for item in proof)):
        status = "partial"
    else:
        status = "ok"
    return {"schema": CHECK_SCHEMA, "host": host, "checked": checked, "ok": ok, "missing": missing,
            "unresolved": [{"repo": repo_id, "why": why, "refs": counts.get(repo_id, 0)}
                           for repo_id, why in sorted(unresolved.items())],
            "heads": dict(sorted(heads.items())), "status": status,
            "vocabularies": vocabularies, "promotable": promotable, "summary": summary,
            "traffic": traffic, "proof": proof}


def export(doc: dict[str, Any], repos: dict[str, dict[str, Any]],
           check_report: dict[str, Any] | None = None) -> dict[str, Any]:
    """The atlas plus a hosted `url` on every evidence, vocabulary source, expect reference and receipt revision."""
    heads: dict[str, str | None] = {}

    def head_of(repo_id: str) -> str | None:
        if repo_id not in heads:
            repo = repos[repo_id]
            head = None
            if repo["path"] and repo.get("mode") != "worktree":
                identity, _ = checkout_identity(repo["path"], repo["remote"])
                if identity:
                    head = remote_head(repo["path"], repo["default_branch"])
            heads[repo_id] = head
        return heads[repo_id]

    def add_url(ref: dict[str, Any]) -> None:
        repo = repos[ref["repo"]]
        ref["url"] = (f"https://github.com/{repo['remote']}/blob/"
                      f"{head_of(ref['repo']) or repo['default_branch']}/{ref['path']}")

    out = json.loads(json.dumps(doc))
    out["schema"] = EXPORT_SCHEMA
    for _, ref in evidence_refs(out):
        add_url(ref)
    for vocab in out.get("vocabularies", []) or []:
        if isinstance(vocab, dict) and isinstance(vocab.get("source"), dict):
            add_url(vocab["source"])
    for _, ref in expect_refs(out):
        add_url(ref)
    for _, block in proof_entries(out):
        for receipt in block["receipts"]:
            for rev in receipt["revisions"]:
                rev["url"] = f"https://github.com/{repos[rev['repo']]['remote']}/commit/{rev['commit']}"
    if check_report is not None:
        out["check"] = check_report
    return out


# --------------------------------------------------------------------------- summary

def _summary(report: dict[str, Any]) -> str:
    """One-line-per-finding human summary of a check report."""
    lines = [f"Atlas check on {report['host']}: {report['ok']}/{report['checked']} references present, "
             f"{len(report['missing'])} missing, {len(report['unresolved'])} repositories unresolved ({report['status']})."]
    for item in report["missing"]:
        anchor = f" [{item['anchor']}]" if item.get("anchor") else ""
        lines.append(f"- missing: {item['owner']}: {item['repo']}/{item['path']}{anchor}: {item['why']}")
    for item in report["unresolved"]:
        lines.append(f"- unresolved: {item['repo']} ({item['refs']} refs): {item['why']}")
    for item in report.get("vocabularies", []) or []:
        if item.get("status") == "drift":
            detail = item.get("why") or (
                f"missing_in_atlas={item.get('missing_in_atlas', [])}, "
                f"missing_in_source={item.get('missing_in_source', [])}")
            lines.append(f"- vocabulary drift: {item['id']}: {detail}")
    for item in report.get("promotable", []) or []:
        lines.append(f"- promotable: {item['owner']} ({item['refs']} refs)")
    for item in report.get("traffic", []) or []:
        lines.append(f"- traffic overlap: {item['flows'][0]}[{item['rules'][0]}] "
                     f"with {item['flows'][1]}[{item['rules'][1]}]")
    proof = report.get("proof", []) or []
    if proof:
        held = sum(1 for item in proof if item["status"] == "ok")
        lines.append(f"Evidence levels: {held}/{len(proof)} claims proven.")
    for item in proof:
        if item["status"] == "ok" and item["proven"] == item["claimed"]:
            continue
        label = "under-claim" if item["status"] == "ok" else item["status"]
        reach = (f", receipts reach {item['receipted'] or 'no level'}"
                 if item["receipted"] != item["proven"] else "")
        lines.append(f"- proof {label}: {item['owner']}: claimed {item['claimed']}, "
                     f"proven {item['proven'] or 'no level'}{reach}")
        for problem in item["revisions"]:
            lines.append(f"  - receipt {problem['receipt']}: {problem['repo']}@{problem['commit'][:12]}: "
                         f"{problem['why']}")
    return "\n".join(lines) + "\n"
