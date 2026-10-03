# estate-atlas: design

estate-atlas keeps a system's architecture as checked data:
- the **components**, the **contracts** between them and the **flows** that connect them, in one JSON file;
- each claim carries **evidence** (a file, and optionally a text anchor) in a git repository;
- a part may also claim an **evidence level** (source, unit, integrated, ... accepted), backed by receipts;
- a checker proves that the evidence still exists and that every claimed level is backed.

On top of that, it can:
- **route records** (log lines, events, links) onto the flows, showing observations separately from declared status;
- **render** the atlas: an offline HTML page, mermaid, an SVG overview and generated Markdown blocks;
- **explain** every part in plain English, from facts only.

It is standard-library Python (3.11+), with no dependencies, and is licensed GPL-3.0-only.

## 1. Package layout

| Module | Owns | Main API |
|---|---|---|
| `estate_atlas/model.py` | loading and validating an atlas | `load(path) -> dict`, `validate(doc) -> None` (raises `AtlasError`), `SCHEMA = "estate-atlas@3"`, `is_atlas_schema(s)`, `schema_version(s)`, `proof_ladder(doc)`, `proof_entries(doc)`, `receipted_level(block, ladder)` |
| `estate_atlas/check.py` | evidence against git, vocabularies, expected evidence, evidence levels, traffic-rule overlap | `check(doc, repos, host) -> report`, `repositories(doc, overrides, host) -> repos`, `export(doc, repos, report=None)`, `patterns_overlap(a, b)`, `rules_overlap(r1, r2)` |
| `estate_atlas/traffic.py` | routing records onto flows: declared rules, then inference; pulse; an incremental index with verify; daily rollups, weekly series, fading/surging flags | `match`, `compile_rules`, `route`, `signature`, `TrafficIndex`, `weekly`, `flags` |
| `estate_atlas/render.py` | the offline HTML page and mermaid | `render_html(doc, check=None) -> str`, `render_mermaid(doc, view) -> str`, `order_cards(doc)` |
| `estate_atlas/docs.py` | marked Markdown blocks and the overview SVG | `glance_svg(doc)`, `owners_block`, `layers_block`, `traffic_block(doc, traffic)`, `replace_blocks(text, blocks)`, `write(...)`, `check_docs(...)` |
| `estate_atlas/explain.py` | plain-English explanations and the tour | `explain(doc, id, traffic=None, live=None)`, `tour(doc, traffic=None)`, `render_tour_md(doc, traffic=None)` |
| `estate_atlas/overlay.py` | a public overlay: public wording and ids for a private atlas, with a leak check | `load_overlay(path)`, `validate_overlay(overlay, doc)`, `apply_overlay(doc, overlay)`, `apply_overlay_traffic(traffic, overlay)`, `leak_terms(doc, overlay)`, `leaks(text, denylist, words=())` |
| `estate_atlas/cli.py` | `python -m estate_atlas <verb>` | the verbs `validate`, `check`, `export`, `render`, `docs`, `explain`, `tour` and `route` |
| `estate_atlas/__main__.py` | the entry point | calls `cli.main()` |

## 2. Format (`estate-atlas@3`)

The format is atlas@3; atlas@2 is the same format without evidence levels (section 2.1):
- `repos`: name → `{remote, default_branch, paths: {host: path}}`;
- `layers`;
- `components`: `id`, `title`, `layer`, `home`, `status`, `summary`, `surfaces`, `owns`, `evidence`,
  `instances` and `expect`;
- `contracts`: `producer`, `consumers`, `format`;
- `flows`: `from`, `to`, `contract`, `trigger`, `status`, `gap`, `evidence`, `expect` and `traffic`;
- `vocabularies`;
- from @3, an optional `proof_ladder`, and an optional `proof` block on any component, contract or flow.

The loader accepts `estate-atlas@3` and `estate-atlas@2`, and any namespaced `<namespace>/atlas@3` or
`<namespace>/atlas@2`, so an embedding project can keep its own schema name.

