# Sosopo

Sosopo is a self-hosted social-media publishing workspace. It supports local accounts, optional OpenID Connect SSO, isolated user data, drafts, image attachments, timezone-aware scheduling, and provider-based delivery.

Licensed under [MIT](LICENSE). See [CONTRIBUTING.md](CONTRIBUTING.md) for local checks and [SECURITY.md](SECURITY.md) for private vulnerability reporting.

## What works today

- Create drafts for Facebook, Instagram, Threads, X, and Telegram.
- Schedule a post in any IANA timezone (for example `Asia/Hong_Kong` or `Europe/London`); it is stored as UTC for reliable delivery.
- Upload one byte-validated PNG, JPEG, GIF, or WebP image per post (maximum 5 MB).
- Publish immediately or through the built-in scheduler after a provider is configured.
- Record each delivery attempt, provider error, and external post ID.
- Persist posts, schedules, and uploads in `./data`.
- Check service readiness at `GET /api/health`.

Provider credentials are not included. On first visit, create the local administrator account (minimum 12-character password). The administrator can provision local users through the protected API; OpenID Connect can automatically provision ordinary users when explicitly enabled. The dashboard supports encrypted manual account entry and multi-account targets; provider OAuth account discovery remains future work.

First-run setup is single-use and atomically guarded: only one initial administrator can be created, even if several browsers open a fresh instance simultaneously.

Sosopo rejects known-invalid posts before scheduling: Instagram requires an image; text limits are Facebook 5,000, Instagram 2,200, Threads 500, X 280, and Telegram 4,096 characters. Provider plans and API policies can change, so use provider sandbox validation before relying on any limit for a large-scale workflow.

## Run with Docker Compose

```sh
docker compose up -d --build
docker compose ps
```

Compose starts two services: `sosopo` serves the authenticated web/API interface and `sosopo-worker` claims and delivers scheduled posts. Keep both running. For production, use PostgreSQL; the scheduled rows and delivery attempts persist in the database so a web-process restart does not discard queued work.

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

`.env` is ignored by Docker build context and must never be committed. `SOSOPO_ENCRYPTION_KEY` is used to encrypt future stored connection credentials; do not change it after accounts have been connected. The current environment variables remain supported for the first single-account publishing path.

Every sensitive setting also accepts a `_FILE` variant. This is recommended for Docker Secrets, Kubernetes Secrets, or a mounted secret-manager volume: for example set `SOSOPO_ENCRYPTION_KEY_FILE=/run/secrets/sosopo_encryption_key` or `DATABASE_URL_FILE=/run/secrets/sosopo_database_url`. The file contents are read at startup and override the matching environment value. Mount the secret read-only and restart both services after rotation.

### Local disk or S3 media storage

Local disk (`SOSOPO_STORAGE_BACKEND=local`) is the default and stores uploads in `./data/uploads`. To use Amazon S3, MinIO, or another S3-compatible store, set `SOSOPO_STORAGE_BACKEND=s3`, `S3_MEDIA_BUCKET`, optional `S3_MEDIA_PREFIX`/`S3_ENDPOINT_URL`, AWS credentials, and `SOSOPO_MEDIA_PUBLIC_URL`. The public URL must be HTTPS and map to the bucket/prefix (for example `https://media.example.com/uploads`); providers must be able to fetch each image after it is scheduled. Keep bucket write credentials private and grant public read only through a scoped CDN/bucket policy for the media prefix.

### Account connections

One owner can hold multiple Facebook Pages, Instagram accounts, Threads profiles, X accounts, or Telegram channels. Connection secrets are encrypted with Fernet before being written to the database and are never returned by the API. The composer can select several platforms and one or more connected accounts per platform in a single post. Delivery is independently recorded for each account, so a failure on one destination can be retried without reposting to accounts that already succeeded. A single-platform post may still use legacy environment credentials when no connected account is selected.

Posts can include up to 10 images, with provider-specific limits checked before saving (X accepts up to 4). Sosopo stores the attachments in their selected order and sends provider carousel/media-group requests where supported. Images are decoded and validated on upload. Use public HTTPS media storage because providers must be able to fetch scheduled attachments.

The publish timezone is an IANA timezone dropdown populated by the browser (for example `Asia/Hong_Kong`). Sosopo converts the chosen local date/time to UTC and stores the selected timezone with the post.

The dashboard's **Connected accounts** form creates and disables account records. The underlying `POST /api/connections` API requires a signed-in session plus its CSRF token and accepts a provider, display name, external account ID, optional settings, and provider secrets. Use `access_token` for Facebook, Instagram, Threads, and X; use `bot_token` for Telegram. `external_account_id` is the Page/account/profile ID (or Telegram chat/channel ID). OAuth account discovery remains future work; manual encrypted token entry is available today.

API clients may include `token_expires_at` as a future ISO 8601 timestamp with a timezone. Expired accounts cannot be selected for a new post and the worker fails them before sending any provider request; the dashboard displays the expiry when present.

Disable a connection with `POST /api/connections/{id}/disable` (CSRF protected). It is retained for delivery history but cannot be selected for new posts; any pending target using it fails safely rather than falling back to unrelated environment credentials. Rotate the provider credential at the provider as well.

Replace an expired or compromised credential using the dashboard's **Rotate token** control or `POST /api/connections/{id}/rotate` with a `secrets` object and optional future `token_expires_at`. Rotation preserves historical references, re-enables the account, and never returns either old or new secrets.

| Provider | Required variables | Current publishing mode |
| --- | --- | --- |
| Facebook Pages | `FACEBOOK_PAGE_ID`, `FACEBOOK_PAGE_ACCESS_TOKEN` | Text posts and one public-image post. |
| Instagram | `INSTAGRAM_ACCOUNT_ID`, `INSTAGRAM_ACCESS_TOKEN`, `SOSOPO_PUBLIC_URL` for images | One image feed post. Image is required. |
| Threads | `THREADS_USER_ID`, `THREADS_ACCESS_TOKEN`; `SOSOPO_PUBLIC_URL` for images | Text or one public-image post. |
| X | `X_ACCESS_TOKEN` | Text or one image post. Token needs `post.write`. |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Text or one image message; the bot must be an administrator of the target channel/group. |

Facebook and Instagram use `META_GRAPH_BASE_URL` (default shown in `.env.example`); Threads uses `THREADS_API_BASE_URL`. Keep API versions configurable and review provider changelogs before upgrading.

For Facebook, Instagram, and Threads, create and approve the corresponding Meta developer app and obtain long-lived tokens with the required publishing permissions. Their servers fetch an attached image directly, so `SOSOPO_PUBLIC_URL` must be a publicly reachable HTTPS address—not `localhost`. X needs an approved developer project and a user-context access token. Telegram is configured through BotFather plus a target chat/channel ID. See the official [Meta Graph API documentation](https://developers.facebook.com/docs/graph-api/), [Threads API documentation](https://developers.facebook.com/docs/threads/), [X post management guide](https://docs.x.com/x-api/posts/manage-tweets/introduction), and [Telegram Bot API](https://core.telegram.org/bots/api).

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

## Users and SSO

Sosopo has `admin` and `user` roles. Posts and account connections belong to one user and are filtered server-side. The initial local account is an administrator. Administrators use the dashboard's **User administration** section or `GET`/`POST /api/admin/users` to list/create local users; creation needs `username`, a 12-character-or-longer `password`, optional `role` (`user` or `admin`), and optional IANA `timezone`. Disable a compromised or departed user with `POST /api/admin/users/{id}/disable` (CSRF protected); it immediately invalidates every session and blocks both local and SSO login. An administrator cannot disable themself.

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
