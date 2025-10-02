# Inbox Steward

[![Build Docker image](https://github.com/DJAMIRSAM/inbox-steward/actions/workflows/docker-image.yml/badge.svg)](https://github.com/DJAMIRSAM/inbox-steward/actions/workflows/docker-image.yml)

Inbox Steward keeps your inbox tidy with a local-first workflow that classifies new mail, files it into a human-friendly folder tree, manages sticky follow-up items, and creates calendar events when it spots real-world appointments. A modern FastAPI web UI exposes live status, configuration details, and a What-if dry run so you can preview actions before committing. Notifications are delivered through Home Assistant to your mobile devices.

## Features

- **Local LLM classification** – Prompts a local Ollama model and repairs JSON output automatically. The prompt now ships the live IMAP folder tree and historical hints on every call so the model can create or reuse folders confidently. Includes deterministic fallbacks when the model is unreachable.
- **Smart foldering** – Enforces the provided naming rules, remembers past overrides through lightweight folder hints, and creates folders on demand when confidence is high.
- **Sticky lane handling** – Flags actionable email and files it once you archive the thread.
- **Calendar automation** – Creates, updates, and cancels events with deterministic UIDs, conflict detection, and a persisted registry for follow-ups.
- **PDF understanding** – Extracts PDF text to improve filing accuracy and duplicate detection.
- **What-if planner & undo** – Preview bulk actions with lane, move-now status, and confidence directly in the UI, then run a full sort without leaving the dashboard. Undo tokens remain available for every batch.
- **Home Assistant notifications** – Decision requests, conflict alerts, and daily digests are routed through your HA mobile app target.
- **Gorgeous admin UI** – Tailwind-powered dashboard with live stats, calendar history, and one-click full sort sweeps.

## Architecture overview

```
├── app
│   ├── core          # configuration, logging, and database bootstrap
│   ├── services      # email client, LLM promptor, calendar + notification logic
│   ├── routes        # FastAPI API + server-rendered UI endpoints
│   ├── utils         # pdf helpers
│   └── web           # Jinja templates and static assets
├── Dockerfile        # application image
├── docker-compose.yml
└── requirements.txt
```

Primary components:

- **FastAPI app** – Serves both API and web UI, orchestrates a background poller that watches for newly read mail.
- **PostgreSQL** – Stores message metadata, folder hints, calendar events, conflicts, and undo tokens.
- **Redis** – Reserved for future task queue/cache extensions (already wired into the compose network).
- **Home Assistant** – External service (not bundled) receiving actionable notifications.
- **Ollama** – External local model runtime reached through `OLLAMA_ENDPOINT`.

## Getting started

1. Copy `.env.example` to `.env` and fill in your mail + Home Assistant credentials.
2. Review `docker-compose.yml` and update IMAP, Home Assistant, and Ollama settings as needed. Adjust `INBOX_STEWARD_PORT` in your `.env` if you need a different host port for the UI. Set `IMAP_ENCRYPTION` to `SSL`, `STARTTLS`, or `NONE` and choose an `IMAP_AUTH_TYPE` (`LOGIN` by default, or `XOAUTH2` if your tenant disallows basic auth).
3. On a host running Docker/Portainer, deploy the stack (see below). The web UI appears at `http://localhost:${INBOX_STEWARD_PORT:-8003}` by default.
4. (Optional) If you prefer to skip local builds, pull the prebuilt container published to GitHub Container Registry (see Continuous image builds).


### Portainer deployment

1. Open Portainer and create a new stack named **inbox-steward**.
2. Paste the contents of [`docker-compose.yml`](docker-compose.yml) into the editor.
3. In the Environment variables section add secrets for:
   - `IMAP_USERNAME`
   - `IMAP_PASSWORD`
   - `HOME_ASSISTANT_TOKEN`
   - (Optional) `IMAP_ENCRYPTION` if your provider requires `STARTTLS` or an unencrypted connection.
   - (Optional) `IMAP_AUTH_TYPE` and `IMAP_OAUTH2_TOKEN` if your provider requires OAuth 2.0 app passwords/tokens.
4. (Optional) Override `INBOX_STEWARD_PORT` if host port 8003 is already in use. Portainer will publish the UI on that port.
5. Deploy the stack. Portainer will start three containers: `inbox-steward`, `inbox-steward-db`, and `inbox-steward-redis`.
6. Ensure your existing `ollama` container is attached to the same network or reachable at the hostname specified in `OLLAMA_ENDPOINT`.
7. (Optional) To use the prebuilt image, update the stack so the app service references `ghcr.io/djamirsam/inbox-steward:latest` instead of building locally. The GitHub Action below keeps that tag fresh after every merge.

### Manual Docker CLI deployment

```bash
git clone https://github.com/DJAMIRSAM/Inbox-Steward.git
cd Inbox-Steward
cp .env.example .env
# edit .env
mkdir -p storage/pdfs
sudo docker compose up -d --build
```

To pull the prebuilt image instead of building locally:

```bash
sudo docker compose pull app
sudo docker compose up -d
```

The FastAPI docs live at `http://localhost:${INBOX_STEWARD_PORT:-8003}/docs`. The dashboard gives you:

- Pending automations count
- Needs-decision queue (sticky lane items)
- Recent calendar entries with location/notes
- Conflict inspector and manual full-sort trigger

### What-if planner

- Visit `/what-if` to see the latest dry-run plan, including each message's lane, destination, flag status, and confidence.
- Use **Refresh preview** to request a new plan without leaving the page.
- When you're satisfied, hit **Run full sort** to apply the moves without getting bounced to a raw JSON response.
- A toast confirms how many messages were filed, and the table updates automatically.

## Home Assistant notification setup

Add a [Long-Lived Access Token](https://developers.home-assistant.io/docs/auth_api/#long-lived-access-token) and set the following environment variables:

- `HOME_ASSISTANT_BASE_URL` – e.g. `https://ha.yourdomain.com`
- `HOME_ASSISTANT_TOKEN`
- `HOME_ASSISTANT_MOBILE_TARGET` – the slug of your `notify.mobile_app_*` service

Notifications include actionable buttons for safe defaults and undo links.

## Undo + decision workflows

Every batch of actions is tagged with a deterministic session ID. Undo tokens are emailed as part of notification payloads and can be exercised through the API (`POST /api/undo/{token}` – extend as needed). Low-confidence moves and ambiguous folder choices automatically trigger a Home Assistant push.

## Development

- Install dependencies: `pip install -r requirements.txt`
- Run the API locally: `uvicorn app.main:app --reload`
- Format/linters: this project intentionally keeps dependencies minimal—use your preferred tooling.

## Testing workflow

Integrate with your mail provider by granting IMAP access. The assistant inspects `SEEN` messages, so mark something as read to trigger classification. Acceptance tests:

1. **Receipts** – Send a PDF receipt. It should land in `Finance/Receipts` within two minutes.
2. **Newsletters** – Mark a newsletter as read. It should jump to `Newsletters` immediately.
3. **Action requests** – An email asking for work remains flagged until you archive it.
4. **Appointments** – Appointment emails generate `Family` or `Home` events with clean titles.
5. **Reschedules** – Follow-up changes update the existing event, while true conflicts raise a single notification.

## Maintenance & observability

- All structured actions are logged into PostgreSQL (`action_logs`).
- Conflicts are surfaced via the UI and notifications.
- Background poller interval is configurable with `POLL_INTERVAL_SECONDS`.

## Security considerations

- Secrets remain inside Docker environment variables—use Portainer secrets for production.
- All outbound actions are limited to folder moves and calendar writes; deletes are not implemented.
- Timezone defaults to America/Vancouver and uses UTC storage for safety.

Enjoy your calm inbox! ✉️

## Continuous image builds

This repository publishes Docker images to [GitHub Container Registry](https://ghcr.io) on every pull request and push to `main`.

- Pull request builds are tagged as `ghcr.io/djamirsam/inbox-steward:pr-<PR_NUMBER>` so reviewers can deploy the exact changes under test.
- Merged changes are tagged as `ghcr.io/djamirsam/inbox-steward:latest` for production use.

The workflow lives in [`.github/workflows/docker-image.yml`](.github/workflows/docker-image.yml). It runs the same build as `docker compose build` and pushes when the source branch is inside this repository. Forked pull requests still run the build for validation but skip the push.