- **Statuses.** Components and contracts can be live, partial, planned or retired; flows can be live, partial,
  documented, planned or absent. Only a planned item (or a documented or absent flow) may carry `expect`.
- **Traffic rules.** Each rule has a `source` (`journal`, `links` or `events`) and optional `producer`, `verb`,
  `actor` and `subject` patterns. A pattern is exact or a prefix ending in `*`. A rule may also carry `pulse: true`.
- **No overlap.** Rules on different flows may not overlap. `check` proves this statically, so one record crosses at
  most one flow.
- **Events.** An events `subject` matches any one of the event's nodes, so events rules are compared ignoring their
  subject.

### 2.1 Evidence levels (`proof`, from @3)

`evidence` says where a part lives in source. A `proof` block says how far it has been proven, on an ordered
**ladder** of levels, lowest first. An atlas may declare its own ladder as `"proof_ladder": ["...", "..."]` (1-16
unique ids, lowest first), which then replaces the default for every block. The default ladder, and what each level
is meant to record:

| Level | A passed receipt at this level records |
|---|---|
| `source` | the part exists in source at the pinned commit |
| `unit` | its own tests pass in isolation |
| `integrated` | it works with the parts it talks to, run together (say what was replaced by a double) |
| `native` | it runs from a clean build on its real target platform, outside the development checkout |
| `installed` | it is installed the way its users install it, and works there |
| `device` | it works on the physical device or environment it serves |
| `accepted` | its owner or user has accepted it |

The engine checks order and receipts, not the meaning of a level: these meanings are a convention for the people
writing receipts.

```json
"proof": {
  "claim": "integrated",
  "receipts": [
    {"id": "api-unit-7", "level": "unit",
     "revisions": [{"repo": "shop", "commit": "<full 40- or 64-hex commit id>"}],
     "check": "python -m unittest", "outcome": "passed",
     "unavailable": "no coverage report", "date": "2026-10-01"}
  ]
}
```

- `claim` is the level the part claims. `receipts` lists what backs it (it may be empty, which proves nothing).
- A **receipt** records one run of a check that someone already ran; estate-atlas never runs it. Its fields:
  `id` (unique in the block), `level` (on the ladder), `revisions` (1-20 `{repo, commit}` pairs naming everything
  that was checked together, as full lowercase commit ids in declared repos; all of them must resolve), `check`
  (the command or check name that was run), `outcome` (`passed`, `failed` or `partial`), `unavailable` (a free-text
  caveat: what that run could not cover, or `nothing`) and an optional `date`. Only `outcome: passed` holds a level;
  `unavailable` is for readers and never changes the result.
- **The rule.** A part may claim a level only when that level and every level below it has a passed receipt, and
  every receipt's revisions resolve. Levels count from the bottom up: a level whose lower neighbour has no passed
  receipt is not reached, whatever its own receipts say. `check` reports, per block, the `claimed` level, the
  highest level reached by passed receipts (`receipted`, read from the block alone) and the highest level reached
  by passed receipts whose revisions all resolve in git (`proven`), with a `status`:
  - `ok`: proven at least as high as claimed (a higher `proven` is an under-claim, printed but not drift);
  - `over-claim`: some level up to the claim has no passed receipt (drift, even when no checkout is available);
  - `unverified`: a receipt revision, at any level, names a commit that the available checkout does not have,
    or (in the default mode) one that is not on its default branch (drift, exit 1);
  - `unresolved`: the claim is receipted, but a needed repository has no usable checkout here, so nothing could be
    resolved (partial, exit 0, like unresolved evidence).
- **Resolving a revision** is read-only, like every other read: `git cat-file -t <commit>` must say `commit`, and
  in the default mode `git merge-base --is-ancestor <commit> origin/<default_branch>` must hold, so a receipt
  pins a commit that reached the default branch. `--worktree` only requires the commit to exist in the checkout's
  repository. Lazy fetching is disabled for every read (`GIT_NO_LAZY_FETCH=1`), so a partial clone never fetches.
  A checkout that is not a git repository leaves a revision unresolved.
