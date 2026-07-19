---
status: draft
class: project
created_at: 2026-07-18
last-verified: 2026-07-18
description: "Yatagarasu CMUX plugin QA acceptance hooks, part A (Y-CMUX-001..010) plus the shared rendering rule, test scaffolding, and the hook-to-event index covering all 18 hooks. Hooks 011-018 are in part B. Owned by Tama."
tags:
  - harem-ops/project
sources: []
---

# Yatagarasu — CMUX plugin acceptance hooks, part A (Y-CMUX-001..010)

**Owner:** Tama (perception seat; held-line discipline).
**Parent design:** [[projects/active/yatagarasu/design/02-cmux-plugin]]
**GATE: design** — no code authorized.

> **Why this page exists.** These hooks lived in the parent design until it hit
> tsumugi's **hard** 1500-line cap. This is a pure extraction — **no content was
> changed in the move**; corrections are recorded in their own commits.

## Acceptance hooks (for Tama's QA)

**Owner:** Tama (perception seat; held-line discipline).
**Status:** design — no code yet.
**Cross-stream:** Yua for core-conformance, Shiori for plugin-contract symmetry.

These hooks fail the build if they're sloppy. Several are **adversarial** —
they intentionally suppress, drop, restart, or partition a dependency to
make the plugin report the truth, not a comfortable lie. Each adversarial
test is paired with a **negative assertion**: what the plugin must NOT
report when the truth is a failure.

**Rendering rule (load-bearing).** Each hook's harness prints **literal
observations** (HTTP-style status codes, event names, sequence numbers,
timestamps, count of frames) inside the data grid — never classifications
like `OK` / `FAIL` / `GREEN` / "submitted" tucked between the cell and the
reader. The classification belongs in the verdict block *outside* the
grid; the artifact-of-record is the literal data. This is the
`substrate-honest-probe-output` discipline: a 200 reading is **the failure
mode** the hook exists to catch, so a label that reads "OK" defeats the
hook at the visible layer (the same way the 2-part-only probe false-cleared
for weeks). Every hook below is written so the artifact cannot be
misread by a future scanner.

### Test scaffolding (the harness, for the build phase)

A thin Go test fixture (or shell harness) that drives a real cmux
socket, a real `~/.cmuxterm/events.jsonl` log, and a stub
binding. The fixture must expose hooks for:

- `inject(*event)` — submit via the socket exactly the way the plugin does.
- `suppress_composer_submit(true)` — drop the composer-submit leg of the
  receipt pair (Hook 1); leaves the `surface.input_sent` event live.
- `restart_cmux(boot_id)` — kill cmux, replay; or change `boot_id` only.
- `restart_seat_session(identity)` — kill the seat's session while the
  plugin holds a pending delivery.
- `force_slow_consumer(seq)` — pause the reader for ≥1,024 frames;
  expect `slow_consumer` disconnect frame.
- `set_focused_surface(surface_id)` — drive `surface.focused` to the
  non-target so banner withdraw behavior is observable.
- `require_suppressOnlyFocusedSurface(true)` — set the flag; check
  config watch.
- `two_seats_one_workspace()` — provision seats A (focused) and B
  (visible but not focused).

Every test assertion logs: `observation` (raw value) → `verdict`
(`PASS` / `FAIL` / `HOLD`). The verdict is the human-facing summary;
the observation is the line of record.

---

### Y-CMUX-001 — `agent-bridge` parity proven old-vs-new before cutover

> **Conformance.** No regression in the legacy `agent-bridge send` surface
> while the core + CMUX plugin land. The CLI name, envelope, and SENT
> semantics must be byte-identical (modulo the legacy `CID` →
> `correlation_id` rename).

- **Verification:** B (golden-path behavioral).
- **Fixture:** drive 100 deliveries through the legacy path and 100
  through the new path (Vesper only). For each delivery, capture:
  the rendered envelope rendered into the pane (literal bytes), the
  pane that received it (`workspace_id` + `surface_id`), and the
  SENT signal's timestamp + return code.
- **Observations per row:** `envelope_hash`, `pane_id`, `sent_ts`,
  `sent_code`. Byte-equality and exact-pane match required.
