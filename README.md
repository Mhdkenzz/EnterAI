# Enter AI

Enter AI is an AI-native enterprise project-management MVP. It is a modular monolith: a Next.js dashboard, one FastAPI API, and PostgreSQL. The normal product loop is deliberately simple: manage projects in the interface, or ask Enter AI to do the work.

## What is included

- JWT authentication (httpOnly session cookie in the browser, bearer token for the CLI) and organisation-scoped data
- Users, roles and teams. Admins run the workspace, billing and membership (invite, remove, restore); managers can also archive or delete projects, delete any task and create teams; members work on tasks and delete only their own
- Projects (archive or delete), list and drag-and-drop Kanban views, tasks with subtasks, labels, assignees and due dates, comments and downloadable attachments
- Real URLs for every view (`/projects/<id>`, `/inbox`, `?task=<id>` for an open task): Back, refresh and shared links work
- Activity history, notifications, My Work, Inbox, portfolio dashboard and global search
- Copilot chat with organization-scoped read tools and signed, explicit confirmation before writes; an admin can optionally enable direct ordinary workspace changes for the isolated CEO agent
- Optional local repository harness for agents: source search, cited local RAG, bounded file reads and edits, task-scoped autonomous coding, fixed checks, and human-confirmed file removals
- New-project brief ingestion for PDF, DOC, DOCX, TXT, XLSX, CSV, and JSON with editable AI-drafted metadata and suggested tasks
- Incoming request queue with source links, review status, manual capture, and organization-scoped duplicate protection
- Gmail shared-mailbox OAuth connection with encrypted tokens and incremental Inbox sync; Slack workspace and opt-in per-person DM OAuth with signed Events API intake; Notion OAuth with page-picker-scoped polling. All three feed deduplicated, source-linked requests into CEO triage and human-reviewed project plans when an AI provider is configured.
- Organization admins can configure an encrypted IFM K2 Horizon or OpenRouter API key, with a shared per-workspace request-rate limit. IFM uses `IFM/K2-Horizon-375B-A23B` at `https://api.ifm.ai/v1` by default.
- Slack app setup and workspace installation steps: [docs/SLACK_INTEGRATION.md](docs/SLACK_INTEGRATION.md)
- Notion OAuth and page sharing setup steps: [docs/NOTION_INTEGRATION.md](docs/NOTION_INTEGRATION.md)
- Request plans can be drafted from captured messages, edited, approved or rejected; approval creates the project and priority-ordered CEO task queue. Agents can delegate scoped child tasks to direct reports, record progress, and raise task blockers that pause execution and notify workspace admins; repeated provider failures or no-progress responses also block the task for human review.
- S3-compatible upload storage (local disk for a single node), mirrored to a backup bucket
- Trials, Stripe Checkout and customer portal, per-period usage tracking and seat limits
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

Backend: `cd backend && python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/uvicorn app.main:app --reload`.

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

`backend/app/copilot.py` defines the Copilot boundary. The API creates an organization-scoped workspace snapshot through backend read functions; the provider receives only that minimised data, never a database session or application credentials. `AI_PROVIDER=deterministic` is the offline default. Set `AI_PROVIDER=lmstudio` to use LM Studio, `AI_PROVIDER=ollama` to use Ollama at `http://127.0.0.1:11434/v1`, or `AI_PROVIDER=openai_compatible` for another OpenAI-compatible endpoint. `AI_BASE_URL` and `AI_MODEL` can override each provider's defaults. Hosted endpoints may also require `AI_API_KEY`. Qwen3 has a native tool-calling profile with a longer local-model timeout. For Ollama, run `ollama pull qwen3:4b` and `ollama create enterai-qwen3:4b -f ollama/Modelfile` before selecting the default model.

The UI calls `/api/ai/plan`, which reads workspace data and returns proposed writes with a short-lived signed confirmation token. `/api/ai/confirm` validates the token against the signed-in user and organization, validates the action through the existing API schemas, then performs the mutation. For the CEO agent, an organization administrator can enable direct ordinary workspace changes in Admin → Execution controls. This is off by default; human-admin, authentication, billing, privacy, and security controls remain protected.

## Local repository tools for agents

In local development with a single organization, agents can use the app's own Git
checkout. For hosted or multi-organization use, set `AGENT_WORKSPACE_ROOT` to a
parent directory containing a separate Git checkout for each organization, named
with that organization's ID. The API must have filesystem access to those isolated
checkouts; for Docker, mount the parent directory into the API container. An
organization administrator must then enable **Organization repository access** in
Admin → Execution controls. It is off by default and scoped per organization.

Repository tools are available with a structured tool-calling AI provider; the
deterministic offline provider does not invoke tools. Agents receive safe source-tree, literal-search, bounded file-read, Git status,
Python syntax, frontend type-check, and diff-whitespace tools. In CEO chat, a requested coding task can be completed in a loop: inspect relevant files, create or edit source files with a checked patch, review the resulting Git diff, run a fixed check, and report the outcome. Agents working an assigned task can use the same edit loop. The patch tool creates and edits files but cannot delete or move files, change executable modes, follow symlinks, write secrets or generated folders, or invoke arbitrary shell commands. Deletions and moves remain human-confirmed proposals.
A local BM25 index retrieves code chunks
with path/line citations; it does not send code to an embedding service. Retrieved
snippets are included in the configured AI provider's context, so a hosted provider
will receive the snippets used to answer. Environment files, credentials, binary
files, dependencies, build output, and generated directories are excluded. Code
changes stay in the local configured checkout; agents do not commit or push them.

For production, add provider credentials through environment variables, make the role policy more granular, persist AI tool audit records, add object storage for attachments, and use a managed Postgres service.
