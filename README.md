# estate-atlas

**Your architecture as checked data.** You describe the components, contracts and flows of your system in one JSON
file. estate-atlas then:

- **proves** each claim against your git repositories, so the map can't quietly drift from the code;
- **lights** each flow with real traffic from your logs and events, so you see which wiring is alive, which is
  silent, and which records cross no flow at all;
- **draws** it: an offline HTML atlas, mermaid, an SVG overview, and Markdown blocks your docs can embed and CI can
  check;
- **explains** every part in plain English, from facts only.

Standard-library Python. No dependencies. GPL-3.0-only.

```
python -m estate_atlas check examples/shop/atlas.json --repo shop=examples/shop
python -m estate_atlas route examples/shop/atlas.json --journal examples/shop/journal.jsonl --events examples/shop/events.jsonl
python -m estate_atlas explain examples/shop/atlas.json worker
```

Design: [docs/DESIGN.md](docs/DESIGN.md).

> Status: pre-release.