- **Negative:** any byte-difference in the rendered envelope; any
  pane drift; any SENT-time delta > 50ms (latency floor for the path
  change).
- **Cutover gate:** `fail=0` on 100/100 deliveries on both paths
  before the legacy CLI is even hidden behind the shell.

### Y-CMUX-002 — `transport-submitted` requires BOTH events (true negative, HOLD-not-requeue on UNKNOWN)

> **Adversarial, load-bearing.** The old `agent-bridge verify_submitted`
> was a composer scrape that **assumed submitted on scrape failure** —
> the documented cause of 9+ silently-lost messages. The native pair
> `surface.input_sent` + `workspace.prompt.submitted` is a true
> negative: when `workspace.prompt.submitted` is missing within the
> timeout, the plugin MUST NOT claim `transport-submitted`.
>
> **Aoi's build-lane correction (2026-07-18, issue #1):** the
> observation `input_sent-observed-but-submit-unobserved` is
> genuinely ambiguous. The cmux TUI queues injected text as the
> next turn when the pane is busy, so a missing `workspace.prompt.submitted`
> does not by itself prove the transport dropped the message; it
> could be a correctly queued next-turn. Reverting to `queued`
> from this state carries a duplicate-turn risk (the re-attempt
> races the busy-queue's admission). Reverting is **only** safe
> when the outcome is `NOT_SUBMITTED` — the clean-negative case
> where no host events at all were observed. The `UNKNOWN`
> outcome (some events observed, submit absent at timeout) MUST
> HOLD rather than requeue.

- **Verification:** D + P (dynamic + property).
- **Fixture:** inject a real event; the harness holds the
  `workspace.prompt.submitted` event (does not emit it) while
  emitting `surface.input_sent`.
- **Observations per row:**
  - `surface.input_sent` boolean — observed
  - `workspace.prompt.submitted` boolean — `false` (suppressed)
  - Plugin-reported `transport-submitted` boolean — `false` (the truth)
  - BusObserver outcome — captured literally:
    `{NOT_SUBMITTED, UNKNOWN}` (the
    `BusObserver.observe(marker, timeout_s)` return value)
  - Delivery post-state — captured:
    `{queued_revert, held_on_unknown}`
  - elapsed since `surface.input_sent` (ms)
  - Timeout fired: `true`
- **Verdict:** the plugin reports `transport-submitted = false`.
  - On `NOT_SUBMITTED` (no events at all): the delivery
    reverts to `queued` — requeue is safe because no
    busy-queue admission is pending.
  - On `UNKNOWN` (events observed, submit absent at timeout):
    the delivery HOLDS in `held_on_unknown` — no requeue,
    because the busy-queue case is unresolved and
    re-attempting would create a duplicate turn. The
    audit log gets an `outcome=unknown` entry.
  - In BOTH cases, the audit log carries one entry per
    delivery: `not_transport_submitted` with the captured
    `delivery_id`.
- **Property test:** for any pair `(input_sent=true, prompt_submitted=false)`
  the reducer HOLDS at `dispatching` or reverts to `queued`; it
  never transitions to `transport-submitted`. The revert-vs-hold
  choice is governed by the `BusObserver.observe(marker, timeout_s)`
  return value: `NOT_SUBMITTED` → revert; `UNKNOWN` → hold.
- **Adversarial target:** the exact regression test for the
  9-lost-messages class. If this hook goes green with the harness
  suppressed, the lenient-scrape pattern is back, and the SEV-1
  reopen is warranted.
- **Negative:** plugin reports `transport-submitted = true` while
  `workspace.prompt.submitted = false` → SEV-1 reopen. Reverting
  to `queued` on `UNKNOWN` (the busy-pane-pending case) is **not**
  a SEV-1; it is the failure the design now disallows. Hold-on-UNKNOWN
  is the contract; requeue-on-UNKNOWN is the regression to flag
  separately as a follow-on if it ever occurs.

### Y-CMUX-003 — No cached surface binding; restart re-resolves by identity

> **Hard rule.** Surface IDs are not stable across session restarts.
> Eric's restart incident reproduced live 2026-07-18: Tama's seat moved
> `surface:33 → surface:3`. Delivery still succeeded only because the
> plugin re-resolves by identity on every send.

- **Verification:** B (behavioral) + S (static, code grep).
- **Fixture:** mid-flight delivery (send accepted, transport pending).
  Capture the resolved `surface_id` recorded at inject time as
  `pre_restart_surface`. Restart the seat's session (close the
  pane; a new one opens; identity unchanged, new surface). Re-trigger
  the next send.
