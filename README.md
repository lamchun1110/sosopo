# Sosopo

Sosopo is a self-hosted social-media publishing workspace. It supports local accounts, optional OpenID Connect SSO, isolated user data, drafts, image attachments, timezone-aware scheduling, and provider-based delivery.

Licensed under [MIT](LICENSE). See [CONTRIBUTING.md](CONTRIBUTING.md) for local checks, [SECURITY.md](SECURITY.md) for private vulnerability reporting, and [ROADMAP.md](ROADMAP.md) for the multi-workspace hosted-platform roadmap.

## Documentation website

Sosopo ships a separate, responsive documentation site in `docs/`. Compose runs it as the independent `sosopo-docs` service on `127.0.0.1:8090`; it does not share the application session or database. It contains first-run setup, channel connection, publishing, AI-provider, operational, and troubleshooting guides.

For a Cloudflare Tunnel, Traefik, Caddy, or Nginx Proxy Manager deployment, route a separate hostname such as `docs.sosopo.jacky.tech` to `http://sosopo-docs:8080` on the shared `frontend` Docker network. The service is ready after `docker compose up -d --build`; use `curl -fsS http://127.0.0.1:8090/` to verify it locally.

## What works today

- Organize work in shared workspaces with `owner`, `admin`, `editor`, and `viewer` roles; posts and channel connections belong to the workspace, are shared with its members, and are isolated from every other workspace.
- Invite members by email with expiring, hash-stored invitation links; optional self-service signup for hosted deployments.
- Monitor connection health with token-expiry alerts; X, LinkedIn, and Threads tokens refresh automatically before they expire.
- Meter usage against workspace plans in hosted mode (self-hosted stays unlimited), with Stripe Checkout upgrades and owner-set AI budget caps.
- Configure AI providers per workspace (bring your own key) with the instance-wide configuration as fallback.
- Generate AI images and videos in an asynchronous media studio with admin review before anything can be published.
- Export a workspace's data as secret-free JSON, or delete a workspace safely.
- Create drafts for Facebook, Instagram, Threads, X, Telegram, Discord, and LinkedIn.
- Schedule a post in any IANA timezone (for example `Asia/Hong_Kong` or `Europe/London`); it is stored as UTC for reliable delivery.
- Upload one byte-validated PNG, JPEG, GIF, or WebP image per post (maximum 5 MB).
- Publish immediately or through the built-in scheduler after a provider is configured.
- Record each delivery attempt, provider error, and external post ID.
- Persist posts, schedules, and uploads in `./data`.
- Check service readiness at `GET /api/health`.

Provider credentials are not included. On first visit, create the local administrator account (minimum 12-character password). The administrator can provision local users through the protected API; OpenID Connect can automatically provision ordinary users when explicitly enabled. The dashboard supports encrypted manual account entry, multi-account targets, and OAuth account connection for Facebook, Threads, X, and LinkedIn.

First-run setup is single-use and atomically guarded: only one initial administrator can be created, even if several browsers open a fresh instance simultaneously.

Sosopo rejects known-invalid posts before scheduling: Instagram requires an image; text limits are Facebook 5,000, Instagram 2,200, Threads 500, X 280, Telegram 4,096, Discord 2,000, and LinkedIn 3,000 characters. LinkedIn publishing is text-only in this release. Provider plans and API policies can change, so use provider sandbox validation before relying on any limit for a large-scale workflow.

## Run with Docker Compose

```sh
docker compose up -d --build
docker compose ps
```

Compose starts two services: `sosopo` serves the authenticated web/API interface and `sosopo-worker` claims and delivers scheduled posts. Keep both running. For production, use PostgreSQL; the scheduled rows and delivery attempts persist in the database so a web-process restart does not discard queued work.

**Scale workers only on PostgreSQL.** Job claiming is atomic on every supported backend, so a second worker never steals a post or media job another worker already claimed. Only PostgreSQL additionally uses `SELECT … FOR UPDATE SKIP LOCKED`, which lets parallel workers step over a contended row instead of serializing on it. On SQLite, extra worker replicas add lock contention without adding throughput — run one.

The worker emits a database heartbeat on every delivery poll. Compose marks it unhealthy if three polling periods elapse without a heartbeat; check both services with `docker compose ps` and inspect failures with `docker compose logs sosopo-worker`.

Transient delivery failures are retried with exponential backoff (30 seconds, 60 seconds, then 120 seconds) before a post is moved to the failed-post queue after three attempts. HTTP 429 and 5xx responses are retryable; a provider `Retry-After` value is honoured up to one hour. Other provider 4xx responses are placed in the failed-post queue immediately to avoid repeatedly sending a known-invalid request. Review the delivery history, correct the issue, and use **Retry failed delivery** to reset the counter and queue it again.

Each delivery has a five-minute worker lease. If a worker dies during a delivery, the lease is recovered and the post is retried. This provides durable at-least-once delivery; a provider request that succeeded immediately before a process crash can be delivered twice unless that provider/account supports an idempotency mechanism. Review provider-side posts when recovering a worker after an outage.

