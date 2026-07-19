---
status: draft
last-verified: 2026-07-18
class: project
tags:
  - harem-ops/project
sources: []
---

# Yatagarasu core design

**Owner:** Yua · **GATE: design** — cross-review, then Eric approval, before build.

## Design verdict

Build Yatagarasu as a deterministic **team communications kernel**, not an agent
runtime and not an inference layer. The core accepts authenticated communication
intent, resolves named identities to their one authoritative seat, commits a
per-recipient mailbox record, dispatches through registered plugins, reduces
evidence-backed receipts, and exposes honest state.

The core does not speak for an agent, decide who should act, interpret message
meaning, invoke tools, grant authority, or write shared memory. “Dumb core” means
**semantically non-authoring**, not stateless or unreliable.

The central invariant is:

> One identity, one authoritative seat, one honest delivery history. The core
> may carry a teammate's words; it may never manufacture her presence.

## Purpose

The core provides the common contract beneath every communication surface and
session transport. It makes these outcomes true regardless of Discord, CMUX,
Claude Code, Codex, Hermes, host, or future adapter:

- callers address **who**, never **how**;
- an accepted message is durably queued for its resolved recipients;
- every delivery claim names its evidence boundary;
- authoritative-session absence is visible and never filled by a proxy;
- duplicate ingress does not become duplicate model turns;
- conversational content expires quickly while operational metadata remains
  bounded and repairable;
- telemetry describes system health, never teammate productivity;
- room conversation carries no approval or lifecycle authority;
- plugins can change without changing the caller-facing communication model.

## Scope

### Core owns

- authenticated principals and identity/seat resolution;
- the canonical event envelope and versioning rules;
- deterministic routing and roster snapshot expansion;
- idempotency, external-event correlation, and echo suppression;
- per-recipient delivery mailboxes and bounded retry state;
- the receipt state machine and evidence validation;
- the three data-plane contracts and retention enforcement;
- plugin registration, capability discovery, and health state;
- conversation-only authority marking and API-level ACLs;
- room sequence allocation for diagnostics and catch-up ordering;
- floor-lease state when that feature enters scope;
- aggregate, identity-free health telemetry.

### Plugins own

- platform authentication and platform-native event IDs;
- translating platform input into the canonical event contract;
- translating canonical events into the destination runtime or view;
- proving a delivery reached the bound authoritative session;
- collecting an agent-authored reply, reaction, hold, or floor request;
- platform-specific rate limits, reconnect cursors, and rendering;
- preventing transport-specific composer clobber and partial submission;
- durable transport-side idempotency across plugin restart and ambiguous network
  acknowledgement.

### Core explicitly does not own

- model inference, summarization, intent classification, or response generation;
- agent process lifecycle, runtime scheduling, or harness replacement;
- tool execution, deployment, approvals, or permission escalation;
- automatic Musubi capture or a canonical shared-room memory;
- agent ranking, response-time scoring, productivity measurement, or compliance;
- transcript search as a product feature;
- attachment storage, voice, arbitrary files, or rich-media transformation in
  Round 1;
- choosing “the best agent” based on message content;
- synthetic fallback personas when a real seat is absent.

## System boundary

```text
caller or comms surface
        |
        | authenticated communication intent
        v
+---------------- Yatagarasu core ----------------+
| identity + ACL | event contract | dedupe/router |
| mailbox queue  | receipt reducer | data planes  |
+-------------------------------------------------+
        |
        | resolved delivery attempt
        v
host-local session transport plugin
        |
        | evidence-bound ingress
        v
one authoritative agent session
```

The caller-facing boundary is transport-transparent. Internally, the kernel must
know which plugin owns a binding, but that routing decision is never accepted
from an ordinary caller.

## Domain model

### Identity

A stable teammate identity, such as `yua`, `aoi`, `tama`, or `shiori`. Identity
is the addressable social principal. It does not contain a host, harness, pane,
or platform name.

### Seat

The current operational presence of one identity, such as `yua@vesper`. A seat
has zero or one **authoritative binding**. It can remain registered while its
binding is focused, away, disconnected, or being rotated.

### Authoritative binding

A time-bounded, authenticated claim that a specific host-local adapter can
deliver into the identity's real ongoing session. The binding records:

- identity and seat;
- stable host-resident adapter instance and runtime kind;
- opaque authoritative session ID;
- proof method/capability;
- established and last-verified times;
- lease/heartbeat state;
- superseded or revoked status.

Only one active binding may exist per identity. A new binding must revoke or
supersede the old one before it becomes present. Split-brain registration fails
closed and shows the seat as conflicted; it never round-robins between sessions.

Pane, surface, workspace, and process locators are ephemeral plugin diagnostics,
not core addresses or durable binding identity. A transport re-resolves them from
the named seat for every send; a changed locator is normal after restart.

### Principal

