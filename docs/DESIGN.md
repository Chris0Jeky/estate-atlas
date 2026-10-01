# estate-atlas: design

estate-atlas keeps a system's architecture as checked data:
- the **components**, the **contracts** between them and the **flows** that connect them, in one JSON file;
- each claim carries **evidence** (a file, and optionally a text anchor) in a git repository;
- a checker proves that the evidence still exists.

On top of that, it can:
- **route real records** (log lines, events, links) onto the flows, so the map shows which wiring is alive;
- **render** the atlas: an offline HTML page, mermaid, an SVG overview and generated Markdown blocks;
- **explain** every part in plain English, from facts only.

It is standard-library Python (3.11+), with no dependencies, and is licensed GPL-3.0-only.

## 1. Package layout

| Module | Owns | Main API |
|---|---|---|
| `estate_atlas/model.py` | loading and validating an atlas | `load(path) -> dict`, `validate(doc) -> None` (raises `AtlasError`), `SCHEMA = "estate-atlas@2"`, `is_atlas_schema(s)` |
| `estate_atlas/check.py` | evidence against git, vocabularies, expected evidence, traffic-rule overlap | `check(doc, repos) -> report`, `repositories(doc, overrides, host) -> repos`, `patterns_overlap(a, b)`, `rules_overlap(r1, r2)` |
| `estate_atlas/traffic.py` | routing records onto flows: declared rules, then inference; pulse; an incremental index with verify; daily rollups, weekly series, fading/surging flags | `match`, `compile_rules`, `route`, `signature`, `TrafficIndex`, `weekly`, `flags` |
| `estate_atlas/render.py` | the offline HTML page and mermaid | `render_html(doc, check=None) -> str`, `render_mermaid(doc, view) -> str`, `order_cards(doc)` |
| `estate_atlas/docs.py` | marked Markdown blocks and the overview SVG | `glance_svg(doc)`, `owners_block`, `layers_block`, `traffic_block(doc, traffic)`, `replace_blocks(text, blocks)`, `write(...)`, `check_docs(...)` |
| `estate_atlas/explain.py` | plain-English explanations and the tour | `explain(doc, id, traffic=None, live=None)`, `tour(doc, traffic=None)`, `render_tour_md(doc, traffic=None)` |
| `estate_atlas/cli.py` | `python -m estate_atlas <verb>` | the verbs `validate`, `check`, `export`, `render`, `docs`, `explain`, `tour` and `route` |
| `estate_atlas/__main__.py` | the entry point | calls `cli.main()` |

## 2. Format (`estate-atlas@2`)

The format is atlas@2:
- `repos`: name → `{remote, default_branch, paths: {host: path}}`;
- `layers`;
- `components`: `id`, `title`, `layer`, `home`, `status`, `summary`, `surfaces`, `owns`, `evidence`,
  `instances` and `expect`;
- `contracts`: `producer`, `consumers`, `format`;
- `flows`: `from`, `to`, `contract`, `trigger`, `status`, `gap`, `evidence`, `expect` and `traffic`;
- `vocabularies`.

The loader accepts `estate-atlas@2` and any namespaced `<namespace>/atlas@2`, so an embedding project can keep its
own schema name.

- **Statuses.** Components and contracts can be live, partial, planned or retired; flows can be live, partial,
  documented, planned or absent. Only a planned item (or a documented or absent flow) may carry `expect`.
- **Traffic rules.** Each rule has a `source` (`journal`, `links` or `events`) and optional `producer`, `verb`,
  `actor` and `subject` patterns. A pattern is exact or a prefix ending in `*`. A rule may also carry `pulse: true`.
- **No overlap.** Rules on different flows may not overlap. `check` proves this statically, so one record crosses at
  most one flow.
- **Events.** An events `subject` matches any one of the event's nodes, so events rules are compared ignoring their
  subject.

## 3. Repos and hosts

Evidence refs name a repo. `repositories(doc, overrides, host)` resolves each repo to a local checkout:
- an override from the CLI (`--repo name=path`) comes first;
- then `repos[name].paths[host]`;
- otherwise the repo is unresolved.

An unresolved repo makes its refs `unresolved`, not missing. `check` reads `origin/<default_branch>` through `git
show`, and it never fetches. With `--worktree` it reads the checkout's working files instead, so uncommitted
edits count; it does not read `HEAD`.

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

Counts are kept in hourly buckets over 168 hours. `verify()` rebuilds from scratch and compares.

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
python -m estate_atlas tour atlas.json [--md] [--traffic T]
python -m estate_atlas route atlas.json --journal F.jsonl [--events F.jsonl] [--links F.json]
```

`route` takes JSONL record files, prints `estate-atlas-traffic@1`, and exists so anyone can try routing without a
running service.

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
- `examples/shop/atlas.json` has its evidence in `examples/shop/src/`, which is in this repository, so `check` runs
  offline with `--repo shop=.`.
- Sample `journal.jsonl` and `events.jsonl` let `route` light some flows, leave one silent, and show one pulse
  flow.

## 7. What never goes in

There are no references to private systems, repositories, people, hosts or paths: not in code, tests,
examples or docs. The engine, the format and the example are generic.
