---
status: draft
class: project
created_at: 2026-07-18
last-verified: 2026-07-18
description: "Yatagarasu CMUX session-transport plugin (Round 1) design — agent-bridge no-regression migration + broadcast + queue primitive, on NATIVE cmux event-bus receipts (in-session/processed verified live, not built); conforms to Yua's core contract 01-core.md."
tags:
  - harem-ops/project
sources: []
---

# Yatagarasu — CMUX session-transport plugin (Round 1) design

**Owner:** Aoi
**GATE: design** — requires team cross-review, then Eric approval, before build.

Conforms to Yua's core contract (`01-core.md`). This is the **first
session-transport plugin**, so building it against the core is also the
cross-check that the core's plugin API is sufficient. Where this design needs
something the core doesn't yet offer, it is flagged as a **core dependency**, not
worked around.

## Purpose & scope

The CMUX plugin delivers a resolved core event into an identity's **one
authoritative session** by injecting it into that identity's live CMUX pane —
the proven `agent-bridge` primitive (`surface.send_text` + Enter, queued as the
next turn if the pane is busy). Round 1, on Vesper only.

**Round 1 delivers:**
1. **No-regression `agent-bridge` migration** — the existing
   `agent-bridge send <agent> <msg>` surface keeps working *identically* while its
   guts move under the core. Prove parity before anything new.
2. **`broadcast`** — the core's one-event/one-roster-snapshot/per-seat-delivery
   primitive, injected into each resolved pane, with a per-seat outcome matrix.
3. **Queue primitive integration** — the core owns the mailbox; this plugin
   claims/accepts deliveries and reports honest transport state for present,
   busy, and absent seats.

**Explicitly NOT Round 1:** Discord/Gateway (Shiori), remote-host adapters,
focus/away digests, floor-lease execution, reactions rendering beyond what a pane
supports.

## Conformance to the core plugin contract

Declares to the core:
- **kind:** `session-transport`
- **`delivery_mode`: `session-bound`** (Yua, `b580bf3`) — this plugin keeps the
  strict chain `transport-submitted → in-session → processed`. The
  `channel-native` shortcut exists for comms-view plugins and **never applies
  here**: a cmux seat can always prove session entry, so it must.
- **host-locality:** Vesper-local; connects **outbound** to the core; never
  accepts inbound. (A disconnected edge is visibly absent — no proxy.)
- **capabilities:** `text`, `reply`, `presence` (basic), and — the load-bearing
  one — `session-proof` at whatever evidence class the harness actually supports
  (see Receipts).
- **`reconnect-replay`: `true`** — the plugin resumes and replays its source stream
  after disconnect or gap, via its `(source_instance_id, boot_id, seq)` cursor.
  *Trap: do not set this `false` to mean "replay never re-drives injection." That is
  a different guarantee (stated below) and overloading the capability name hides a
  real capability behind a wrong label.*
  > **Replay never re-drives delivery.** Resuming the source stream rebuilds
  > *evidence and state*; it never re-injects into a pane. On gap: re-snapshot, do
  > not reinject. Local restart safety is the plugin's own injection journal — the
  > core cannot see an injection that already fired, so core dedupe cannot cover it.
- **actor/seat claims:** may bind only the Vesper seats it is credentialed for.
- **receipt evidence classes it can honestly emit** — *these are provider-emitted
  **classes**, not reducer **states**; `transport-submitted` / `in-session` are what
  the core computes and must never appear here.* Canonical classes:
  - `transport.submit_ack` — always;
  - `harness.prompt_accepted` — where the harness relays a prompt-accepted hook
    (Claude, Codex live-observed; hermes pending emission check);
  - `harness.turn_started` — supported where the harness distinguishes it;
  - `harness.turn_completed` — correlated turn-end.

  `proof_method` (`cmux.event_bus.harness_hook_relay`) is recorded **separately** and
  is never itself an evidence class. This plugin emits **no** `participant.*` class
  under any input.

## Delivery mechanism

1. Core resolves `identity → seat → authoritative binding` and hands this plugin
   a queued delivery for a seat it owns.