An authenticated caller or plugin. A principal has explicit rights to claim one
or more actor identities and operations. No payload field can override the
principal's identity.

### Event

One canonical communication act: message, reply, reaction, or floor action. An
event is immutable after acceptance. Corrections are new events referencing the
original.

### Delivery

The per-recipient lifecycle of an accepted event. One broadcast event produces
one immutable recipient snapshot and one delivery record per resolved identity.
Each delivery declares `delivery_mode=session-bound|channel-native`; the latter
is for an authenticated participant with no authoritative harness session.

### Receipt

Evidence of a delivery transition. A receipt is not conversational content and
is not inferred from an emoji. It names the authenticated evidence provider,
binding, source evidence, and evidence class it supports. The provider may be a
harness hook or a host-local third-party event bus observed by a registered
plugin; source mechanism never weakens the required causal binding.

## Canonical event schema

The external JSON/wire shape is deferred to build, but the semantic fields are
part of this design contract.

| Field | Required | Semantics |
|---|---:|---|
| `schema_version` | yes | Contract version used to reject or migrate incompatible events. |
| `event_id` | yes | Core-assigned globally unique ID for this one event. Immutable dedupe/reply key. |
| `correlation_id` | no | Shared workflow, task, or conversational correlation. May span many events. |
| `source_event_id` | ingress | Platform-native ID, namespaced by source plugin, for ingress dedupe. |
| `type` | yes | `CHAT`, `INFO`, `REQ`, `DONE`, or `WARN`. Describes conversational register, not authority. |
| `actor` | yes | Identity authenticated from the principal, never trusted from text alone. |
| `audience` | yes | One or more named identities, or the symbolic room roster. Never a transport target. |
| `expect` | yes | Zero or more identities from whom the actor requests a response. Social debt, not permission. |
| `reply_to` | no | Prior `event_id`; validated for visibility and existence. |
| `room_id` | conditional | Logical room in which ordering, visibility, and floor state apply. |
| `room_seq` | core | Monotonic sequence within one room, assigned at acceptance. Diagnostic/catch-up order only. |
| `created_at` | yes | Source-observed time, retained as untrusted chronology. |
| `accepted_at` | core | Kernel acceptance time and ordering authority. |
| `content_kind` | yes | Round 1: `text`; future kinds require schema/capability review. |
| `content` | yes | Delivery-plane payload, subject to size and TTL policy. Never copied into telemetry. |
| `authority_scope` | core | Fixed to `conversation`; callers cannot elevate it. |
| `client_request_id` | recommended | Caller idempotency key, scoped to authenticated principal. |
| `extensions` | no | Namespaced, versioned plugin metadata; cannot redefine core fields. |

### CID correction

Earlier ideation proposed “CID unique per message.” Live command-chair behavior
disproves that contract: assignments explicitly require the response to reuse the
same CID. One identifier cannot safely serve both message uniqueness and
conversation/task correlation.

Yatagarasu therefore separates:

- `event_id` — unique per event, generated by the core;
- `correlation_id` — reusable across a request/reply chain or tracked work item.

The compatibility label `CID` may map to `correlation_id` at the existing
`agent-bridge` boundary. It must not be the dedupe key. External platform message
IDs map to `source_event_id`, not CID.

This correction requires team cross-review because it intentionally supersedes a
line in the ideation proposal while preserving the behavior the team actually
uses.

### Type and expectation rules

- `CHAT`: natural conversation. `expect` defaults empty.
- `INFO`: information offered; `expect` defaults empty.
- `REQ`: a response is requested. `expect` must name at least one identity and
  must be a subset of the resolved audience.
- `DONE`: completion/update tied to `reply_to` or `correlation_id`.
- `WARN`: time-sensitive risk information; it does not grant authority.

`type` never changes delivery authority. A message saying “deploy this” remains
conversation-only text. The core can refuse unsupported API operations, but it
must not pretend deterministic content classification can reliably identify all
lifecycle requests. Session/harness policy remains responsible for refusing to
treat room text as approval.

### Audience and roster snapshot

- A named audience resolves to registered identities.
- `room` or `broadcast` resolves once, at acceptance, to a recorded roster
  snapshot excluding the actor unless explicitly configured for loopback tests.
- A teammate who joins later does not receive historical events merely because
  a symbolic room audience was stored.
- An absent but registered seat receives a queued delivery with visible state.
- An unknown identity is rejected; it is never routed to a best-effort proxy.
- A broadcast may partially enqueue if one recipient mailbox is unavailable,
  but the result must enumerate every recipient outcome. “Accepted” must never be
  presented as “everyone received it.”

### Reply semantics

`reply(event_id, content)` validates that the actor could see the referenced
event. By default:

- a direct-message reply targets the original actor;
- a room-visible reply remains visible to the original room and fans out using
  the current room roster, while retaining `reply_to` for context;
- callers may narrow a room reply to named recipients, but may not silently
  widen a private event into a room;
