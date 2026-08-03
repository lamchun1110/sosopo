# Sosopo product roadmap

This roadmap describes the work required to evolve Sosopo from its current
single-instance, per-user publishing application into a self-hosted and hosted
multi-workspace platform.

## Phase 1 — Workspaces and memberships

- [ ] Add `workspaces`: name, slug, owner, plan, status, and timestamps.
- [ ] Add `workspace_memberships`: user, workspace, role, invite state, and timestamps.
- [ ] Support `owner`, `admin`, `editor`, and `viewer` roles.
- [ ] Add an active-workspace selector to the portal.
- [ ] Migrate existing posts, media, connections, AI settings, and relevant audit records to workspace ownership.
- [ ] Keep a safe default workspace for existing installations during migration.
- [ ] Allow one user to belong to multiple workspaces.
- [ ] Scope every server-side query and action to membership in the active workspace.
- [ ] Make channels/connections available to authorized workspace members.
- [ ] Make posts and media workspace-owned while retaining the author for audit history.
- [ ] Add tenant-isolation and role-permission tests.

## Phase 2 — Invitations and identity

- [ ] Replace temporary-password member creation with expiring email invitations.
- [ ] Store only hashed invitation tokens, with sender, role, expiry, and acceptance time.
- [ ] Add an invite acceptance page for password setup or SSO sign-in.
- [ ] Add transactional email configuration.
- [ ] Add self-service signup for hosted deployments.
- [ ] Create the first workspace automatically for a newly registered user.
- [ ] Support Google Workspace/OIDC just-in-time provisioning where configured.
- [ ] Retain a local-administrator recovery route for self-hosted installs.

## Phase 3 — Hosted OAuth and tenancy

- [ ] Introduce explicit `self_hosted` and `hosted` deployment modes.
- [ ] Scope OAuth connection state and returned destinations to a workspace.
- [ ] Keep OAuth client secrets server-side and tokens encrypted at rest.
- [ ] Add connection health, token-expiry alerts, refresh, and reconnect flows.
- [ ] Support per-workspace Facebook, Instagram, Threads, X, LinkedIn, Telegram, and Discord connections.
- [ ] Add privacy, data-export, and deletion workflows required by provider policies.
- [ ] Document OAuth app-review requirements for hosted deployments.

## Phase 4 — Plans, AI credits, and billing

- [ ] Add workspace plans, subscriptions, limits, and usage records.
- [ ] Meter posts, connected channels, members, AI copy, generated media, and storage.
- [ ] Add quotas, budget caps, and clear limit messages.
- [ ] Integrate a billing provider.
- [ ] Support platform-provided AI credits and bring-your-own provider keys.
- [ ] Move AI provider configuration from instance-wide to workspace-level settings.

## Phase 5 — AI media studio

- [ ] Add a workspace-scoped media library.
- [ ] Add asynchronous AI image-generation jobs with status/progress.
- [ ] Add asynchronous video-generation jobs with status/progress.
- [ ] Add prompt, aspect ratio, brand style, and variation controls.
- [ ] Add moderation and review before generated media can be published.
- [ ] Store generated assets in local disk or S3-compatible media storage.
- [ ] Make generated assets selectable in the composer.
- [ ] Charge AI-media usage against workspace credits and budgets.

## Phase 6 — Operations and product maturity

- [ ] Add workspace dashboard, channel health, post analytics, and usage screens.
- [ ] Add tightly audited support/admin access controls.
- [ ] Add tenant-aware backup, export, restore, and deletion workflows.
- [ ] Add monitoring, error tracking, rate limits, abuse prevention, and incident runbooks.
- [ ] Extend the separate documentation site with hosted and self-hosted guides.
- [ ] Add migration, integration, and end-to-end regression coverage.

## Recommended implementation order

1. Complete Phase 1 before offering a multi-customer hosted service.
2. Add invitations and hosted identity in Phase 2.
3. Complete OAuth tenancy isolation in Phase 3.
4. Add billing and platform AI credits in Phase 4.
5. Add image generation, then video generation, in Phase 5.