2. Plugin resolves the seat's **CMUX surface/pane** by **identity, re-resolved on
   every send** — never from a cached surface ID (see the hard rule below).
3. Injects the rendered event into the pane (`surface.send_text` + submit),
   **preserving the `[FROM/TO/TYPE/CID]` envelope and the `authority_scope`
   marker** so the receiving session sees a real, attributable turn — never a
   stripped or router-authored one.
4. Busy pane → the native TUI buffer queues it as the next turn (proven this
   session: mid-turn messages never clobbered an active turn). This is what lets
   a held-line survive — the injected turn can be absorbed into a hold.

### HARD RULE: address by identity, re-resolve every send, never cache a surface

**Surface IDs are not stable across session restarts.** Proven live 2026-07-18:
Eric restarted sessions mid-incident and Tama's seat moved `surface:33 → surface:3`.
Delivery still succeeded **only because `agent-bridge` re-resolves by agent name on
every send.** Any component that had cached `surface:33` would have injected into a
dead or reassigned pane — silently, with a successful-looking local result.

This makes Eric's founding constraint ("the caller addresses **WHO**, not HOW") a
**correctness** requirement, not an ergonomic one. Consequences:
- The plugin caches **no** surface/pane binding between sends.
- Session restart is a first-class event: detect via `agent.hook.SessionStart` and
  via `boot_id` change on the event stream (cmux app restart), then re-resolve.
- A resolution that yields a *different* surface than last time is normal, not an
  error; a resolution that yields **none** is visible absence, never a silent drop.

### Focus and threading discipline (cmux-socket-policy)

Per cmux's own socket policy, which this plugin must obey:
- **Socket commands must not steal macOS app focus.** Delivering a message must
  never yank Eric (or any agent) to the receiving pane. Only explicit focus-intent
  commands may move focus — delivery is not one of them.
- **Telemetry-rate work stays off the main actor.** Receipt/event processing is a
  hot path; parse, dedupe, and coalesce off-main, touching the model only when
  needed.

## Receipts — largely NATIVE

**`in-session` proof does not need to be built — cmux already emits it.** Verified
against live traffic 2026-07-18.

cmux exposes a reconnectable, sequenced event stream (`events.stream` over the
socket, mirrored to `~/.cmuxterm/events.jsonl`). Every agent hook fires as a
two-phase event (`received` → `completed`) carrying **payload `session_id`,
`workspace_id`, `cwd`, and `tool_name`**. Verified firing live for **both** Claude
and Codex seats, 2026-07-18.

> **INVARIANT — `agent.hook.*` events carry NO `surface_id`.**
> `CmuxEventPublishing.publishWorkstreamEvent` passes `workspace_id` and a payload
> `session_id`; the envelope's `surface_id` is **`null`** on every agent-hook frame.
> **Correlate a hook event to a surface through the binding/session registry, never
> by reading a `surface_id` field off the event.** *Trap: the field exists in the
> envelope and is simply null, so code that reads it gets a plausible-looking
> `None` rather than a missing-key error.*

| Core state | CMUX evidence | Evidence class (Yua, ruled) |
|---|---|---|
| `queued` | (core-owned) | mailbox committed |
| `dispatching` | plugin holds the delivery lease | one attempt at a time |
| `transport-submitted` | `surface.input_sent` + the **exact marked** `workspace.prompt.submitted` | `transport.submit_ack` |
| `in-session` | matching `agent.hook.UserPromptSubmit` for the **bound** `session_id` | `harness.prompt_accepted` |
| `processed(completed)` | matching `agent.hook.Stop`, **no ambiguous intervening prompt** | `harness.turn_completed` |

**INVARIANT — `notification.read` is NOT a receipt state.** The chain is
`queued → dispatching → transport-submitted → in-session → processed`; `seen` is not
a member of it, and `notification.read` is named among the signals that **may not**
advance a receipt (`b580bf3`). It is a **plugin-local presentation signal only** — useful for
banner lifecycle and for telling Eric a banner was acknowledged. It never advances
`processed`, never substitutes for `in-session`, and never enters the receipt chain.

