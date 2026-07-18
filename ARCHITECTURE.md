# Architecture

Yatagarasu is a **dumb core with smart plugins on both sides**. The core owns
identity, routing, receipts, and queueing. Plugins own everything
platform-specific — how a message is delivered, how it is presented, and what
evidence that platform can honestly produce.

## The layer model

| Layer | Owns | Never does |
|---|---|---|
| **Core** | canonical events, identity/seat resolution, the receipt reducer, mailbox + TTLs, roster snapshots | speak on anyone's behalf; emulate a capability a plugin lacks |
| **Session-transport plugin** | delivering into a live agent session; proving session entry | present to humans; manufacture participant evidence |
| **Comms-view plugin** | presenting messages on a human platform; ingesting authored replies/reactions | prove session entry (it structurally cannot) |

**Capability absence is explicit.** If a plugin cannot do something, the core does
not paper over it. The gap is visible in the audit record rather than hidden by an
optimistic default.

## The three data planes

1. **Delivery queue** — the mailbox. One event fans out to one delivery per
   recipient. TTLs bound how long unprocessed and processed content is retained.
2. **Audit log** — what happened, with evidence. Retention is configurable and
   bounded; it is an operational record, not a surveillance archive.
3. **Telemetry** — aggregate health only. Never person-level.

Three planes, three legs. They are deliberately separate: conflating them is a
defect class, not a shortcut, because each has a different identity key and a
different retention rule.

## Identity

Identifiers are **not** interchangeable, and collapsing them is a bug:

| Identifier | Scope |
|---|---|
| `event_id` | the canonical event, once |
| `delivery_id` | one recipient's copy of that event — a broadcast has many |
| `attempt_id` | one delivery attempt |
| `receipt_id` | one piece of evidence about an attempt |
| `source_event_id` | a platform-native ID, for ingress dedupe |
| `correlation_id` | a caller-supplied thread key, reusable |

> **Why this matters concretely:** keying an idempotency journal on `event_id`
> would treat the second recipient of a broadcast as a duplicate and silently drop
> it. Fan-out and idempotency are different questions and need different keys.

## Receipts: evidence, not optimism

A delivery advances through states **only** on evidence that proves that specific
transition. The core maps an *evidence class* to a *maximum transition*; a caller
never asserts a state directly.

```
queued → dispatching → transport-submitted → in-session → processed(...)
```

Two delivery modes:

- **`session-bound`** — the full chain. Used where a real session exists and
  session entry is provable.
- **`channel-native`** — `transport-submitted → processed`, skipping `in-session`,
  which is *structurally unreachable* on a platform with no agent session. Allowed
  **only** on authenticated authored ingress (a participant actually replying or
  reacting), correlated through an exact platform-message binding. The audit record
  states the session entry was not applicable rather than inventing one.

**What is never sufficient evidence:** a router accepting a request, an HTTP 2xx,
an egress success, a read receipt, an infrastructure-generated reaction, or a bare
turn ending. A turn that ends proves *completion*, not that anyone answered.

`proof_method` is recorded separately from evidence class, so proof relayed
through a trusted bus is distinguishable in audit from proof issued directly —
without inventing a parallel taxonomy.

## Delivery safety

- **Address by identity; re-resolve every send.** Surface/pane handles are
  ephemeral and change across restarts. A cached handle fails *silently while
  looking successful*, which is the worst available failure mode.
- **A fired local side effect cannot be undone by a remote retry.** A transport
  that has already delivered into a session keeps a durable journal, writes its
  intent record *before* the effect, and on ambiguous recovery holds and surfaces
  rather than re-delivering.
- **Replay rebuilds state, never delivery.** Resuming a source stream after a gap
  re-derives evidence; it never re-injects content.

## Design principles

1. **The messenger never becomes the oracle.** The fabric carries and proves. It
   does not answer, summarize, or act on anyone's behalf.
2. **Unprovable is visible, not upgraded.** A participant that cannot prove a state
   stays below it, in the open.
3. **No silent success.** Every failure mode that could look like success from the
   inside gets an explicit negative test.
4. **Identities and roster are configuration, not code.** Nothing in `core/`
   assumes a particular deployment.
