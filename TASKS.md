# Sosopo task board

Open tasks toward the CLAUDE.md vision: an AI-first, open-source social media
management platform that evolves into an AI agent platform for marketing.
Each task is written to be handed to an agent with no other context than this
file plus the repository. Read **Current state** and **Working agreements**
before starting any task. Work on one task per session/branch; respect the
listed dependencies.

## Current state (do not redo)

All six phases of ROADMAP.md are complete and deployed:

- Workspaces with `owner`/`admin`/`editor`/`viewer` roles, memberships, and
  server-side tenant isolation on every query (Phase 1).
- Hashed email invitations, `/invite` acceptance, SMTP with link fallback,
  self-service signup gating, `self_hosted`/`hosted` deployment modes (Phase 2).
- Connection health states, automatic X/LinkedIn/Threads token refresh,
  secret-free workspace export, owner-only workspace deletion (Phase 3).
- Plan limits (free/starter/pro; self-hosted unlimited), monthly usage
  metering (`usage_records`), owner AI budget caps, Stripe Checkout plus a
  signature-verified webhook, workspace-level AI provider keys with instance
  fallback (Phase 4).
- Async AI media jobs (image + OpenAI-style video) with progress, mandatory
  admin moderation, approved library, composer attach, credit refund on
  failure (Phase 5).
- `GET /api/workspaces/status` dashboard data, audited metadata-only
  `GET /api/admin/workspaces`, media/workspace Prometheus gauges (Phase 6).

Suite: 178 tests, all passing (1 skipped without PyYAML).

`app/` is split into focused modules (A1). Dependency order, which is also the
reload order in `app/server.py`: `errors` → `config` → `database` → `security`
→ `http_client` → `audit` → `workspaces` → `plans` → `billing` →
`invitations` → `organizations` → `credits` → `media_storage` → `ai_adapters` → `ai_providers` →
`media_jobs` → `oauth` → `connections` → `schema` → `publishing`.
`app/routes/` then holds the ten HTTP route-family mixins (A1b, B1), which import
from the modules above and are reloaded after them. `app/server.py` holds the
`Handler` (dispatch, shared request helpers, static files), the entrypoint, and
a hand-written re-export block that keeps `app.server` the one public namespace
for tests, `app/worker.py`, `scripts/`, and the container healthcheck.

Two conventions exist because the test suite calls
`importlib.reload(app.server)` after changing the environment:

- Every module supports both entry modes with
  `try: from . import x / except ImportError: import x`, because Docker runs
  `python /app/app/server.py` (script) while tests import `app.server`
  (package).
- `app/server.py` reloads every sibling in dependency order, and the handful of
  names tests replace by assignment (`request_json`, `request_form`,
  `request_get_json`, `request_get_bytes`, `request_delete`,
  `telegram_request`, `publish`, `stripe_request`, `PyJWKClient`,
  `VIDEO_POLL_SECONDS`) are
  **absent** from `app/server.py`'s own namespace. `_SEAMS` plus the `_Facade`
  module type forward reads and writes to the module that defines them, so a
  test replacement is visible to every caller. Add a new patchable seam to
  `_SEAMS`, never to the re-export block.

Other key files: `app/index.html` (single-page portal), `tests/` (fourteen files;
`test_workspaces.py` exports the `WorkspaceHttpCase` live-HTTP harness),
`docs/index.html` + `docs/openapi.yaml` (separate docs site), `scripts/`
(backup/restore/preflight).

## Working agreements

Verification for every task (from CONTRIBUTING.md):

```sh
uv venv .venv --python 3.12 && uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
docker compose config --quiet
docker compose up -d --build
curl -fsS http://127.0.0.1:8088/api/health
```

Hard rules:

1. **Tenant isolation is server-side, always.** Every new query touching
   posts, connections, media, settings, or usage must filter by the session's
   active workspace via `Handler._require_workspace(session, role)`.
2. **Secrets never reach browsers.** Provider/AI credentials are encrypted
   with Fernet (`encrypt_secrets`/`decrypt_secrets`) and are never returned by
   any API, export, or log.
3. **Migrations are additive and idempotent** across SQLite, PostgreSQL,
   MariaDB, and MySQL: `CREATE TABLE IF NOT EXISTS` + `add_table_column()` in
   `setup_database()`, following the existing `%s`-id-column pattern. Never
   rewrite or drop existing columns.
