# Deployment plan (draft for discussion)

Status: draft for discussion with engineering. Owner: project lead. Nothing here is decided yet.

## 1. What is being deployed

Today the voice agent runs on one Windows PC, reached by Twilio through a temporary Cloudflare
tunnel. That is fine for building and staff testing, but not for an always-on service.

| Component | What it is | Runs |
|---|---|---|
| **Voice agent** (`voice-agent/app.py`) | Python/FastAPI web service. Twilio sends each call to it over HTTPS (`POST /voice`, `POST /end`) and a WebSocket (`/relay`) | Always on, one process |
| **Private configuration** | Twilio SID/token, Anthropic API key (OpenAI optional), Zoho OAuth client and refresh token, non-secret settings | Read on every call |
| **Hospital inventory** (`lspcare-inventory.json`) | Approved, sanitised bed/device list for RAH and RNS (read-only) | Loaded at start, re-checked when changed |
| **Knowledge library** (`knowledge/`) | Reviewed, fingerprinted documents; ships with the code | Part of the image |
| **Call records** | Transcripts, spoken problem reports, review results, dry-run tickets | Written during and after calls |
| **Dashboard** (`dashboard.py`) | Admin pages for calls, reviews, tests, settings, knowledge | On demand; currently this PC only |
| **Review / test jobs** (`review.py`, `harness.py`) | Command-line jobs that call the Claude API (cost-capped) | On demand or scheduled |

External services the server must reach (outbound HTTPS only):
`api.twilio.com`, `api.anthropic.com`, `accounts.zoho.com` and `desk.zoho.com` (US data centre for
our Zoho org), optionally `api.openai.com`. Twilio must reach the service inbound on 443 (HTTPS and WSS).

## 2. Technical requirements engineering should know

- **Fixed public HTTPS address** (for example a subdomain) with a valid certificate. WebSockets
  (`wss://.../relay`) must pass through any proxy or load balancer, with idle timeouts of at least
  15 minutes (a call can last that long).
- **Exact public URL in config.** Twilio signs every request using the URL it called. The service
  checks the signature against its configured public base URL, so a proxy must not change the
  host or path, and the configured URL must match the Twilio webhook exactly.
- **One process, one instance (for now).** Call state (sessions, nonces, ticket status) is kept
  in memory. Run a single worker and a single replica. Scaling out later needs shared state
  (for example Redis) — a small, known code change.
- **Capacity.** Currently limited to 2 simultaneous calls (a setting in code). Each call uses
  little CPU; the work is in the external APIs. A small instance is enough.
- **Health check:** `GET /health` returns service status, inventory status and ticket mode.
- **Python 3.14**, dependencies pinned in `voice-agent/requirements.txt`. No database.
- **Time zone:** tickets and transcripts use local time stamps; run in Australia/Adelaide or
  Australia/Sydney time, or switch to UTC everywhere (decide one).
- **Logs** currently go to the console: one timing line per turn, errors with secrets redacted,
  and, when transcripts are on, the conversation text. Log handling must respect that.

## 3. Decisions needed

