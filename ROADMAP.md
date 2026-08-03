# Sosopo product roadmap

This roadmap describes the work required to evolve Sosopo from its current
single-instance, per-user publishing application into a self-hosted and hosted
multi-workspace platform.

## Phase 1 — Workspaces and memberships (complete)

- [x] Add `workspaces`: name, slug, owner, plan, status, and timestamps.
- [x] Add `workspace_memberships`: user, workspace, role, invite state, and timestamps.
- [x] Support `owner`, `admin`, `editor`, and `viewer` roles.
- [x] Add an active-workspace selector to the portal.
- [x] Migrate existing posts, media, connections, and relevant audit records to workspace ownership. AI provider settings deliberately stay instance-wide until Phase 4 moves them to workspace-level configuration.
- [x] Keep a safe default workspace for existing installations during migration: every existing user receives an isolated personal workspace, so no historical data becomes visible to another user.
- [x] Allow one user to belong to multiple workspaces.
- [x] Scope every server-side query and action to membership in the active workspace.
- [x] Make channels/connections available to authorized workspace members.
- [x] Make posts and media workspace-owned while retaining the author for audit history.
- [x] Add tenant-isolation and role-permission tests.

## Phase 2 — Invitations and identity (complete)

- [x] Replace temporary-password member creation with expiring email invitations. Local account creation remains available to instance administrators as a recovery path.
- [x] Store only hashed invitation tokens, with sender, role, expiry, and acceptance time.
- [x] Add an invite acceptance page for password setup or an already signed-in account.
- [x] Add transactional email configuration (SMTP with STARTTLS); without it, invite links are surfaced to workspace admins for manual sharing.
- [x] Add self-service signup for hosted deployments (`SOSOPO_ALLOW_SELF_SIGNUP`, on by default in hosted mode).
- [x] Create the first workspace automatically for a newly registered user.
- [x] Support Google Workspace/OIDC just-in-time provisioning where configured.
- [x] Retain a local-administrator recovery route for self-hosted installs.

## Phase 3 — Hosted OAuth and tenancy (complete)

- [x] Introduce explicit `self_hosted` and `hosted` deployment modes.
- [x] Scope OAuth connection state and returned destinations to a workspace.
- [x] Keep OAuth client secrets server-side and tokens encrypted at rest.
- [x] Add connection health, token-expiry alerts, refresh, and reconnect flows. Automatic refresh covers X, LinkedIn, and Threads; other providers surface expiry alerts for manual rotation/reconnection.
- [x] Support per-workspace Facebook, Instagram, Threads, X, LinkedIn, Telegram, and Discord connections.
- [x] Add privacy, data-export, and deletion workflows: secret-free workspace JSON export and owner-only workspace deletion that disables connections and unschedules queued posts.
- [x] Document OAuth app-review requirements for hosted deployments.

## Phase 4 — Plans, AI credits, and billing (complete)

- [x] Add workspace plans (free/starter/pro; self-hosted stays unlimited), subscriptions, limits, and usage records.
- [x] Meter posts, connected channels, members, AI copy, generated media, and storage.
- [x] Add quotas, budget caps (owner-set monthly AI cap), and clear limit messages.
- [x] Integrate a billing provider (Stripe Checkout plus an HMAC-verified webhook; enabled only in hosted mode with keys configured).
- [x] Support platform-provided AI credits (instance-wide keys metered by plan) and bring-your-own workspace provider keys.
- [x] Move AI provider configuration from instance-wide to workspace-level settings, with the instance configuration retained as a fallback.

## Phase 5 — AI media studio (complete)

- [x] Add a workspace-scoped media library.
- [x] Add asynchronous AI image-generation jobs with status/progress.
- [x] Add asynchronous video-generation jobs with status/progress (OpenAI-style `/videos` providers; unsupported providers fail with a clear message).
- [x] Add prompt, aspect ratio, and brand style controls. Variations are achieved by re-running a job with edited controls.
- [x] Add moderation and review before generated media can be published.
- [x] Store generated assets in local disk or S3-compatible media storage.
- [x] Make generated assets selectable in the composer (approved images attach directly).
- [x] Charge AI-media usage against workspace credits and budgets, with automatic refund when a job fails.

## Phase 6 — Operations and product maturity (complete)

- [x] Add workspace dashboard, channel health, post analytics, and usage screens.
- [x] Add tightly audited support/admin access controls (instance-admin oversight is metadata-only and always audit-logged).
- [x] Add tenant-aware backup, export, restore, and deletion workflows (per-workspace export/deletion plus the existing instance-level backup/restore drill).
- [x] Add monitoring, error tracking, rate limits, abuse prevention, and incident runbooks.
- [x] Extend the separate documentation site with hosted and self-hosted guides.
- [x] Add migration, integration, and end-to-end regression coverage.

## Recommended implementation order

All phases are implemented. Before selling a hosted service, additionally complete provider app review (Meta/X/LinkedIn), configure Stripe products and webhooks, run the backup/restore drill against the production database backend, and load-test with realistic tenant counts.