4. **Self-hosted behavior must not regress.** New limits, billing, or hosted
   features default off (or unlimited) when `SOSOPO_DEPLOYMENT_MODE` is
   `self_hosted`.
5. **Tests first, HTTP-level where possible.** Subclass `WorkspaceHttpCase`
   for endpoint tests; mock outbound HTTP by monkeypatching
   `request_json`/`request_form`/`request_get_json` as existing tests do.
6. Conventional commits (`feat:`/`fix:`/`refactor:`/`docs:`), no attribution
   trailers. Update README.md, ROADMAP.md, and docs/index.html when
   user-facing behavior changes.
7. Evaluate every design against CLAUDE.md's decision framework: simpler?
   generalizable? extensible? works for OSS and hosted? scales to large
   organizations?

Priorities: **P0** unblocks other work · **P1** core vision · **P2** valuable
· **P3** research/design first. Effort: S (≤half day), M (a day-ish), L
(multi-day).

---

## A. Architecture and technical debt

### A1 · Modularize `app/server.py` — **done**
Split into 17 modules of 12–329 lines plus `app/server.py`. See
**Current state** for the layout and the two conventions it introduced.
93/93 tests green with no test edits; all three containers healthy.

### A1b · Split the `Handler` route chain — **done**
`app/routes/` holds nine mixins, each owning one slice of the HTTP surface and
returning `True` once it has answered: `public` (setup/sign-in/SSO/health plus
the billing webhook), `connections`, `posts`, `ai`, `admin`, `media`, `team`,
`account`, `billing`. `Handler` keeps dispatch, the shared `_json`/`_session`/
`_require_workspace` helpers, and static file serving.

Reordering routes across families is safe here because **no two route
predicates in `do_GET` or `do_POST` can match the same path** — this was
proven mechanically before the split, and is worth re-proving if you add a
route whose pattern overlaps an existing one. What still matters is the phase
order inside the dispatchers: public routes, then the rate-limit gate, then
sign-out, then the auth gate, then authenticated families, then 404.

`app/server.py` is now 501 lines and every module is under 800.

### A2 · Safe multi-worker job claiming — **done**
`claim_post` and `claim_media_job` add `SELECT … FOR UPDATE SKIP LOCKED` on
PostgreSQL and keep the existing conditional `UPDATE` everywhere else.

The distinction matters and is worth stating precisely: claiming was already
*correct* on every backend — the conditional UPDATE is atomic, so a second
worker sees `rowcount 0` and loses. What PostgreSQL adds is *throughput*:
workers step over a contended row instead of serializing on it. On SQLite
extra replicas add lock contention without adding throughput, so run one.
Documented in README.md and `app/worker.py`.

`tests/test_job_claiming.py` races four threads for the same row and asserts
exactly one wins, for both posts and media jobs, plus lease recovery.

### A3 · Extract portal JavaScript — **done**
The inline script moved to `app/portal.js`, served statically and loaded with
`defer`. `app/index.html` went from ~80 KB to ~30 KB and is now markup only.

The real win is the header: `script-src` dropped `'unsafe-inline'`, so the CSP
now forbids inline script outright. Keep it that way — any new behavior goes
in `portal.js`, never in an inline `<script>` or an `onclick=` attribute.

Deliberately **one** file with section banners rather than several: the script
shares mutable state (`csrfToken`, `aiScope`, `activeOrganization`) across
sections, and with no build step, splitting it would mean hanging that state
on `window`, which is worse than the problem being solved.

**Verified:** sign-in renders under the strict CSP with no console errors, and
every `el('…')` target resolves to an id in the markup (checked mechanically).
The authenticated views were not click-tested, since that needs credentials.

### A4 · OpenAPI description of the HTTP API — **done**
`docs/openapi.yaml` (OpenAPI 3.1) describes all 65 paths / 77 operations,
declares the auth model (`sosopo_session` cookie plus `X-CSRF-Token` on every
state-changing request), and is linked from the docs site at `#api`.

`tests/openapi_routes.py` discovers the HTTP surface from the route mixins by
reading the source, and is the single source of truth shared by the spec and
its check — so the two cannot disagree about what a route is. The test checks
**both** directions: no route missing from the spec, and no spec path that no
longer exists. It also asserts every authenticated POST declares `csrfToken`.

