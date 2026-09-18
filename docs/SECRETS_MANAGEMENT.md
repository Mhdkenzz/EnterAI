# Secrets Manager Integration
- Use environment variables injected at deploy time (not committed to repo)
- Secret rotation: rotate JWT_SECRET, DATABASE_URL credentials, OPENAI_API_KEY on schedule
- Rotation procedure: deploy new secret, restart services, verify health, revoke old secret
- Never log secrets; audit all secret access via audit_events
