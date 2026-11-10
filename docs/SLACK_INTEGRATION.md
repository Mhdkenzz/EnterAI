# Slack message intake

Enter AI installs a Slack bot into one workspace at a time. An active human admin connects the workspace from **Integrations**. New messages are delivered through Slack's Events API; Enter AI does not backfill older Slack history.

## Slack app setup

1. Create a Slack app and enable distribution so each customer workspace can install it.
2. Under **OAuth & Permissions**, add this redirect URL, replacing the host with the public API address:
   `https://api.example.com/api/integrations/slack/callback`
   Also add the per-person authorization redirect URL:
   `https://api.example.com/api/integrations/slack/user/callback`
3. Add these bot token scopes:
   - `channels:history`
   - `groups:history`
   - `im:history`
   - `mpim:history`
   Enter AI separately requests `im:history` and `mpim:history` as user token scopes only when a person chooses **Connect my DMs**.
4. Under **Event Subscriptions**, enable events and set the request URL to:
   `https://api.example.com/api/integrations/slack/events`
5. Subscribe the bot to `message.channels`, `message.groups`, `message.im`, `message.mpim`, and `app_uninstalled`.
   Under **Subscribe to events on behalf of users**, also subscribe to `message.im` and `message.mpim` so people who authorize their own Slack account can share their direct and group messages.
6. Copy the app's Client ID, Client Secret, and Signing Secret into `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, and `SLACK_SIGNING_SECRET`. Set `API_BASE_URL` to the public HTTPS API address and `SECRETS_ENCRYPTION_KEY` to the hosted secrets manager value.
7. In Enter AI, sign in as a workspace admin and choose **Integrations → Connect Slack**. Invite the bot to each channel it should monitor. Each person who wants their own direct and group messages reviewed must also choose **Connect my DMs** and authorize Slack access.

The bot receives channel messages only in channels where it is a member; bot DMs are received when a user messages the installed app. A person-level authorization lets Enter AI receive new direct and group messages that person can access. Connecting is optional and does not backfill older messages. By connecting, the person agrees that new messages are copied with source links into the organization's Requests queue, where workspace members can see them and the CEO agent may review them. New events are verified with Slack's request signature and timestamp before they are accepted.

OAuth redirect and Events API request URLs must use HTTPS on the public deployment. If token rotation is enabled for the Slack app, Enter AI refreshes expiring bot tokens in the background. An invalidated refresh token changes the connection to **Reconnect required**.
