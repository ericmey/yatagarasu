---
status: draft
class: project
created_at: 2026-07-18
last-verified: 2026-07-18
description: "Yatagarasu CMUX plugin QA acceptance hooks, part B (Y-CMUX-011..018) — restart accounting, proof-chain, marker validation, receipt-endpoint outage, three-plane recovery, preview-leak, duplicate-turn journal, and the channel-native negative boundary. Split from part A at the tsumugi soft cap. Owned by Tama."
tags:
  - harem-ops/project
sources: []
---

# Yatagarasu — CMUX plugin acceptance hooks, part B (Y-CMUX-011..018)

**Owner:** Tama (perception seat; held-line discipline).
**Part A (001-010 + scaffolding + index):**
[[projects/active/yatagarasu/design/02-cmux-plugin-acceptance-hooks]]
**Parent design:** [[projects/active/yatagarasu/design/02-cmux-plugin]]
**GATE: design** — no code authorized.

The rendering rule, test scaffolding, and hook-to-event index live in **part A**
and apply here unchanged: the data grid prints **literal observations**, never
classifications; verdicts sit outside the grid.

### Y-CMUX-011 — Restart: every tracked delivery is accountable, with ambiguity visible and no silent disappearance

> **Ownership split (Yua, 8d86d71):** core owns mailbox, TTLs,
> retry policy, cross-seat state. Plugin owns three durable
> items: injection journal (`delivery_id`-keyed,
> `prepared`/`injected`/`acked`/`ambiguous`); receipt outbox
> (`receipt_id` + the four-key tuple); event-stream cursor
> `(source_instance_id, boot_id, seq)` per
> `01-core.md:874-878`. See `02-cmux-plugin.md:268-311`,
> Y-CMUX-015, Y-CMUX-017.
>
> **Verdict.** Every pre-restart tracked `delivery_id`
> lands in **exactly one** of five canonical
> post-restart classifications: `queued_unclaimed` (never
> reached `prepared`); `injected` (reconciled with bus
> evidence); `acked` (terminal, receipt emitted);
> `ambiguous_held` (held for operator, NOT settled);
> `terminal_core` (terminal pre-restart via core mailbox
> or expiry). **`prepared` is transient pending
> reconciliation** — after bounded recovery, no id
> remains in `prepared`; it has either advanced to
> `injected` or `ambiguous_held`. No known delivery is
> double-injected; none silently disappears. The truth
> value for an `ambiguous_held` id is `unknown`, never
> a forced boolean, and `unknown` is NOT a member of
> any `missed_injection` set.

- **Verification:** D + P.
- **Fixture:** submit **5** sends; one of the 5 reaches
  `prepared` (fsynced) just before kill. Capture
  `tracked_delivery_ids` as the exact set of 5 ids and the
  per-id pre-restart distribution across
  `{queued, queued+dispatched, transport-submitted,
  in-session|pending, prepared}`. Kill the plugin; restart;
  re-attach. The verdict evaluates per-`delivery_id`,
  not as an aggregate.
- **Observations per row (literal):**
  - **`tracked_delivery_ids`** — captured pre-restart as
    the **exact set** of 5 ids (NOT a count; this is the
    set the post-restart observations index by)
  - **`tracked_delivery_ids` post-restart state** — for
    each id, the classification is captured as exactly
    one of: `{queued_unclaimed, injected, acked,
    ambiguous_held, terminal_core}` (the canonical
    post-restart set; `prepared` is transient pending
    reconciliation and never a classification)
  - **`plugin.injection_journal.rows`** — captured; a row
    exists for **every delivery that reached `prepared`
    or later** (queued/unclaimed deliveries legitimately
    have no row — they never claimed the attempt lease)
  - **`plugin.events_stream_cursor.post_restart`** —
    `(source_instance_id, boot_id, seq)` triple; if the
    same boot has processed NEW source events during the
    kill, the post-restart `seq` is monotonic-above the
    pre-restart `seq` and `boot_id` is unchanged; if
    `boot_id` changed, the tuple changes and
    topology/binding re-snapshot fires per Y-CMUX-015.
    Equality of `seq` across restart is only meaningful
    when no new source event was committed between
    persist and reconnect.
  - **`plugin.receipt_outbox`** — captured; rows carry
    `receipt_id` + four-key tuple
  - **`double_injection.delivery_id`** — empty
  - **`injection_outcome.tri_state_per_id`** — captured
    per id; value is `unknown` for any id whose journal
    state is `ambiguous`, `missed` only for ids with
    concrete evidence of no model turn firing, `fired`
    for ids reconciled to `injected` or `acked`.
    **Booleans (`missed=true` or `missed=false`) without
    proof are SEV-1 reopen** — the truth value for an
    `ambiguous` id is `unknown`, never a forced boolean.
    **`unknown` is NOT a member of any
    `missed_injection` set**; `injection_outcome=unknown`
    and `missed_injection=present` would otherwise turn
    `unknown` into "missed=true," an SEV-1 overclaim.
- **Verdict — accountability:** every id in
  `tracked_delivery_ids` lands in exactly one
  post-restart canonical classification:
  `queued_unclaimed` (never reached `prepared`);
  `injected` (reconciled with bus evidence); `acked`
  (terminal, receipt emitted); `ambiguous_held` (held
  for operator, NOT settled); `terminal_core`
  (terminal pre-restart via core mailbox or expiry).
  `prepared` is transient pending reconciliation —
  after bounded recovery, no id remains in `prepared`;
  it has either advanced to `injected` or
  `ambiguous_held`. No id absent from
  `tracked_delivery_ids`; no id in two classifications.
