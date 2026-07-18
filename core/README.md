# core

**Role:** core kernel

Routing, identity resolution, the receipt reducer, mailbox/TTLs, roster snapshots. Owns no platform specifics.

The first implemented vertical slice is the evidence-bound receipt reducer. It
persists `delivery_mode` on every delivery and supports both canonical paths:

```text
session-bound:  transport-submitted -> in-session -> processed
channel-native: transport-submitted -------------> processed
```

Run its contract tests from the repository root:

```bash
PYTHONPATH=core python -m unittest discover -s core/tests -v
```

Round 1 exercises the channel-native contract with a fake comms-view provider;
the Discord plugin remains Round 2. See ../ARCHITECTURE.md and ../CONTRIBUTING.md.
