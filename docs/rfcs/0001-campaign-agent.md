# RFC 0001 — Campaign agent

**Status:** Draft, awaiting review · **Depends on:** D1 (brand voice), D2 (content calendar) · **Supersedes:** nothing

## Summary

D2 turns one brief into one batch of drafts in a single provider call. A
campaign agent runs the same journey as *several* steps that persist between
them — brief → strategy → calendar → drafts → (later) performance feedback —
so a human can inspect and correct the reasoning, not just the output.

This RFC proposes no new execution machinery. Sosopo already has a durable job
queue with leases, retries, and a mandatory human approval gate; the agent is
those pieces composed differently.

## Why not just a longer prompt

D2's single call is the right tool for "plan my week". It breaks down when:

- **The strategy is wrong but the drafts are fine.** Today you regenerate
  everything and pay again. With a persisted strategy step you correct the
  strategy and re-run only what follows.
- **A step fails.** A malformed plan today loses the whole run. With steps,
  the completed work survives and only the failed step retries.
- **Cost is invisible.** One call is one charge. Multi-step work should show
  what each step cost *before* the expensive steps run.
- **Nothing can be reviewed mid-flight.** Approval today is all-or-nothing at
  the end. Marketing work needs a decision point after strategy.

## Design

### Steps

| Step | Input | Output | Credits |
|---|---|---|---|
| `strategy` | brief, brand voice, workspace metrics | positioning, themes, channel mix | 1 |
| `calendar` | strategy, cadence, channels | dated slots (theme + channel per slot) | 1 |
| `drafts` | calendar slot | one post draft | 1 per slot |
| `review` | drafts | ranked issues, no mutation | 1 |

Steps are pure functions of persisted input. Re-running a step never depends
on in-memory state from a previous one, which is what makes recovery and
correction cheap.

### Schema

```
agent_runs        id, workspace_id, campaign_id, goal, state, created_by, created_at, updated_at
agent_steps       id, run_id, kind, position, state, input_json, output_json,
                  credit_account_id, error, started_at, finished_at
```

`state` for a run: `planning`, `awaiting_approval`, `running`, `succeeded`,
`failed`, `cancelled`. For a step: `pending`, `running`, `succeeded`,
`failed`, `skipped`.

`agent_steps.output_json` is the only channel between steps. No step reads
another step's provider response directly.

### Orchestration

Reuse the media-job pattern exactly (`app/media_jobs.py`): a worker claims the
oldest `pending` step whose predecessors have succeeded, with `FOR UPDATE SKIP
LOCKED` on PostgreSQL (A2). One step per claim. The run advances when a step
finishes; a step that fails leaves the run resumable rather than restarting it.

This gives leases, retry, crash recovery, and multi-worker safety for free.
**Do not build a separate scheduler.**

### Approval gates

Reuse the media moderation model (Phase 5): a run pauses at
`awaiting_approval` after `strategy` and again after `calendar`. An admin
approves, edits, or cancels. Drafts still land as `state='draft'` and are
never scheduled or published by the agent — **the agent's output ceiling stays
exactly where D2 put it.** Nothing an agent produces reaches an audience
without a human scheduling it.

### Credit accounting

One charge per step, at step start, through `charge_ai_credit`, storing
`credit_account_id` on the step so a failed step refunds the account that
actually paid (B3's rule). A run estimates its total cost up front and refuses
to start if the funding chain cannot cover the *approved* portion — the user
should never discover mid-run that they cannot afford the drafts.

### Failure recovery

- Step fails retryably → worker retries with the existing backoff, capped.
- Step fails permanently → run goes `failed`, completed steps persist, the
  user may correct the input of the failed step and resume.
- Worker dies → lease expires and the step is re-claimed, exactly as posts and
  media jobs recover today.
- Provider returns unparseable output → that step fails; earlier steps are
  untouched. There is no partial-calendar state, because each step commits
  transactionally (D2's rule).

### Multi-agent boundaries

Explicitly **out of scope** for the first slices. When it arrives, the boundary
is: agents communicate only through `agent_steps.output_json` rows, never
through shared memory or direct calls. That keeps every hand-off auditable and
replayable, and it means a second agent is a new step kind, not new
infrastructure.

## Decision framework check

- **Simpler?** Fewer new concepts than it looks: a run is a list of jobs, and
  jobs already exist.
- **Generalizable?** `agent_steps.kind` is a registry, like the AI provider
  registry (C1). A new step is a new kind plus a handler.
- **Extensible?** The feedback loop (E4 post metrics) becomes one more step
  kind reading `post_metrics`, not a redesign.
- **OSS and hosted?** Yes. Self-hosted is unlimited, so cost estimation is
  informational there and enforcing in hosted, matching B2.
- **Scales?** Steps are claimed independently, so a large org's runs
  parallelize across workers on PostgreSQL.

## Incremental delivery

**Slice 1 (≤1 week, shippable alone):** `agent_runs` + `agent_steps`, the
worker claim loop, and exactly two step kinds — `strategy` and `calendar` —
with one approval gate between them. Drafts still come from D2's existing
endpoint, invoked manually after the calendar is approved. This delivers the
"correct the strategy without regenerating everything" win, which is the main
complaint D2 leaves open, and it proves the orchestration before any of the
harder steps exist.

**Slice 2:** `drafts` as a step kind, so an approved calendar produces drafts
without a second manual action. D2's endpoint stays as the one-shot path.

**Slice 3:** cost estimation and the pre-flight affordability check.

**Slice 4:** `review` step; then the performance feedback loop once E4 lands.

## Open questions for review

1. Should a run own its campaign, or attach to an existing one? Attaching is
   more flexible; owning makes cleanup obvious.
2. Is one approval gate (after strategy) enough for slice 1, or do reviewers
   want a second after the calendar?
3. Should a failed step be editable in place, or should correction always fork
   a new run so the audit trail is strictly append-only?
