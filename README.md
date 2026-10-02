# estate-atlas

**Your architecture as checked data.** You describe the components, contracts and flows of your system in one JSON
file. estate-atlas then:

- **proves** each claim against your git repositories, so the map can't quietly drift from the code;
- **lights** each flow with real traffic from your logs and events, so you see which wiring is alive, which is
  silent, and which records cross no flow at all;
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

> Status: pre-release.
