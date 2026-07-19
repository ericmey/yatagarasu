# cmux

**Role:** session-transport

Delivers into a live cmux-hosted agent session and proves session entry from the host event bus. Declares session-bound only; never emits participant evidence.

## Harness next-turn profiles

Delivery never inspects the composer or guesses whether a seat is busy. It
injects immediately and uses the authoritative binding's harness profile:

| Harness | Text sent | Submit key | Plain Enter while busy |
| --- | --- | --- | --- |
| Claude Code | signed envelope | `enter` | queues another message |
| Codex | signed envelope | `tab` | steers the active turn |
| Hermes | `/queue ` + signed envelope | `enter` | interrupts by default |

The harness/TUI owns next-turn buffering. The plugin owns selecting the explicit
non-interrupting action, then classifying the observed CMUX receipt chain. It has
no busy/idle gate, external FIFO, composer scrape, or focus mutation.

The profile table is grounded in the harness contracts, not terminal scraping:
Claude Code documents Enter as queuing additional messages while it works;
Codex's TUI keymap and tooltip source reserve Tab for “queue for next turn” and
use Enter to steer an active turn; Hermes 0.18.2 defaults busy input to
``interrupt`` and exposes ``/queue <prompt>`` as its explicit pending-input
command. Unsupported or absent harness identities fail before terminal contact.

> Status: scaffold. See ../../ARCHITECTURE.md and CONTRIBUTING.md.
