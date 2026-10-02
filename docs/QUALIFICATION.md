# Qualifying a consumer

Use the fictional shop to qualify source references, observation routing and public output. Use the
[workspace design](../examples/workspace/README.md) to distinguish captures, human intent, execution attempts,
results, authoritative receipts and attention. Neither example is a deployment inventory or runtime acceptance.

## Run the offline proof

From the repository root, with Python 3.11 or later and Git available:

```sh
python -m unittest discover -s tests -p test_consumer_qualification.py -v
python -m unittest discover -s tests -p test_workspace_example.py -v
python -m unittest discover -s tests -p test_traffic.py -v
```

The consumer tests run actual `python -m estate_atlas` subprocesses. They copy shop source stubs into a disposable
Git repository and set a fictional origin URL and `origin/main` ref locally. No network access, fetch or real remote
is needed. Before and after every source check they compare HEAD, refs, index bytes, local config and status,
including a dirty working file and staged draft. Fixture setup intentionally writes its own Git state; `check`
must leave it unchanged. Temporary files and Git repositories are removed after the tests.

These source-check subprocess tests set PYTHONPATH to the source root so an outside working directory can find
the package. They do not prove wheel installation; use the separate installation path below.

The existing workspace tests validate the planned design and distinct intent/result/receipt/attention owners.
The existing traffic tests exercise declared and inferred routing, competing rules and inferred ambiguity,
internal records, unrouted records, pulse counts, expiry and empty input. The consumer subprocess tests add a
planned shop flow with synthetic observations: `off_status` appears while its planned status and gap remain intact.

## Interpret the evidence

| Result or field | Meaning | What it does not prove |
| --- | --- | --- |
| `check`: exit 0, `ok` | The named source files and anchors resolve at the local `origin/<default_branch>` ref. | Current remote freshness, executable behavior or deployment. |
| `check`: exit 0, `partial` | A repository is unresolved, such as a mismatched origin or unavailable ref. | All references proven; partial is not green source coverage. |
| `check`: exit 1, `drift` | A resolvable repository lacks an asserted file or anchor. | That every unresolved repository is also missing those files. |
| `--worktree` | Evidence reads working files, including uncommitted edits. | Committed, pushed or deployed state. |
| `status` | The atlas author's declared implementation state. | A status transition inferred from traffic. |
| `expect` / `promotable` | Expected references now resolve; review the item for a possible promotion. | Automatic promotion or runtime acceptance. Missing expectations are not evidence drift. |
| `gap` | The explicit remaining work for a non-live flow. | A task completed by resolving an expectation. |
| `declared` / `inferred` | A routing rule matched, or endpoints uniquely selected a flow. | Correctness or authorization of the observed operation. |
| `pulse` | A heartbeat observation. | Business volume or a completed commitment. |
| `internal`, `unrouted`, `ambiguous` | A record stays within one component, has no route, or cannot select one route. | A crossing silently assigned to a convenient flow. |
| `off_status` | Observations cross a flow whose declared status is not live. | Permission to change status or erase its gap. |
| Empty or expired observations | No records remain in the selected observation window. | Connection health, absence of historical activity or successful delivery. |
| Stale tour: exit 1 | A saved document differs from regenerated wording. | Runtime freshness; document equality is a separate check. |

Source proof, observation evidence, snapshot freshness and human acceptance are separate. By default `route`
uses the newest timestamped input record as its clock, so an old input can still show heat. Supply an explicit
`--now` to assess an intended window, inspect source completeness and record the observation time outside the
public example. A successful execution result does not complete a human commitment: the workspace owns the
authoritative receipt and human completion decision; attention membership is not an execution queue.

## Try the positional commands

Run from the repository root. The default shop check is intentionally partial because its origin is fictional;
the working-file check proves the stubs:

```sh
python -m estate_atlas validate examples/workspace/atlas.json
python -m estate_atlas check examples/shop/atlas.json --repo shop=examples/shop
python -m estate_atlas check examples/shop/atlas.json --repo shop=examples/shop --worktree
python -m estate_atlas route examples/shop/atlas.json --journal examples/shop/journal.jsonl --events examples/shop/events.jsonl --out traffic.json
python -m estate_atlas explain examples/shop/atlas.json worker --traffic traffic.json
python -m estate_atlas render html examples/shop/atlas.json --out atlas.html
python -m estate_atlas tour examples/shop/atlas.json --md --overlay tests/fixtures/shop-overlay.json --out public-tour.md
```

Keep generated outputs in a disposable directory. Apply a complete consumer-owned overlay before public
publication. Public tour failure returns exit 2, emits a fixed refusal without repeating denied terms or private
paths, and preserves any existing output file. Inspect private inputs privately. Passing this fictional overlay
does not qualify a real consumer's privacy vocabulary or authorize publication.

Rerun identical commands with identical inputs and clock to compare output bytes. Compare stdout to stdout and
saved files to saved files: Windows stdout may use CRLF while saved files use UTF-8 and LF. A normalized comparison
between those channels is useful, but is not the same byte comparison.

## Qualify the installed package separately

Build a wheel in a disposable environment using `python -m pip wheel --no-deps --wheel-dir <wheel-directory>
<source-directory>`. Build tooling may need network access; the offline proving tests above do not. Build from a
disposable source copy if build artifacts must not enter the checkout. Setuptools is a build dependency; the
installed package has no runtime dependencies.

Create a second clean venv whose path includes spaces, install that wheel with its Python using
`-m pip install --no-index --no-deps <wheel-file>`, and change to a directory outside the source tree. Remove
PYTHONPATH and PYTHONHOME from the environment. Copy the fictional examples and overlay fixture into that
directory, preserving their relative layout; the wheel supplies code, not examples or tests. Run both the venv
Python's `-m estate_atlas --help` and its installed `estate-atlas --help`, then repeat the positional commands above
with each entry point. Confirm imports come from that venv's site-packages, inspect package metadata for runtime
requirements and run `python -m pip check` with that venv's Python.

Repeat on each supported interpreter available to you. On Windows, `py -0p` lists candidates and
`py -3.11 -m venv "atlas qualification env"` selects the minimum version when installed. If Python 3.11 is absent,
record **Python 3.11: NOT RUN, interpreter unavailable** with the command to repeat later. Success on a newer
interpreter or a syntax inspection is not minimum-version execution proof. Keep raw commands, interpreter versions,
tested commit/wheel identity and results outside Git; do not add operational receipts or private paths to this guide.
