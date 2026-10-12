# estate-atlas

**Your architecture as checked data.** You describe the components, contracts and flows of your system in one JSON
file. estate-atlas then:

- **proves** each claim against your git repositories, so the map can't quietly drift from the code;
- **projects** observations from your logs and events onto flows, showing volumes, pulses, silent live flows
  and records that cross no flow;
- **draws** it: an offline HTML atlas, mermaid, an SVG overview, and Markdown blocks your docs can embed and CI can
  check;
- **explains** every part in plain English, from facts only;
- **publishes** a tour of a private atlas in public wording, with total coverage and a leak check (a public overlay).

Standard-library Python. No dependencies. GPL-3.0-only.

Atlas files use strict UTF-8 JSON, with or without a leading UTF-8 BOM. File-based commands validate the
same document they render or explain; duplicate keys, non-finite numbers and malformed UTF-8 are rejected.

```
python -m estate_atlas check examples/shop/atlas.json --repo shop=examples/shop
python -m estate_atlas route examples/shop/atlas.json --journal examples/shop/journal.jsonl --events examples/shop/events.jsonl
python -m estate_atlas explain examples/shop/atlas.json worker
python -m estate_atlas tour examples/shop/atlas.json --md --overlay tests/fixtures/shop-overlay.json
```

Design: [docs/DESIGN.md](docs/DESIGN.md).

Docs site: https://chris0jeky.github.io/estate-atlas/

Consumer proof and its limits: [docs/QUALIFICATION.md](docs/QUALIFICATION.md).

## Public check

Before publishing, check both the content about to be staged and the committed content:

```
python scripts/check_public.py
python scripts/check_public.py --committed
python scripts/check_public.py --history
```

The default mode scans working-tree text and tracked file names. `--committed` scans the names and blobs
at HEAD, so a dirty cleanup, deletion or staged replacement cannot hide committed content. Both modes scan
symlink text without following the link; default mode also checks the index's symlink text. Gitlink names
are checked, but submodule contents are not. Deleted working files have no content in default mode.
UTF-16/UTF-32 text with a BOM is decoded. Without a BOM, a NUL byte in the first 8 KiB marks binary content,
which is not scanned; its name is still checked.

`--history` scans `git log -m -p --all`, including merge-parent diffs, messages, author lines and ref names.
Git C-quoted paths are decoded before matching. Wrapped private terms are checked within each text file,
commit message and each side of each diff hunk, never by joining separate files, hunk sides or metadata.
History is a text-diff scan, not a complete binary/archive scan: an unchanged part outside a diff hunk is
not examined in that commit. Commits and repeated merge-parent hits are counted once.

The scan reuses the evidence checker's read-only Git boundary, disables replacement objects and lazy
fetching, and refuses partial clones or unreadable Git configuration. It never repairs history. Exit 0
means no detected hits in the selected scope, 1 means hits, and 2 means the scan could not complete.
A hit prints `path:line: category` with at most a masked excerpt.

The committed patterns are generic: absolute home-directory paths, default Windows host names, e-mail
addresses (examples and no-reply forms excepted) and `.ts.net` hosts. Private terms live one per line in
the gitignored `.public-scan.local` (or `--terms FILE`); they are reported only by their position in that
file. `allow: PATH = TERM` accepts that term only in the exact file path and its name, in tree and history
modes. Accepted hits are counted. File allowances never suppress author, commit-message or ref findings.
Keep that terms file private. A clean scan does not establish that an artifact is safe to publish.

> Status: pre-release.
