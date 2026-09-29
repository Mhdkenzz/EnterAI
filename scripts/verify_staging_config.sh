#!/usr/bin/env bash
# Validate the host environment and immutable release bundle before Compose is
# allowed to create any staging containers.  This script deliberately prints
# names and paths only; it never dumps the resolved Compose configuration because
# that output contains database passwords and provider keys.
set -euo pipefail

usage() {
  echo "Usage: $0 HOST_ENV [RELEASE_ENV]" >&2
  exit 2
}

fail() {
  echo "staging preflight failed: $*" >&2
  exit 1
}

[[ $# -ge 1 && $# -le 2 ]] || usage
host_env=$1
release_env=${2:-}
[[ -r "$host_env" ]] || fail "host environment is not readable: $host_env"

env_files=("$host_env")
if [[ -n "$release_env" ]]; then
  env_files+=("$release_env")
fi
for env_file in "${env_files[@]}"; do
  [[ -r "$env_file" ]] || fail "release environment is not readable: $env_file"
done

# The host file contains credentials and is intentionally outside the release
# archive.  Refuse a world/group-readable file before Docker can consume it.
mode=$(stat -c '%a' "$host_env")
[[ "$mode" == "600" ]] || fail "$host_env must have mode 600 (found $mode)"

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
compose_file="$script_dir/../docker-compose.prod.yml"
[[ -r "$compose_file" ]] || fail "missing docker-compose.prod.yml next to scripts"

compose=(docker compose -p enterai-staging -f "$compose_file" --env-file "$host_env")
if [[ -n "$release_env" ]]; then
  compose+=(--env-file "$release_env")
fi

# `config` evaluates all `${NAME:?required}` expressions and gives us the
# effective service environment for checks below.  Keep it in memory so no
# secret-bearing JSON is written to the host or included in CI logs.
if ! config_json=$("${compose[@]}" config --format json); then
  fail "docker compose could not resolve the staging bundle"
fi

config_value() {
  local service=$1 name=$2
  jq -r --arg service "$service" --arg name "$name" \
    '.services[$service].environment[$name] // empty' <<<"$config_json"
}

require_value() {
  local service=$1 name=$2 value
  value=$(config_value "$service" "$name")
  [[ -n "$value" ]] || fail "$service.$name is empty"
}

api_env=(ENVIRONMENT DATABASE_URL JWT_SECRET REDIS_URL EMAIL_PROVIDER SMTP_HOST EMAIL_FROM
  APP_BASE_URL API_BASE_URL SECRETS_ENCRYPTION_KEY INVITE_ONLY_REGISTRATION TERMS_URL
  PRIVACY_URL TRUSTED_HOSTS HEALTHCHECK_HOST SEED_ADMIN_EMAIL SEED_ADMIN_PASSWORD
  CORS_ALLOWED_ORIGINS STORAGE_BACKEND STORAGE_BUCKET STORAGE_ENDPOINT_URL
  AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AI_PROVIDER EMBEDDING_PROVIDER)
for name in "${api_env[@]}"; do
  require_value api "$name"
done

[[ "$(config_value api ENVIRONMENT)" == staging ]] || fail "api.ENVIRONMENT must be staging"
case "$(config_value api INVITE_ONLY_REGISTRATION | tr '[:upper:]' '[:lower:]')" in
  1|true|yes|on) ;;
  *) fail "invite-only registration must remain enabled for staging" ;;
esac
[[ "$(config_value api AI_PROVIDER)" != "" ]] || fail "choose an explicit AI_PROVIDER"

for name in POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD; do
  require_value db "$name"
done

for name in DATABASE_URL BACKUP_INTERVAL_SECONDS BACKUP_RETENTION_DAYS; do
  require_value backup "$name"
done
for name in BACKUP_OBJECTS_BUCKET BACKUP_OBJECTS_ACCESS_KEY BACKUP_OBJECTS_SECRET_KEY; do
  require_value objectstore-backup "$name"
done

primary_bucket=$(config_value api STORAGE_BUCKET)
backup_bucket=$(config_value objectstore-backup BACKUP_OBJECTS_BUCKET)
[[ "$primary_bucket" != "$backup_bucket" ]] || fail "backup object bucket must differ from the primary bucket"

for service in api web; do
  image=$(jq -r --arg service "$service" '.services[$service].image // empty' <<<"$config_json")
  [[ "$image" == *@sha256:* ]] || fail "$service image must be pinned by digest"
done

env_file_value() {
  local file=$1 key=$2
  awk -F= -v key="$key" '$1 == key { value = $0; sub(/^[^=]*=/, "", value) } END { print value }' "$file"
}

tls_cert=$(env_file_value "$host_env" TLS_CERT_PATH)
tls_key=$(env_file_value "$host_env" TLS_KEY_PATH)
[[ -n "$tls_cert" && -r "$tls_cert" ]] || fail "TLS_CERT_PATH is missing or unreadable"
[[ -n "$tls_key" && -r "$tls_key" ]] || fail "TLS_KEY_PATH is missing or unreadable"

# Reuse the backend's canonical hosted-environment validator.  It checks URL,
# CORS, proxy, mail, storage, and encryption invariants without contacting the
# database or provider.  The API image is used so this works on a host that has
# no checkout or Python environment beyond the reviewed release artifact.
if ! "${compose[@]}" run --rm --no-deps --entrypoint python api -c \
  'from app.deployment_config import deployment_configuration_errors; errors = deployment_configuration_errors(); print("; ".join(errors)); raise SystemExit(1 if errors else 0)'; then
  fail "backend hosted configuration validator rejected the staging environment"
fi

echo "staging preflight passed: immutable images, hosted configuration, TLS files, invite-only mode, and backup isolation verified"