- **Rendering.** `check` text adds an `Evidence levels: N/M claims proven.` line and one line per claim whose
  proven level differs from the claim. `explain` and the tour add one "Evidence:" sentence per component that
  carries a block, comparing the claim with how far its passed receipts reach (no git is read there). The HTML page
  gains an Evidence view (claimed, receipts reach, proven when a check report is given, every receipt with its
  commit links); an atlas without `proof` blocks renders as before. `export` adds a commit `url` to every receipt
  revision. Ordering is fixed: blocks in document order in reports, by kind and id in the HTML, receipts by
  ladder position then id.
- **Migration.** atlas@2 documents stay valid unchanged; to use evidence levels, change `schema` to
  `estate-atlas@3` (or `<namespace>/atlas@3`). A @2 document that carries `proof` or `proof_ladder` is invalid.
  The check report is now `estate-atlas-check@3` and the export `estate-atlas-export@3`, for every atlas: they add
  a `proof` list and the summary counts `proof_claims`, `proof_over_claims`, `proof_unverified` and
  `proof_unresolved`; nothing else in them changed. A consumer that pinned `@2` should accept `@3`;
  `render.parse_check` accepts both. `unresolved[].refs` still counts evidence references only.

## 3. Repos and hosts

Evidence refs name a repo. `repositories(doc, overrides, host)` resolves each repo to a local checkout:
- an override from the CLI (`--repo name=path`) comes first;
- then `repos[name].paths[host]`;
- otherwise the repo is unresolved.

An unresolved repo makes its refs `unresolved`, not missing. `check` reads `origin/<default_branch>` through
read-only git commands (`rev-parse`, `ls-tree` and `cat-file`), and it never fetches. With `--worktree` it reads the checkout's working files instead, so uncommitted
edits count; it does not read `HEAD`.

Git reads discover the repository from the selected path. Inherited Git variables that redirect repositories,
objects, command configuration or tracing are removed, keeping evidence tied to the selected checkout and
preventing trace writes. Normal repository discovery and global user configuration remain available;
optional Git locks are disabled for these reads. Trace2 targets are explicitly disabled in the subprocess
environment because removing inherited trace variables alone would still allow owner-configured trace files.

## 4. Traffic engine (`traffic.py`)

The traffic engine is a library: the caller injects the readers:
- `journal_rows(after_id, since) -> [{id, at, producer, verb, actor, subject}]`;
- `event_rows(after_id, since) -> [{id, at, kind, source, nodes}]`;
- `links() -> [(doc_source, {from, rel, to, at?})]`;
- `atlas() -> (version, doc)`;
- `now()`.

The routing works as follows:
1. A record matched by a declared rule gets basis `declared`, or `pulse` when only pulse rules matched.
2. Otherwise its endpoints are mapped through `instances`, and if exactly one flow joins them it gets basis
   `inferred`.
3. If its endpoints all map to one component, it is `internal`.
4. Otherwise it is `unrouted` or `ambiguous`.

Counts are kept in hourly buckets over 168 hours. `verify()` rebuilds from scratch and compares; its result is
cached for 60 seconds and describes that comparison, not continuous source monitoring. Declared rules can match
producer-only events without endpoints. A pulse is an observation, not business volume or health proof.

Rollups: `rollup` refuses to run (returns `skipped: "index not built"`, writes nothing and leaves `last_day` alone)
while the traffic index is not built or has no flows, so a day is never sealed without rules to route it.
`weekly(rows, last_day, weeks, flows)` and `flags(series, days_stored, first_seen)`:
- **fading**: last week is below 40% of the 4-week average, and that average is at least 10;
- **surging**: last week is at least 20 and above 3 times the average;
- a flow is flagged only after 35 days of its own history.

## 5. Command line