PyYAML is not a runtime dependency, so the coverage check uses a strict
structural parse and the full YAML load runs only when PyYAML happens to be
installed. **When you add a route, add it to the spec in the same change** —
the test will fail otherwise.

## B. Organizations and the credit system

### B1 · Organization layer above workspaces — **done**
`organizations` + `organization_memberships` (roles `owner`/`admin`/`member`)
and a nullable `workspaces.organization_id`. `app/organizations.py` holds the
domain helpers, `app/routes/organizations.py` the endpoints:
`GET|POST /api/organizations`, `GET|POST /api/organizations/<id>/workspaces`,
`GET|POST /api/organizations/<id>/members`. Minimal UI block in the Team tab.

Two rules to keep in mind when building on this:

- **Organization membership is administrative, not a content grant.** An org
  owner still cannot read a workspace's posts; workspace membership and
  workspace roles govern content. There is a regression test for this.
- **Organizations a caller does not belong to answer 404, not 403**, so
  membership is not discoverable by probing IDs.

Personal workspaces keep `organization_id NULL`, and an installation that
never creates an organization behaves exactly as before.

### B2 · Auditable AI credit ledger — **done**
`credit_accounts` (owner_type organization/workspace/user, owner_id, balance,
granted_period) and append-only `credit_transactions` (delta, `balance_after`,
reason, actor, reference). `app/credits.py` owns all of it.

Invariants worth preserving:

- **Only AI usage spends credits.** One credit per AI text generation and per
  media job, charged at the existing quota points in `/api/ai/generate` and
  media job creation, refunded by `run_media_job` on failure. Publishing,
  scheduling, storage, seats, and organizations never touch the ledger; there
  is a regression test asserting that.
- **`record_credit_transaction` is the only write path for a balance**, and it
  raises *before* writing anything when a movement would go negative.
- **The ledger is append-only.** Rows carry `balance_after` so an auditor can
  verify without replaying the table.
- **Self-hosted is unlimited and records nothing.** `credits_enforced()` is
  False unless hosted or `SOSOPO_CREDITS_ENFORCED` is set, and the charge
  helpers are no-ops when it is False.

Hosted plans map to a monthly grant (`ai_generations_per_month +
ai_media_per_month`), topped up lazily on first charge in a new period rather
than by a scheduled job. `usage_records` still carries analytics: usage is a
counter that may be reset, the ledger is an audit trail that may not.

### B3 · Hierarchical credit allocation — **done**
`allocate_credits()` moves credits between accounts as a paired
`allocation_out`/`allocation_in`; the debit runs first, so an over-allocation
raises before the target is credited and the ledger never holds half a
transfer. Every allocation also writes a `credits.allocated` audit event.

`funding_chain()` gives the resolution order for a debit — **user → workspace
→ organization** — and `charge_ai_credit()` spends from the first account in
that chain with a positive balance, returning which one paid. `media_jobs`
carries a `credit_account_id` so a failed job refunds *exactly* the account
that paid, not merely the workspace.

Endpoints (paths deviate from the original task text to carry the org id,
matching the rest of the organization API):
`POST /api/organizations/<id>/credits/allocate`, `GET
/api/organizations/<id>/credits` (org admin: org, per-workspace, and
per-member balances), `POST /api/workspaces/credits/allocate` (workspace
admin, members only), `GET /api/workspaces/credits` (any member: the accounts
that fund their own actions, in resolution order).

Allocation targets are checked for ownership, so credits cannot cross into
another organization's workspace or fund a non-member.

### B4 · Stripe credit top-ups — **done**
One-time `mode=payment` Checkout for credit packs, credited through the
existing signature-verified webhook. `POST /api/workspaces/billing/credits`
(workspace owner) and packs defined by `STRIPE_CREDIT_PACKS`
(`STRIPE_PRICE_CREDITS_SMALL`/`_MEDIUM`/`_LARGE`, overridable with the
`SOSOPO_CREDIT_PACKS` JSON value). A pack is only offered once its Stripe
price ID is set. Subscription plans are unchanged.

`billing_events` gives the webhook idempotency by Stripe event id, so a
replayed `checkout.session.completed` credits exactly once — this protects
subscription events too, not only top-ups. An event with no `id` cannot be
deduplicated and is applied as-is rather than silently dropped.

**Watch out:** `audit()` opens its own connection, so it must never be called
inside an open `db()` block — that deadlocks on SQLite. `apply_billing_event`
collects what happened and audits after the transaction closes.

