# Contributing

## The gated process

Work moves through gates, and the gates are real:

```
DESIGN → CROSS-REVIEW → APPROVAL → BUILD-OPEN → BUILD-CLOSED
```

- **Design first.** Nothing is built from a ticket alone. A stream gets a design
  document, and that document names its own open questions.
- **Cross-review is adversarial by intent.** A review that finds nothing is not a
  passing grade — it is an unusable review. Reviewers are expected to reject,
  including rejecting their own earlier approval when they find they skimmed.
- **Conditions are gate content, not a delay of it.** "Yes, with conditions" is a
  complete answer. A reviewer who refuses both a clean yes and a no, and instead
  names what is missing, is doing the job correctly.
- **Build-open is not build-closed.** A tracer subset passing opens build. The
  full acceptance suite passing closes it. **A partially-run suite is no evidence
  at all** — that is the same false-clear as a test that probes the wrong path.

## Writing acceptance tests

Tests here are written to catch failure modes that are **invisible at runtime**.
Conventions:

- **Print literal observations, never classifications.** A result grid shows the
  actual status code, event name, or count. `OK` / `PASS` belongs in a verdict
  block *outside* the data, because a cell reading `OK` is one keystroke from
  reading as `200 OK` — and if `200` is the failure you are hunting, the label has
  defeated the test at the visible layer.
- **Every adversarial test names its reopen condition.** State explicitly what
  result would mean the defect is back.
- **Assert on the assembled evidence bundle, not on every entry.** Requiring a
  field on an event type that does not emit it makes the test go red on correct
  data, which pressures the implementation into fabricating fields to stay green.

## Documentation

- Documents state **present truth**. Git carries edit history.
- Do not leave a review diary inline ("an earlier revision said…", "this was
  wrong"). **But never delete the rationale that prevents recurrence** — rewrite it
  as a present-tense invariant plus a one-sentence note on the trap.
- When you change one section, grep the document for claims that now contradict
  it. Fixing a section is not the same as making a document consistent.

## Verification habits

Borrowed from incidents, kept because they were cheap and would have caught real
defects:

- After `git push`, confirm the remote actually moved.
- A search **you** constructed is evidence about your search, not about the world.
  Show the command output; never summarize a sweep whose filters you wrote.
- Cite by anchor text, not bare line numbers, in any document under active
  revision — line-number citations rot silently.

## Collaboration

Multiple agents and people work these repos concurrently.

- **Use a worktree per workstream.** Concurrent edits to shared files produce
  silent clobbers, and the loser of the race gets a result that looks like success.
- **Announce a shared file before opening it**, or hand the owner a patch.
- The **merge authority** for this repo is a single named role. Route conflicts
  there rather than resolving them in parallel.
