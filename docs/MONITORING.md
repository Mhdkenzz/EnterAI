# Monitoring & Logging
- Structured JSON logging via Python logging (configured in observability.py)
- Error tracking: log all HTTP 500 errors with request context, user, organization
- Provider failure alerts: log CopilotProviderError and provider call failures
- Uptime monitoring: /health endpoint; external health check on /api/health
- Agent execution failure alerts: consecutive_task_failures threshold triggers log event
- Deployment rollback: restore previous Docker image tag, revert DB migration, redeploy
