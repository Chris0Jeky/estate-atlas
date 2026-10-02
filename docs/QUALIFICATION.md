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

Run the offline installation proof from the source root, with Python 3.11 or later and Git available:

```sh
python scripts/qualify_wheel.py --report <directory-outside-git>/wheel-qualification.json
```

The runner inspects the selected interpreter's pip cache before installing anything. It recognizes local wheel
files and cached HTTP wheel bodies for setuptools, wheel and packaging (build tooling only). Supply an additional
existing offline cache with `--wheelhouse <directory>`; this option is repeatable. No index or network installation
is permitted by the runner. If compatible cached tooling is unavailable, it fails with a receipt explaining the
missing prerequisite. Cache contents and selected build-tool versions are recorded; keep compatible tooling wheels
to reproduce a run with the same build environment.

The runner copies source into a disposable directory, excluding Git metadata and build products, creates a build
venv and builds with `pip wheel --no-index --no-deps --no-build-isolation`. It creates a separate clean consumer
venv whose path contains spaces and installs the built wheel with `pip install --no-index --no-deps`. It copies
fictional fixtures and the consumer test file into an outside directory. Neither PYTHONPATH nor PYTHONHOME is
passed to the build, install or installed CLI subprocesses. Inherited pip settings are removed before setting
controlled offline options, so external find-links or installation destinations cannot redirect the proof.

Both the venv Python's `-m estate_atlas` and the installed `estate-atlas` console entry point run help and the same
consumer proof suite. Installed tests use copied fixtures and temporary repositories outside the checkout;
ordinary source tests continue to use source PYTHONPATH. The proof covers clean and dirty Git preservation
(HEAD, refs, index, config, status and working-file contents), source drift and partial coverage, planned-flow
semantics, observations, positional commands, byte-stable repeated outputs, public refusal with empty stdout and
preserved output files, and document/SVG preservation during checks and invalid-input failure. An import probe
requires the package to come from the consumer venv's site-packages, verifies no runtime metadata requirements,
and runs `pip check`. Import and requirement checks remain active under optimized Python. A structured result
must include every known consumer test with no failures, errors or skips, plus actual CLI command records;
an empty or incomplete suite fails the installed proof.

`--report` is required and must be outside every Git checkout, including ignored directories. The JSON receipt
records raw commands, return codes and output, interpreter and build-tool versions, source HEAD and dirty status,
a digest of copied source contents, wheel filename/SHA-256, installed import location, both tested entry points and
the final result. A dirty source run qualifies the copied content digest, not HEAD alone. Temporary environments,
fixtures and built wheel are removed after the run. Receipts can contain local paths: keep them outside Git and
inspect them privately.

Repeat on each supported interpreter available to you with `--python <interpreter-path>`. On Windows, `py -0p`
lists candidates, and `py -3.11 scripts/qualify_wheel.py --report <directory-outside-git>/wheel-311.json` selects the
minimum version when installed. If Python 3.11 is absent, record **Python 3.11: NOT RUN, interpreter unavailable**
with that repeat command in the private receipt. Success on a newer interpreter or syntax inspection is not
minimum-version execution proof.