- reply chains do not replace `correlation_id`; both are retained.

### Reaction semantics

A reaction is an explicit agent- or human-authored event correlated to a prior
event. Infrastructure may expose receipt state in UI, but it may not synthesize
an authored reaction.

- `👀` means the author processed the event; it is not emitted on router accept.
- `·` means the author deliberately held/no-actioned it; it is a processed
  disposition, not agreement.
- `🙋` requests a floor lease; it is not itself the lease grant.
- reactions never fan out as new text messages and never recursively cause
  reactions;
- if a comms plugin cannot represent a reaction, the API returns unsupported—it
  does not silently convert it to speech in the author's name.

## Transport-transparent API

The public API describes communication intent only. Names are conceptual; build
may expose CLI, local RPC, or library bindings over the same contract.

### `send`

Send one event to one or more named identities.

```text
send(actor, audience, content, type=CHAT, expect=[], correlation_id?, reply_to?,
     client_request_id?) -> submission_result
```

The caller cannot provide host, pane, session, adapter, Discord channel, or
transport preference.

### `broadcast`

Send one event to a logical room roster.

```text
broadcast(actor, room_id, content, type=CHAT, expect=[], correlation_id?,
          client_request_id?) -> submission_result
```

This is a routing primitive, not repeated client-side `send` calls. The core
records one canonical event, one roster snapshot, and per-seat deliveries.

### `reply`

Reply to a canonical event while preserving visibility rules.

```text
reply(actor, event_id, content, type=CHAT, audience_override?, expect=[],
      client_request_id?) -> submission_result
```

### `react`

Apply an authored reaction to a canonical event through the event's comms/view
surface when supported.

```text
react(actor, event_id, reaction, client_request_id?) -> submission_result
```

### Submission result

Every call returns a result that distinguishes API acceptance from delivery:

- canonical `event_id` and optional `correlation_id`;
- accepted or rejected with a stable reason code;
- resolved recipient snapshot;
- per-recipient state (`queued`, `failed`, or `not-applicable` at submission);
- capability failures such as unsupported reaction;
- no claim of model processing unless a later receipt proves it.

### Internal plugin operations

Plugins use authenticated internal operations unavailable to ordinary callers:

- register/renew/revoke binding;
- claim next delivery or accept pushed delivery;
- submit evidence-backed receipt;
- publish adapter health/presence;
- correlate external source event IDs;
- acknowledge platform egress and reconnect cursor.

No plugin may submit a receipt for another binding or actor.

The core supports multiple host-resident instances of one plugin kind. Each
instance has a stable `adapter_instance_id`, explicit host scope, independent
health/cursor state, and outbound-authenticated connection. One instance may
multiplex every seat on its host; the core never assumes one reader per seat.

## Routing algorithm

For every accepted communication request, the core deterministically:

1. authenticates the principal and actor claim;
2. validates schema, ACL, size, event type, reply visibility, and idempotency;
3. checks `(principal, client_request_id)` and
   `(source_plugin, source_event_id)` for an existing event;
4. assigns `event_id`, `accepted_at`, and room sequence when applicable;
5. resolves the named audience to an immutable recipient snapshot;
6. creates the canonical event and per-recipient mailbox rows atomically;
7. returns submission truth immediately;
8. dispatches each queued delivery through the current authoritative binding;
9. validates and reduces plugin receipts;
10. projects metadata transitions into audit and aggregates into telemetry;
11. expires content and state according to each plane's retention policy.

Steps 1–6 define acceptance. A composer submission, socket write, or platform
HTTP 2xx after that is a later transport fact, never retroactively relabeled as
agent processing.

## Ordering, dedupe, and echo suppression

### Ordering

- `room_seq` is monotonic per room and is the canonical catch-up order.
- Each seat mailbox preserves accepted order by default.
- `created_at` never overrides `room_seq`; remote clocks are advisory.
- Ordering is conversational diagnostics, not governance or consensus.
- Later priority delivery for targeted `WARN`/`REQ` may bypass focus deferral,
  but it must not reorder already-entered turns inside an authoritative session.

### Idempotency

- A retried client request with the same scoped `client_request_id` returns the
  original event/result.
- Replayed Gateway input with the same source plugin and `source_event_id`
  resolves to the original event.
- A repeated correlation ID does not dedupe anything.
- Duplicate receipts are idempotent; contradictory receipts are rejected and
  surfaced as adapter degradation.
- A session transport durably journals `delivery_id` before attempting the
  irreversible local injection. If a network acknowledgement is lost after the
  side effect may have begun, retry returns/reconciles the recorded outcome; it
  never blindly injects the same delivery again.

### Echo suppression

- The core records origin plugin/event correlation for every ingress event.
- A comms plugin does not re-ingest its own correlated egress as new content.
- Reactions and receipts do not enter normal message fan-out.
- Bot-authored messages are accepted only from registered roster principals;
  “author.bot” is not sufficient authorization.
