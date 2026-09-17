# Enter AI

Enter AI is an AI-native enterprise project-management MVP. It is a modular monolith: a Next.js dashboard, one FastAPI API, and PostgreSQL. The normal product loop is deliberately simple: manage projects in the interface, or ask Enter AI to do the work.

## What is included

- JWT authentication and organisation-scoped data
- Users, roles (`admin`, `manager`, `member`) and teams
- Projects, list and Kanban views, tasks, subtasks, comments and file attachments
- Activity history, notifications, My Work, Inbox, portfolio dashboard and global search
- Copilot chat with organization-scoped read tools and signed, explicit confirmation required before any write
- New-project brief ingestion for PDF, DOC, DOCX, TXT, XLSX, CSV, and JSON with editable AI-drafted metadata and suggested tasks
- Local storage adapter for uploads with a stable interface for a future cloud backend
- Seeded demo workspace (three projects, users, teams, tasks and notifications)
- Alembic migration, smoke tests, Docker Compose, and environment configuration

## Start it

1. Install Docker Engine and the Docker Compose plugin on your machine.
2. Copy the environment file: `cp .env.example .env`.
3. Edit `.env` and set `JWT_SECRET`, `SEED_ADMIN_EMAIL`, and `SEED_ADMIN_PASSWORD` to your own values (the Compose default runs with `ENVIRONMENT=production`, which refuses to start with the placeholder secret or the published demo password -- see the comments in `.env.example`).
4. Run `docker compose up --build`.
5. Open `http://localhost:3000` and sign in with the `SEED_ADMIN_EMAIL`/`SEED_ADMIN_PASSWORD` you set.

For local, non-production evaluation only, set `ENVIRONMENT=development` in `.env` instead of setting the three values above; that restores the fixed demo login (`admin@demo.enterai.com` / `enterai-demo`) and the relaxed startup checks. Never use `ENVIRONMENT=development` on a deployment reachable by anyone but you.

The API docs are available for local development at `http://localhost:8000/docs`. The Compose configuration disables `/docs`, `/redoc`, and `/openapi.json` by default; enable them explicitly with `ENABLE_API_DOCS=true` only in a trusted development or CI environment.

## Development

Frontend: `cd frontend && npm install && npm run dev`.

Backend: `cd backend && python -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/uvicorn app.main:app --reload`.

Set `DATABASE_URL=sqlite:///./enterai.db` for a local no-Docker backend; Docker Compose uses PostgreSQL automatically.

## CLI

The dependency-free CLI covers the same API actions as the dashboard: authentication, dashboards, projects, tasks and subtasks, comments, attachments, teams, users, inbox, activity, search, risks, and confirmed AI actions.

```bash
python3 -m pip install -e ./cli
enter-ai auth login --email admin@demo.enterai.com
```

Or run it directly:

```bash
python3 cli/enter_ai.py auth login --email admin@demo.enterai.com
python3 cli/enter_ai.py dashboard
python3 cli/enter_ai.py ai ask "What is at risk?"
```

Writes ask for confirmation. Append `--yes` for a deliberate non-interactive write or `--json` for automation. See [`cli/README.md`](cli/README.md) for the full reference.

## AI safety boundary

`backend/app/copilot.py` defines the Copilot boundary. The API creates an organization-scoped workspace snapshot through backend read functions; the provider receives only that minimised data, never a database session or application credentials. `AI_PROVIDER=deterministic` is the offline default. Set `AI_PROVIDER=lmstudio` or `openai_compatible`, plus `AI_BASE_URL`, `AI_MODEL`, and (when needed) `AI_API_KEY`, to use LM Studio or a hosted OpenAI-compatible endpoint.

The UI calls `/api/ai/plan`, which only reads workspace data and returns proposed writes with a short-lived signed confirmation token. `/api/ai/confirm` validates the token against the signed-in user and organization, validates the action through the existing API schemas, then performs the mutation. The client never supplies raw tool arguments for a Copilot write.

For production, add provider credentials through environment variables, make the role policy more granular, persist AI tool audit records, add object storage for attachments, and use a managed Postgres service.