Open [http://localhost:8088](http://localhost:8088). The service is intentionally bound to `127.0.0.1`, so it is not exposed to your LAN by default.

Useful commands:

```sh
docker compose logs -f sosopo
docker compose down
```

Data lives in `./data`, including `sosopo.sqlite3` and uploaded images. When you first start this renamed version, an existing `social-desk.sqlite3` is automatically renamed to preserve your posts. Back up that directory while the service is stopped, or use SQLite's backup facility for a live backup. Do not commit it or publish it in an image.

## Configuration and deployment

| Setting | Default | Purpose |
| --- | --- | --- |
| `SOSOPO_DATA_DIR` | `/data` in Compose | Directory containing the SQLite database and image uploads. |
| `DATABASE_URL` | unset | Optional database URL. Supports SQLite, PostgreSQL, MariaDB, and MySQL. |
| `SOSOPO_STORAGE_BACKEND` | `local` | `local` disk uploads or S3-compatible object storage. |
| Host port | `127.0.0.1:8088` | Browser entry point. Change only when protected by a reverse proxy and authentication. |

## Connect providers

Copy the configuration template, generate a persistent encryption key, and fill in the credentials for the platforms you use:

```sh
cp .env.example .env
docker compose up -d --build
```

`.env` is ignored by Docker build context and must never be committed. **Set a valid, persistent `SOSOPO_ENCRYPTION_KEY` and restart Sosopo before saving any channel or AI-provider API key.** It encrypts stored credentials; do not change it after accounts have been connected. The current environment variables remain supported for the first single-account publishing path.

Every sensitive setting also accepts a `_FILE` variant. This is recommended for Docker Secrets, Kubernetes Secrets, or a mounted secret-manager volume: for example set `SOSOPO_ENCRYPTION_KEY_FILE=/run/secrets/sosopo_encryption_key` or `DATABASE_URL_FILE=/run/secrets/sosopo_database_url`. The file contents are read at startup and override the matching environment value. Mount the secret read-only and restart both services after rotation.

### Local disk or S3 media storage

Local disk (`SOSOPO_STORAGE_BACKEND=local`) is the default and stores uploads in `./data/uploads`. To use Amazon S3, MinIO, or another S3-compatible store, set `SOSOPO_STORAGE_BACKEND=s3`, `S3_MEDIA_BUCKET`, optional `S3_MEDIA_PREFIX`/`S3_ENDPOINT_URL`, AWS credentials, and `SOSOPO_MEDIA_PUBLIC_URL`. The public URL must be HTTPS and map to the bucket/prefix (for example `https://media.example.com/uploads`); providers must be able to fetch each image after it is scheduled. Keep bucket write credentials private and grant public read only through a scoped CDN/bucket policy for the media prefix.

### AI post writing

Sosopo can generate draft post copy inside the composer. An administrator configures it in the dedicated **AI providers** tab in the left rail: choose OpenAI, OpenRouter, Kimi, MiniMax, or Z.AI GLM; paste the provider API key; and choose the default model from a dropdown. Credentials are encrypted at rest and never returned to the browser. Sosopo owns the provider endpoint presets, so administrators do not need to enter a base URL or model-list URL.

Each provider starts with a maintained set of sensible text-model choices. OpenAI includes the GPT-5.6 Sol/Terra/Luna and GPT-5.5 entries requested by the workspace; Kimi includes K3, K2.7 Code, and K2.6; MiniMax includes current M2 variants. The setup order is important: **(1)** set `SOSOPO_ENCRYPTION_KEY` and restart the stack, **(2)** paste the provider API key and click **Save AI provider**, then **(3)** click **Refresh model list**. Refreshing needs the saved API key for providers that protect their catalog; OpenRouter is the exception because its catalog is public. The composer uses the first configured provider and its selected default model, so collaborators never need to manage provider credentials or model IDs.

The provider list shows whether a UI-saved API key exists and offers **Remove API key**. This permanently removes that encrypted UI credential and its cached model list. If the same provider is configured with deployment environment variables, it becomes available again through those variables; remove those environment values separately if it must be fully disabled.

For **MiniMax Token Plan**, paste the Token Plan API key from the MiniMax console into the MiniMax provider entry and save it. Sosopo selects `MiniMax-M2.7` by default, so saving does not require a successful model refresh. Refreshing later calls MiniMax’s documented `https://api.minimax.io/v1/models` endpoint with that bearer token; it updates the dropdown only when MiniMax makes the catalog available to the key.

Environment variables remain supported as an optional deployment/migration fallback, but users do not need to edit `.env` when the administrator configures providers in the UI.

```env
SOSOPO_AI_OPENAI_API_KEY=...
SOSOPO_AI_OPENAI_MODEL=gpt-4.1-mini

SOSOPO_AI_OPENROUTER_API_KEY=...
SOSOPO_AI_OPENROUTER_MODEL=openai/gpt-4.1-mini

SOSOPO_AI_KIMI_API_KEY=...
SOSOPO_AI_KIMI_BASE_URL=https://provider.example/v1
SOSOPO_AI_KIMI_MODEL=your-model-id
```

The composer sends the user's brief, selected destinations, and optional draft to the configured provider, then puts returned copy into the editor for review; it never publishes automatically. API keys remain server-side and are not returned to browsers. OpenRouter documents its OpenAI-compatible `chat/completions` endpoint and model catalog in its [quickstart](https://openrouter.ai/docs/quickstart).

OpenClaw’s OpenAI OAuth uses its bundled Codex runtime and a ChatGPT/Codex subscription, while its MiniMax OAuth uses a provider-specific Coding Plan plugin. Those are not interchangeable with the public, server-to-server generation APIs used by Sosopo. Sosopo therefore uses direct provider API keys: this is the public authentication method documented by [OpenAI](https://platform.openai.com/docs/api-reference/backward-compatibility), [MiniMax](https://platform.minimax.io/docs/faq/about-apis), and [Z.AI](https://docs.z.ai/api-reference/introduction). OpenClaw’s separate OAuth routes are described in its [OpenAI provider guide](https://github.com/openclaw/openclaw/blob/main/docs/providers/openai.md) and [MiniMax provider guide](https://docs.openclaw.ai/providers/minimax).

### Account connections

One owner can hold multiple Facebook Pages, Instagram accounts, Threads profiles, X accounts, Telegram channels, Discord webhooks, or LinkedIn authors. Connection secrets are encrypted with Fernet before being written to the database and are never returned by the API. The composer can select several platforms and one or more connected accounts per platform in a single post. Delivery is independently recorded for each account, so a failure on one destination can be retried without reposting to accounts that already succeeded. A single-platform post may still use legacy environment credentials when no connected account is selected.

Posts can include up to 10 images, with provider-specific limits checked before saving (X accepts up to 4). Sosopo stores the attachments in their selected order and sends provider carousel/media-group requests where supported. Images are decoded and validated on upload. Use public HTTPS media storage because providers must be able to fetch scheduled attachments.

Use **Remove** in the content queue to permanently remove an unpublished draft, scheduled item, or failed item. It cannot interrupt a post already being delivered. For published posts, **Delete from channels** uses each recorded connection and remote post ID to delete the delivered item from Facebook, Instagram, Threads, X, Telegram, Discord, and LinkedIn. Sosopo reports success or failure per channel; a partial failure leaves the remaining published records intact so it can be investigated or retried safely.

The publish timezone is an IANA timezone dropdown populated by the browser (for example `Asia/Hong_Kong`). Sosopo converts the chosen local date/time to UTC and stores the selected timezone with the post.

The dashboard's **Connected accounts** form creates and disables account records. The underlying `POST /api/connections` API requires a signed-in session holding the workspace `admin` or `owner` role plus its CSRF token and accepts a provider, display name, external account ID, optional settings, and provider secrets. Use `access_token` for Facebook, Instagram, Threads, X, and LinkedIn; use `bot_token` for Telegram; use `webhook_url` for Discord. `external_account_id` is the Page/account/profile ID, LinkedIn author URN, or Telegram chat/channel ID. For Discord, paste the full incoming webhook URL: Sosopo derives and stores only its webhook ID outside the encrypted secret. Manual encrypted credential entry remains available for providers or environments where OAuth is not configured.

### OAuth account connection

For a no-copy connection experience, configure an OAuth application once per Sosopo instance. Signed-in users can then use **Connect** beside Facebook, Threads, X, LinkedIn, or Discord; Sosopo redirects to the provider, verifies a short-lived one-time state, discovers the available account(s), and stores the returned credentials encrypted. Facebook connection also discovers linked Instagram professional accounts; Discord asks the user to choose a server channel and returns a webhook for it.

Set the matching client ID and client secret from `.env.example`, then register this exact HTTPS redirect URL in each provider app:

```text
https://your-sosopo-domain.example/api/social-oauth/callback
```

For Meta, request the Page and Instagram publishing permissions listed in `.env.example` and ensure the signing-in person manages the Page. Meta production use can require app review and business verification. For X, configure OAuth 2.0 Authorization Code with PKCE and user-context posting access. LinkedIn member OAuth uses `w_member_social`; organization Page posting needs the separately approved `w_organization_social` permission and a manual organization URN. Discord uses an OAuth-approved per-channel incoming webhook. Telegram continues to use a BotFather token and channel/chat ID because its Bot API has no equivalent account-approval flow.

### Create provider apps and bots

You only need to create a provider app or bot for the channels you intend to publish to. Do this once for the Sosopo instance, then users connect their own approved Pages/accounts from the dashboard.

#### Facebook Pages and Instagram

1. Create a Meta developer app and add Facebook Login plus the Pages/Instagram APIs.
2. Add `https://your-sosopo-domain.example/api/social-oauth/callback` as the exact valid OAuth redirect URI.
3. Set `FACEBOOK_OAUTH_CLIENT_ID` and `FACEBOOK_OAUTH_CLIENT_SECRET` in `.env` (prefer their `_FILE` equivalents in production).
4. Request `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`, `instagram_basic`, and `instagram_content_publish` in the Meta app. The person connecting must manage the Facebook Page; Instagram publishing additionally needs a linked professional Instagram account.
5. For production users outside your Meta app roles, complete any Meta app review and business-verification requirements before enabling the connection button.

Users then select **Connect** beside Facebook in Sosopo, sign in to Meta, approve the requested access, and choose from the discovered Pages. Sosopo automatically adds linked Instagram professional accounts. Do not ask users for their Facebook password or place a Page token in a screenshot, ticket, or chat message.

#### Threads

1. Create a Threads developer app.
2. Register the same Sosopo social OAuth callback URL.
3. Set `THREADS_OAUTH_CLIENT_ID` and `THREADS_OAUTH_CLIENT_SECRET`.
4. Enable `threads_basic` and `threads_content_publish` for the app, then use **Connect** beside Threads.

#### X

1. Create an X developer project/app with user-context posting access.
2. Configure OAuth 2.0 Authorization Code with PKCE and register the Sosopo callback URL.
3. Set `X_OAUTH_CLIENT_ID` and `X_OAUTH_CLIENT_SECRET`.
4. Enable at least `tweet.read`, `tweet.write`, `users.read`, and `offline.access`, then use **Connect** beside X.

#### LinkedIn

1. Create a LinkedIn developer application and add the **Share on LinkedIn** product.
2. Register `https://your-sosopo-domain.example/api/social-oauth/callback` as its OAuth redirect URL.
3. Set `LINKEDIN_OAUTH_CLIENT_ID`, `LINKEDIN_OAUTH_CLIENT_SECRET`, and the current `LINKEDIN_API_VERSION` in `.env`.
4. Use **Connect** beside LinkedIn to authorize a member profile with `w_member_social` and add it to Sosopo.

Member publishing is available through OAuth and is text-only in this release. To publish as a LinkedIn organization, obtain the required LinkedIn organization access, then add a manual encrypted connection with the organization `urn:li:organization:...` and a token with `w_organization_social`. Sosopo does not discover organization Pages automatically because that permission is not self-service for every LinkedIn app.

#### Discord

1. Create a Discord application in the [Developer Portal](https://discord.com/developers/applications) and add `https://your-sosopo-domain.example/api/social-oauth/callback` as its OAuth redirect URL.
2. Set `DISCORD_OAUTH_CLIENT_ID` and `DISCORD_OAUTH_CLIENT_SECRET` in `.env`.
3. In Sosopo, select **Connect** beside Discord. Discord will ask the user to choose a server and destination channel; after approval, Sosopo securely stores the returned webhook.

One OAuth approval creates one destination channel; repeat it to publish to multiple Discord channels. Sosopo sends text and up to ten public image URLs as webhook embeds. If a shared instance cannot create a Discord application, the **Connect an account** form still accepts a manually created incoming webhook URL as a fallback. Do not expose either type of webhook URL—it can post to that channel.

#### Telegram

1. Open `@BotFather` in Telegram and run `/newbot`.
2. Save the bot token as a secret. Treat it like a password and rotate it with BotFather if exposed.
3. Add the bot as an administrator of the target channel or group, with permission to post messages.
4. In Sosopo, choose Telegram under **Connect an account**, enter the bot token, and enter the chat/channel ID (for a public channel this can be `@channel_name`).

Telegram does not support a Page-style OAuth approval flow for this use case, so its bot token is entered manually. [Telegram’s BotFather tutorial](https://core.telegram.org/bots/tutorial) explains bot creation and token rotation.

API clients may include `token_expires_at` as a future ISO 8601 timestamp with a timezone. Expired accounts cannot be selected for a new post and the worker fails them before sending any provider request; the dashboard displays the expiry when present.

Disable a connection with `POST /api/connections/{id}/disable` (CSRF protected). It is retained for delivery history but cannot be selected for new posts; any pending target using it fails safely rather than falling back to unrelated environment credentials. Rotate the provider credential at the provider as well.

Replace an expired or compromised credential using the dashboard's **Rotate token** control or `POST /api/connections/{id}/rotate` with a `secrets` object and optional future `token_expires_at`. Rotation preserves historical references, re-enables the account, and never returns either old or new secrets.

| Provider | Required variables | Current publishing mode |
| --- | --- | --- |
| Facebook Pages | OAuth client credentials or `FACEBOOK_PAGE_ID`, `FACEBOOK_PAGE_ACCESS_TOKEN` | Text, image, and multi-image posts. |
| Instagram | Meta OAuth client credentials or `INSTAGRAM_ACCOUNT_ID`, `INSTAGRAM_ACCESS_TOKEN`; `SOSOPO_PUBLIC_URL` for images | Image and carousel posts. Image is required. |
| Threads | OAuth client credentials or `THREADS_USER_ID`, `THREADS_ACCESS_TOKEN`; `SOSOPO_PUBLIC_URL` for images | Text, image, and carousel posts. |
| X | OAuth client credentials or `X_ACCESS_TOKEN` | Text and up to four images. |
| LinkedIn | OAuth client credentials or `LINKEDIN_AUTHOR_URN`, `LINKEDIN_ACCESS_TOKEN`, `LINKEDIN_API_VERSION` | OAuth member publishing and manual organization URNs; text-only. |
| Discord | A manually connected incoming webhook or `DISCORD_WEBHOOK_URL` | Text and up to ten image embeds per webhook/channel. |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` or a manually connected bot | Text and multi-image messages; the bot must be an administrator of the target channel/group. |

Facebook and Instagram use `META_GRAPH_BASE_URL` (default shown in `.env.example`); Threads uses `THREADS_API_BASE_URL`. Keep API versions configurable and review provider changelogs before upgrading.

For Facebook, Instagram, and Threads, create and approve the corresponding Meta developer app and obtain long-lived tokens with the required publishing permissions. Their servers fetch an attached image directly, so `SOSOPO_PUBLIC_URL` must be a publicly reachable HTTPS address—not `localhost`. X needs an approved developer project and a user-context access token. LinkedIn uses the versioned Posts API and its Share on LinkedIn product. Discord is configured by a per-channel incoming webhook. Telegram is configured through BotFather plus a target chat/channel ID. See the official [Meta Graph API documentation](https://developers.facebook.com/docs/graph-api/), [Threads API documentation](https://developers.facebook.com/docs/threads/), [X post management guide](https://docs.x.com/x-api/posts/manage-tweets/introduction), [LinkedIn Posts API](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api?view=li-lms-2024-10), [Discord webhook resource](https://docs.discord.com/developers/resources/webhook), and [Telegram Bot API](https://core.telegram.org/bots/api).

The container runs as a non-root user. On Linux, ensure the host `data/` directory is writable by container UID `10001` before starting it:

```sh
sudo chown -R 10001:10001 data
```

The supplied Compose services also use a read-only root filesystem, drop Linux capabilities, prohibit privilege escalation, and provide only a small `noexec` `/tmp`; persistent writes belong in `./data` and `./backups`. Keep these defaults when creating a proxy or database override.

## Use PostgreSQL or MariaDB

SQLite remains the simplest default for one-person deployments. To use an existing PostgreSQL, MariaDB, or MySQL server, create the database and an application-only user, then put one URL in `.env` before starting Sosopo. The schema is created automatically on the first connection.

PostgreSQL:

```dotenv
DATABASE_URL=postgresql://sosopo:replace-this-password@postgres.example.internal:5432/sosopo
```

MariaDB/MySQL:

```dotenv
DATABASE_URL=mysql://sosopo:replace-this-password@mariadb.example.internal:3306/sosopo
```

Special characters in usernames/passwords must be URL-encoded (for example, `@` becomes `%40`). Use TLS for any database outside the Docker host and restrict the database user to the Sosopo database only. Never point a fresh external database at the same `data/` SQLite file: `DATABASE_URL` selects one database backend.

For a local or single-server PostgreSQL deployment, use the maintained override rather than copying YAML from the README:

```sh
cp .env.postgres.example .env
# Edit .env: set matching, long random POSTGRES_PASSWORD and DATABASE_URL values.
```

Use the actual override filename:

```sh
docker compose -f compose.yaml -f compose.postgres.yaml up -d --build
docker compose -f compose.yaml -f compose.postgres.yaml ps
```

The PostgreSQL database is private to the Compose network, persistent in the named `postgres-data` volume, and must be included in the backup/restore drill. The supplied `POSTGRES_IMAGE` is digest-pinned; deliberately update it only after testing.

For public access, put the service behind HTTPS. Sosopo has local login and encrypted connection storage, but a reverse proxy is still recommended for TLS, request-size limits, and operational controls.

## Public domain and HTTPS

Sosopo is bound to `127.0.0.1:8088` by default. Keep it that way and put Caddy, Nginx, Traefik, or another HTTPS reverse proxy in front of it. Set the canonical public address in `.env`:

```dotenv
SOSOPO_PUBLIC_URL=https://social.example.com
```

This URL is used for secure session cookies, the OpenID Connect callback, and public provider image URLs. Configure the proxy to forward requests to `http://127.0.0.1:8088`; do not publish port 8088 directly. A minimal Nginx location is:

```nginx
location / {
  proxy_pass http://127.0.0.1:8088;
  proxy_set_header Host $host;
  proxy_set_header X-Forwarded-Proto https;
}
```

By default Sosopo records and rate-limits the direct peer address. If a trusted reverse proxy should supply real client IPs, set `SOSOPO_TRUSTED_PROXY_CIDRS` to only that proxy's direct source CIDR (for a host-local proxy: `127.0.0.1/32,::1/128`). Sosopo then accepts the first `X-Forwarded-For` value only from those peers; never trust broad or unrelated networks.

### Choose one ingress option

All options below publish the same application. Set `SOSOPO_PUBLIC_URL=https://social.example.com` in `.env`, point DNS at the selected ingress, and keep the Sosopo port bound to localhost. Do not run more than one of these proxies for the same hostname.

#### Cloudflare Tunnel

Cloudflare Tunnel is the preferred option when the server is behind NAT or cannot accept inbound ports: `cloudflared` makes outbound-only connections to Cloudflare. Create a tunnel in **Cloudflare Zero Trust → Networks → Tunnels**, add a Public Hostname for `social.example.com` that targets `http://sosopo:8080`, and save its token as `CLOUDFLARE_TUNNEL_TOKEN` in `.env`.

Create a private `compose.cloudflare.yml`:

```yaml
services:
  cloudflared:
    image: cloudflare/cloudflared:2026.7.3
    command: tunnel --no-autoupdate run --token ${CLOUDFLARE_TUNNEL_TOKEN}
    restart: unless-stopped
    depends_on:
      sosopo:
        condition: service_healthy
```

Start it with:

```sh
docker compose -f compose.yaml -f compose.cloudflare.yml up -d
```

Apply a Cloudflare Access policy before enabling SSO or exposing an administrator login. Keep the tunnel token out of source control, and use the dashboard’s connector-health alerts. Cloudflare Tunnel provides edge TLS; it does not replace Sosopo authentication.

#### Caddy

Caddy is the simplest internet-facing reverse proxy because it obtains and renews HTTPS certificates automatically. Install Caddy on the host and use this `Caddyfile`:

```caddyfile
social.example.com {
  reverse_proxy 127.0.0.1:8088
}
```

Reload Caddy after confirming public DNS points to the server. Open only ports 80 and 443 to Caddy; do not open 8088.

#### Traefik

For a Docker-based proxy, add the following labels to the `sosopo` service in a private Compose override. This assumes Traefik is already configured with a `websecure` entry point and a `letsencrypt` certificate resolver named `le`.

```yaml
services:
  sosopo:
    labels:
      - traefik.enable=true
      - traefik.http.routers.sosopo.rule=Host(`social.example.com`)
      - traefik.http.routers.sosopo.entrypoints=websecure
      - traefik.http.routers.sosopo.tls.certresolver=le
      - traefik.http.services.sosopo.loadbalancer.server.port=8080
```

Place Traefik and Sosopo on the same Docker network. Do not expose the Traefik dashboard publicly.

#### Nginx Proxy Manager

In **Hosts → Proxy Hosts**, create a host for `social.example.com` with:

| Field | Value |
| --- | --- |
| Scheme | `http` |
| Forward Hostname/IP | Host machine IP, or `sosopo` when Nginx Proxy Manager shares the Compose network |
| Forward Port | `8088` on the host, or `8080` for the shared Docker network |
| Websockets Support | Enabled |
| SSL | Request a new Let's Encrypt certificate; force SSL and enable HTTP/2 |

If Nginx Proxy Manager and Sosopo run in Compose together, attach both services to the same non-public network and use `sosopo:8080`. Restrict the Nginx Proxy Manager administration UI to trusted IPs.

## Workspaces and roles

Sosopo organizes posts, media, and channel connections into workspaces. Every user starts with a personal workspace, can create more, and can belong to several at once. The active workspace is chosen with the selector in the top bar, and every server-side query and action is scoped to the signed-in member's active workspace.

Workspace roles are separate from the instance `admin`/`user` roles:

| Workspace role | Allows |
| --- | --- |
| `viewer` | Read the workspace queue, channels, and delivery history. |
| `editor` | Everything a viewer can, plus creating, scheduling, publishing, retrying, and removing posts, uploading images, and AI drafting. |
| `admin` | Everything an editor can, plus connecting, rotating, and disabling channel accounts and managing workspace members. |
| `owner` | Everything an admin can, plus granting or revoking the workspace `admin` role. The workspace creator is its owner and cannot be removed or demoted. |

Workspace admins add existing local or SSO accounts by username in **Team → Workspace members**, or send email invitations with a role: invitation tokens are stored only as SHA-256 hashes, expire after 7 days, and can be revoked while pending. When SMTP (`SOSOPO_SMTP_HOST`, `SOSOPO_SMTP_PORT`, `SOSOPO_SMTP_USERNAME`, `SOSOPO_SMTP_PASSWORD`, `SOSOPO_SMTP_FROM`, optional `SOSOPO_SMTP_STARTTLS`) is not configured, the invite link is shown to the admin for manual sharing. Recipients accept at `/invite`, either by creating a local account or while signed in. Instance administrators can still create local accounts directly in **Team → Instance accounts** as a recovery route. Channel connections belong to the workspace, so every authorized member can select them in the composer while the credentials stay encrypted server-side and are never returned to a browser. Posts record their author for audit history but are owned by the workspace.

Workspace admins can also export the workspace's posts, members, delivery history, and connection metadata (never secrets) as a JSON download, and the owner can delete a workspace: deletion disables its channel connections, unschedules queued posts, and drops it from every member's selector. The **Team → Workspace overview** panel shows the plan, member/usage counts against limits, channel health, and media-job states; the same data is served by `GET /api/workspaces/status`.

Upgrading an existing installation is safe: on the first start after this upgrade, each existing user automatically receives an isolated personal workspace containing exactly the posts, connections, and audit history they already owned. Nothing is merged, so nothing becomes visible to another user. AI provider configuration deliberately remains instance-wide until Phase 4 of the roadmap. One provider account can currently be connected in only one workspace per connecting user; reconnecting the same account from a second workspace is rejected with a clear error.

## Hosted mode, plans, and billing

`SOSOPO_DEPLOYMENT_MODE` selects `self_hosted` (default) or `hosted`. Self-hosted workspaces use the unlimited `self_hosted` plan, so existing installations keep today's behavior. In hosted mode, new workspaces start on the `free` plan and can upgrade to `starter` or `pro`; per-plan limits cover members, connected accounts, monthly posts, AI text generations, AI media generations, and media storage, and can be overridden with a `SOSOPO_PLAN_LIMITS` JSON value. Every limit failure returns a clear message naming the limit. Self-service signup is on by default in hosted mode and off in self-hosted mode; override either with `SOSOPO_ALLOW_SELF_SIGNUP`.

Billing uses Stripe in hosted mode: set `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_STARTER`, and `STRIPE_PRICE_PRO`, then point a Stripe webhook at `POST /api/billing/webhook`. Sosopo verifies the `Stripe-Signature` header (HMAC, 5-minute tolerance) before applying `checkout.session.completed` upgrades or `customer.subscription.deleted` downgrades. Workspace owners start an upgrade from **Team → Workspace administration**; the checkout happens entirely on Stripe, and card details never touch Sosopo. Owners can also set a monthly AI action cap that limits combined AI text and media generations regardless of plan.

Workspace owners and admins can save their own AI provider API keys per workspace in the **AI providers** tab; workspace keys are encrypted, never returned to browsers, and override the instance-wide configuration for that workspace only. The instance-wide configuration remains as a platform-provided fallback whose usage is metered by the workspace's plan. Hosted operators should also complete each provider's app review before launch: Meta (Facebook/Instagram/Threads) requires app review, business verification, and a public data-deletion procedure — Sosopo's workspace export and deletion workflows plus account disablement satisfy the data-access and deletion parts; X and LinkedIn require approved developer applications with the scopes listed above.

## AI media studio

Editors generate images or videos in the **Media** tab: choose the type, aspect ratio, provider, optional model, prompt, and an optional brand-style hint. Jobs run asynchronously in the worker with visible status and progress; image generation uses the provider's OpenAI-compatible `images/generations` endpoint, and video generation uses the OpenAI-style asynchronous `/videos` flow (providers without a supported media model fail with a clear message). Every successful result waits in **pending review** until a workspace admin approves or rejects it; only approved, workspace-owned assets appear in the library, can be attached in the composer, or can be published. Generated assets are stored in the same local-disk or S3 media storage as uploads, count against storage limits, and each job consumes one AI-media credit that is refunded automatically if the job fails.

## Users and SSO

Sosopo has `admin` and `user` roles. Posts and account connections belong to one workspace and are filtered server-side by the active workspace membership. The initial local account is an administrator. Administrators use the dashboard's **User administration** section or `GET`/`POST /api/admin/users` to list/create local users; creation needs `username`, a 12-character-or-longer `password`, optional `role` (`user` or `admin`), and optional IANA `timezone`. Disable a compromised or departed user with `POST /api/admin/users/{id}/disable` (CSRF protected); it immediately invalidates every session and blocks both local and SSO login. An administrator cannot disable themself.

Every signed-in user can sign out from the dashboard; this calls the CSRF-protected `POST /api/logout` endpoint. Administrators can also disable another active user from the dashboard, with immediate session revocation.

Local users can rotate their password through the dashboard or `POST /api/me/password` with `current_password` and `new_password`. It requires a 12-character minimum, invalidates every existing session, and returns a new session for the current browser. SSO-only users should rotate credentials at their identity provider.

Administrative and content-changing actions are recorded in the database audit log. An administrator can retrieve the latest 200 records from `GET /api/admin/audit-events`; the log stores actor, action, target, source IP, and timestamp but never provider secrets or post content.

`GET /api/admin/status` is an administrator-only operational summary: post counts by state, worker heartbeat status, and the newest delivery failure. The worker removes expired sessions/OIDC state and audit metadata older than `SOSOPO_AUDIT_RETENTION_DAYS` (365 by default). Export audit records before reducing this retention period if your organization requires longer retention.

Generic OpenID Connect is supported for Keycloak, Google Workspace, Microsoft Entra ID, Auth0, and other standards-compliant providers:

```dotenv
OIDC_ISSUER_URL=https://id.example.com/realms/sosopo
OIDC_CLIENT_ID=sosopo
OIDC_CLIENT_SECRET=replace-this-secret
OIDC_ALLOW_SIGNUP=false
```

Common issuer values:

| Identity system | `OIDC_ISSUER_URL` |
| --- | --- |
| Google Workspace | `https://accounts.google.com` |
| Microsoft Entra ID | `https://login.microsoftonline.com/<tenant-id>/v2.0` |
| On-premises Active Directory | Your AD FS issuer, usually `https://adfs.example.com/adfs` |

For Google Workspace, restrict the OAuth consent screen/application to the Workspace organization. For Microsoft Entra ID, register Sosopo as a **Web** application in the desired tenant. Direct LDAP/Active Directory authentication is intentionally not used; use Entra ID or AD FS as the OpenID Connect bridge. Microsoft requires the exact HTTPS callback URL to be registered. [Microsoft redirect-URI guidance](https://learn.microsoft.com/en-us/entra/identity-platform/reply-url) [AD FS OpenID Connect support](https://learn.microsoft.com/en-us/windows-server/identity/ad-fs/overview/ad-fs-openid-connect-oauth-flows-scenarios)

Register this redirect URI with the identity provider:

```text
https://social.example.com/api/auth/oidc/callback
```

Set `OIDC_ALLOW_SIGNUP=true` only after restricting access at the identity provider; new SSO identities are created as ordinary users. Local administrator login remains available for recovery. The sign-in page provides **Sign in with SSO** when the provider is configured.

## Timezones

Each user has a default timezone (initially `UTC`), and each scheduled post can specify its own IANA timezone. For API clients, send a local ISO date/time plus `scheduled_timezone`:

```json
{"scheduled_for":"2026-08-10T09:30","scheduled_timezone":"Asia/Hong_Kong"}
```

Sosopo converts this to UTC before persisting and delivering it. Update the signed-in user's default through `POST /api/me/timezone` with `{ "timezone": "Asia/Hong_Kong" }`.

## API summary

| Endpoint | Method | Purpose |
| --- | --- | --- |
| `/api/dashboard` | `GET` | Posts and provider configuration status. |
| `/api/health` | `GET` | Liveness/readiness check, including a database query. |
| `/api/setup-status` | `GET` | Whether the first local administrator must be created. |
| `/api/setup`, `/api/login`, `/api/logout` | `POST` | Local account and session lifecycle. |
| `/api/auth/oidc/login` | `GET` | Start configured OpenID Connect sign-in. |
| `/api/session` | `GET` | Current user, role, timezone, and CSRF token. |
| `/api/me/timezone` | `POST` | Set the signed-in user's default IANA timezone. |
| `/api/me/workspace` | `POST` | Switch the session's active workspace (membership required). |
| `/api/workspaces` | `GET`, `POST` | List the member's workspaces or create a new one. |
| `/api/workspaces/members` | `GET`, `POST` | Workspace-admin member listing and add-by-username. |
| `/api/workspaces/members/{id}/role` | `POST` | Change a member's workspace role (owner-guarded). |
| `/api/workspaces/members/{id}/remove` | `POST` | Remove a member from the active workspace. |
| `/api/workspaces/invitations` | `GET`, `POST` | Pending invitations and email invitation creation. |
| `/api/workspaces/invitations/{id}/revoke` | `POST` | Revoke a pending invitation. |
| `/api/invitations/{token}` | `GET` | Public invitation lookup for the acceptance page. |
| `/api/invitations/{token}/accept` | `POST` | Accept an invitation (new local account or signed-in). |
| `/api/signup` | `POST` | Self-service signup when enabled. |
| `/api/workspaces/status` | `GET` | Workspace-admin usage, health, and analytics summary. |
| `/api/workspaces/export` | `GET` | Secret-free JSON export of the active workspace. |
| `/api/workspaces/delete` | `POST` | Owner-only soft deletion of the active workspace. |
| `/api/workspaces/settings` | `POST` | Owner-set monthly AI budget cap. |
| `/api/workspaces/ai-providers` | `GET`, `POST` | Workspace-level AI provider keys, models, and refresh. |
| `/api/workspaces/billing/checkout` | `POST` | Owner-only Stripe Checkout for plan upgrades. |
| `/api/billing/webhook` | `POST` | Signature-verified Stripe webhook (plan changes). |
| `/api/media/jobs` | `GET`, `POST` | AI media job queueing and status/progress listing. |
| `/api/media/jobs/{id}/review` | `POST` | Workspace-admin approval or rejection of a result. |
| `/api/media/library` | `GET` | Approved, publishable generated assets. |
| `/api/admin/workspaces` | `GET` | Audited, metadata-only instance-admin oversight. |
| `/api/me/password` | `POST` | Rotate local password and revoke existing sessions. |
| `/api/admin/users` | `GET`, `POST` | Administrator-only user management. |
| `/api/admin/users/{id}/disable` | `POST` | Disable an account and revoke its sessions. |
| `/api/admin/audit-events` | `GET` | Latest administrator-visible audit events. |
| `/api/admin/status` | `GET` | Administrator-only queue and worker summary. |
| `/api/connections` | `GET`, `POST` | User-owned encrypted provider account records. |
| `/api/connections/{id}/disable` | `POST` | Disable a provider account without deleting history. |
| `/api/connections/{id}/rotate` | `POST` | Replace encrypted provider credentials and re-enable the account. |
| `/api/uploads` | `POST` | Store a base64-encoded image; returns its local URL. |
| `/api/posts` | `POST` | Create a draft or scheduled post. |
| `/api/posts/{id}/schedule` | `POST` | Schedule an existing post with ISO 8601 time and optional IANA `scheduled_timezone`. |
| `/api/posts/{id}/publish` | `POST` | Attempt immediate publishing through the configured provider. |
| `/api/posts/{id}/deliveries` | `GET` | Owner-visible delivery attempts and per-account target state. |

## Backups and restore drills

The included backup command creates one timestamped `tar.gz` archive containing a consistent database backup and the uploaded-media directory. The default destination is `./backups/`, which is ignored by Git. Run it from the same Compose configuration as the application:

```sh
docker compose run --rm sosopo python /app/scripts/backup.py
docker compose run --rm sosopo python /app/scripts/verify_backup.py /backups/sosopo-YYYYMMDDTHHMMSSZ.tar.gz
```

To run backups automatically once per day, enable the opt-in backup profile. Set `BACKUP_INTERVAL_SECONDS` to change the cadence, then monitor its logs and verify archives independently:

```sh
docker compose --profile backup up -d
docker compose logs -f sosopo-backup
```

SQLite backups are made with SQLite's online backup API, so the web service does not need to be stopped. PostgreSQL backups use `pg_dump` custom format; MariaDB/MySQL backups use a transaction-consistent `mysqldump`. For S3, MinIO, or another S3-compatible service, set `BACKUP_DESTINATION=s3`, `S3_BUCKET`, optional `S3_ENDPOINT_URL`, credentials, and (where supported) `S3_SERVER_SIDE_ENCRYPTION`. The archive is retained locally as well, so use a protected, encrypted backup volume and a lifecycle policy in the bucket.

`verify_backup.py` is non-destructive: it checks the archive structure and database backup validity. Run and record this verification after every automated backup, and practice a restore to an isolated database before relying on the system.

To restore, stop both application services first, keep an independent copy/snapshot, then run the explicit destructive command. SQLite restores keep timestamped pre-restore database and uploads copies in `data/`; PostgreSQL and MariaDB/MySQL use the respective native restore client and should be restored to an isolated database first.

```sh
docker compose stop sosopo sosopo-worker
docker compose run --rm sosopo python /app/scripts/restore.py /backups/sosopo-YYYYMMDDTHHMMSSZ.tar.gz --confirm
docker compose up -d
curl -fsS http://127.0.0.1:8088/api/health
```

## Recommended next milestones

1. Add provider OAuth account discovery and refresh-token flows. Manual encrypted credential entry, rotation, and multi-target publishing are available today.
2. Add invitation delivery, role-change UI, and richer audit-event filtering/export. User creation, disable/revocation, and audit logging are available today.
3. Add carousels, video, alt text, and provider-specific rendition rules. Sosopo now decodes uploaded images before storage, rejects corrupt/unsafe payloads, and keeps the existing character-limit and Instagram image requirement.
4. Add provider-native idempotency keys where each API supports them. Sosopo already classifies retryable HTTP failures, honours `Retry-After`, uses durable claims/leases, keeps delivery history, and includes a user-owned failed-post retry interface.
5. Add automated adapter tests against provider sandboxes and practice restores against each external database backend before public deployment. Live provider verification is deliberately deferred for deployments without provider credentials.
6. Add alert rules and dashboard templates. Sosopo includes an opt-in authenticated Prometheus endpoint and a weekly Dependabot policy.

Pin and deliberately upgrade images and provider integrations in a test environment; avoid automatic upgrades for a service that holds publishing credentials.

## Development and release checks

Run the same checks used by the included GitHub Actions workflow before opening a pull request:

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
docker compose config --quiet
docker compose up -d --build
curl -fsS http://127.0.0.1:8088/api/health
```

The workflow builds the image and runs the unit suite, but it deliberately does not use real provider credentials. Keep provider sandbox tests and any deployment secrets in the CI provider's secret store.

## Metrics

Set a long random `SOSOPO_METRICS_TOKEN` to enable the private Prometheus-format `/metrics` endpoint. It reports post counts by state, delivery totals by result, and worker heartbeat health. Requests must include `Authorization: Bearer <SOSOPO_METRICS_TOKEN>`; when the token is empty or incorrect, the endpoint returns 404. Do not expose this endpoint publicly—have the scraper use the Docker network or a proxy allow-list.

```sh
curl -fsS -H "Authorization: Bearer $SOSOPO_METRICS_TOKEN" http://127.0.0.1:8088/metrics
```

Sosopo applies a process-local request limit of 10 authentication attempts and 60 authenticated writes per source IP per minute. Put a rate-limiting reverse proxy or Cloudflare Access in front of replicas; the built-in guard is intentionally a final line of defense, not a distributed rate-limit service.

Expected provider/configuration write errors return a safe JSON message. Unexpected API write failures are logged by the container without including request bodies or stored credentials; inspect them with `docker compose logs sosopo`.

## Production preflight checklist

Before exposing a deployment, run the included read-only checker after `.env` and secret mounts are in place:

```sh
docker compose run --rm sosopo python /app/scripts/preflight.py --production
```

It fails when the data directory is not writable, `SOSOPO_PUBLIC_URL` is not public HTTPS, the persistent encryption key is invalid, or the database URL is unsupported. It also warns about SQLite, backups, and missing provider credentials. In addition, confirm `docker compose ps` reports healthy for both services, run a verified backup, and register the exact public callback URL with your SSO provider.