| # | Decision | Options | Suggested starting point | Who |
|---|---|---|---|---|
| 1 | **Where it runs** | Company cloud (Azure / AWS, Australian region), existing on-premises server, or a managed container service | Whatever engineering already operates, in an Australian region | Engineering |
| 2 | **Packaging** | Docker container, or a plain VM with a service | Docker (see section 6) | Engineering |
| 3 | **Address and TLS** | Subdomain on our domain via existing reverse proxy / load balancer; who manages DNS | e.g. `voice.<company domain>`; certificate managed by the proxy | Engineering + DNS owner |
| 4 | **Secrets** | Mounted secret file (works today), environment variables, or a secret manager (Azure Key Vault / AWS Secrets Manager) | Secret manager → mounted file at start; small code change for env vars if preferred | Engineering |
| 5 | **Where call records live** | Persistent volume, network share, or blob storage; access control; backup | Persistent volume for the pilot, encrypted at rest, project team only | Engineering + project lead |
| 6 | **Retention** | How long to keep transcripts, reports and reviews | e.g. 90 days for the staff pilot, then review before real patients | Project lead + privacy |
| 7 | **Environments** | One staging instance, or staging + production | Staging now (test number), production later on the real support number | Engineering + project lead |
| 8 | **Release process** | Manual, or CI/CD (GitHub Actions) building an image per commit | CI runs the free offline tests on every push and builds an image tagged with the commit; the paid conversation tests run before a release | Engineering |
| 9 | **Dashboard access** | Stay on the project lead's PC (reading records from the server), or hosted behind company sign-in / VPN | Hosted behind company sign-in (SSO) or VPN only; never public | Engineering + project lead |
| 10 | **Monitoring and alerts** | Uptime on `/health`, error alerts, Twilio error alerts (e.g. 11200), API spend alerts | Uptime + error alerts to the project channel; monthly spend limits on the Anthropic and Twilio accounts | Engineering |
| 11 | **When it is down** | Twilio fallback URL that plays a message or forwards to staff | Fallback: forward to the existing support line or play "please call back" | Project lead |
| 12 | **Account ownership** | Twilio, Anthropic API, Zoho API client owned by company accounts with billing and admin owners named | Company-owned accounts, two admins each | Project lead + finance |
| 13 | **Data protection** | Caller audio/text passes through Twilio, Deepgram, ElevenLabs (via Twilio) and Anthropic; agreements, data locations, retention settings | Privacy review before any real patient calls; staff pilot uses fictional details only | Project lead + privacy |
| 14 | **Who operates it** | Updates, restarts, credential rotation, incidents | Engineering runs the platform; project lead owns prompts, knowledge and settings | Both |

## 4. What engineering needs from the project lead

- Access to the repository and a walkthrough of `docs/technical-reference.pdf` and `docs/testing.md`.
- The list of external services and accounts above, and who owns each.
- How credentials will be handed over (never by email or chat): through the chosen secret store.
- The inventory file, where it comes from and how often it changes.
- Expected call volumes and hours, and the target for availability and reply speed.
- Which Twilio number(s) point at staging and production.
- The settings that should differ between staging and production (tickets live or dry-run,
  transcripts on or off, ticket subject prefix).

## 5. What the project lead needs from engineering

- The decisions in section 3, especially hosting, address, secrets and call-record storage.
- A staging instance with a fixed address, so the Twilio test number can point at it permanently.
- A simple way to deploy a new version (ideally: push, tests pass, image built, one command or button to deploy).
- Access to logs and health status, and an agreed way to view call records (decision 9).
- A rough timeline and any security review requirements.

## 6. Packaging: Docker

**Status:** `Dockerfile`, `compose.yaml` (agent, Cloudflare named tunnel, dashboard) and the code
changes below now exist, for a staff-testing server. Setup and operation: [home-server.md](home-server.md).

Docker suits this service well:
- **Versioning:** every image is tagged with its git commit, which is also the version stamp
  written into each call transcript. Rolling back means running the previous image.
- **Same everywhere:** the image contains Python, the pinned dependencies, the code and the
  reviewed knowledge files, so staging and production run exactly what was tested.
- **Clear boundaries:** secrets and call records stay outside the image (mounted at run time).

What it means for the code (small, known changes):
- A `Dockerfile` (Python 3.14 slim, non-root user, `uvicorn app:app --workers 1`) and a
  `docker-compose.yml` for staging (service, volume for call records, secret file mount).
- Read the private folder from a mounted path (already supported: `LSPCARE_PRIVATE_DIR`).
  Optionally also accept secrets as environment variables.
- `run.ps1` and the Cloudflare tunnel are not used on the server; the fixed address replaces them.
- The `.gitattributes` line-ending rule keeps knowledge fingerprints valid in the image.
- The dashboard and the review/test jobs can run from the same image with a different command.

## 7. Suggested phases

1. **Staging (first):** one container on a company server, fixed HTTPS address, Twilio test
   number pointed at it permanently, call records on a persistent volume, basic uptime alert.
   Staff testing continues there instead of on a PC.
2. **Operations:** CI building tagged images, offline tests on every push, spend alerts,
   dashboard behind sign-in, retention job, runbook (restart, rotate keys, roll back).
3. **Production readiness:** privacy review complete, production settings (transcripts policy,
   AI disclosure, fallback routing), real support number routed through the existing phone
   system with a fallback to staff, then retire the previous provider.
