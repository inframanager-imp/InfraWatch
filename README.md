# InfraWatch

Centralized dashboard for monitoring VMs, Docker containers, and systemd services across environments. Agents run on each monitored VM and push data to this central server — the server never connects into a VM directly.

## Status: live in production

- FastAPI backend + PostgreSQL, schema managed by Alembic
- JWT auth, password hashing (bcrypt), RBAC (`admin` / `readonly`), VMs scoped to `readonly` users via `user_vm_access`, audit log
- Self-contained agent (`GET /install-agent.sh`) — Docker + systemd inventory, resource metrics, outbound-only token auth, self-updating (checks in and replaces itself when the install script changes, no manual re-install needed after the first time)
- Real-time live log streaming over WebSocket (agent ↔ server ↔ browser), plus custom log file paths for apps that don't log to stdout/journal
- Per-resource Monitor/Logs toggles, with a smart default (custom app units start on, OS/package units start off) so a VM with hundreds of standard services doesn't need manual cleanup
- Real-time alerting: VM offline, container stopped/restart-looping, systemd service failed, high CPU/mem/low disk — deduplicated (one row per ongoing incident, not one per heartbeat), respects the Monitor toggle. In-app only for now, no outbound notifications yet.
- Real frontend (plain HTML/JS, no build step, served by nginx)

Not built yet: outbound alert notifications (email/Slack/webhook), agent token rotation without deleting/re-adding the VM.

## Run locally

```bash
cp .env.example .env
docker compose up --build
```

- UI: http://localhost:8080
- API: http://localhost:8000
- Interactive docs: http://localhost:8000/docs
- Default admin (from `.env`): `admin@infrawatch.local` / `changeme123` — change this before any real deployment

In `/docs`, click **Authorize** and log in with the admin email as the username.

## Project structure

```
backend/           FastAPI app, SQLAlchemy models, JWT auth, RBAC, alembic/ migrations
backend/app/alerts.py   alert rule evaluation (heartbeat-triggered + periodic offline sweep)
backend/app/static/install-agent.sh   single source of truth for the agent — also self-fetched for self-update
frontend/app/      the real frontend — plain HTML/JS calling the live API, served by nginx
frontend/mockup/   the original static design mockup (fake data), kept as a design reference
```

## API overview

| Endpoint | Method | Access | Purpose |
|---|---|---|---|
| `/auth/login` | POST | public | Get a JWT (form fields: `username`=email, `password`) |
| `/auth/me` | GET | any logged-in user | Current user info |
| `/environments` | GET | any logged-in user | List environments |
| `/environments` | POST | admin | Add an environment |
| `/vms` | GET | any logged-in user | List VMs (admin sees all; readonly sees only assigned) |
| `/vms` | POST | admin | Register a VM, returns a one-time agent token |
| `/vms/{id}` | DELETE | admin | Remove a VM |
| `/vms/{id}/containers`, `/services` | GET | any logged-in user | Live inventory, includes Monitor/Logs state |
| `/vms/{id}/resource-settings` | PUT | admin | Toggle Monitor/Logs for one container/service |
| `/vms/{id}/resource-settings/bulk` | PUT | admin | Toggle Monitor/Logs for every container or service on a VM at once |
| `/vms/{id}/log-sources` | GET/POST/DELETE | admin (write) | Custom log file paths |
| `/vms/{id}/logs/ws` | WS | any logged-in user | Live log stream for one container/service/file |
| `/alerts` | GET | any logged-in user | Active alerts across all accessible VMs |
| `/vms/{id}/alerts` | GET | any logged-in user | All alerts (active + resolved) for one VM |
| `/users` | GET | admin | List users |
| `/users` | POST | admin | Create a user, assign role + VM access |

## Next up

1. Outbound alert notifications (email and/or Slack/webhook) — alerting itself is done, delivery isn't
2. Agent token rotation (currently: delete + re-add the VM if a token is lost)
3. Security hardening pass (token rotation, audit expansion)

## Notes for production

- Set a real `JWT_SECRET` and `ADMIN_PASSWORD` — the defaults in `.env.example` are for local dev only.
- `CORSMiddleware` currently allows all origins (`*`) for local dev — restrict this to the real frontend origin before deploying.
- Schema is managed by Alembic (`alembic upgrade head` runs automatically in the backend container's entrypoint). On an existing database from before Alembic was added, run `alembic stamp head` once before the first deploy of that change.
