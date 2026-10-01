# Fictional human-workspace integration

This example is a design scenario, not a deployment inventory. Every component, contract and flow is **planned**. Repository names are fictional and there are no host paths, credentials, traffic samples or runtime evidence.

The workspace owns human commitments and authoritative operation receipts. An assistant carries captures; a runtime owns execution attempts; a cockpit observes those attempts; a Focus surface owns attention membership and order. A successful attempt is not automatically a completed human commitment. Source facts are not a priority queue.

Run from the repository root:

```sh
python -m estate_atlas validate examples/workspace/atlas.json
python -m unittest discover -s tests -p test_workspace_example.py -v
```

The tests validate the existing model and renderer, separate the contract owners and reject a dangling target or missing planned-flow gap. They do not test a real adapter, authorization, event delivery, user journey or traffic match. A map with zero observed traffic is not evidence of a healthy connection.

A consumer can use this structure in its own private canonical atlas. This example does not alter that atlas, the package defaults, the schema or any runtime. Promote each private flow only after its actual producer/consumer evidence and observation vocabulary are qualified; apply the consumer's privacy overlay before publication. Keep planned functionality visibly planned.