- **Observations per row:**
  - `pre_restart_surface` — recorded
  - `post_restart_surface` — re-resolved at send time
  - `pre_restart_surface == post_restart_surface` boolean — `false`
  - Identity resolved against: the same seat identity string
  - The send landed in: `post_restart_surface` (literal)
- **Verdict:** plugin re-resolves; `pre_restart_surface != post_restart_surface`;
  the send lands in `post_restart_surface`. Audit log records one
  `binding_resolved` entry per send with `surface_id` matching the
  literal landing site.
- **Static test:** grep the codebase for surface/pane IDs cached
  across sends. Banned patterns: any module-scoped or class-field
  cache of `surface_id`; any TTL > 0 on a surface-binding map; any
  in-process map keyed on surface IDs that lives longer than one
  send. The only persistent binding may be `identity → binding_id`
  in the core, not in the plugin.
- **Adversarial:** mutate the local in-process state so the next
  send would target a stale `surface_id` (force the cache); assert
  the plugin re-resolves anyway and the assertion `landed_in_stale`
  is `false`.
- **Negative:** any send ever lands in a stale `surface_id` →
  SEV-1 reopen (silent delivery loss to a dead or reassigned pane).

### Y-CMUX-004 — Delivery never steals focus

> **cmux-socket-policy.** Socket commands must not steal macOS app
> focus. Delivery is **not** a focus-intent command.

- **Verification:** D + B.
- **Fixture:** identify the focused window + focused workspace +
  focused surface + focused pane *immediately before* delivery. Send
  a message to a background seat. Re-read focus state immediately
  after delivery returns, plus after the `surface.input_sent` event
  fires, plus after the `workspace.prompt.submitted` event fires.
- **Observations per row (literal):**
  - `pre_focus.window_id` — captured
  - `pre_focus.workspace_id` — captured
  - `pre_focus.surface_id` — captured
  - `pre_focus.pane_id` — captured
  - `pre_focus.is_key_window` — captured
  - `post_focus.window_id` — captured at three checkpoints
  - `post_focus.workspace_id` — same three checkpoints
  - `post_focus.surface_id` — same three checkpoints
  - `post_focus.pane_id` — same three checkpoints
  - `post_focus.is_key_window` — same three checkpoints
- **Verdict:** focus is unchanged across every checkpoint,
  byte-for-byte. Sending to a background seat must leave focus
  alone; the receiving pane receives its message and lights up its
  own indicator (status pill, sidebar metadata) but does not
  promote itself to key.
- **Negative test:** inject a send with a transport option that
  *attempts* focus on the receiving pane (legacy `agent-bridge`
  had this foot-gun); assert the CMUX plugin refuses that option
  at the API surface with reason `focus_intent_not_in_transport_surface`.
- **Negative:** any drift in pre/post focus on any of the four
  IDs → SEV-1 reopen.

### Y-CMUX-005 — Gap handling: re-snapshot, do not assume continuity

> **Resume contract.** `ack.resume.gap == true` means the cursor fell
> outside the retained buffer (4,096 events) or `boot_id` changed.
> On a gap: process replayed tail, then **re-snapshot**
> (`extension.sidebar.snapshot`, `list-workspaces`, `tree`) rather
> than assume continuity.

- **Verification:** D + P.
- **Fixture A — buffer gap:** persist cursor at `seq=N`. Process a
  batch of 4,097+ events that pushes `seq=N` out of the
  in-memory buffer. Reconnect with `--after-seq=N`.
- **Fixture B — `boot_id` change:** persist cursor; kill cmux;
  restart; reconnect.
- **Observations per row:**
  - `ack.resume.gap` boolean from the first ack frame after connect — observed `true`
  - `received.replay_tail_count` — captured (events the server replayed)
  - `received.snapshots_taken` — captured (count of snapshot commands the plugin emitted)
    Expected: ≥ 3 (`extension.sidebar.snapshot`, `list-workspaces`, `tree`)
  - `assumed_continuity` boolean — `false`
