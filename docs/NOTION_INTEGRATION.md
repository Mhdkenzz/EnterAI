# Notion intake

Enter AI connects a Notion public integration to one organization. A workspace admin completes OAuth and chooses pages or databases in Notion's sharing screen. Only content shared with that integration is accessible. Enter AI polls for page edits, copies readable text and the Notion link into Requests, and sends the request through the same CEO triage and approval flow used for Gmail and Slack.

## Set up the integration

1. Create a public integration in the Notion developer portal and enable its **Read content** capability.
2. Add the public API redirect URL, for example `https://api.example.com/api/integrations/notion/callback`.
3. Configure `NOTION_CLIENT_ID`, `NOTION_CLIENT_SECRET`, `API_BASE_URL`, and `SECRETS_ENCRYPTION_KEY` on the hosted Enter AI server. Keep the client secret and encryption key in the host's secret manager.
4. Sign in to Enter AI as a workspace admin, open **Integrations**, and select **Connect Notion**. Choose only the pages and databases that should be visible to the Enter AI organization.

## Data and timing

- The first connection establishes a sync starting point; older page content is not imported.
- New and edited shared pages are checked about every 30 seconds. Page blocks are read when an edit is found; deeply nested page content is capped to limit API load.
- A page edit appears as a request with its title, readable text, edit time, and source link. Members of the Enter AI organization can see queued request content.
- OAuth tokens are encrypted at rest. Disconnecting clears Enter AI's tokens and attempts to revoke the access token at Notion. If Notion is unreachable, revoke the integration in Notion's connection settings as a follow-up.
- Notion webhooks are not required. Polling avoids a shared webhook verification secret across customer workspaces.