- **Property test (accountability):** for any
  `delivery_id` in `tracked_delivery_ids`, exactly one
  post-restart classification; the **DOMAIN of the
  classification map** equals `tracked_delivery_ids`
  (the map's values are labels — `queued_unclaimed` /
  `injected` / `acked` / `ambiguous_held` / `terminal_core`
  — not ids). (Not `core.deliveries.queued_count` — that's
  a core-mailbox aggregate, not the same set.)
- **Property test (no silent drop):** any
  `delivery_id` in `tracked_delivery_ids` that reached
  `dispatching` / `prepared` or later pre-restart with
  **no** post-restart entry in the audit chain → SEV-1
  reopen. **Never-claimed `queued_unclaimed` ids do not
  require an audit chain entry**; they are accounted for by
  the core mailbox / current-state evidence alone.
- **Property test (ambiguity-window may surface
  `ambiguous`):** restart during `prepared → injected` MAY
  yield `ambiguous`. The test fails only on (a) `ambiguous`
  labelled `injected` (missing reconciliation proof), or
  (b) `ambiguous` absent from the audit chain.
- **Negative (silent drop):** any pre-restart
  `tracked_delivery_id` that reached `dispatching` /
  `prepared` or later, absent from the post-restart
  accounting set → SEV-1 reopen (the audit chain's
  integrity invariant across restart; never-claimed
  `queued_unclaimed` ids are not subject to this).
- **Negative (false exactly-once across the ambiguity
  window):** any delivery classified `injected` WITHOUT
  exact marker-or-source-chain reconciliation proof
  (Y-CMUX-017 D1 — marker on the bus OR an ordered chain
  from plane a/b that proves the effect fired) → SEV-1
  reopen. `injected` IS valid WITH that proof; the
  trigger is missing proof, not the `injected`
  classification. **Y-CMUX-017 D1 says `injected` is the
  right answer when reconciliation proves the effect
  fired; Y-CMUX-011 is consistent with that.**
- **Negative (double-issuance across restart):** any
  `delivery_id` classified into two contradictory
  buckets (e.g., `acked` AND `ambiguous` for the same id)
  OR a stored proof re-transmitted under a NEW
  `receipt_id` (double-issuance) OR two distinct payloads
  for the same `receipt_id` → SEV-1 reopen. The outbox IS
  at-least-once by design; repeated transmission of the
  SAME `receipt_id` with the SAME payload is idempotent at
  the core reducer and is **not** a defect.
- **Negative (overclaim on `ambiguous`):** any id with
  state `ambiguous_held` reporting `missed_injection` as
  EITHER `missed=true` OR `missed=false` → SEV-1 reopen.
  The tri-state rule holds: `unknown` is not a member of
  either boolean set, so any boolean claim is an
  overclaim (OK-vs-200-OK foot-gun, both directions).

**Cross-refs:** Y-CMUX-015 (three-plane), Y-CMUX-017
(ambiguity window).

---

### Y-CMUX-012 — `harness.turn_completed` → only `processed(completed)`