- **Verdict:** the plugin runs the snapshot re-fetch on every gap;
  no state asserts "we know what's here" without a literal
  snapshot. Audit log gets one `gap_resnapshot` entry per reconnect,
  carrying the snapshot commands run.
- **Property test:** for any `(gap==true)` reconnect, the
  plugin's next read on derived state (focus, workspace list,
  sidebar metadata) originates from a snapshot ≤ 1 second old.
- **Negative:** any read on derived state without a prior
  snapshot → SEV-1 reopen (stale-state reads look like success).

### Y-CMUX-006 — Backpressure: `slow_consumer` reconnect, no double-injection

> **Bounded pending queue.** Each subscriber has a 1,024-event pending
> queue. Falling behind triggers `slow_consumer` disconnect.
> Reconnect from the persisted `seq`; dedupe by `event_id`.

- **Verification:** D + P.
- **Fixture:** start the reader; force a flood by
  broadcasting 1,500 events while the reader is paused; release
  the reader; expect `slow_consumer` disconnect; reconnect from
  the last persisted `seq` (call this `S_persisted`).
- **Observations per row:**
  - `slow_consumer_received` boolean — `true`
  - `disconnect_at_seq` — captured
  - `S_persisted` — captured (literal cursor at disconnect)
  - `reconnect_at_seq` — captured (cursor sent on reconnect, equals `S_persisted`)
  - `replay_event_count` — captured (events the server replayed between `S_persisted` and live)
  - `injection_event_count` — captured (injections to panes)
  - `injection_event_count == sum(inject_aspect_of_events)` — boolean
  - `double_injection.event_id` set — must be empty; any non-empty set fails the hook
- **Verdict:** the reconnect replay covers all events from
  `S_persisted` to live, none double-injected, none skipped.
  Audit log records one `reconnect_replay` entry with the four
  literal counts above.
- **Property test:** across 100 reconnect cycles (each forced
  to `slow_consumer`), `double_injection` event-id set is
  empty for every cycle.
- **Negative:** any event delivered to the pane twice → SEV-1
  reopen (the cmux receipt flow expects one event per
  `event_id`; double-injection corrupts the in-session /
  processed state machine).

### Y-CMUX-007 — Banner survival: two seats, one workspace, non-focused does not withdraw

> **Notification policy.** By default cmux withdraws a banner when
> *its workspace* becomes visible — which can retract a banner
> for a **non-focused** surface (a second agent in the same
> visible workspace) before anyone notices. The fix is
> `notifications.suppressOnlyFocusedSurface: true`; auto-withdraw
> scopes to the exact focused surface only.

- **Verification:** D + S.
- **Fixture:** configure `suppressOnlyFocusedSurface: true`. Seat
  A is focused; seat B shares the same workspace (visible but
  not focused). Deliver an `INFO`-class message to seat B (the
  non-focused seat).
- **Observations per row:**
  - `config.suppressOnlyFocusedSurface` literal — `true`
  - `delivery.to_seat` — `B`
  - `delivery.focused_seat_at_delivery` — `A`
  - `notification.created` event — `true`
  - `notification.withdrawn` event within 2s — `false`
  - `window.keyed` events during the 2s window — captured (no
    focus shift expected)
- **Verdict:** seat B's notification persists; seat A's session
  state is unchanged. Audit log records one `banner_persisted`
  entry per delivery.
- **Negative:** any withdraw event on B's surface during the 2s
  window → SEV-1 reopen (silent message loss via UI).
- **Static test:** the plugin's notification hook chain
  (`notifications.hooks`) must NOT mutate the `desktop` /
  `markUnread` effects for seat B without an explicit
  mark-read action.

### Y-CMUX-008 — `in-session` / `processed` advance ONLY on full proof chain

