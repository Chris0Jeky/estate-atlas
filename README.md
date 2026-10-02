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

```
python -m estate_atlas check examples/shop/atlas.json --repo shop=examples/shop
python -m estate_atlas route examples/shop/atlas.json --journal examples/shop/journal.jsonl --events examples/shop/events.jsonl
python -m estate_atlas explain examples/shop/atlas.json worker
python -m estate_atlas tour examples/shop/atlas.json --md --overlay tests/fixtures/shop-overlay.json
```

Design: [docs/DESIGN.md](docs/DESIGN.md).

Consumer proof and its limits: [docs/QUALIFICATION.md](docs/QUALIFICATION.md).

> Status: pre-release.