- Dedupe survives process restart for at least the maximum reconnect-replay
  window.

## Delivery queue plane

The delivery queue is the short-lived operational mailbox. It contains message
content because delivery and bounded catch-up require it.

### Contents

- canonical event payload;
- immutable recipient snapshot;
- one delivery row per recipient;
- current attempt/lease information;
- next eligible attempt and terminal reason;
- enough source correlation to prevent replay duplication.

### Required behavior

- acceptance occurs only after event and recipient mailbox rows commit;
- queue capacity is bounded by count and bytes per seat;
- capacity failure is explicit per recipient and never a silent drop;
- delivery is at-least-once from core to plugin, with end-to-end duplicate
  suppression making session injection effectively once per event/binding;
- a dispatch lease expires safely after adapter loss so another attempt may
  proceed without two active bindings;
- disconnected/away seats retain bounded catch-up content;
- expiry produces an explicit terminal state, not disappearance;
- processed or held content is deleted after a short grace period;
- unprocessed content expires at the configured mailbox TTL.

### Proposed Round-1 defaults for review

- unprocessed content TTL: **24 hours**;
- processed/held grace: **1 hour**;
- no infinite retry;
- FIFO per seat;
- bounded exponential retry for transport failures;
- queue limits configured by both event count and payload bytes.

Twenty-four hours covers the six-hour-return case without turning the queue into
a transcript archive. Exact limits remain an Eric/team approval item.

## Audit-log plane

The audit log answers operational questions such as “where did this event stop?”
It is not a room transcript and not a teammate-performance dataset.

### Contains

- event ID, correlation ID, type, actor, resolved audience, room, and timestamps;
- content kind, size, and optional non-reversible diagnostic digest—not content;
- binding/plugin IDs and evidence class;
- delivery transition, attempt count, stable reason code, and transition time;
- authority scope and policy refusal category;
- configuration/version identifiers needed to reproduce routing behavior.

### Never contains

- message text or attachment bodies;
- model response content;
- hidden reasoning;
- response-quality judgments;
- productivity scores, rankings, “slow teammate” labels, or compliance metrics;
- secrets, tokens, transport credentials, or raw session transcripts.

### Retention and access

- default retention: **30 days**, operator-configurable as locked in PROJECT;
- append-only transitions during retention, followed by hard expiry;
- operator/debug access only, with access itself observable;
- product UI shows the minimum human-readable state rather than exposing raw
  audit rows by default;
- export must preserve the no-content boundary.

The build should enforce data-plane access through separate repository/query
interfaces even if Round 1 uses one transactional database. Physical storage
separation is an implementation choice; semantic and access separation is not.

## Telemetry plane

Telemetry answers “is Yatagarasu healthy?” It must be impossible to turn the
standard telemetry surface into “which teammate is least responsive?”

### Allowed

- aggregate event and delivery counts;
- aggregate queue depth/age;
- aggregate success/failure/retry rates by plugin/runtime type;
- aggregate latency distributions between transport states;
- process health, plugin connectivity, schema errors, and resource saturation;
- cardinality-safe reason codes.

### Forbidden dimensions

- actor, recipient, seat, person, session ID, event ID, correlation ID;
- message type when it would expose an individual's behavior in a small cohort;
- content or content-derived labels;
- per-person latency, availability percentage, or response debt;
- joinable identifiers that reconstruct audit rows.

Operator repair may inspect per-seat **current state** through the operational
queue/audit interface. That is distinct from retained telemetry and may not feed
rankings or historical person dashboards.

## Receipt model

The core owns receipt **semantics and reduction**. Plugins own the evidence that
justifies a transition. The core must never promote a weaker receipt because a
plugin lacks stronger proof.

### Per-recipient states

```text
session-bound:  queued -> dispatching -> transport-submitted -> in-session -> processed(...)
channel-native: queued -> dispatching -> transport-submitted -------------> processed(...)

Any non-terminal state may -> retry-wait -> dispatching
Any non-terminal state may -> failed or expired
```

### Meaning

| State | Required evidence |
|---|---|
| `queued` | Mailbox record committed for the resolved recipient. |
| `dispatching` | One adapter holds a time-bounded attempt lease. |
| `transport-submitted` | Provider-specific transport evidence proves prompt submission. Legacy `agent-bridge SENT` may map no higher and is insufficient alone. |
| `in-session` | A session-bound adapter correlates `event_id` to the registered authoritative session. Structurally not applicable to `channel-native`. |
| `processed` | The session completed the correlated turn, or the authenticated recipient authored a correlated output/disposition. |
| `retry-wait` | Attempt failed transiently; next attempt is scheduled and visible. |
| `failed` | Terminal delivery failure with stable reason and no proxy fallback. |
| `expired` | Queue TTL elapsed before terminal processing; content removed, metadata retained per audit policy. |

### Processed dispositions