> **Receipt reducer (Yua's ruling 2026-07-18).** For CMUX-plugin
> seats, the canonical in-session evidence is
> `harness.prompt_accepted` with `proof_method =
> cmux.event_bus.harness_hook_relay` (the bus is trusted local
> relay, not the semantic issuer; the originating harness event
> determines the evidence class). The full proof chain requires
> `(event_id, delivery_id, attempt_id, binding_id)` **plus** the
> signed Yatagarasu marker **plus** ordered CMUX source-event
> references. `session_id` alone is insufficient — `01-core.md:696`
> and `01-core.md:728` both require the source-event chain to
> satisfy the registered proof-method correlation rule.
>
> For the matching `agent.hook.Stop` (no ambiguous intervening
> prompt), the class is `harness.turn_completed` and the maximum
> transition is `processed(completed)` only. Answered /
> acknowledged / held / declined require a correlated authored
> output or disposition; a bare turn-end does not advance to any
> of those. Any harness absent from the host event bus remains
> at `transport-submitted`.
>
> A receipt endpoint outage leaves the event at
> `transport-submitted` and queues the receipt locally for bounded
> retry — the core does **not** claim `in-session` until it
> accepts the proof, and **must not** reinject the message to
> chase the proof (`01-core.md:704-711`). This is the
> `session-proof-unavailable` discipline.

- **Verification:** P + S.
- **Fixture:** drive a Claude seat (event bus present) and a
  Hermes seat (no event bus). Inject the same `correlation_id`
  shape, same `event_id`, into both. Drive a full receipt chain
  through the Claude seat's relay.
- **Observations per row (literal):**
  - `claude_seat.userPromptSubmit.session_id` — captured
  - `claude_seat.userPromptSubmit.event_id` — captured
  - `claude_seat.marker_signature_verified` — captured (`true`)
  - `claude_seat.marker_age_ms` — captured (≤ lifetime per
    binding)
  - `claude_seat.evidence_class_recorded` — literal
    `harness.prompt_accepted` (or `harness.turn_started` for the
    pre-turn variant)
  - `claude_seat.proof_method_recorded` — literal
    `cmux.event_bus.harness_hook_relay` for bus-relayed
    harnesses; `direct.harness_callback` for direct
    (none expected in Round 1)
  - `claude_seat.source_event_chain` — captured ordered list
    (`[surface.input_sent, workspace.prompt.submitted, agent.hook.UserPromptSubmit]`
    for `in-session`;
    `[surface.input_sent, workspace.prompt.submitted, agent.hook.UserPromptSubmit, agent.hook.Stop]`
    for `processed(completed)`)
  - `claude_seat.receipt_state` — captured
  - `hermes_seat.evidence_class_recorded` — must be
    `transport.submit_ack`
  - `hermes_seat.proof_method_recorded` — must NOT be
    `cmux.event_bus.harness_hook_relay`
  - `hermes_seat.receipt_state` — must be `transport-submitted`
- **Verdict:** Claude transitions
  `transport-submitted → in-session` only when the full proof
  chain is present and signed-marker-validated, and
  `in-session → processed(completed)` only on the matching
  `agent.hook.Stop` with no intervening prompt. Hermes stays at
  `transport-submitted`. Audit log records both
  `evidence_class` and `proof_method` per receipt.
- **Property test (forge — full forgery rejection):** submit a
  forged receipt with
  - the four `(event_id, delivery_id, attempt_id, binding_id)`
    keys matching a real event,
  - a tampered or expired marker signature, **or**
  - a source-event chain that does not satisfy the registered
    proof-method correlation rule (out-of-order, missing entry,
    wrong surface),
  - and **any** of: a binding_id never registered for that
    `session_id`; the `session_id` not matching the binding's
    authoritative session; a binding that has been revoked or
    superseded.

  Assert the core returns `rejected(reason)` (`01-core.md:589`)
  with a reason corresponding to one of the eight reducer
  validation rules enumerated at `01-core.md:713-723`:
  principal-not-bound-for-this-binding, binding-no-longer-active,
  binding-does-not-own-delivery-recipient,
  session-id-not-binding's-authoritative-session,
  marker-fields-disagree-or-expired,
  evidence-class-not-declared-or-not-legal-from-current-state,
  proof-method-source-chain-fails-correlation-rule, or
  contradictory-receipt. The literal `reason` string returned
  by the core is implementation-defined; the contract is the
  outcome (rejected, no state advance, audit-log entry, and —
  for the contradictory case — adapter health degraded
  per `01-core.md:723`). The test asserts the outcome and the
  audit-log entry shape, not specific reason strings.
- **Property test (session_id-alone rejected):** submit a receipt
  with only `session_id` matching (i.e., no signed marker, no
  full source-event chain, no `event_id`/`delivery_id`/
  `attempt_id` triple). Assert the core returns
  `rejected(reason)` (no state advance) — the literal reason
  corresponds to the chain-fails-correlation rule at
  `01-core.md:720-721`. The seat MUST NOT advance. This is the
  regression test for the
  "session_id-alone-cannot-advance-state" rule.
- **Property test (turn-end alone, no output):** submit only
  `agent.hook.Stop` without `agent.hook.UserPromptSubmit` for
  the same chain. Assert the reducer does NOT advance to
  `processed(answered)` / `processed(acknowledged)` /
  `processed(held)` / `processed(declined)`. The maximum
  transition for `harness.turn_completed` alone is
  `processed(completed)`; the other four require a
  correlated authored output or disposition event.
- **Negative (Hermes in-session forgery):** forge a Hermes-side
  receipt that claims `evidence_class = harness.prompt_accepted`
  + `proof_method = cmux.event_bus.harness_hook_relay` without a
  matching `agent.hook.UserPromptSubmit` event in the bus.
  Assert the core returns `rejected(reason)` corresponding to
  the chain-fails-correlation rule (no state advance). This is
  the receipt-floor honesty invariant: a harness absent from
  the host event bus must not be promoted to `in-session`,
  regardless of what the receipt claims.
- **Negative (endpoint outage, no reinject):** take the receipt
  endpoint offline. Submit `transport-submitted`; the audit log
  records `session-proof-unavailable`; the message body is NOT
  reinjected through the transport. Assert delivery state
  stays `transport-submitted` and the provider durably queues
  the receipt for retry; once the endpoint returns, the queue
  drains and the receipt advances to `in-session`. No
  duplicate-injection through the round trip.
- **Negative:** any receipt-state advance to `in-session`
  without the full proof chain (or with a forged / expired /
  copied marker) → SEV-1 reopen.
- **Negative:** any receipt-state advance to
  `processed(answered|acknowledged|held|declined)` on
  `harness.turn_completed` alone → SEV-1 reopen (the bare
  turn-end proves completion only, never disposition).

### Y-CMUX-009 — Broadcast yields per-seat matrix; absent = `queued`, never proxy

> **Group primitive.** One event + one roster snapshot + per-seat
> outcome. An absent seat's delivery stays `queued` (visible
> absence). No "everyone received" rollup; no invisible
> success.