**All rows carry `proof_method: cmux.event_bus.harness_hook_relay`** — ruled by Yua
(`01-core.md:717`). The bus is a **trusted local relay, not the semantic issuer**:
the originating harness event determines the evidence *class*, and the relay path is
recorded separately as `proof_method`, so audit can distinguish bus-relayed proof
from a directly-issued hook without inventing a parallel class.

> **INVARIANT — `session_id` alone is NOT exact-delivery proof.** Proof requires
> **all three**: a **signed Yatagarasu marker**, the **complete ordered source-event
> chain**, and the bound `session_id` (`01-core.md:728`). *Trap: a `session_id`
> match proves that session took **a** turn, not that it took **our** turn — a
> concurrent injection, or Eric typing into the same composer, satisfies a
> session-only check.*

> **INVARIANT — `agent.hook.Stop` proves completion, never an answer.** It advances
> **only** `processed(completed)`, never `processed(answered)` or
> `processed(acknowledged)`. Authored-output dispositions require
> `session.reply_authored` / `session.reaction_authored` (`01-core.md:938`).
> *Trap: a turn ending does not mean the seat answered the event — it may have
> ignored it entirely, and the turn-end event looks identical either way.*

**What this replaces, and why it matters most.** The original `transport-submitted`
proof was `agent-bridge`'s `verify_submitted` — a **scrape of the target's composer
that is lenient on scrape failure (assumes submitted)**. That leniency is the
documented cause of 9+ silently-lost messages. The native pair above is a **true
negative** when it fails: if `workspace.prompt.submitted` never arrives for that
surface within the timeout, the send genuinely did not land. **Replacing the
lenient scrape with the event pair is the single highest-value change in Round 1.**

### The bus is NOT content-free

**INVARIANT — the event bus carries real prompt text and must be treated as
local-sensitive.** `workspace.prompt.submitted` carries `message_preview`, which
cmux's events doc labels local-sensitive and says consumers should forward only with
explicit user opt-in. That event is load-bearing for our `transport-submitted`
proof, so **the channel we depend on contains user content.**

*Trap: `tool_input` **is** redacted to `tool_input_length`, which makes the bus look
content-free on casual inspection. Generalizing from that one field to the whole bus
leaks previews into receipt/audit storage.*

**Binding rule (`01-core.md:729-731`):** the host-local provider **extracts the
signed Yatagarasu marker from the preview and discards the text**. Raw
preview/content **never leaves the host-local provider and never enters core receipt
or audit storage**. The marker — not the text — is what travels.

This is also *why* the marker requirement exists: the same field that makes exact
correlation possible is the field that carries private text, so extraction and
discard must happen in one place, locally, at the edge.

**Remaining gap — non-cmux hosts.** Any seat whose harness does not emit into a
cmux event bus stays visibly at `transport-submitted`. As of 2026-07-18 that is
**hermes** — no `hermes-hook-sessions.json` alongside the claude/codex/gemini/pi
records, and zero `source: hermes` events across 5000 sampled. Unconfirmed whether
it has simply not taken a turn since install; **must be settled before the receipt
floor is set.** Claude and Codex are confirmed wired.