`stripe_request` is now a `_SEAMS` entry, so tests can intercept Checkout
calls the same way they intercept other outbound HTTP.

## C. AI provider expansion

### C1 · Provider adapter seam — **done**
`app/ai_adapters.py` holds `ChatAdapter` (the OpenAI-compatible default),
`MiniMaxAdapter` (own chat path, no cache-busted catalog URI), and
`OpenRouterAdapter` (`catalog_needs_key = False`). Adapters are pure — they
build requests and parse responses without I/O — so every wire format is
directly unit-testable (`tests/test_ai_adapters.py`).

`AI_PROVIDERS` values are now `AiProvider(slug, environment_prefix, base_url,
adapter)` named tuples; `adapter` defaults to `ChatAdapter`, so **a provider
that speaks the OpenAI shape needs one line in the registry and nothing
else**. No call site branches on a provider name any more.

### C2 · Anthropic Claude provider — P1, M (needs C1)
Native Messages API: `POST {base}/v1/messages` with `x-api-key` +
`anthropic-version` headers, `max_tokens` required, content blocks in the
response; model list from `GET /v1/models`. Preset models: current Claude
generation (e.g. `claude-sonnet-4-5`, check the docs current at
implementation time). Works at both instance and workspace scope; key
encrypted like all others.
**Accept:** mocked-HTTP tests for request shape, auth header (never Bearer),
response parsing, and model refresh; README/docs updated.

### C3 · Google Gemini provider — P1, S/M (needs C1)
Use Gemini's OpenAI-compatible endpoint (`…/v1beta/openai/`) to reuse the
default adapter; only base URL, key handling, and model presets differ.
Verify model listing works through the compatible endpoint, otherwise adapt.
**Accept:** same test coverage as C2.

### C4 · Grok (xAI) and DeepSeek providers — P2, S (needs C1)
Both are OpenAI-compatible (`https://api.x.ai/v1`,
`https://api.deepseek.com`). Add presets, default models, env fallbacks
(`SOSOPO_AI_GROK_*`, `SOSOPO_AI_DEEPSEEK_*`), docs.
**Accept:** provider appears in both scopes with save/refresh/remove tests.

## D. AI marketing capabilities

### D1 · Brand voice profiles — P1, M
Per-workspace brand profile (tone, audience, do/don't phrases, sample posts,
default hashtags; ≤4 KB) stored in `workspace_settings` as plain JSON, edited
by workspace admins in the AI tab, and injected as system-prompt context into
`generate_post_copy` and media `media_job_prompt` (style hint). A composer
toggle ("Apply brand voice", default on when a profile exists).
**Accept:** HTTP tests: only admins edit; prompt injection verified via
captured mock payloads; generation without a profile unchanged.

### D2 · AI content calendar generation — P1, L (better after D1)
"Plan my week": editor supplies a brief, cadence, and target channels; the AI
returns N post drafts with proposed local schedule times; Sosopo creates them
as **drafts** (state `draft`, never auto-scheduled or published) tagged in a
new `campaigns` table (id, workspace, name, brief, created_by) with
`posts.campaign_id` nullable column. UI: a "Plan with AI" panel that lists the
generated drafts for review; scheduling stays the existing manual flow.
Charge one AI credit per generated draft.
**Accept:** drafts land unscheduled and workspace-scoped; malformed AI output
degrades to an error, never partial junk (parse strictly, insert
transactionally); quota/credit tests.

### D3 · AI analytics summarization — P2, M
`POST /api/workspaces/summary` (admin+): feed `GET /api/workspaces/status`
data plus the last 30 days of deliveries into the configured text provider
and return a plain-language summary with observations and suggestions.
Read-only, one AI credit, output clearly labeled as AI-generated in the
overview panel.
**Accept:** mocked-provider test asserting the prompt contains real metrics
and no secrets; viewer/editor get 403.

### D4 · Campaign agent design (RFC) — P3, M (needs D1, D2)
Design doc (`docs/rfcs/0001-campaign-agent.md`) for a multi-step agent:
brief → strategy → calendar → drafts → (later) performance feedback loop.
Cover: step orchestration on the existing job-queue pattern, human approval
gates (reuse moderation model), credit accounting per step, failure recovery,
and multi-agent collaboration boundaries. No implementation.
**Accept:** RFC reviewed against the CLAUDE.md decision framework, with an
incremental delivery plan whose first slice is shippable in ≤1 week.