```
python -m estate_atlas validate atlas.json
python -m estate_atlas check atlas.json [--repo name=path ...] [--host H] [--worktree] [--json]
python -m estate_atlas export atlas.json [--check] [--out F]
python -m estate_atlas render html|mermaid atlas.json [--out F]
python -m estate_atlas docs write|check atlas.json --doc ARCH.md [--traffic T]
python -m estate_atlas explain atlas.json <id> [--traffic T] [--live L]
python -m estate_atlas tour atlas.json [--md] [--traffic T] [--overlay O]
python -m estate_atlas route atlas.json --journal F.jsonl [--events F.jsonl] [--links F.json]
```

`route` takes JSONL record files, prints `estate-atlas-traffic@1`, and exists so anyone can try routing without a
running service.

File-backed JSON and JSONL use strict UTF-8 (with an optional leading BOM): duplicate keys and non-finite numbers,
including exponents that overflow a finite float, are invalid. Valid JSON records with unusable timestamps and
non-object entries in a links list retain the documented skip behavior; these are distinct from JSON errors.
Integer timestamps outside the supported finite-float range are invalid input: `parse_at` raises `ValueError`
with `timestamp is outside the supported numeric range`, and file routing rejects the input rather than
skipping or clamping the record. The CLI exits 2, prints no snapshot, and preserves an existing output file.

Exit codes: 0 means ok, 1 means drift or stale, 2 means invalid input.

### The `live` input to `explain`

`--live` (or the `live` argument of `explain.explain`) takes a JSON object of live facts, keyed by component id. A
file shaped `{"schema": ..., "live": {...}}` is also accepted; the inner `live` object is used. For each component
that has an entry, `explain` adds one "Running now" line. The entry reads:
- `instances`: a count of running instances;
- `by_status`: an object of status name to count, written `live` first and then alphabetically, for example
  `{"live": 2, "idle": 1}`;
- `failing`: a list of objects with an `intent` and a `subject`, one per failing condition (other keys are ignored).
  The line names the first three as `<intent> on <subject>` and counts them all.

Missing or malformed values count as zero or are skipped. A component with no entry gets no "Running now" line.

```json
{"web": {"instances": 3, "by_status": {"live": 2, "idle": 1},
         "failing": [{"intent": "checkout-up", "subject": "web"}]}}
```

## 6. Example (`examples/shop`)

A small fictional system, "a little shop": web, api, queue, worker, db and a payments provider.
- `examples/shop/atlas.json` has its evidence in `examples/shop/src/`, which is in this repository. From the
  repository root, `check examples/shop/atlas.json --repo shop=examples/shop --worktree` proves the local
  example files offline. Without `--worktree`, the fictional remote does not match this repository and the
  report is `partial` with unresolved references; exit 0 for that report does not mean the references were proven.
- Sample `journal.jsonl` and `events.jsonl` let `route` light some flows, leave one silent, and show one pulse
  flow.
- Evidence levels: every level of the default ladder is claimed by some part, the storefront carries receipts for
  all seven, the worker under-claims, and `examples/shop/atlas-over-claim.json` is a one-component atlas that
  over-claims, which `check` rejects (exit 1). The receipts pin commits of this repository that hold the example,
  so `--worktree` resolves them; rewriting this repository's history would make them unverified.

## 7. Public overlay (`overlay.py`)

A project can keep its atlas private and still publish a tour of it. An **overlay** (`estate-atlas-overlay@1`) is a
JSON file that gives every layer, component, contract and flow public wording and a public id:

```json
{"schema": "estate-atlas-overlay@1",
 "title": "optional public name of the system",
 "denylist": ["extra term", "..."],
 "layers":     {"<layer id>":     {"id": "<public id>", "title": "...", "summary": "..."}},
 "components": {"<component id>": {"id": "<public id>", "title": "...", "summary": "...", "home": "<public label>"}},
 "contracts":  {"<contract id>":  {"id": "<public id>", "title": "...", "summary": "..."}},
 "flows":      {"<flow id>":      {"id": "<public id>", "trigger": "...", "gap": "..."}}}
```

- **Coverage is total.** Every layer, component, contract and flow has an entry and no entry names an unknown id;
  `validate_overlay` lists every missing or unknown id. A flow entry carries `gap` when, and only when, the flow has
  one. Public text follows the model's limits for that field, and public ids follow its id patterns and are unique
  per kind.