- **Verification:** D.
- **Fixture:** `broadcast` to a roster of 5 seats; revoke the
  binding for one seat mid-flight (simulate crash); observe the
  submission result and the outcome matrix.
- **Observations per row:**
  - `broadcast.outcome_count` — captured (must equal 5)
  - `outcome.for_seat_X.state` — captured per seat
  - `outcome.rollup.all_delivered` boolean — must be `false`
    if any seat is `queued` or `failed`
  - `audit.broadcast_id` — captured (one per broadcast)
  - `audit.roster_snapshot_size` — captured
- **Verdict:** the result is a per-seat matrix of length 5; the
  revoked seat shows `failed-because-binding-absent` (or
  `queued` if pending); no rollup claim of uniform delivery.

### Y-CMUX-010 — Busy-pane injection queues as next turn, never clobbers

> **Pane discipline.** The native TUI buffer queues a delivered
> message as the next turn when the pane is busy. Mid-turn
> messages never clobber an active turn or a half-typed human
> composer.

- **Verification:** D + B.
- **Fixture:** drive the seat into mid-turn (an LLM is generating);
  submit a send; while the LLM continues, fire a second send.
  Inspect the rendered composer at three checkpoints:
  immediately, after first injection (during busy), after first
  turn completes (next-turn admission).
