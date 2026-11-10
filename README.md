<div align="center">

<img src="src/web/static/icon.svg" width="96" height="96" alt="E5Pulse logo">

# E5Pulse

### A lightweight, reliable, self-hosted keep-alive service for the Microsoft 365 E5 developer subscription

Keep your E5 developer tenant active by generating realistic Microsoft Graph
activity on a randomized, human-like schedule — fully under your own control,
deployable with a single Docker command.

[![License: GPL-3.0](https://img.shields.io/badge/License-GPL%203.0-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/docker-ready-2496ED.svg)](https://www.docker.com/)
[![Code style: PEP 8](https://img.shields.io/badge/code%20style-PEP%208-green.svg)]()

</div>

---

## Why E5Pulse?

A Microsoft 365 E5 developer subscription is renewed automatically only while
the tenant keeps producing *development activity*. The recommended approach is
to call the Microsoft Graph API on a recurring basis. Existing community tools
have real drawbacks:

- The official Windows desktop client needs an always-on PC and wastes resources.
- Third-party Docker images are often unmaintained and suffer compatibility drift.
- Hosted services ask you to hand your credentials to someone else's server.

**E5Pulse** gives you the core capabilities of the well-known desktop tool in a
small, auditable, self-hosted daemon you can run anywhere Docker runs. Your
secrets never leave your own infrastructure.

> [!IMPORTANT]
> No tool can guarantee a successful renewal. Microsoft's decision is a black
> box. E5Pulse only maximizes the legitimate Graph activity signal that the
> renewal process looks for. Use it at your own responsibility.

## Features

- **Multi-account** — keep any number of E5 tenants alive from one instance, and
  add/remove accounts directly from the panel (credentials are validated with a
  real token exchange before anything is written).
- **Two authentication flows** — Client Credentials (recommended) and ROPC (legacy).
- **Randomized calling** — picks random APIs in random order, with randomized
  intervals and inter-call delays to avoid a detectable fixed pattern.
- **Working-hours window** — restrict activity to chosen days and time ranges.
- **Smart pause** — backs off automatically on consecutive failures or a high
  sliding-window error rate, then resumes on its own.
- **Automatic token management** — caches and refreshes tokens, re-authenticates on 401/403.
- **Configurable API list** — enable or disable individual Graph endpoints.
- **Embedded PWA control panel** — monitor status, trigger runs, pause/resume
  accounts, and hot-edit most configuration from a mobile-friendly dashboard.
- **Hot config reload** — changes apply without a restart; every write is backed
  up automatically before it touches `config.yaml`.
- **Admin consent wizard** — grant the read-only Graph permissions from the
  panel via a device-code flow: an admin signs in once on Microsoft's official
  page, and E5Pulse assigns the roles automatically. The admin token is held
  only in memory and discarded; no password is ever entered into E5Pulse.
- **Resilient HTTP** — exponential backoff for retryable errors, honors `Retry-After` on 429.
- **Observability** — rotating file + console logs, a `status.json` snapshot,
  and a `/health` endpoint.
- **Hardened by default** — non-root container, secret redaction in logs,
  signed-cookie panel auth with brute-force lockout.
- **Tiny footprint** — idle memory stays well under ~150 MB; near-zero CPU between calls.

## Architecture

```
┌──────────────────────────────────────────────┐
│                   E5Pulse                     │
├──────────────────────────────────────────────┤
│  Config loader  (YAML → Pydantic validation)  │
├──────────────────────────────────────────────┤
│  Account manager                              │
│   ├─ Auth provider (MSAL)                     │
│   ├─ Token cache                              │
│   └─ State (error count, pause-until, …)      │
├──────────────────────────────────────────────┤
│  Scheduler                                    │
│   ├─ Working-hours / weekday checks           │
│   ├─ Random interval generation               │
│   └─ Account concurrency control              │
├──────────────────────────────────────────────┤
│  Graph executor                               │
│   ├─ Random API selection                     │
│   ├─ Error classification & retry             │
│   └─ Per-account statistics                   │
├──────────────────────────────────────────────┤
│  Embedded FastAPI PWA panel + logger/metrics  │
└──────────────────────────────────────────────┘
```

A single long-running process maintains independent state per account. Graph
calls are all outbound; the only inbound port is the optional web panel.

## Quick start (Docker)

```bash
# 1. Get the files
git clone https://github.com/TheMrxk/E5Pulse.git
cd E5Pulse

# 2. Create your configuration from the template
cp config.example.yaml config.yaml
mkdir -p data/logs
#   then edit config.yaml and fill in your Azure app credentials

# 3. Protect the secrets file
chmod 600 config.yaml

# 4. Run it
docker compose up -d --build
```

The daemon starts, validates the configuration, and begins scheduling calls.
Logs stream with `docker compose logs -f e5pulse`.

## Azure setup

E5Pulse never creates apps or grants permissions for you — that is intentionally
manual, to avoid risky automation against your directory.

1. In **Entra ID → App registrations**, create an application (or reuse one).
2. Under **API permissions → Add a permission → Microsoft Graph → Application
   permissions**, add the read-only `Read.All` permissions you need, for example:
   `User.Read.All`, `Directory.Read.All`, `Group.Read.All`, `Sites.Read.All`,
   `Device.Read.All`, `Application.Read.All`, `AuditLog.Read.All`.
3. Click **Grant admin consent**.
4. Create a **client secret** and copy the tenant ID, client ID, and secret into `config.yaml`.

> In client-credentials mode there is no signed-in user, so `/me/*` endpoints
> cannot be used. E5Pulse automatically filters them out and logs a warning.
> Use organization-level endpoints such as `/users` and `/groups` instead.

## Configuration

Everything is controlled through `config.yaml` and strictly validated on startup.
The annotated [`config.example.yaml`](config.example.yaml) documents every key,
including intervals, working hours, error handling, call policy, the Graph API
list, and panel settings. A minimal account block looks like:

```yaml
accounts:
  - name: "primary"
    enabled: true
    auth_type: client_credentials
    tenant_id: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
    client_id: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
    client_secret: "your-secret-here"
```

### Control panel

Enable the panel in `config.yaml`, then open `http://<host>:8090` and sign in
with the configured password. On a phone you can add it to the home screen and
use it as a full-screen, installable PWA.

- **Status** — per-account success/failure counts, pause state, last/next run,
  with Run now / Pause / Resume actions and 15-second auto-refresh.
- **Config** — add/remove accounts (client-credentials mode, validated live),
  hot-edit working hours, per-account and global intervals, concurrency, timeout,
  error handling, call policy, and individual Graph API toggles.
- **Logs** — tail the runtime log.

Changes are written back to `config.yaml` only after passing validation, and a
timestamped backup is saved to `data/backups/` first. New accounts are validated
with a real token exchange before they are stored, and apply without a restart.
The panel password and ROPC-account creation stay file-only by design, to limit
the blast radius of a compromised panel session.

## Running without Docker

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml   # edit it first
python -m src.main                    # long-running daemon
python -m src.main --once             # run a single round and exit (handy for testing)
```

## Testing

```bash
pip install -r requirements-dev.txt
pytest -q
```

The suite is fully offline: external HTTP is intercepted with mocks and the MSAL
client is stubbed, so no real network or credentials are required. It covers
configuration validation, both auth flows, randomized execution, retries,
smart-pause state transitions, hot config reload (including secret preservation),
and the complete panel API and PWA assets.

## Project layout

```
E5Pulse/
├── src/
│   ├── main.py            # entry point, signals, graceful shutdown
│   ├── config.py          # YAML loading
│   ├── models.py          # Pydantic models and validation
│   ├── config_store.py    # hot-edit, backup and secret-safe serialization
│   ├── auth.py            # MSAL client-credentials / ROPC
│   ├── api_executor.py    # Graph calls, retry and backoff
│   ├── account.py         # per-account state machine
│   ├── scheduler.py       # randomized scheduling loop
│   ├── logger.py          # rotating logs with secret redaction
│   └── web/               # FastAPI panel + static PWA assets
├── tests/
├── Dockerfile
├── docker-compose.yml
├── config.example.yaml
└── requirements.txt
```

## Security

- Client secrets live only in `config.yaml` (mounted, never baked into the image)
  and are excluded from version control.
- The container runs as a non-root user.
- Logs redact secrets, passwords, and bearer tokens before anything is written.
- The panel uses signed session cookies, constant-time password comparison, and
  per-IP brute-force lockout.
- Only read-only Graph calls are used by default; no privilege-escalation or
  permission-modifying automation is included.

## Disclaimer

E5Pulse is an independent project and is **not affiliated with, endorsed by, or
connected to Microsoft**. It interacts with the official Microsoft Graph API but
is provided solely for learning, research, and legitimate administration of your
own developer subscription. You are responsible for complying with Microsoft's
terms of service and for securing your own credentials. The software is provided
"as is", without warranty of any kind; you bear all risk of using it.

## License

Copyright © 2026 TheMrxk

Licensed under the **GNU General Public License v3.0** (GPL-3.0). See
[LICENSE](LICENSE) for the full text. Any derivative work must keep this license,
preserve the copyright and warranty notices, and make its source available on
the same terms.
