# core

**Role:** core kernel

Routing, identity resolution, the receipt reducer, mailbox/TTLs, roster snapshots. Owns no platform specifics.

The first implemented vertical slice is the evidence-bound receipt reducer. It
persists `delivery_mode` on every delivery and supports both canonical paths:

```text
session-bound:  transport-submitted -> in-session -> processed
channel-native: transport-submitted -------------> processed
```

Session-bound advancement is proof-bearing. A session transport must register one
active `SessionBinding` with its allowed `ProofMethodRegistration` values. The
core mints a short-lived `DeliveryMarker`; `harness.prompt_accepted` and
`harness.turn_completed` receipts then carry a `SessionProof` containing the
authoritative session ID, marker, and ordered content-free `SourceEventRef` chain.
The reducer rejects missing, forged, expired, stale-binding, wrong-provider,
wrong-session, copied-delivery, and out-of-order evidence without advancing the
delivery. A Stop receipt must close the exact prompt chain previously accepted.

Markers are conversation-scoped correlation, not credentials. Raw prompt text is
never represented by the core proof types or stored in receipt/audit tables.

`BroadcastKernel` is the Round-1 group primitive. It atomically records one
canonical event, freezes the room roster at acceptance, and creates one queued
delivery per resolved recipient. A registered seat without a live authoritative
binding still receives a durable queued row with visible absence; later roster
changes do not rewrite the snapshot. `BroadcastResult` always exposes the
literal per-seat matrix, and `all_delivered` remains false while any row is
queued or dispatching.

Run its contract tests from the repository root:

```bash
PYTHONPATH=core python -m unittest discover -s core/tests -v
```

Round 1 exercises the channel-native contract with a fake comms-view provider and
the session-bound contract with content-free CMUX event references; the Discord
plugin remains Round 2. See ../ARCHITECTURE.md and ../CONTRIBUTING.md.