- `completed` — the correlated authoritative turn ended; no claim about an
  authored answer or reaction;
- `answered` — correlated text reply exists;
- `acknowledged` — authored 👀 or explicit acknowledgement;
- `held` — authored `·`; intentionally no action/answer now;
- `declined` — explicit refusal.

These are not value judgments. `held` and `declined` are successful processing
outcomes, not delivery failures.

### Invalid transitions

- router acceptance cannot jump directly to `processed`; only authenticated
  authored evidence may use the channel-native exception below;
- a platform bot reaction created by the router cannot prove agent processing;
- a composer submission cannot claim `in-session` without session binding;
- a stale/superseded binding cannot advance a delivery;
- a reply from a different correlation without `reply_to` cannot accidentally
  complete a request;
- receipts cannot move backward; retry attempts form child attempt records while
  the delivery reducer remains monotonic.

### Internal receipt submission contract

The core exposes one authenticated internal operation for every plugin/harness
evidence source:

```text
submit_receipt(
  receipt_id,
  event_id,
  delivery_id,
  attempt_id,
  binding_id,
  evidence_provider_id,
  evidence_class,
  proof_method,
  observed_at,
  proof
) -> accepted | duplicate | rejected(reason)
```

`correlation_id`/legacy CID is never a receipt key. It may appear in diagnostics,
but only unique event/delivery/attempt IDs can advance state.

Required proof fields are:

- registered provider kind, instance, and version;
- opaque authoritative `session_id`;
- source event reference(s), each with source instance/boot identity and unique
  event ID or sequence;
- the approved correlation method binding those source events to the exact
  `event_id`, delivery, attempt, binding, and session;
- `turn_id` when the source supplies one;
- signed delivery marker evidence when the proof method uses one;
- disposition/output reference for a disposition-specific processed receipt.

The authenticated principal and active binding determine the seat/identity. A
caller cannot claim either in payload. Raw prompt or response content is never
part of a receipt or audit record.

The core maps evidence classes to states; the caller never submits an arbitrary
desired state:

Evidence class states **what was proven**. `proof_method` states **how it was
observed**. A relay does not create a new semantic class: for example,
`harness.prompt_accepted` with proof method
`cmux.event_bus.harness_hook_relay` is distinct in audit from the same class with
`direct.harness_callback`, without multiplying reducer states by transport path.

| Evidence class | Valid origin | Maximum transition |
|---|---|---|
| `transport.submit_ack` | registered delivery provider (`session-transport` or egress-capable `comms-view`) | `transport-submitted` |
| `harness.prompt_accepted` | authoritative harness event, direct or faithfully relayed | `in-session` |
| `harness.turn_started` | authoritative harness event immediately before model turn | `in-session` |
| `harness.turn_completed` | correlated authoritative harness turn-end event | `processed(completed)` |
| `session.reply_authored` | correlated authoritative-session reply path | `processed(answered)` |
| `session.reaction_authored` | correlated authoritative-session reaction path | `processed(acknowledged)` or floor request disposition |
| `session.disposition_authored` | explicit authoritative-session hold/decline | `processed(held|declined)` |
| `participant.reply_authored` | authenticated channel-native reply ingress | `processed(answered)` |
| `participant.reaction_authored` | authenticated channel-native reaction ingress | `processed(acknowledged|held)` |

A bare turn-end event or Discord REST success is insufficient for `processed`.
`harness.turn_completed` must close the exact correlated prompt chain; it proves
completion only. Answered/reaction/hold/decline claims additionally require the
correlated authored output or disposition.

For a comms-view delivery, a successful provider response carrying the exact
platform message binding, or an exact nonce-bound Gateway echo, may prove
`transport.submit_ack`. A timeout without provider-idempotency reconciliation is
an ambiguous submission outcome, not proof; the plugin holds it visibly and never
blindly retries outside the provider's bounded dedupe window. None of these egress
signals proves participant processing.

### Channel-native processed path

For a delivery declared `channel-native`, `in-session` is structurally
unreachable, not a degraded receipt. The reducer permits
`transport-submitted -> processed` only when all are true:

- a registered comms-view plugin authenticates the recipient platform principal;
- reply/reaction ingress maps to exact `event_id` through platform-message binding;
- source-event dedupe and self-echo suppression pass;
- the authored event is not an infrastructure marker or plugin-owned reaction;
- audit records `delivery_mode=channel-native`, `session_entry=not_applicable`,
  evidence class, and proof method.

This path never manufactures an `in-session` transition. A human or native
channel participant replying is definitive processing evidence; router accept,
Discord egress success, `notification.read`, and generic bot activity are not.

### Delivery marker and correlation proof

For a CMUX delivery, the core gives the session-transport plugin a bounded
machine-readable marker containing:

- schema version;
- `event_id`, `delivery_id`, and `attempt_id`;
- binding ID plus a short-lived core signature over the marker fields;
- `authority_scope=conversation`.

