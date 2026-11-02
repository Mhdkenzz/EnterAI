# Enter AI CLI

`enter-ai` lets you use the same authenticated API that powers the dashboard from a terminal. It has no third-party dependencies and supports normal project management as well as the confirmed AI tool flow.

## Run it

Start the backend, then from the repository root. For an installable `enter-ai` command:

```bash
python3 -m pip install -e ./cli
enter-ai auth login --email admin@demo.enterai.com
```

Or run it directly without installing anything:

```bash
python3 cli/enter_ai.py auth login --email admin@demo.enterai.com
python3 cli/enter_ai.py dashboard
```

The access token is stored in `~/.config/enter-ai/config.json` with owner-only permissions. Override it for automation with `ENTER_AI_TOKEN`, and override the API with `ENTER_AI_API_URL`.

## Everyday commands

```bash
# Find work and inspect a project
python3 cli/enter_ai.py projects list
python3 cli/enter_ai.py projects tasks PROJECT_ID
python3 cli/enter_ai.py search "launch"

# Create and manage work (writes ask before executing)
python3 cli/enter_ai.py tasks create --project-id PROJECT_ID --title "Prepare release notes" --priority high
python3 cli/enter_ai.py tasks update TASK_ID --status in_progress
python3 cli/enter_ai.py tasks comment TASK_ID "Draft is ready for review"
python3 cli/enter_ai.py tasks attach TASK_ID ./release-brief.pdf

# AI reads first. Proposed writes only run with --confirm and a confirmation prompt.
python3 cli/enter_ai.py ai ask "What is at risk?"
python3 cli/enter_ai.py ai ask "Create a task to test the release" --confirm
```

Use `--yes` to explicitly approve a write in a script, and `--json` for machine-readable output. Run `python3 cli/enter_ai.py --help` for the complete command reference.

## CEO UI CLI

In the CEO agent chat, the CEO can call the restricted `enter_ai_cli` tool to
inspect the rendered Enter AI screen and navigate, click or hover accessible
controls, fill labeled fields, select options, press keys, drag controls, scroll,
or zoom. It runs in an isolated browser
session authenticated as that CEO agent and cannot navigate outside Enter AI.
UI changes are blocked by default. A human administrator can enable **CEO full
access** in Admin → Execution controls. When enabled, the CEO can directly make
ordinary workspace changes through its isolated session. Authentication, billing,
privacy, security, human-admin operations, and agent delegation remain protected
by their normal authorization rules. The setting is off by default, and enabling
it requires an explicit confirmation in the admin UI.
This tool is available when `AI_PROVIDER` is configured for structured tool
calling (`anthropic`, `lmstudio`, or `openai_compatible`); the offline deterministic
provider does not execute tools.

For Docker deployments, the API container uses `ENTER_AI_BROWSER_APP_URL` and
`ENTER_AI_BROWSER_API_URL` to reach the web and API services. The development
Compose defaults use `host.docker.internal` so the browser's app and API origins
remain aligned with the local frontend; hosted deployments can override them.
The API image includes headless Chromium for this tool.
