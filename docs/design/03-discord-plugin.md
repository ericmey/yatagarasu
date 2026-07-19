---
status: draft
class: project
created_at: 2026-07-18
last-verified: 2026-07-18
description: "Yatagarasu Discord comms-view plugin (Round 2) design — Gateway room ingress/egress, reactions. Owned by Shiori; collected into the shared project by Aoi for cross-review."
tags:
  - harem-ops/project
sources: []
---

# Yatagarasu — Discord Plugin (Round 2) Design

**Owner:** Shiori
**Status:** DRAFT (Gate 1 - Revised for Core Conformance)
**Authority:** Conforms to `01-core.md` (Yua)

## 1. Scope, Capabilities, & Responsibility
The Discord Plugin is the "Comms/View" adapter for Yatagarasu. It connects the central Yatagarasu routing core to a human-visible Discord channel. It leverages a standard, high-level Discord library (e.g., `discord.js` or `discord.py`) to abstract WebSocket and REST interactions.

**Capability Declaration:**
- **Kind:** `comms-view`
- **Supported:** `text`, `reply`, `reaction`, `reconnect-replay`.
- **Unsupported:** `session-proof`, `presence` (handled by transport adapters).
- **Source-Event Namespace:** `discord`
- **Actor Credential Model:** Multi-token. Each seat (e.g., Aoi, Yua, Shiori, Tama) provides its own authenticated Discord bot token. The plugin multiplexes these tokens so each seat authors its own messages and reactions natively. (No single router bot masquerading as everyone, no self-bots).
- **Health/Shutdown:** Declares rate-limit backpressure to the core; cleanly closes WebSockets on shutdown.

## 2. Ingress (Discord -> Yatagarasu Core)
The plugin maintains a persistent WebSocket connection to the Discord Gateway using the configured bot tokens.

**Authentication, Allowlisting, & Authority:**
- Listens *only* to configured room channels.
- Enforces an identity allowlist (e.g., Eric, Aoi, Yua, Shiori, Tama). Messages from unauthorized users or unknown bots are immediately dropped before hitting the core.
- Discord ingress has **no authority-bearing fields**. The plugin never parses Discord roles, channel permissions, or mentions as authorization. The Core canonicalization always assigns `authority_scope=conversation`, and the plugin never strips or mutates this field.

**Canonical Ingress Mapping (The Envelope):**
Discord `MESSAGE_CREATE` events are parsed into the deterministic Yatagarasu Core schema:
- Discord Channel ID -> `room_id`
- Discord Author ID -> authenticated `actor` principal
- Discord Message ID -> `source_event_id` (namespaced to `discord`)
- Discord Timestamp -> untrusted `created_at`
- Message Body -> `content` (with `content_kind=text`)
- Default `type=CHAT`, `audience=room`, `expect=[]` (unless explicit addressing syntax overrides).
- Discord Reply Reference -> source reply reference, which the core resolves to canonical `reply_to=event_id`.
- (Legacy/Task CID mapped strictly to optional `correlation_id`, never used for event identity).
- The Core assigns `event_id`, `accepted_at`, and `room_seq`.

**Channel-Native Receipt Ingress:**
- For Discord-only participants (e.g., humans, or agents without a local CMUX session), the plugin declares `delivery_mode=channel-native`.
- When the plugin receives an authenticated `MESSAGE_CREATE` or `MESSAGE_REACTION_ADD` via the Discord Gateway that binds to a known platform message, it explicitly emits `participant.reply_authored` or `participant.reaction_authored` to the core.
- The core accepts these native events as definitive proof of processing. The audit log reflects `session_entry=not_applicable`. The plugin MUST NOT synthesize fake `in-session` proofs for these participants.

**Reaction Source Identity:**
- A Discord `MESSAGE_REACTION_ADD` payload has no unique event snowflake. To ensure deduplication survives `RESUME` without collisions, the plugin assigns a restart/replay-stable `source_event_id` to reactions: `discord:<shard_id>:<session_id>:<dispatch_seq>`.
- The target message ID, user ID, and emoji are retained as proof.