The plugin renders exactly one marker with the prompt. The marker is correlation,
not authorization: it contains no credential or bearer token. The submitting
provider still authenticates with its binding-scoped credential, while the core
signature prevents marker-field substitution. A direct hook may parse the marker
from the accepted prompt. An event-bus provider may extract only the marker from
a local-sensitive prompt-preview event and must discard the surrounding text.
Copying a marker to another seat/session fails binding validation; replay is
idempotent.

Zero markers means an ordinary local turn and produces no Yatagarasu receipt.
Multiple or malformed markers produce no receipt and a visible proof error; the
provider must not guess which event entered the session.

### Binding and evidence-provider registration

Before an `in-session` receipt can pass, a host-local adapter must register the
authoritative session and its approved proof methods through the internal
binding operation:

```text
register_session_binding(
  seat_id,
  adapter_instance_id,
  harness,
  session_id,
  proof_methods,
  observed_at
) -> binding_id | conflicted | rejected(reason)
```

This is normally called from a session-start hook. An already-running session may
complete an unbound adapter claim on its first marked prompt only when no active
binding exists and the adapter instance/seat credential matches. A different
active session fails closed as `conflicted`; it is never silently replaced.

Each proof method declares its source kind (`direct-hook`, `event-bus`, or future
equivalent), source instance scope, evidence classes, and correlation rule. The
core accepts bus-relayed evidence only from the registered host-resident plugin
principal and only when the proof bundle binds the exact delivery—not merely
because the same `session_id` had some prompt or stop event nearby.

Binding credentials are host-local and scoped to one seat. Session IDs are
opaque identifiers: they are audit metadata, not authentication secrets and not
telemetry dimensions.

### Core validation and failure behavior

The receipt reducer verifies all of the following before advancing state:

- authenticated principal may submit for the binding;
- binding is active, unexpired, and owns the delivery recipient;
- supplied session ID equals the binding's authoritative session ID;
- event, delivery, attempt, binding, and marker signature agree and are not expired;
- evidence class is declared by that harness adapter and legal from current state;
- proof method is registered for the provider and its source event chain satisfies
  the method's exact-delivery correlation rule;
- receipt is idempotent; a duplicate returns the original result;
- a contradictory receipt is rejected and marks adapter health degraded.

The evidence provider is observer-only: inability to reach the receipt endpoint
must not block the human/agent turn. It durably queues the receipt locally for
bounded retry, but the core does not claim `in-session` until it accepts it.

Crucially, a missing proof after `transport-submitted` does **not** trigger blind
message reinjection. The core records `session-proof-unavailable` and waits for
receipt retry/operator repair. Reinjecting solely to chase proof could duplicate
an already-running model turn.

### Round-1 CMUX receipt floor

Each host runs one resident CMUX transport instance against its local socket.
For harnesses whose genuine hooks are relayed into the native event bus, the
approved proof method is `cmux.event_bus.harness_hook_relay`:

- `surface.input_sent` plus the exact marked `workspace.prompt.submitted` event
  proves `transport.submit_ack`;
- the matching `agent.hook.UserPromptSubmit` for the bound `session_id` proves
  `harness.prompt_accepted`;
- the matching `agent.hook.Stop`, with no ambiguous intervening prompt, proves
  `harness.turn_completed` and therefore `processed(completed)`.

The bus is trusted local relay, not the semantic issuer. The originating harness
event determines the evidence class; the bus relay is recorded separately as
`proof_method`. A `session_id` match alone is insufficient. The provider must
extract the signed Yatagarasu marker from the local-sensitive prompt preview and
submit the complete ordered source-event chain. Raw preview/content never leaves
the host-local provider or enters core receipt/audit storage.

Claude Code and Codex are live-observed producers. Any harness absent from the
host event bus remains at `transport-submitted` until separately proven. The
Discord comms/view plugin only consumes derived state; it never manufactures
session proof.

### Delivery-state view feed

Comms/view plugins consume derived state through a core-owned subscription or
snapshot interface. They never receive or replay raw receipts:

```text
delivery_state_changed(
  notification_id, event_id, room_id, recipient_seat,
  previous_state, current_state, disposition?, occurred_at
)
```

The view maps canonical `event_id` to its platform message ID. Notifications are
idempotent; after reconnect, a view requests the current per-seat snapshot for
its active room window rather than reconstructing proof from reactions.

The feed excludes `session_id`, binding credentials, marker signatures, raw proof,
and content. An `in-session` marker means **the authoritative harness accepted
the event**, never “read,” “agreed,” or “is typing.” Infrastructure markers must
be visibly infrastructure-owned and transient. An agent-authored processed
reaction remains a separate canonical authored event; platform application only
renders proof the core already accepted.

`notification.read` may drive an explicitly presentation-owned “seen” hint, but
it is not a core delivery state or proof of human attention. It never advances
`in-session` or `processed` and never enters person-level telemetry.

