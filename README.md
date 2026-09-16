# Enter AI

Enter AI is an AI-native enterprise project-management MVP. It is a modular monolith: a Next.js dashboard, one FastAPI API, and PostgreSQL. The normal product loop is deliberately simple: manage projects in the interface, or ask Enter AI to do the work.

## What is included

- JWT authentication and organisation-scoped data
- Users, roles (`admin`, `manager`, `member`) and teams
- Projects, list and Kanban views, tasks, subtasks, comments and file attachments
- Activity history, notifications, My Work, Inbox, portfolio dashboard and global search
- AI workspace and command bar with a provider boundary and explicit confirmation required before any write
- New-project brief ingestion for PDF, DOC, DOCX, TXT, XLSX, CSV, and JSON with editable AI-drafted metadata and suggested tasks
- Local storage adapter for uploads with a stable interface for a future cloud backend
- Seeded demo workspace (three projects, users, teams, tasks and notifications)
- Alembic migration, smoke tests, Docker Compose, and environment configuration

## Start it

1. Install Docker Engine and the Docker Compose plugin on your machine.
2. Copy the environment file: `cp .env.example .env`.
3. Run `docker compose up --build`.
4. Open `http://localhost:3000`.

Use the seeded login:

- Email: `admin@demo.enterai.com`
- Password: `enterai-demo`

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

`backend/app/services.py` defines `AIProvider`, the single seam for replacing the deterministic MVP provider with an LLM provider. The UI calls `/api/ai/plan`, which only reads workspace data and returns proposed tool calls. Writes go through `/api/ai/confirm`; every action is labelled and user-confirmed before the API mutates a task.

For production, add provider credentials through environment variables, make the role policy more granular, persist AI tool audit records, add object storage for attachments, and use a managed Postgres service.