**Core dependencies this still raises:** (a) an internal `submit-receipt` operation
the plugin calls with evidence class; (b) a binding-registration op carrying
`session_id` + proof-method; (c) the Round-1 receipt *floor* decided in
cross-review (Yua decision #6) — now a much smaller question, since the floor for
cmux-hosted seats is `processed`, not `transport-submitted`.

## The cmux event bus — consumption contract

The receipt evidence above arrives over a bounded, resumable stream. Consuming it
correctly is a design requirement, not an implementation detail:

- **Cursor identity is `(source_instance, boot_id, seq)` — never `seq` alone**
  Persist the **triple**, only after the side effect succeeds, and resume with it.
  *Trap: `seq` alone is process-local, so the same integer denotes different events
  on different resident instances under per-host federation.*
- **On gap: re-snapshot, and NEVER reinject.** Replay exists to rebuild *state*, not
  to re-drive *delivery*. Reinjecting from a replayed tail would produce duplicate
  model turns — the exact failure the journal below prevents.
- **Gap detection is explicit.** `ack.resume.gap == true` means the cursor fell
  outside the retained buffer (4,096 events in memory) or cmux restarted. On a gap:
  process the replayed tail, then **re-snapshot** (`extension.sidebar.snapshot`,
  `list-workspaces`, `tree`) rather than assuming continuity. This is exactly the
  pause/resume catch-up question from `01-core.md` — cmux solves it *within* the
  buffer and tells us honestly when it can't.
- **Dedicated reader connection.** `events.stream` **takes over its socket
  connection** — no commands may be multiplexed on it. The plugin needs a reader
  connection separate from its command connections.
- **Backpressure is fatal if ignored.** A subscriber that falls 1,024 events behind
  is disconnected with `slow_consumer`. The reader must never block on delivery
  work; reconnect from the last persisted `seq`.
- **`boot_id` change = cmux app restart** (distinct from an agent session restart).
  Both invalidate cached topology; see the hard rule above.

## Per-host federation

**Every host runs cmux**, so the plugin is installed **per host**, co-located with
that host's cmux socket and `events.jsonl`. *Trap: reasoning from a single cmux
instance suggests native receipts are a local-only fast path needing a degraded path
for remote seats. They are not — the plugin is a per-host resident.*

Therefore **native receipts are universal, not local-only**, and the receipt split
narrows to one leg:

| Leg | Who proves it |
|---|---|
| core → remote-host plugin instance (**network hop**) | Yatagarasu-native receipt — the *only* place one is required |
| plugin → local agent (inject → in-session → processed) | **native cmux, on every host** |

The plugin is necessarily a **per-host resident** (it reads a local Unix socket),
which is the same "persistent presence on every machine" shape the fabric wanted
anyway. The core federates resident instances; it does not reach into remote panes.

### Durable injection journal (required — Yua's cross-review finding)

**The plugin owns durable state**, and under federation the failure it prevents is
severe:

> The plugin injects into the local pane → the model turn **has already started** →
> the network ACK back to core is lost → core retries the delivery → **the message
> is injected a second time and the agent takes a duplicate turn.**

A local side effect that has already fired cannot be made idempotent by a remote
retry. So the resident **must** keep a durable **injection journal / outbox**:

**Keyed by `delivery_id`, never `event_id`** (Yua conformance finding 1). One
broadcast event fans out to many recipient deliveries; keying the journal on
`event_id` would treat the *second seat's legitimate delivery* as a duplicate and
suppress it — silently breaking broadcast, a Round-1 deliverable. The key is
`delivery_id`, carrying its `binding`/`seat`.

**Two-phase write — the `prepared` record goes down BEFORE injection** (Yua
conformance finding 2). *Trap: writing the journal entry after injecting but before
ACKing core still duplicates — a crash between the inject and the journal write
leaves no record, so recovery sees "never injected" and re-injects a message the
model already took.* Correct sequence:

1. **`prepared` / `effect_maybe_started`** — durable + fsync **before** touching the
   pane. Records `delivery_id`, binding/seat, and the marker.
2. **inject** into the pane.
3. **`injected`** — transition the record with the resulting ordered source-event
   refs, then ACK core.

**Recovery from `prepared`-without-outcome is the whole point.** That state means
"the effect may or may not have fired." Reconcile against the event bus / outbox to
find whether a marked `workspace.prompt.submitted` exists for that marker; if it
cannot be determined, **hold the delivery as ambiguous and surface it**. Never blind
re-inject from `prepared` — that is precisely the duplicate-turn failure.

- On redelivery of a known `delivery_id`, **do not re-inject** — re-emit the
  journaled receipt instead.
- The journal survives plugin restart, cmux restart, and core reconnect. It is one
  of exactly **three** durable items the plugin owns (journal, receipt outbox,
  event-stream cursor — enumerated under *Queue primitive integration*), and it
  exists solely to make at-least-once *network* delivery safe against exactly-once
  *local* injection.
- Entries retire on the core's mailbox TTL.

## Notification policy seam (presentation lives in the plugin)

cmux exposes a composable **notification hook chain** (`notifications.hooks` in
`cmux.json`): every notification is piped through as JSON on stdin, the hook returns
modified JSON on stdout, and the hook controls `effects` — `record`, `markUnread`,
`desktop`, `sound`, `command`, `paneFlash`, `reorderWorkspace`. Hook `context`
carries `appFocused` and `focusedPanel`.

This directly satisfies Eric's constraint that a sender **"just calls the
appropriate send, and whatever plugin is at the other side presents the message"**:

- **Message type → presentation policy, in the plugin, natively.** `WARN` →
  `desktop + sound + paneFlash`; `INFO` → `record` only, silent. No cmux patching.
- **`appFocused` / `focusedPanel` = a HUMAN-attention signal, for presentation
  only.** It answers "is Eric already looking at this?" — so a banner does not
  interrupt someone who is already reading. It is **not** an agent-availability
  signal. See the correction below.

> **The UI-focus fallacy.**
> **INVARIANT — UI focus is never a presence input.** `appFocused` /
> `focusedPanel` drive **presentation only**. Agent presence (`live` / `busy` /
> `away`) derives **exclusively from harness event-loop state**
> (`UserPromptSubmit` / `PreToolUse` → busy; `Stop` → idle).
> *Trap: human TUI focus and agent cognitive availability are independent. A seat's
> pane can be the focused panel on screen while that agent's model is blocked
> mid-tool-call for minutes, and a hidden background seat can be idle and ready.
> `appFocused` measures where the human is looking, never whether the agent can
> take work.*
- **Fails safe.** A hook that errors, times out, or emits invalid JSON falls back to
  default behavior *and* posts a failure alert — the transport cannot silently
  swallow a message.

**Required config — `notifications.suppressOnlyFocusedSurface: true`.** By default
cmux withdraws a delivered banner when its workspace becomes visible, which can
retract the banner for a **non-focused** surface (a second agent in the same visible
workspace) before anyone notices. For a multi-agent fabric where several seats share
a workspace view, that is **silent message loss**. This flag scopes auto-withdraw to
the exact focused surface.

### Banner lifecycle — the receipt IS the clear trigger

> **The banner leak this fix creates.** Setting `suppressOnlyFocusedSurface: true` stops the silent
> withdrawal, and in exchange **guarantees a background seat's banners never
> auto-clear** — that seat is never focused, so the auto-withdraw never fires. In a
> chatty room, a background seat accumulates stacked banners indefinitely.
> *Trap: solving message loss without also solving banner clearing does not remove
> the defect, it relocates it.*

The clear trigger must be **deterministic and agent-driven, never human-driven** —
nothing should require Eric to focus a pane to tidy up. We already have the exact
signal: **the `in-session` receipt.**

- On delivery, the plugin records `event_id → notification_id`.
- When `agent.hook.UserPromptSubmit` correlates that `event_id` (the same event that
  advances the receipt to `in-session`), the plugin **actively dismisses that
  notification** (`dismiss-notification --id <notification_id>`).
- The agent *ingesting* the message is what clears its banner. Delivery proof and UI
  cleanup are the same event, so they cannot drift apart.
- A seat that never reaches `in-session` (no event bus) keeps its banner — correct:
  that banner is the visible evidence of an unproven delivery, not litter.
- Bound the residual: cap retained banners per seat and age out beyond the mailbox
  TTL, so a permanently-unproven seat still cannot grow without limit.

**QA:** extends `Y-CMUX-007` — assert the non-focused banner survives until its
`in-session` receipt lands, then assert it is dismissed within the timeout, and that
N delivered-and-ingested messages leave zero residual banners.

## No-regression migration (the care point)

`agent-bridge send <agent> <msg>` is the team's live comms — it cannot break mid-
build. Approach:
1. Stand the core + CMUX plugin up **alongside** the current agent-bridge.
2. Re-implement `agent-bridge send` as a **thin compatibility shell** over the
   core's `send` (identity-addressed), mapping legacy `CID` → `correlation_id`
   (per Yua's CID correction) and legacy `--type` → event `type`.
3. **Parity proof before cutover:** a golden-path test that the legacy surface
   produces identical delivery behavior (same pane, same envelope, same SENT
   semantics) old-vs-new. Only cut over when parity is green.
4. Keep `chair-msg` durable-fallback semantics intact (out of Round-1 scope to
   change, but must not regress).

## Broadcast

`broadcast(actor, room_id, content, …)` → core records one event + one roster
snapshot + per-seat deliveries; this plugin injects into each present seat's pane
and reports a **per-seat outcome matrix**. An absent seat's delivery stays
`queued` (visible), never a false "all delivered." This is the group primitive
that lets Round 1 prove fan-out **without Discord**.

## Queue primitive integration

The core owns the mailbox and TTLs (Yua's 24h unprocessed / 1h processed
proposal). This plugin: claims deliveries for its seats, injects, reports state,
and for a busy/away seat leaves the delivery queued so it's caught up when the
seat returns.

> **Restart recovery is JOINT**, not core-only: the core restores the mailbox, the
> plugin restores idempotency and resume position.

**The plugin owns a narrow, enumerated set of durable state** — narrow because
every item exists to protect a *local* side effect that the core cannot see:

| Durable item | Key | Why the plugin must own it |
|---|---|---|
| **Injection journal** | `delivery_id` (+ binding/seat) | Pane injection is a local effect that has already fired; a remote retry cannot undo it. Prevents duplicate model turns. (`:260-301`) |
| **Receipt outbox** | `receipt_id`, with exact `(event_id, delivery_id, attempt_id, binding_id)` | Receipts earned locally must survive a core disconnect, or proven deliveries silently regress to unproven. **Keyed `receipt_id`, not `delivery_id`** (Yua, `da6d96f`): one delivery can legitimately yield several receipts across attempts and evidence classes, so `delivery_id` alone would collapse distinct receipts into one. |
| **Event-stream cursor** | `(source_instance_id, boot_id, seq)` | Resume position is per resident instance; `seq` alone collides across hosts. Gaps re-snapshot and never reinject. |

**Everything else stays core-owned:** the mailbox itself, TTLs, retry policy,
roster snapshots, and cross-seat state. The plugin's durable state is deliberately
**not** a second mailbox — it is an idempotency and resume record, nothing more,
and it retires on the core's mailbox TTL.

> **Three planes, three keys — do not collapse them** (Yua's Y-CMUX-015 ruling,
> `da6d96f`). These identifiers are *not* interchangeable, and conflating them is
> its own defect class:
> - **injection / no-second-turn** → `delivery_id` (+ binding/seat context);
> - **stream replay & dedupe** → native `(source_instance_id, boot_id, seq)` /
>   source-event-id — **not** `delivery_id`; this plane is about the event bus's own
>   identity, not about a delivery;
> - **derived receipt / outbox idempotency** → `receipt_id` + the exact tuple above.
>
> My own first analysis of this proposed replacing `event_id` with `delivery_id`
> *throughout*, which was directionally right about the defect and **wrong about the
> fix** — it swaps one over-broad key for another and would have mis-keyed the
> stream-replay plane. Canonical `event_id` cannot key injection or outbox recovery;
> that does not make `delivery_id` the universal answer.

## Presence (Round-1 minimum)

Only `live` / `disconnected` + queueing, per Yua's Round-1 slice. `live` = pane
resolvable + binding healthy; `disconnected` = pane/route gone (visible absence).

**Presence is also largely native**, which makes the deferred `focus` state cheap
when we want it (all verified firing 2026-07-18):
- `sidebar.metadata.updated` — the per-seat status pill (`Running` vs idle), driven
  by the harness's own turn lifecycle. Writable by us too via `cmux set-status` /
  `clear-status`, so Yatagarasu presence can render **inside each seat's sidebar**.
- `window.keyed` / `window.unkeyed` + `surface.focused` — which seat is on **Eric's
  screen**. This is a *human*-attention fact and is **explicitly NOT a presence
  input** (see the UI-focus-fallacy correction under Notification policy seam). It
  may inform presentation; it must never set `live` / `busy` / `away`.
- **Agent presence derives from harness event-loop state only:**
  `UserPromptSubmit` / `PreToolUse` → `busy`; `Stop` → idle/`live`; session gone →
  `away`. A seat is available or not because of what its *model* is doing, never
  because of where a window manager put it.
- `~/.cmuxterm/<agent>-hook-sessions.json` — cmux already maintains
  session → workspace/surface with lifecycle (`running` / `idle` / `needsInput` /
  `unknown`). **We read cmux's binding table rather than maintaining our own.**

Round 1 still ships only `live` / `disconnected`; this is recorded so `focus` and
`away` are a wiring job later, not a build.

## Open questions for cross-review

1. **✅ RULED (Yua, `01-core.md:614-631`, `:717-731`).** Evidence class stays
   `harness.prompt_accepted` — no parallel class invented — with the relay path
   recorded separately as `proof_method: cmux.event_bus.harness_hook_relay`. The bus
   is a trusted local relay, not the semantic issuer. Three corrections to this
   design came with it, all now folded in: `session_id` alone is insufficient
   (signed marker + ordered source-event chain required); `Stop` advances only
   `processed(completed)`, never `answered`; and the bus is **not** content-free.
2. **Round-1 receipt floor** — cmux-hosted seats can prove `processed(completed)`,
   not merely `transport-submitted`. Claude + Codex are live-observed producers.
   Open only for **hermes**. (Yua decision #6.)
3. **Hermes event coverage — narrowed, not closed.** Hooks **are** installed:
   `~/.hermes/config.yaml` registers real `cmux hooks hermes-agent` commands
   including **`prompt-submit`** and **`agent-response`**, which map to
   `harness.prompt_accepted` and `harness.turn_completed`. So hermes is **not
   structurally excluded** from the floor. But zero `source: hermes` events have
   been observed. The hooks are guarded by `[ -n "$CMUX_SURFACE_ID" ]`, so they
   no-op outside a cmux-managed surface — the likely cause. **Operational check,
   not a design limit:** confirm emission on one observed hermes turn before the
   floor is set. Until observed, hermes stays at `transport-submitted`
   (`01-core.md:733`).
4. **Compatibility surface** — keep the `agent-bridge send` CLI name as the shell,
   or introduce a `yata send`/`ygr send` name and alias the old one? (Naming +
   muscle-memory tradeoff.)
5. **Reader topology** — one events reader per host shared by all seats on that
   host, or one per seat? (One per host is cheaper and matches the resident model;
   confirm against the core's plugin-instance assumptions.)
6. **CORE DEPENDENCY — must the reducer allow `transport-submitted` → `processed`,
   bypassing `in-session`?** Raised by Shiori (cross-review, finding 1) and it is a
   real break, not a CMUX-side issue. A **Discord-only participant** (a human in the
   channel, or an agent with no cmux seat) can *never* produce `in-session` — there
   is no harness hook to prove entry into a context window. But when they reply or
   react, that ingress **is** definitive proof of processing. `01-core.md:517-519`
   states the chain as `transport-submitted → in-session → processed`; if that
   ordering is strictly enforced, **every Discord-only participant breaks the
   reducer the moment they speak.** Yua's call: permit a documented comms-view jump
   `transport-submitted → processed(answered|acknowledged)` that skips `in-session`,
   with the evidence class naming why the intermediate state is unreachable.
   **✅ RULED (Yua, `b580bf3`).** Deliveries now declare
   `delivery_mode = session-bound | channel-native`.
   - **`session-bound`** (this plugin) keeps the strict chain
     `transport-submitted → in-session → processed`. **No shortcut applies to us.**
   - **`channel-native`** (Discord/comms-view) may transition
     `transport-submitted → processed` **only** on authenticated
     `participant.reply_authored` or `participant.reaction_authored` ingress,
     correlated through the exact platform-message binding. Audit must record
     `session_entry = not_applicable` and **may not synthesize `in-session`**.
   - **Explicitly barred from the shortcut:** router accept, Discord egress success,
     `notification.read`, infrastructure reactions, generic bot activity.

   The no-inflation principle survives intact: the jump requires *authenticated
   authored ingress* — a participant actually speaking — never mere delivery
   success, and the audit records the missing session entry honestly instead of
   faking it. Contract-only Round-2 seam; **no code authorized.**

## Acceptance hooks (for Tama's QA)

**Owner:** Tama. **Moved 2026-07-18** to
[[projects/active/yatagarasu/design/02-cmux-plugin-acceptance-hooks]] when this
page hit tsumugi's hard 1500-line cap. Pure extraction, no content change.

**18 hooks (Y-CMUX-001..018).** Each carries literal-observation fields, a verdict
block *outside* the data grid (per `substrate-honest-probe-output` — the grid prints
observations, never classifications), an adversarial fixture where applicable, and a
negative assertion naming the SEV-1 reopen condition.

Load-bearing ones to know by name:
- **Y-CMUX-002** — `transport-submitted` requires BOTH events; suppress the composer
  submit and assert a **true negative**. Regression test for the 9-lost-messages class.
- **Y-CMUX-003** — no cached surface binding; restart re-resolves by identity.
- **Y-CMUX-008/012** — receipts advance only on the assembled proof **bundle**;
  turn-end proves `processed(completed)` only, never `answered`.
- **Y-CMUX-016** — preview-leak: raw `message_preview` never reaches receipt, audit,
  journal, cursor, retry queue, or error log.
- **Y-CMUX-017** — duplicate-turn: drop the network ACK, assert `model_turn_count == 1`.
- **Y-CMUX-018** — NEGATIVE capability boundary: CMUX declares `session-bound` only
  and never emits `participant.*` evidence. (Positive channel-native fixtures moved
  to Discord/comms-view + core QA per Yua's plugin-boundary ruling.)

## Risks

- **Receipt inflation** — materially reduced: evidence is sourced from cmux's own
  harness-hook events rather than a scrape we interpret. Residual risk is
  correlating the *wrong* turn; mitigated by requiring the **full assembled proof
  bundle** — signed marker + complete ordered source-event chain + bound
  `session_id` — never `session_id` alone (`01-core.md:728`), plus adversarial QA.
- **Duplicate model turn on retry** — a fired local injection cannot be undone by a
  remote retry. Mitigated by the durable injection journal keyed on **`delivery_id`**
  (never `event_id`, which would suppress legitimate broadcast fan-out), with the
  `prepared` record fsynced *before* injection and ambiguous recoveries held rather
  than blind-reinjected.
- **Silent delivery loss via stale surface binding** — the highest-severity
  failure mode found, and it looks like success locally. Mitigated by the
  re-resolve-every-send hard rule + the restart acceptance test.
- **Reader falls behind → `slow_consumer` disconnect** — receipts stop arriving
  while delivery still appears to work, so seats silently stall at
  `transport-submitted`. Mitigated by off-main reader, persisted cursor, reconnect,
  and an alert if a reader is disconnected longer than a threshold.
- **Hermes has no observed event coverage** — those seats cannot prove
  `in-session`/`processed`. Mitigated by settling the wiring question before the
  floor is set, and by keeping unproven seats visibly below the floor.
- **Silent banner withdrawal** for a non-focused seat in a shared workspace —
  mitigated by requiring `suppressOnlyFocusedSurface: true`.
- **No-regression break** — mitigated by parity-proof-before-cutover and running
  alongside the legacy path until green.

## Gate & handoff

DESIGN draft only — authorizes no code, no `agent-bridge` change, no cutover.
Next: cross-review (Yua for core-conformance, Shiori for plugin-contract
symmetry, Tama for the acceptance hooks) → Eric approval → BUILD.