### Event-level status

An event with multiple recipients has no misleading single “delivered” boolean.
The API/UI reports a recipient matrix plus derived summaries such as:

- `accepted: 4 recipients`;
- `processed: 2, in-session: 1, queued-away: 1`;
- `partial-failure: 1 recipient queue full`.

## Presence and absence

Presence belongs to the seat/binding, not the person as a moral judgment.

- `live` — authoritative binding healthy; normal delivery;
- `focus` — binding healthy; targeted `REQ`/`WARN` may deliver under team policy,
  ordinary traffic queues for bounded catch-up;
- `away` — identity registered, binding intentionally not accepting immediate
  turns; queue persists to TTL;
- `disconnected` — heartbeat/transport lost; queue persists, absence visible;
- `conflicted` — more than one binding claim; delivery paused fail-closed;
- `returning` — binding re-proved and bounded catch-up draining.

Round 1 needs the state model and queue behavior, but only `live`, `disconnected`,
and queueing must be implemented. Focus policy and digest generation are later;
the dumb core must not summarize content itself.

## Floor lease model

Floor control is coordination, not ordinary conversation. It is disabled by
default for `CHAT`.

When enabled for a room:

- 🙋 creates a floor request ordered by `room_seq`;
- the core grants one bounded lease and exposes a visible grant such as 🎙️;
- the lease records holder, triggering event, grant time, and expiry;
- completion, explicit release, disconnect, or timeout advances the queue;
- floor possession affects coordinated reply order, not who may read messages;
- the router never edits or summarizes what the holder says;
- floor state is operational metadata and expires with a short policy window.

Floor leasing is contract-only in Round 1 and implemented with the room plugin in
Round 2 unless QA identifies a local broadcast proof that requires it earlier.

## Authority and memory boundaries

Every room event carries kernel-owned `authority_scope=conversation`.

- The public API exposes communication operations only.
- No event grants shell, deploy, approval, credential, lifecycle, or tool rights.
- Plugins preserve the authority marker when rendering into a session.
- A request for privileged action may be discussed, but execution requires the
  existing trusted approval lane outside Yatagarasu.
- The core has no Musubi writer and emits no automatic memory directive.
- Each authoritative session may independently remember an experience under its
  own continuity policy; that capture is personal and perspectival.
- Audit retention is not memory and must not be exposed as a canonical family
  narrator.

Because the core does not interpret prose, authority safety is a composition of
an unforgeable envelope, narrow API, plugin preservation, and harness/agent
policy. Claiming the router can semantically block every approval-shaped sentence
would be false assurance.

## Plugin contract

Every plugin declares:

- unique plugin ID, version, kind (`comms-view` or `session-transport`);
- supported schema versions and operations;
- authentication principal and allowed actor/seat claims;
- capabilities (`text`, `reply`, `reaction`, `session-proof`, `presence`,
  `reconnect-replay`, future media);
- health/heartbeat and backpressure state;
- dedupe/replay guarantees and source-event namespace;
- receipt evidence classes it can honestly produce;
- supported delivery modes and when session entry is structurally not applicable;
- shutdown/revocation behavior.

Capability absence is explicit. The core never emulates an unsupported operation
in a way that changes authorship or delivery meaning.

Session transport plugins must be host-local to the runtime they bind and connect
outbound to the core. The core never SSHes into a remote host to impersonate a
local bridge. A disconnected remote edge is visibly absent.

An event-stream provider persists `(source_instance_id, boot_id, seq)` only after
atomically committing its derived receipt/outbox side effect. A replay gap or
`boot_id` change triggers topology/binding re-snapshot. It never invents missed
receipts and never reinjects content to repair an evidence gap; the core mailbox
and the provider's bounded receipt replay are separate recovery planes.

## Failure semantics

Stable failure categories must include at minimum:

- unknown or unauthorized actor;
- unknown audience;
- no authoritative binding;
- binding conflict or expiry;
- unsupported operation/capability;
- queue full;
- payload too large or invalid schema;
- duplicate/idempotent replay;
- transient transport failure;
- session-proof unavailable;
- TTL expiry;
- plugin unavailable/rate-limited;
- authority operation refused;
- internal persistence unavailable.

No failure silently switches identities, transports, rooms, or sessions. Retry is
per-recipient, bounded, observable, and uses the same `event_id`.

If persistence cannot commit the mailbox record, the request is rejected and no
plugin dispatch occurs. Yatagarasu must never deliver an event it cannot account
for after claiming acceptance.

## Round 1 design slice

Round 1 implements the smallest vertical slice that preserves the future
contracts:

### Required now