> **Cross-ref Yua core hook 8** (`01-core.md:960-961`, `01-core.md:625`):
> "`harness.turn_completed | correlated authoritative harness
> turn-end event | processed(completed)`." The matching
> `agent.hook.Stop` with no ambiguous intervening prompt proves
> `harness.turn_completed`. **A bare turn-end does not prove
> answered / acknowledged / held / declined** — those require a
> correlated authored output or disposition event. Disposition
> claims that are not correlated to authored output are rejected.
> This hook is the *positive* path for the `processed` transition
> that Y-CMUX-008's negative closes.

- **Verification:** D + P.
- **Fixture A — happy path:** drive a Claude turn end-to-end. The
  bus produces the ordered chain
  `[surface.input_sent, workspace.prompt.submitted, agent.hook.UserPromptSubmit, agent.hook.Stop]`.
  Observe the source-event chain the plugin hands to the core.
- **Fixture B — ambiguous intervening prompt:** while the LLM is
  generating turn A, send a second prompt. The plugin's chain
  must surface BOTH prompts, in order, to the harness; the
  `agent.hook.Stop` for turn A is correlated only to the FIRST
  `UserPromptSubmit`. A second `UserPromptSubmit` between the
  first `Stop` and the next `Stop` is the "ambiguous intervening
  prompt" — turn-end proof for turn A must not be issued if the
  intermediate submission shifts the correlation.
- **Fixture C — bare turn-end no output:** submit only
  `agent.hook.Stop` with no `agent.hook.UserPromptSubmit`
  preceding it. Assert no transition to
  `processed(answered|acknowledged|held|declined)`.
- **Observations per row (literal):**
  - `source_event_chain` — captured ordered list (Fixture A:
    all four; Fixture B: chain shows both prompts and the Stop
    correlated correctly; Fixture C: chain has Stop but no
    preceding UserPromptSubmit)
  - `evidence_class_recorded` — `harness.turn_completed` only
  - `proof_method_recorded` — `cmux.event_bus.harness_hook_relay`
  - `processed.disposition_recorded` — captured (must be
    `completed` only; never `answered`/`acknowledged`/`held`/
    `declined` from a bare turn-end)
  - `audit.disposition_event_id` — captured (one per
    disposition; the bare turn-end has NO authored-output
    event_id)
- **Verdict:** `harness.turn_completed` proves `processed(completed)`
  only. A turn-end without authored output is `processed(completed)`,
  not `processed(answered)`. A disposition claim without a
  correlated authored-output event is rejected as
  `disposition_ungrounded`. The audit log records one entry per
  advance; the `disposition` field equals `completed` when it
  fires.
- **Property test (positive):** for any `(transport-submitted →
  in-session → processed(completed))` chain, the plugin emits
  exactly `[surface.input_sent, workspace.prompt.submitted,
  agent.hook.UserPromptSubmit, agent.hook.Stop]` in order.
  **Assert BUNDLE completeness + ordered refs — never per-entry
  fields** (Yua conformance finding 4). `session_id` and the marker
  signature are properties of the **assembled proof bundle**, not of
  every native event: `surface.input_sent` carries **no**
  `session_id`, and `agent.hook.UserPromptSubmit` carries **no**
  marker (the marker is extracted from
  `workspace.prompt.submitted`'s local-sensitive preview). An
  *Trap: asserting both fields on every entry goes **red on correct
  data** and would pressure an implementation into fabricating fields
  to stay green.* Assert instead:
  - the chain is complete and correctly ordered;
  - the **bundle** resolves exactly one signed marker, sourced from
    the `workspace.prompt.submitted` entry;
  - the **bundle** resolves exactly one bound `session_id`, sourced
    from the `agent.hook.*` entries and consistent across them;
  - no entry is required to carry a field its event type does not
    emit.
- **Property test (negative — disposition_ungrounded):** forge a
  receipt claiming `processed(answered)` from a bare
  `harness.turn_completed` (no `session.reply_authored` event).
  Assert the core returns `rejected(reason)` per
  `01-core.md:631-633` (turn-end proves completion only) and
  does not advance.
- **Negative (overclaim):** any receipt-state advance from
  `harness.turn_completed` to `processed(answered)`,
  `processed(acknowledged)`, `processed(held)`, or
  `processed(declined)` without a correlated authored-output or
  disposition event → SEV-1 reopen (the bare turn-end proves
  completion only; disposition claims require their own evidence).

### Y-CMUX-013 — Signed marker validation; forged/expired/copied/stale rejected, no turn block

> **Cross-ref Yua core hook 18** (`01-core.md:975-976`):
> "A forged, expired, copied-to-another-seat, or stale-binding
> marker signature is rejected without blocking the session turn."
> The marker is correlation, not authorization; the plugin
> renders it alongside the prompt; the core signature prevents
> marker-field substitution. Copying a marker to another seat
> fails binding validation; replay is idempotent. Provider-side
> (the cmux plugin) must never guess which event entered the
> session — zero markers means an ordinary local turn;
> multiple or malformed markers produce visible proof errors.

- **Verification:** P + D.
- **Fixture A — forged marker:** mutate one or more marker
  fields (`event_id`, `delivery_id`, `attempt_id`, schema
  version) after the core issued the marker; submit a receipt
  that re-extracts the (now-tampered) marker from the prompt
  preview.
- **Fixture B — expired marker:** issue a marker with a short
  lifetime; sleep past lifetime; submit a receipt referencing
  the marker.
- **Fixture C — copied to another seat:** issue marker M for
  binding X; copy M into a proof attempt for binding Y.
- **Fixture D — stale-binding marker:** issue marker M for
  binding X; revoke X; install binding Y; submit a receipt
  referencing M under binding Y.
- **Fixture E — replay:** submit the same valid receipt twice;
  assert the second returns the original result (idempotent).
- **Fixture F — zero markers:** a normal local turn with no
  marker; observe no Yatagarasu receipt.
- **Fixture G — multiple markers:** a prompt with two markers;
  observe visible proof error and no advancement.
- **Observations per row (literal):**
  - `marker.schema_version` — captured
  - `marker.event_id`, `marker.delivery_id`,
    `marker.attempt_id` — captured
  - `marker.signature_verified` — `false` for tampered;
    `true` for valid; `false` for expired; `false` for
    copied-to-wrong-binding
  - `core.return_value` — `rejected(reason)` for A–D;
    `accepted`/`duplicate` for replay; no receipt for F
  - `audit.marker_rejection_reason` — captured (literal maps
    to one of the eight reducer checks)
  - `seat.state_after_rejection` — must equal
    `transport-submitted` (no advance)
  - `human_turn.blocked` — `false` (no turn blocking on
    rejection; the seat is observer-only per `01-core.md:725-732`)
- **Verdict:** forged/expired/copied/stale markers all produce
  `rejected(reason)` and zero state advance. Replay returns the
  original result. Zero markers produce no receipt. Multiple
  markers produce visible proof error and no receipt. **The
  human/agent turn is never blocked by a marker rejection.** The
  provider durably records the rejection and the receipt retries
  locally if the marker can be recovered; otherwise the session
  audit gets a `marker_rejected` entry and the seat stays below
  the floor.
- **Property test (no turn block):** for any
  `rejected(reason)` on a marker signature, the
  human/agent turn at the receiving harness completes
  normally. The provider is observer-only;
  `01-core.md:725-727` is the contract.
- **Property test (no guessing):** the provider MUST NOT guess
  which event entered the session when multiple markers are
  present. Assert no receipt is issued and a visible proof
  error is logged (per `01-core.md:654-656`).
- **Negative:** any `accepted` return on a forged / expired /
  copied / stale-binding marker → SEV-1 reopen (the marker
  signature is the boundary that prevents arbitrary content
  from claiming a Yatagarasu event).
- **Negative:** any rejection that BLOCKS the human/agent turn
  at the receiving harness → SEV-1 reopen (provider is
  observer-only; turn continuation is non-negotiable).
- **Negative (overclaim):** any receipt issued in the
  zero-marker or multi-marker fixtures → SEV-1 reopen (a
  normal local turn produces no Yatagarasu receipt; the
  provider cannot guess which event to claim).

### Y-CMUX-014 — Receipt endpoint outage: `transport-submitted` holds; retry proof, NEVER reinject

> **Cross-ref Yua core hook 19** (`01-core.md:977-978`,
> `01-core.md:725-732`): "Receipt endpoint outage leaves the
> event at `transport-submitted`, retries proof without
> reinjecting content, and surfaces `session-proof-unavailable`."
> The evidence provider is observer-only — it cannot block the
> human/agent turn. It durably queues the receipt locally for
> bounded retry. **The core does not claim `in-session` until
> it accepts the proof.** Reinjecting the message body to chase
> proof could duplicate an already-running model turn. This
> hook is the "don't chase a phantom with a duplicate"
> invariant.

- **Verification:** D + P.
- **Fixture:** `transport-submitted` already fires for an
  event; take the receipt endpoint offline before the
  `submit_receipt` call lands.
- **Observations per row (literal):**
  - `transport_submitted.fired_at` — captured
  - `receipt_endpoint.online` — `false` during the outage
  - `plugin.outbound_reinject_count` — captured (must equal
    zero; the message body is NOT reinjected through the
    transport)
  - `plugin.local_receipt_queue_depth` — captured (≥ 1; the
    receipt is queued locally for retry)
  - `core.delivery_state` — captured (`transport-submitted`,
    unchanged while the endpoint is down)
  - `audit.session_proof_unavailable` — observed (`true` —
    surfaced in the audit log per `01-core.md:730`)
  - `audit.reinjection_attempt` — must NOT appear
  - `human_turn.completed_normally` — `true` (the turn was
    not blocked by the offline endpoint)
  - post-recovery `receipt_endpoint.online` → `true`:
    observe the local queue drain and the receipt advances
    to `in-session`; no duplicate event_id emitted
- **Verdict:** the event stays at `transport-submitted` while
  the endpoint is offline. The plugin queues the receipt
  locally; the audit log surfaces `session-proof-unavailable`.
  **The message body is NOT reinjected.** When the endpoint
  returns, the queue drains and the receipt advances exactly
  once. The human/agent turn completed normally throughout
  (provider is observer-only).
- **Property test (no reinject):** for any period in which the
  receipt endpoint is offline, `plugin.outbound_reinject_count`
  stays at zero. The plugin never emits the original message
  body a second time through the transport to chase the
  proof.
- **Property test (idempotent recovery):** after the endpoint
  returns, the local queue drains exactly once per
  `(event_id, delivery_id, attempt_id)`. The audit log shows
  one `proof_received` entry and one `in-session` transition;
  no `double_injection` event_id appears.
- **Negative (phantom chase):** any reinjection of the message
  body through the transport during a receipt endpoint
  outage → SEV-1 reopen (the model turn is already running;
  reinjecting duplicates it).
- **Negative (false in-session):** any advance of delivery
  state to `in-session` while the endpoint was offline
  without a successful `submit_receipt` return → SEV-1
  reopen (the core must accept the proof, not assume it).
- **Negative (turn block):** any human/agent turn blocked on
  the receipt endpoint → SEV-1 reopen (provider is
  observer-only).

### Y-CMUX-015 — Lost remote-plugin ack / event-stream gap: three-plane recovery, no second model turn

> **Cross-ref Yua core hook 22** (`01-core.md:982-983`) +
> **`01-core.md:874-877`** (event-stream-provider side-effect
> ordering): "An event-stream provider persists
> `(source_instance_id, boot_id, seq)` only after atomically
> committing its derived receipt/outbox side effect. A replay
> gap or `boot_id` change triggers topology/binding
> re-snapshot. It never invents missed receipts and never
> reinjects content to repair an evidence gap; the core mailbox
> and the provider's bounded receipt replay are separate
> recovery planes."
>
> *Trap: collapsing the three independent planes under one
> "journal/outbox/cursor" label, with one set of identity keys and one
> cursor-advance rule, is the failure mode for plane-crossing
> behavior — a replayed
> bus event was treated as candidate pane injection; the
> outbox cursor was advanced "after the side effect succeeds"
> without naming which side effect; the cursor plane and the
> injection plane shared keys. Yua's split:
>
> **(a) CMUX bus replay identity/dedupe.** Key =
> `(source_instance_id, boot_id, seq / source-event-id)`. The
> reader's cursor lives in this plane, persists ONLY after the
> derived receipt/outbox side effect commits. A replayed bus
> event re-emits/dedupe receipt evidence from the
> receipt/outbox; it must NEVER trigger pane injection.
>
> **(b) Derived receipt/outbox idempotency.** Idempotency
> keyed on `receipt_id` plus the contract
> `(event_id, delivery_id, attempt_id, binding_id)` — exactly
> the four-key contract that also drives the core's receipt
> reducer (per `01-core.md:578-606, :713-723`). On the bus,
> a replayed `(seq)` reaches this plane and resolves to a
> stored receipt; the cursor advances only after this side
> effect commits. The core mailbox and the provider's bounded
> receipt replay are **separate recovery planes** (per
> `01-core.md:874-877`) — a replay never invents a missed
> receipt.
>
> **(c) Irreversible pane injection dedupe.** Plane (c) is
> `02-cmux-plugin-acceptance-hooks.md` Y-CMUX-017's territory
> (keyed on `delivery_id + binding/seat`); this hook
> **references it without duplication.** The broadcast
> non-collapse assertion already lives in Y-CMUX-017 fixture C
> and is load-bearing there. This hook's contribution is the
> **boundary assertion** between (a/b) and (c): bus replay
> flow does not touch plane (c) at all, even when plane (a)'s
> dedupe fails open on a stale source identity.
>
> All three planes may crash/restart independently. **The
> cursor that advances "after the side effect" is the (a)
> source-event cursor; the (b) receipt/outbox has its own
> atomic-commit guarantee.** Conflating them is exactly what
> is what makes a single-plane treatment unsound.

- **Verification:** D + P.
- **Fixture A — bus replay (plane a) alone.** Replay a
  bus event whose cursor was already past `seq`. The
  reader sees the already-processed `seq` and
  short-circuits; the cursor does NOT advance (it was
  already past this `seq`); no derived receipt is
  re-emitted (the receipt is in plane b); no pane
  injection is fired (plane c is not touched).
- **Fixture B — event-stream gap, atomic-commit ordering
  (planes a + b coupled).** Persist cursor at
  `seq=N`; force the gap (4,097+ events push `seq=N` out
  of the in-memory buffer, OR `boot_id` change). Reconnect
  with `--after-seq=N`. Observe:
  - **`boot_id` change** triggers topology/binding
    re-snapshot (per `01-core.md:982-983`).
  - The cursor advances **only after** the derived
    receipt/outbox side effect commits.
  - Inducing a fault between side effect and cursor
    persist: the next reconnect reads the pre-effect
    cursor, the side effect replays once, dedupe by
    `receipt_id` ensures no double-emit.
  - **No pane injection is fired** during replay —
    plane (a)/(b) replay does not touch plane (c).
- **Fixture C — gap during an in-flight receipt
  (planes a + b gap at the same time as an in-flight
  pane-injection contract).** Mid-flight chain
  (between `transport-submitted` and `processed`);
  force gap. The cursor-driven replay re-emits the
  receipt from the receipt/outbox (plane b); the
  injection journal (plane c) is independent and
  advances its own `delivery_id`-keyed state. The
  model turn that was running does NOT restart; the
  pane is NOT touched a second time.
- **Fixture D — boot_id change without `seq` continuity
  (plane a identity reset).** Source instance restarts;
  `boot_id` changes; same `[seq]` number means a different
  event. Persist `boot_id` alongside `seq` so a reconnect
  reads the right cursor against the right identity.
  Topologically re-resolve any binding that was
  surface-bound.
- **Observations per row (literal):**
  - `cursor.persisted_at_(instance_id, boot_id, seq)` —
    captured (the triple, never `seq` alone)
  - `cursor.reconnect_at_(...same triple...)` — captured
  - `bus_replay_event_count` — captured (events the
    server replayed between pre-gap and live)
  - `bus_replay_receipts_re_emitted` — captured
    (≥1, deduped by `receipt_id`)
  - `bus_replay_pane_injections_fired` — **must be `0`**
    (the boundary assertion between planes a/b and
    plane c)
  - `pending_receipt_ids` — captured (the plane-b
    outbox queue: the IDs for which a receipt was
    accepted by the core but not yet ack'd to the
    bus; the cursor advances after this commits)
  - `pending_delivery_ids` — captured (the plane-c
    journal queue: NOT the same set as
    `pending_receipt_ids`; this is the pane-injection
    journal from Y-CMUX-017; **referenced, not
    duplicated here**)
  - `recovered_in_flight_turn_count` — captured (the
    injection journal carries these; the
    receipt/outbox replay does not start new turns)
  - `new_turn_started_count` — must equal zero for
    events whose plane-c row was already past
    `prepared` pre-gap
  - `double_injection.delivery_id` set — must be empty
    (this is plane-c's invariant; asserted by
    referencing Y-CMUX-017 fixture C's broadcast
    property)
- **Verdict:**
  - Plane (a) cursor persists only after plane (b)
    side-effect commit (per `01-core.md:874-877`);
    `boot_id` changes trigger re-snapshot, never
    invented receipts.
  - Plane (b) dedupe is keyed on `receipt_id` plus
    `(event_id, delivery_id, attempt_id, binding_id)`;
    a replayed `(seq)` re-emits the existing receipt
    (idempotent), does not invent a new one.
  - **Plane (c) is not touched by bus/Receipt replay.**
    This is the boundary assertion — the cleanest,
    most load-bearing negative in the hook.
  - `pending_receipt_ids` and `pending_delivery_ids`
    are **distinct sets** (and the assertion that
    they're distinct is itself a property — the same
    ID in both sets would mean the planes have been
    conflated).
- **Property test (atomic commit / cursor persistence
  ordering):** the cursor that names
  `(source_instance_id, boot_id, seq)` advances
  strictly after the plane-b receipt side effect
  commits. Inducing a fault between side effect and
  cursor persist: the next reconnect reads the
  pre-effect cursor, replays the side effect once,
  dedupe by `receipt_id` ensures no double-emit, the
  cursor advances to the just-committed `seq`.
- **Property test (plane-boundary no-injection):**
  for any bus replay sequence — gap, `boot_id`
  change, dedupe miss, replay-race — the count of
  pane-injection calls during the recovery window is
  **exactly the count from plane-c's own
  `delivery_id`-keyed dedupe**, never the count of
  bus replays. Bus replays do not produce pane
  touches.
- **Property test (planes have distinct queues):**
  the set `pending_receipt_ids` (plane b) and the set
  `pending_delivery_ids` (plane c) are disjoint by
  construction. Any intersection → SEV-1 reopen (the
  planes have been conflated — plane conflation is the
  failure mode this assertion prevents).
- **Negative (replay-injected pane):** any pane
  injection call that traces its origin to a bus
  replay event (rather than to plane-c's own
  `delivery_id`-keyed injection journal entry) →
  SEV-1 reopen (the boundary is broken; replay must
  re-emit receipt evidence only, never pane-touch).
- **Negative (cursor advanced before receipt commit):**
  any cursor advance on plane (a) that occurs before
  the plane (b) receipt side effect commits → SEV-1
  reopen (per `01-core.md:874-877`; on restart,
  recovery would re-fire the side effect, dedupe
  would catch the duplicate `receipt_id`, but the
  audit chain would have a phantom advance).
- **Negative (keying on `event_id` for replay):** any
  cursor / dedupe plane that keys identity on
  `event_id` (rather than the triple
  `(source_instance_id, boot_id, seq)` for plane a
  or the four-key contract for plane b) → SEV-1
  reopen. Per-plane keying is invariant.
- **Negative (skipped receipt):** any in-flight
  receipt that never advances after a gap and is
  silently dropped (no audit entry, no operator
  alert) → SEV-1 reopen (the receipt/outbox must
  surface the gap explicitly, not assume
  continuity).

### Y-CMUX-016 — Preview leak: raw `message_preview` text never enters receipt/audit storage

> **Cross-ref 01-core.md:745-747 (Yua ruling).** `workspace.prompt.submitted`
> payloads include `message_preview` (cmux docs events.md lines 243-244).
> cmux labels this `local-sensitive`. The host-local provider extracts
> the signed Yatagarasu marker from the preview and **discards the
> surrounding text**: "Raw preview/content never leaves the host-local
> provider or enters core receipt/audit storage." This hook is the
> regression test for the "the bus is content-free" overclaim that
> the design conforms to.

- **Verification:** D + S.
- **Fixture:** drive a real Claude delivery end-to-end. Capture
  every byte of `workspace.prompt.submitted.message_preview` from
  the bus. Run the plugin end-to-end. Dump:
  - the full plugin→core receipt payload
  - the core's audit log entries (every column, every row)
  - any side-channel storage the plugin maintains (journal,
    cursor file, retry queue, error log)
- **Observations per row (literal):**
  - `preview_bytes_at_bus` — captured (the full
    `message_preview` text that cmux emitted)
  - `core.receipt_payload_marker` — captured (the marker that
    traveled; should equal the marker embedded in the preview)
  - `core.receipt_payload_preview_chars` — captured (must equal
    zero — no preview text in the receipt payload)
  - `audit.message_preview_chars` — captured per audit-log
    entry (must equal zero across all entries for this event)
  - `audit.message_chars` — captured per entry (must equal zero)
  - `audit.body_chars` — captured per entry (must equal zero)
  - `audit.message_length` — captured per entry (may be set,
    but its value must NOT match any substring of
    `preview_bytes_at_bus`; the length is metadata, not content)
  - `side_storage.preview_chars` — captured (journal / cursor /
    retry / error log; must equal zero)
  - `preview_substring_detected` — captured (any substring ≥ 8
    contiguous bytes from `preview_bytes_at_bus` found in any
    non-marker field, anywhere. Must equal `false`.)
- **Verdict:** the only thing that travels from the preview
  region of `workspace.prompt.submitted` is the signed Yatagarasu
  marker. Raw preview bytes are absent from the receipt payload,
  the audit log, the journal, the cursor, the retry queue, and
  every error log. `message_length` is allowed as metadata; the
  actual text is not. `redacted_fields` is the legitimate
  redaction channel; an audit-log entry that *bypasses* it (stores
  raw text) fails the hook.
- **Static test (grep):** the plugin source MUST NOT read or
  forward `payload.message_preview`, `payload.message`, or any
  field carrying prompt text outside the marker-extraction
  function. Grep for `payload.message_preview` and confirm it
  appears only in the extraction code path; grep for any
  serialization path that touches audit rows and confirm no
  prompt-text field is read.
- **Property test (negative):** construct a plugin that
  *accidentally* forwards `message_preview` to the receipt
  payload (e.g., a buggy logging statement). Assert the hook
  detects the leak (`preview_substring_detected = true`).
- **Negative (preview leak):** any raw preview bytes
  (`message_preview`, `message`, or
  `payload.message_preview` from `workspace.prompt.submitted`)
  found in any field of any receipt payload, audit-log row,
  journal entry, cursor file, retry queue, or error log →
  SEV-1 reopen. This includes debug-level logging that
  pretends-redacts (e.g., truncating to first 200 chars still
  counts as text leaving the host-local provider).
- **Negative (overclaim):** the plugin emitting a receipt
  whose payload claims `redacted_fields = []` when the actual
  bus payload had `redacted_fields` populated → SEV-1 reopen
  (lying about redaction is worse than leaking; it makes the
  leak invisible to downstream audit consumers).

### Y-CMUX-017 — Duplicate-turn: durable injection journal prevents re-injection on core retry

> **Cross-ref 02-cmux-plugin.md:268-311** ("Durable injection journal
> (required — Yua's cross-review finding)" as amended by 8d86d71).
> Network delivery is at-least-once; local injection must be
> exactly-once where the effect is **provably exactly-once** and
> **honestly-ambiguous** where it isn't. The local side effect
> (model turn starting) cannot be made idempotent by a remote
> retry. The plugin's durable injection journal / outbox is the
> only mechanism that prevents the canonical duplicate-turn
> failure: network ACK lost → core retries → second injection →
> second model turn.
>
> **The journal is a four-state machine keyed by `delivery_id`**
> (per `02-cmux-plugin.md:280-284`): `prepared` →
> `injected` (→ `acked`), plus `ambiguous` as a holdable terminal
> state for `prepared`-without-`injected`. The `prepared` /
> `effect_maybe_started` record is durable + fsynced **before** the
> pane is touched (per `:286-296`) so a crash between record and
> pane touch is recoverable instead of duplicating. **Keying by
> `event_id` instead of `delivery_id` would break broadcast** (one
> `event_id` fans out to many `delivery_id`s; an `event_id`-keyed
> dedupe would treat the second seat's legitimate delivery as a
> duplicate and suppress it). Recovery from `prepared`-without-
> outcome reconciles via the event bus / outbox; if the marker
> cannot be located, the delivery is held visible as
> `injection_outcome_unknown` (per `:298-302`), **never** blind
> re-injected. This hook is the regression test for the
> four-state journal's invariants: the success path holds the
> exactly-once claim; the ambiguous path surfaces the
> truthfully-unknown claim without claiming exactly-once
> falsely.

- **Verification:** D + P.
- **Fixture A — success path: lost network ACK after
  `injected`.** Drive a real delivery to its
  `prepared → injected → ACKed` terminal state (`model_turn_count`
  is provably exactly 1). Now **drop the plugin-to-core ACK**
  that would have told core the plugin has the delivery (kill
  the plugin's outbound socket to core before the ACK frame
  lands). Observe the core's retry behavior.
- **Fixture B — redelivery on known `delivery_id`.** Same
  delivery path as A, but the recovery runs cleanly: core
  retries the same `delivery_id`; the plugin reads its own
  journal row in `injected` state and **re-emits the existing
  receipt from the journal** instead of re-injecting. The
  pane is not touched twice; the model turn is not started
  twice.
- **Fixture C — broadcast fan-out does NOT trip dedupe.**
  Broadcast the same `event_id` to 5 seats. Each seat
  receives its own `delivery_id`. The journal keys by
  `delivery_id`, NOT `event_id`. Assert all 5 journal rows
  are independent (`delivery_id` distinct, `event_id`
  shared); the dedupe does not collapse them. (This is the
  load-bearing reason the key changed from `event_id` to
  `delivery_id` per `02-cmux-plugin.md:280-284`; an
  `event_id`-keyed journal would suppress the second seat's
  legitimate delivery.)
- **Fixture D — crash at the `prepared` → `injected`-state-
  commit boundary.** The load-bearing new fixture per
  Yua's ruling. Drive the delivery through `prepared`
  (record fsynced, marker captured, binding/seat
  captured). **The plugin crashes** in the boundary
  where the effect (pane inject call) **may or may not
  have fired** — the plugin's own state tells us only
  that `prepared` is durable; we cannot tell locally
  whether the `inject` syscall landed. After restart the
  recovery loop reconciles the row by looking up
  `workspace.prompt.submitted` for the row's marker on
  the cmux event bus:
  - **Sub-fixture D1 — evidence present (effect
    fired):** the marker IS on the bus (the pane touch
    DID land before the crash; the bus observed it
    before the journal write of `injected` could commit).
    Recovery transitions the row to `injected` from the
    bus evidence, ACKs core, model turn stays at
    provably 1.
  - **Sub-fixture D2 — evidence unprovable (effect
    unknown):** the marker is NOT provable on the bus
    (either the pane touch did not fire, or the bus had
    a simultaneous gap covering the window). Recovery
    surfaces the delivery as `injection_outcome_unknown`
    in the journal and the audit log. **NO second
    injection is fired from this state.** The audit log
    explicit `outcome=unknown` entry with the marker's
    trace IDs and a surface for the operator. The
    model-turn-count for this delivery is
    `unknown`, not `1` and not `2`.
- **Observations per row (literal):**
  - `journal.delivery_id` — captured (every row keys this,
    not `event_id`)
  - `journal.bound_to` — captured (`{event_id, binding_id,
    seat}` for context; the keying rule is by
    `delivery_id`)
  - `journal.state` — captured per row (`prepared |
    injected | acked | ambiguous`)
  - `journal.fsync_timestamp` — captured (Phase 1 fsync;
    precedes the `inject` call by a non-zero wall-clock
    interval; precedes any pane touch; matches the moment
    the record is durable)
  - `journal.marker_field_present` — `true` for every row
    (the marker is recorded `prepared` so the recovery
    loop can reconcile it after a crash)
  - `plugin.inbound.injection_call_count` — captured per
    pane (Fixture A, B: equals 1; Fixture C: equals 5,
    one per seat; Fixture D1: equals 1 from bus
    reconciliation, NOT from re-firing; Fixture D2:
    equals 1 if the bus evidence permits, ELSE equals 0
    with state=ambiguous and the recovered-firing is
    *not* pursued)
  - `model_turn_count` — captured (Fixture A, B,
    Fixture-D1: equals 1 provably; Fixture D2:
    `unknown`; Fixture C: equals 1 for each of the 5
    seats)
  - `journal.survives_plugin_restart` — `true`
  - `journal.survives_cmux_restart` — `true`
  - `journal.survives_core_reconnect` — `true`
  - `double_injection.delivery_id` set — must be empty
  - **`missed_injection.delivery_id` set** — captured
    (must be empty **for Fixtures A, B, C, D1**; for
    Fixture D2, the set MUST contain the unprovable
    `delivery_id`, surfaced explicitly. Treating
    `missed_injection=empty` as success for D2 is a
    SEV-1 reopen condition.)
  - **audit-log `injection_outcome` per `delivery_id`** —
    captured per row (`injected` for success paths;
    `unknown` for Fixture D2). The audit log is the
    ONLY place the truth about an ambiguous delivery
    lives; downstream tooling that reads the log must
    see `unknown`, not `missed` and not `injected`.
  - audit-log: `injection_journal_replay` event count —
    captured (≥ 1 for any recovery; the audit log
    records every replay explicitly)
- **Verdict:**
  - Fixture A: across the retry window, **provably**
    exactly one model turn runs; `model_turn_count == 1`,
    `journal.state == acked`, no double-injection across
    any restart the recovery loop survives.
  - Fixture B: redelivery to a known `delivery_id`
    re-emits the journaled receipt and never re-injects;
    the model turn stays at 1.
  - Fixture C: 5-seat broadcast yields 5 independent
    journal rows; the `event_id` is shared and the
    `delivery_id`s are distinct; dedupe does not suppress
    legitimate fan-out.
  - Fixture D1: crash between `prepared` and `injected`
    is recovered by reconciling the marker against the
    event bus; the row transitions to `injected` from
    bus evidence; the model turn stays at 1 provably.
  - Fixture D2: crash between `prepared` and `injected`
    with the marker unprovable on the bus surfaces
    `injection_outcome_unknown`; **no** blind re-inject;
    the audit log carries `outcome=unknown` for the
    `delivery_id`; the model-turn-count is **unknown**,
    not 1 and not 2. The journal AND the audit log
    MUST surface this, never silently "1" or silently
    "2".
- **Property test (the four-state invariant):** For any
  sequence of events on a `delivery_id`, the journal
  state transitions follow exactly
  `prepared → (injected | ambiguous)` and `(injected
  → acked | ambiguous)`. No other transition is
  valid. `ambiguous` is terminal-and-surfaced, not
  transient.
- **Property test (no blind re-inject):** For any
  `prepared` row whose reconciliation finds no marker
  evidence on the bus, the recovery loop MUST surface
  `injection_outcome_unknown` AND MUST NOT call
  `inject` again. Assert by inducing the crash fixture
  with the bus event-tap destroyed (no
  `workspace.prompt.submitted` was emitted by the
  pane-touch coroutine before the crash). The
  `injection_call_count` MUST stay at 0; the row MUST
  surface `outcome=unknown`.
- **Property test (broadcast dedupe):** for any
  `event_id` that fans out to N recipient seats,
  exactly N distinct `journal.delivery_id` rows exist
  after recovery, and the dedupe does NOT collapse
  them. The fixture proves broadcast works under the
  the delivery_id key.
- **Negative (turn duplication):** any model turn that
  starts twice for the same `delivery_id` across the
  retry window → SEV-1 reopen (the durable journal
  exists exactly to prevent this; missing or racing
  the journal is the failure mode).
- **Negative (silent drop):** any redelivery that is
  silently dropped (no ACK to core, no journal
  re-emit, no audit entry) → SEV-1 reopen (silent
  delivery loss to a seat that thinks it succeeded).
- **Negative (blind re-inject from `prepared`):** the
  recovery loop firing `inject` again for a
  `prepared` row whose reconciliation finds no marker
  → SEV-1 reopen (that is precisely the duplicate-turn
  failure mode the two-phase write prevents; the
  fixture proves the recovery loop chooses
  `injection_outcome_unknown` over blind re-inject).
- **Negative (false `missed_injection=empty`):** any
  audit-log or operational view that reports
  `missed_injection=empty` (or
  `model_turn_count=1`) for a delivery whose journal
  row is in `injection_outcome_unknown` →
  SEV-1 reopen (the truth value is "unknown"; claiming
  "1" or "0" is a false exactly-once claim — the same
  class of failure as the OK-vs-200-OK labeling foot-
  gun).
- **Negative (journal keyed by `event_id`):** any
  journal row whose primary key is `event_id` rather
  than `delivery_id` (whether by code path or by
  schema) → SEV-1 reopen; the broadcast fixture
  collapses on the second seat's delivery and
  silently breaks a Round-1 deliverable.
- **Negative (per-entry `surface_id` from agent hooks):**
  not exercised in this hook directly; flagged for
  cross-reference to Y-CMUX-008's body fixture for the
  bundle-completeness assertion (Aoi conformance fix
  #3 at 8d86d71; `agent.hook.*` carries `workspace_id`
  only — envelope `surface_id` is `null`; correlation
  goes through the binding/session registry, never by
  reading a `surface_id` field off the hook event).

### Y-CMUX-018 — NEGATIVE capability assertion: CMUX never emits `participant.*`

> **SCOPE — negative boundary only** (core citations
> `01-core.md:524-540`, `:626-656`). *Trap: a positive
> channel-native fixture here — driving an authenticated
> reply/reaction and asserting the cmux plugin advanced
> `processed(answered|acknowledged)`. **Those do not belong here.**
> **CMUX is `session-transport` only.** Testing that this plugin can
> *perform* channel-native emission tests behavior it must never have,
> and would have pressured an implementation into growing it. The
> positive fixtures and the eight-case acceptance matrix **move to the
> Discord/comms-view QA page or core QA** — they verify the
> comms-view plugin and the core reducer, not this one.
>
> What CMUX legitimately retains is the **negative capability
> assertion** below: channel-native is *not routed here and not
> supported here*, and this plugin never manufactures participant
> evidence.

- **Verification:** P (property / capability boundary).
- **Assertion 1 — mode declaration.** The plugin declares
  `delivery_mode: session-bound` in its plugin contract and advertises
  **no** `channel-native` capability.
- **Assertion 2 — unsupported, not silently handled.** If a delivery
  carrying `delivery_mode=channel-native` is routed to this plugin,
  it is rejected as `unsupported_delivery_mode` and surfaced. It is
  **never** quietly accepted and handled as if session-bound.
- **Assertion 3 — never emits `participant.*`.** Under any input,
  including an authenticated end-user reply or reaction observed on a
  cmux-managed surface, this plugin emits **no**
  `participant.reply_authored` and **no**
  `participant.reaction_authored` evidence. Those classes are the
  comms-view plugin's to issue.
- **Assertion 4 — never synthesizes `in-session` for channel-native.**
  Even though this plugin *does* hold a session binding for the
  surface, a channel-native delivery must not be advanced through
  `in-session` — `session_entry` is structurally `not_applicable` for
  that mode (`01-core.md:524-526`).
- **Observations per row (literal):**
  - `plugin.declared_delivery_modes` — captured (expect exactly
    `["session-bound"]`)
  - `plugin.declared_evidence_classes` — captured (must contain no
    `participant.*` entry)
  - `rejection_reason` on a channel-native delivery — captured
    (expect `unsupported_delivery_mode`)
  - `emitted_evidence_classes` across the full run — captured
    (assert the set intersection with `{participant.reply_authored,
    participant.reaction_authored}` is **empty**)
- **Negative:** any `participant.*` evidence emitted by the CMUX
  plugin, or any channel-native delivery silently handled as
  session-bound → **plugin-boundary violation**, treat as SEV-1
  reopen. A transport that can manufacture participant evidence can
  forge a human having spoken.

**Moved out of this page** (belongs to Discord/comms-view or core QA):
positive channel-native reply/reaction fixtures, and the eight-case
acceptance-rejection matrix (router accept, Discord POST success,
`notification.read`, plugin-owned reaction, unknown bot, wrong
principal, wrong message binding, replayed `source_event_id`).