**Echo Suppression & Deduplication (Restart-Safe):**
- The legacy rule `if (msg.author.bot) return` is removed to allow agent-authored messages.
- The plugin must bind the egress Discord message ID to the exact canonical `event_id`.
- The plugin persists the `event_id <-> discord_message_id` egress correlation and the Gateway resume state.
- Echo suppression is event-type and authenticated-author aware:
  - `MESSAGE_CREATE`: Suppressed ONLY when the exact outbound message binding AND our author credential match.
  - `MESSAGE_REACTION_ADD`: Suppressed ONLY when the exact actor token + target message + emoji/type matches our own outbound reaction intent. (We do not suppress other participants' reactions to our messages).
- This suppression survives process restart, managed by durable Core TTLs. We rely on exact event/platform binding, not reusable workflow correlation IDs.

## 3. Egress (Yatagarasu Core -> Discord)
When an agent speaks or reacts, the Core dispatches the event to the Discord plugin.
*Note:* The Core never authors conversational text. The plugin only emits `actor-authored events` (via the actor's specific bot token) or explicitly labeled `infrastructure-owned status rendering`.

**Sending Messages & Egress Idempotency:**
- The plugin uses the actor's bot token to post the `content` via the Discord REST API, strictly maintaining `authority_scope=conversation` as defined by the core.
- The canonical envelope is **not** printed in the Discord UI. The UI remains clean. The plugin relies entirely on the Core's `event_id <-> discord_message_id` mapping.
- **Idempotency & Ambiguous Failure (Nonce):** Discord supports `nonce` + `enforce_nonce` uniqueness for the same author. The plugin must require a stable per-event `nonce` on every REST retry and journal the returned `message_id`. Outside Discord's bounded nonce deduplication window, an ambiguous timeout is held as a visible ambiguous outcome—never blind retry.
- **Strict REST Error Mapping:**
  - `404 Not Found` (deleted message/channel) and `403 Forbidden` (lost permissions) are explicitly mapped to terminal Yatagarasu delivery failures (e.g., `target_deleted`, `permission_revoked`) and fed back to the core. They are NEVER treated as retryable.
  - `429 Too Many Requests` is mapped to rate-limit/backpressure handling (bounded retry-wait).
  - Other 4xx client errors are explicitly mapped to non-retryable rejection failures.

**Applying Reactions (The Floor Protocol):**
- When the Core sends a `react` event (e.g., `👀`, `·`), the plugin uses the actor's token to `PUT` the reaction to the corresponding `discord_message_id`.

## 4. Receipt Evidence Classes (Honest Proof)
The Discord plugin is a `comms-view` adapter, not a session adapter.
- A successful Discord REST post (or correlated Gateway echo) emits **at most** `transport-submitted`.
- The plugin **declares `session-proof=false`**.
- The plugin may submit `processed` receipts for **channel-native participants** (humans via Discord ingress), but it NEVER manufactures harness proof or submits `in-session` or `processed` for **session-bound agent seats**. For agents, it ONLY consumes and displays session receipts.
- Applying a reaction via Discord REST merely paints an already-authored outcome onto the UI; it does not create processing evidence.

## 5. Reconnect, Resume, & Content Retention
- **Gateway Resume:** The plugin persists the last applied Gateway sequence. On disconnect, it attempts `RESUME` and replays missed events through the source-event deduplicator. No duplicate canonical events or model turns are created. If resume is impossible, it re-identifies and exposes the unproven gap visibly. (No silent REST-poll fallback).
- **Content Retention:** Discord is the platform of record for human visibility. Plugin-local content buffers are strictly bounded to active processing/replay needs, then immediately discarded. No credentials or raw payloads enter Yatagarasu telemetry.

## 6. QA Dependencies & Test Boundaries
Positive channel-native acceptance fixtures and false-positive negative tests belong exclusively in Core QA (`tests/`), NOT in the CMUX tests. The Discord plugin will supply mock Gateway payload fixtures (e.g., `MESSAGE_CREATE` JSON blobs) to validate the core's ingress parsing and the `delivery_mode=channel-native` receipt behavior without requiring a live Discord network connection during CI.