- **The rewrite.** `apply_overlay` returns a deep copy with public text and public ids, renamed everywhere they are
  referenced, and `home` replaced by the public label. It keeps only what the tour and `explain` read: `repos` is
  `{}` and `evidence`, `expect`, `surfaces`, `owns`, `instances`, `vocabularies`, flow `traffic` rules, `proof`
  blocks and the `proof_ladder` (receipts name checks, commits and repos), and a contract's `format` (the tour never
  prints it) are gone.
  The result is deliberately **not** a valid atlas: do not pass it to `model.validate` or `check`.
- **Traffic.** `apply_overlay_traffic` renames the flow ids of an `estate-atlas-traffic@1` document so the tour's
  "this week" numbers still show. It keeps `generated`, the per-flow counts and the `silent` and `off_status`
  crosschecks, and drops everything else: `unrouted` examples, `rule_gaps`, sources, coverage and any flow the
  overlay does not name. Invalid `flows` or `crosschecks` containers, non-string `silent` entries,
  and `off_status` entries without a string `flow` are an `AtlasError` (exit 2); malformed input is never
  rewritten as an empty snapshot.
- **Leak check.** `leak_terms(doc, overlay)` returns what the output must not contain. As substrings: the overlay's
  `denylist`, every original title that differs from its public title (and has at least 4 characters, `MIN_TITLE_SUBSTRING`), every original summary, flow trigger and flow
  gap that differs from its public text (only when it is at least 12 characters, `MIN_TEXT_TERM`, so a short common
  phrase does not flood the check), and every repo `remote`. As whole words, where only ASCII letters and digits are
  word characters, so `-` and `_` split tokens (`payments-gateway-v2` holds `payments-gateway`): every original id
  that differs from its public id, every original title shorter than 4 characters (a private `DB` flags the word `db`, not `feedback`),
  every repo key and every original `home` that differs from its label. A word that
  the overlay itself publishes (as an id or label) is left out. A derived title or text term (never a `denylist`
  term or a repo `remote`) is also left out when, folded, it equals a whole text the overlay publishes unchanged: an
  entry's public title, summary, trigger, gap or home label that equals that entry's original. So a layer renamed from
  `Core` does not refuse a component whose public title is still `Core`. It cannot whitelist a private term the overlay
  does not publish verbatim: it needs equality with a whole published text (`Core` does not excuse `Core services`),
  and a private text copied into another entry's public text, where that entry's original differs, is still a leak. `leaks(text, denylist, words=())` folds the text and
  every term first (NFKC, `casefold()`, whitespace runs collapsed to one space) and returns the terms found, sorted.
  `find_leaks` runs it where escaping cannot hide anything: on every string of the overlaid document and the overlay
  title (before any escaping), on the rendered output, on the Markdown with `\|` and HTML entities undone, and on
  every string and key of the parsed JSON output. Each hit is `(term, field)`.
- **CLI.** `tour atlas.json --md --overlay O.json [--traffic T]` validates the overlay, renders the tour from the
  overlaid document, runs the leak check on the output and exits 2 with a fixed refusal message if anything
  leaks or an input is invalid. Neither stdout nor stderr echoes private terms, ids, paths or input values;
  a refused tour leaves an existing output file unchanged. For detailed diagnostics, use the validation
  and leak-check library APIs privately; their exceptions and findings can contain private input and must
  not be published. A `title` in the overlay replaces "the estate" in the heading. `--overlay` does not
  combine with `--check`.
- **Keep the overlay private.** Its keys are the original ids, so the overlay file belongs with the private atlas;
  only its output is published.

Atlas text in generated Markdown blocks and tours is escaped as literal text, including public tour titles;
it cannot introduce raw HTML, Markdown links or inline code. Generated HTML and SVG have their own escaping.
Deterministic output does not itself establish source freshness, privacy coverage or runtime acceptance.

## 8. What never goes in

There are no references to private systems, repositories, people, hosts or paths: not in code, tests,
examples or docs. The engine, the format and the example are generic.
