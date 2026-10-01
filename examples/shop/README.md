# examples/shop

A small fictional system, "a little shop": a storefront (`web`), an `api`, a job `queue`, a fulfilment `worker`,
a `db`, and an external `payments` provider. `atlas.json` describes them; `src/` holds the tiny stubs that the
evidence points at; `journal.jsonl` and `events.jsonl` are sample records (40 lines each).

Run these from the repository root.

```
python -m estate_atlas check examples/shop/atlas.json --repo shop=examples/shop
python -m estate_atlas route examples/shop/atlas.json --journal examples/shop/journal.jsonl --events examples/shop/events.jsonl
python -m estate_atlas explain examples/shop/atlas.json worker
```

- **check** proves each reference. By default it reads `origin/<default branch>` of the checkout, and the shop's
  remote is a placeholder, so this checkout is reported as unresolved (exit 0, status `partial`). Add `--worktree`
  to read the files in the working tree instead and see all references proven.
- **route** prints an `estate-atlas-traffic@1` document, taken at the time of the newest record so the answer
  never goes stale. In it, `api-to-queue`, `queue-to-worker`, `api-to-db` and `web-to-api` are hot,
  `worker-to-db` is warm, `worker-heartbeat` shows as pulse (heartbeats are not traffic), and `api-to-payments`
  is a live flow with a rule and no crossings, so it is listed as silent. `web-to-api` has no rule, so its
  crossings are inferred from the components' instances.
- **explain** describes one component in plain words; add `--traffic` with the saved output of `route` to include
  volumes.