### D5 · Auto-reply research (RFC) — P3, S
Inbound comment/mention APIs differ wildly per platform and most need extra
review/permissions. Produce `docs/rfcs/0002-auto-reply.md`: per-provider
feasibility (Meta, X, Telegram, Discord), webhook vs polling, safety rails
(never reply without a workspace-approved template/policy), and moderation.
**Accept:** RFC only; explicit go/no-go recommendation per platform.

## E. Publishing depth (Postiz parity)

### E1 · LinkedIn image publishing — P1, M
LinkedIn is text-only today (`publish()` rejects images). Implement the
member-image flow: register upload (`POST /rest/images?action=initializeUpload`),
PUT the bytes, attach the image URN to the post payload. Respect
`CHANNEL_MEDIA_LIMITS` (raise LinkedIn from 0 to its real limit) and update
validation, README, and docs.
**Accept:** mocked-HTTP test covering initialize/upload/post sequence and
error paths; text-only posts unchanged.

### E2 · Publish approved library videos — P2, L
Media studio produces videos, but posts only attach images. Start narrow:
allow one approved library video per post for Telegram (`sendVideo`) and
Discord (webhook attachment/URL embed). Extend `post_media` usage, composer
picker, and per-channel validation (`CHANNEL_MEDIA_LIMITS` split into
image/video rules). Meta reels/X video are follow-ups — note them in
ROADMAP, do not attempt in this task.
**Accept:** validation rejects videos on unsupported channels with clear
messages; delivery tests with mocked providers; moderation gate still applies.

### E3 · Alt text end-to-end — P2, S/M
`post_media.alt_text` exists but is unused. Add alt-text inputs per attached
image in the composer, store it, and send it where providers support it
(X `media/metadata`, Facebook `alt_text_custom`, LinkedIn image alt). Include
alt text in workspace export.
**Accept:** persisted + delivered in mocked provider payloads; export test.

### E4 · Post-performance ingestion — P2, L
Pull basic metrics for published posts where the connected credential allows
(X public metrics, Facebook page post insights, Telegram views via Bot API
where available). New `post_metrics` table (post_target, metric, value,
fetched_at); worker refreshes on a slow cadence (≥1h) with per-provider
backoff; surface per-post metrics in delivery history and aggregate into
`GET /api/workspaces/status` (feeds D3).
**Accept:** mocked fetch tests, graceful skip when a provider/credential
cannot report metrics, no rate-limit hammering (cadence test).

## F. Platform and operations

### F1 · Plugin architecture RFC — P3, M
CLAUDE.md requires a plugin-friendly architecture. After A1 lands, write
`docs/rfcs/0003-plugins.md`: registration points for channel providers and AI
providers (the C1 adapter seam is the prototype), packaging (pip entry
points vs drop-in modules), sandboxing/trust model for self-hosted installs,
and what stays core vs plugin.
**Accept:** RFC with a migration path for the two existing registries.

### F2 · Browser end-to-end test — P2, M
One Playwright (or equivalent) test in `tests/e2e/` covering: setup → create
post → invite flow page renders → switch workspace → media tab renders.
Runs against `docker compose up` locally and in CI as an optional job; keep
the unittest suite dependency-free.
**Accept:** e2e job green locally; README dev section documents how to run it.

### F3 · Workspace import (restore) — P2, M
The export exists; imports don't. `POST /api/workspaces/import` (owner of a
fresh, empty workspace) accepting the export JSON: recreates posts (as drafts,
media URLs preserved when reachable), members are **not** imported (invite
separately), connections are recreated disabled and secretless, requiring
rotation. Strict schema validation; size cap.
**Accept:** round-trip test export→import; injected/malformed JSON rejected;
no secret fields accepted even if present in the file.

---

## Suggested order

1. ~~**A1**~~ and ~~**C1**~~ are done. C2/C3/C4 can now run in parallel; A1b
   alongside them.
2. ~~**B1 → B2 → B3 → B4**~~ done: the credit system arc is complete.
3. D1 → D2 → D3 build directly on the credit + provider work.
4. E-tasks are independent of A–D and safe for parallel agents.
5. RFCs (D4, D5, F1) can run any time; implementation waits for review.
