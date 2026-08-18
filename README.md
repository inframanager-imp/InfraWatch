# InfraWatch

Centralized dashboard for monitoring VMs, Docker containers, and systemd services across environments. Agents run on each monitored VM and push data to this central server — the server never connects into a VM directly.

## Status: Phase 1 — backend, database, auth, RBAC

This phase ships a working API with no agents yet:

- FastAPI backend + PostgreSQL
- JWT auth, password hashing (bcrypt)
- RBAC: `admin` / `readonly` roles
- VMs scoped to `readonly` users via `user_vm_access`
- Configurable environments (seeded with production, staging, development, qa, demo, das, sandbox)
- Audit log for admin actions
- `POST /vms` generates a one-time agent token (shown once, stored hashed) — this is what the future agent install command will use

Not built yet: the agent itself, live log streaming, Docker/systemd inventory, alerting. See "Next phases" below.

## Run locally

```bash
cp .env.example .env
docker compose up --build
```

- API: http://localhost:8000
- Interactive docs: http://localhost:8000/docs
- Default admin (from `.env`): `admin@infrawatch.local` / `changeme123` — change this before any real deployment

In `/docs`, click **Authorize** and log in with the admin email as the username.

## Project structure

```
backend/     FastAPI app, SQLAlchemy models, JWT auth, RBAC
frontend/    UI — currently the static dashboard mockup used to validate the design;
             a real app (React) replaces this in phase 2, wired to the API above
agent/       placeholder — the per-VM agent (phase 2)
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
| `/users` | GET | admin | List users |
| `/users` | POST | admin | Create a user, assign role + VM access |

## Next phases

1. Agent (Docker + systemd inventory, heartbeat, outbound-only, token auth)
2. Live log streaming (WebSocket)
3. Alerting + deduplication
4. Per-resource monitoring/log/alert toggles
5. Security hardening (HTTPS, token rotation, audit expansion) → deploy

## Notes for production

- Set a real `JWT_SECRET` and `ADMIN_PASSWORD` — the defaults in `.env.example` are for local dev only.
- `CORSMiddleware` currently allows all origins (`*`) for local dev — restrict this to the real frontend origin before deploying.
- No DB migration tool yet (tables are created via `Base.metadata.create_all` on startup); add Alembic once the schema stabilizes.
