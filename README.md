# Yatagarasu

**A comms fabric for persistent AI agent sessions.**

Yatagarasu routes messages between long-lived agent sessions that live in
different places — different harnesses, different hosts, different surfaces —
without any of them needing to know how the others are reached.

The caller addresses **who**, never **how**.

> Named for the three-legged crow: a guide and a messenger. Three legs, three
> data planes. **A messenger, never an oracle** — the fabric carries messages and
> proves delivery; it never speaks on anyone's behalf.

## Why

Agent-to-agent messaging usually degrades into one of two things: a shared log
nobody reliably reads, or a stateless bot that loses identity between turns.
Yatagarasu assumes the opposite constraints:

- **Sessions are persistent and identity-bearing.** A message is delivered *into*
  an existing session as a real, attributable turn — not to a fresh context.
- **Delivery is real-time**, a push stream rather than a polled log.
- **Receipts are evidence-bound.** The fabric never claims a message was
  delivered, entered a session, or was processed without proof of that specific
  claim. Unprovable states stay visibly unproven rather than being optimistically
  upgraded.

## Architecture in one paragraph

A **dumb core** (routing, identity, receipts, queueing) plus **smart plugins** on
both sides. *Session-transport* plugins deliver into a live agent session and can
prove session entry. *Comms-view* plugins present messages on a human platform and
consume derived state. The core never emulates a capability a plugin lacks — an
absent capability is explicit, and its absence is visible.

See [ARCHITECTURE.md](ARCHITECTURE.md).

## Status

**Pre-alpha. Round 1 is core + the CMUX session-transport plugin.**
Design is complete and gate-approved; implementation is starting.

Round 1 ships behind a build gate with binding conditions — including that a
specific tracer test must pass *before* build opens, and that the full acceptance
suite must pass before build closes. A partially-run suite is treated as no
evidence at all.

## Layout

| Path | What |
|---|---|
| `core/` | routing kernel, event schema, receipt reducer, the three data planes |
| `plugins/cmux/` | session-transport plugin for cmux-hosted agent sessions |
| `plugins/agent-bridge/` | compatibility surface over the core's send path |
| `plugins/discord/` | comms-view plugin (Round 2) |
| `tests/` | acceptance suite |
| `docs/` | architecture, design records, decisions |

## Licensing

[MIT](LICENSE). Use it, fork it, ship it.

## Status, plainly

**Nothing here runs yet.** There is no entry point, no service, and no
deployment — the modules have never touched a live socket. What exists is a
receipt reducer, a session-transport injector, a durable injection journal, and
an acceptance suite that is honest about its own gaps: tests that cannot yet be
proven are **skipped with a tracked issue**, never quietly passed.

The only thing you can run today is the test suite:

```bash
make install   # dev deps + the four editable subpackages
make check     # exactly what CI runs: lint, format, tests
```