- **Observations per row:**
  - `composer_text_at_inject_1` — captured
  - `composer_text_after_inject_1_during_busy` — captured (must
    equal `composer_text_at_inject_1` — no clobber)
  - `composer_text_at_inject_2` — captured (must equal
    `composer_text_at_inject_2` immediately before the second
    send — no clobber from inject 2 either)
  - `turn_1.complete` event — observed
  - `composer_text_after_turn_1_complete` — captured (must
    contain the two injected messages as the next two turns, in
    order)
- **Verdict:** zero clobbers; messages queue; the
  `workspace.prompt.submitted` event fires only when the human
  composer (or seat harness) actually submits.
- **Negative:** any composer mutation during a busy turn →
  SEV-1 reopen (data-loss for the human composer).

---

## Hooks 011–018 → part B

Hooks **Y-CMUX-011 through Y-CMUX-018** live in
[[projects/active/yatagarasu/design/02-cmux-plugin-acceptance-hooks-b]]
(split at the tsumugi soft cap; pure extraction, no content change):
restart accounting, the full proof chain, signed-marker validation,
receipt-endpoint outage, three-plane recovery, preview-leak, the
duplicate-turn journal, and the channel-native negative boundary.

The hook-to-event index below covers **all 18** hooks across both pages.

### Hook-to-event matrix (cross-reference)

For each native event the plugin depends on, the cmux docs that
authorize it:

| Hook | Native events / contracts relied on | Doc |
| --- | --- | --- |
| Y-CMUX-002 | `surface.input_sent`, `workspace.prompt.submitted` | `cmux/docs/events.md` lines 237, 283 |
| Y-CMUX-005 | `boot_id`, `seq`, `ack.resume.gap` | `cmux/docs/events.md` lines 20, 132, 168–169 |
| Y-CMUX-006 | `slow_consumer`, dedupe by `id` | `cmux/docs/events.md` lines 133, 179, 181 |
| Y-CMUX-007 | `notifications.suppressOnlyFocusedSurface` | `cmux/docs/notifications.md` lines 61–73 |
| Y-CMUX-008 | `agent.hook.UserPromptSubmit` keyed by `session_id`; signed marker; ordered CMUX source-event chain | `cmux/docs/events.md` line 332; `01-core.md:635-656` (marker); `01-core.md:713-723` (reducer) |
| Y-CMUX-010 | `workspace.prompt.submitted` during busy | `cmux/docs/events.md` line 237 |
| Y-CMUX-011 | sequence continuity on reconnect (also covers the durable-injection journal premises at `02-cmux-plugin.md:268-311`) | `cmux/docs/events.md` lines 161–171 |
| Y-CMUX-012 | `agent.hook.Stop` correlated to `agent.hook.UserPromptSubmit` (no ambiguous intervening prompt) | `01-core.md:744-745`, `01-core.md:631-633` |
| Y-CMUX-013 | signed Yatagarasu marker fields (`event_id`/`delivery_id`/`attempt_id`/schema) + core signature | `01-core.md:637-656` (marker contract) |
| Y-CMUX-014 | receipt endpoint outage behavior; `session-proof-unavailable` audit entry | `01-core.md:725-732` |
| Y-CMUX-015 | three-plane recovery: (a) bus replay keyed `(source_instance_id, boot_id, seq)`, (b) receipt/outbox keyed `receipt_id + (event_id, delivery_id, attempt_id, binding_id)`, (c) pane injection referenced from Y-CMUX-017; cursor advances ONLY after plane-b atomic commit | `cmux/docs/events.md` lines 161–171, 179–181; `01-core.md:874-877` (cursor/atomic-commit), `01-core.md:982-983` (no-second-turn); plane (c) invariant at Y-CMUX-017 in this file |
| Y-CMUX-016 | `workspace.prompt.submitted.message_preview` extraction boundary; `redacted_fields` channel | `cmux/docs/events.md` lines 243-244; `01-core.md:745-747` |
| Y-CMUX-017 | four-state injection journal keyed on `delivery_id`; `prepared`/`effect_maybe_started` fsynced before pane touch; reconcile-or-hold for crash-window; `injection_outcome_unknown` surfaced for unprovable recoveries | `02-cmux-plugin.md:268-311` (durable injection journal — keys `:280-284`, two-phase write `:286-296`, recovery `:298-302`, redelivery `:304-305`) |
| Y-CMUX-018 | **NEGATIVE boundary only** — plugin declares `session-bound`; asserts NO `participant.*` evidence is ever emitted and channel-native is rejected as `unsupported_delivery_mode` | `01-core.md:524-540`, `01-core.md:626-649` (channel-native belongs to comms-view, not this plugin) |