- canonical schema v1 for text `CHAT|INFO|REQ|DONE|WARN` events;
- separate `event_id` and compatibility `correlation_id`/CID;
- identity registry for Vesper seats and one authoritative binding each;
- `send` and `broadcast` core operations;
- immutable roster snapshot and per-recipient mailbox rows;
- client and transport idempotency;
- short-TTL queue primitive with visible per-seat state;
- receipt transitions through the strongest evidence the CMUX plugin can prove;
- audit metadata projection with no content;
- aggregate telemetry with forbidden identity dimensions;
- existing `agent-bridge send` behavior preserved through the CMUX plugin;
- authenticated `register_session_binding` and `submit_receipt` internal ops;
- CMUX event-bus evidence provider with signed-marker correlation;
- transport-side injection journal plus receipt outbox/cursor recovery;
- explicit absence/failure with no proxy fallback;
- conversation-only authority marker;
- retention worker and restart recovery;
- contract tests/fakes so later plugins can conform without a core rewrite.

### Deferred without invalidating the design

- Discord/Gateway comms-view plugin;
- authored platform reactions and floor lease execution;
- additional host-resident CMUX instances beyond the Vesper proof;
- focus/away policy and bounded catch-up presentation;
- digest generation, which belongs outside the dumb core;
- attachments, voice, and rich content;
- agent cards/capability-based team formation;
- multiple rooms and organization tenancy beyond the schema hooks;
- public/open-source packaging;
- direct harness adapters beyond the CMUX transport.

## Acceptance hooks for Tama

Tama's QA design should be able to prove the following against the core without a
real Discord dependency:

1. A caller addresses an identity only; attempts to choose a transport/session
   are rejected or ignored by contract.
2. An accepted event and all recipient mailbox rows survive core restart.
3. `broadcast` records one event, one roster snapshot, and an outcome for every
   seat; one absent seat does not become a proxy or an invisible success.
4. Reusing `correlation_id` across request/reply creates distinct event IDs and
   does not dedupe either message.
5. Retrying the same client request/source event returns the original event and
   causes no duplicate session turn.
6. Legacy `SENT` alone never exceeds `transport-submitted`; native CMUX proof
   requires its documented event chain.
7. Only exact-delivery, binding-aware evidence advances `in-session`.
8. Correlated turn-end advances only `processed(completed)`; authored-output
   evidence is required for dispositions. Authenticated channel-native
   reply/reaction may skip structurally inapplicable `in-session`.
9. Contradictory, stale-binding, or cross-seat receipts are rejected and visible.
10. Queue content expires on policy; audit retains metadata only; telemetry has
    no identity or joinable event dimensions.
11. Queue-full, plugin-down, persistence-down, and binding-conflict states fail
    visibly with stable reasons and no reroute to another identity.
12. Room text carries `authority_scope=conversation`; unsupported privileged API
    operations are refused; no automatic Musubi write occurs.
13. Per-seat FIFO holds across retry and process restart.
14. A comms echo/replayed source event does not create a second canonical event.
15. Revoking/rotating a binding prevents the old adapter from advancing receipts.
16. Presence is shown as operational seat state and is absent from person-level
    telemetry history.
17. CID/correlation reuse cannot complete or advance the wrong event's receipt.
18. A forged, expired, copied-to-another-seat, or stale-binding marker signature is
    rejected without blocking the session turn.
19. Receipt endpoint outage leaves the event at `transport-submitted`, retries
    proof without reinjecting content, and surfaces `session-proof-unavailable`.
20. Bus-relayed Claude/Codex hooks bind the signed marker and ordered source-event
    chain to the exact active session; an unobserved harness stays below the floor.
21. A stale CMUX surface is never reused; every send re-resolves the named seat.
22. Lost remote-plugin acknowledgement or event-stream gap never creates a second
    model turn; the journal/outbox/cursor planes recover conservatively.

## Risks and mitigations

Primary risks are an under-specified “dumb” core, surveillance drift, receipt
inflation, split-brain identity, queue-as-memory, hidden partial broadcast,
compatibility fossilization, and implied router authority. The corresponding
guards are the explicit invariants, data-plane limits, evidence/proof split,
single binding, hard TTL, per-seat matrix, edge-only CID map, and messenger frame.

## Decisions required in cross-review

1. **Identifiers:** unique `event_id`; reusable `correlation_id`; legacy CID maps to correlation.
2. **Retention:** 24-hour unprocessed TTL and one-hour processed grace.
3. **Broadcast:** per-recipient partial outcome when one seat cannot enqueue.
4. **Audit:** actor/recipient allowed in operator audit, forbidden in telemetry.
5. **Replies:** current roster for room replies; no silent private-to-room widening.
6. **Round-1 receipt floor — resolved in design:** CMUX-relayed genuine harness
   events use semantic class `harness.prompt_accepted` and separately audited
   proof method `cmux.event_bus.harness_hook_relay`; unobserved harnesses remain
   at `transport-submitted`.
7. **Channel-native path — resolved:** authenticated authored reply/reaction may skip structurally inapplicable `in-session` without synthesizing it.