Every adversarial hook references a documented native event
contract — none depend on a plugin-synthesized scrape. The
post-Yua hooks (012–018) cite `01-core.md` first where the
contract lives at the core seam; the cmux event-bus is
secondary. Channel-native evidence classes are part of
`01-core.md`'s evidence taxonomy at `01-core.md:626-627`.

### Hook numbering & build ordering

The numbering `Y-CMUX-NNN` is the plugin-level namespace;
core-level hooks stay `Y-QA-NNN`. Core-level hooks 17–22 in
`01-core.md` (added by Yua's contract push ed2f6ed) are
mirrored at the plugin level by Y-CMUX-012–015. The
channel-native ruling at `01-core.md` b580bf3 is **not** mirrored
into a positive plugin hook: CMUX is `session-transport` only, so
Y-CMUX-018 mirrors it as a **negative capability boundary** and the
positive fixtures live in the Discord/comms-view + core QA pages. Y-CMUX-016 and Y-CMUX-017 are plugin-side hooks
that target Aoi's preview-content and duplicate-turn concerns
respectively — they have no `01-core.md` hook-number mirror
because their premises are plugin-side (extraction boundary,
injection journal), not reducer-side.

The build may run the hooks in this order, because each later
hook depends on the existence of the earlier ones (a green
restart requires a working gap handling; a working gap
handling requires a working reader; etc.):

1. Y-CMUX-001 (parity)
2. Y-CMUX-002 (transport-submit pair)
3. Y-CMUX-003 (re-resolve)
4. Y-CMUX-004 (focus)
5. Y-CMUX-005 (gap)
6. Y-CMUX-006 (slow_consumer / backpressure)
7. Y-CMUX-007 (banner survival)
8. Y-CMUX-013 (signed marker — must land before Y-CMUX-008's
   forged-test can run; before Y-CMUX-016's
   marker-extraction contract can be asserted)
9. Y-CMUX-008 (receipt-state honest-visible)
10. Y-CMUX-012 (turn-end → completed — needs the source-event
    chain from Y-CMUX-008)
11. Y-CMUX-014 (receipt endpoint outage — needs the proof
    pipeline from Y-CMUX-008 / Y-CMUX-013)
12. Y-CMUX-018 (negative capability boundary — needs only the
    plugin's declared contract; no receipt state machine
    dependency, since it asserts emissions that must never occur)
13. Y-CMUX-009 (broadcast — group path, can land any time
    after the receipt state machine exists)
14. Y-CMUX-010 (busy-pane)
15. Y-CMUX-011 (restart)
16. Y-CMUX-016 (preview-leak — needs Y-CMUX-013 marker
    contract)
17. Y-CMUX-015 (lost-ack / gap outbox recovery — plane
    boundary: bus replay must not cross into pane
    injection; cursor advances only after plane-b atomic
    commit per `01-core.md:874-877`)
18. Y-CMUX-017 (duplicate-turn journal — depends on the
    injection journal section being implemented in code;
    this hook is the regression test for the four-state
    machine keyed on `delivery_id`. If the journal keys on
    `event_id` or doesn't track `prepared`-vs-`injected`, the
    hook fails by design. The broadcast fixture proves the
    `delivery_id` key doesn't break fan-out; the crash-window
    fixture proves the recovery loop surfaces
    `injection_outcome_unknown` instead of blindly re-
    injecting.)

The substrate-honest test harness lands first, then the hooks
in this order. Any reordering between 9–14 is local and safe.
Y-CMUX-016 should land after Y-CMUX-013 (the marker
extraction contract is the prerequisite); Y-CMUX-017 should
land after the durable-injection journal section is
implemented in code at the four-state level (the hook proves
the journal prevents duplicate turns AND surfaces ambiguity
honestly; if the journal keys on `event_id` or collapses
`prepared` and `injected` into a single write, the hook
fails by design).
