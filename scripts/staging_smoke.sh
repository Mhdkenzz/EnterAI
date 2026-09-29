#!/usr/bin/env bash
# Public, read-only release smoke checks.  Optional credentials exercise the
# session cookie and logout path without printing or persisting the password.
set -euo pipefail

base_url=${1:-${STAGING_PUBLIC_URL:-}}
[[ -n "$base_url" ]] || { echo "Usage: $0 https://staging.example.test" >&2; exit 2; }
base_url=${base_url%/}
case "$base_url" in
  https://*) ;;
  *) echo "staging smoke requires an HTTPS base URL" >&2; exit 2 ;;
esac

curl_args=(--fail --silent --show-error --location --max-time "${STAGING_SMOKE_TIMEOUT_SECONDS:-20}")
check_status() {
  local path=$1
  local body
  body=$(curl "${curl_args[@]}" -H 'Accept: application/json' "$base_url$path")
  [[ -n "$body" ]] || { echo "empty response from $path" >&2; return 1; }
  printf '%s\n' "$path: ok"
}

for path in /live /ready /health; do
  check_status "$path"
done

# Hosted staging must keep self-serve registration closed. This probe is opt-in so
# the same script remains useful for local demo environments that intentionally
# allow registration; it creates no account because the expected response is 403.
if [[ "${STAGING_SMOKE_REQUIRE_INVITE_ONLY:-false}" == "true" ]]; then
  invite_probe=$(jq -cn --arg suffix "$(date +%s)-$$" \
    '{organization_name:("Smoke probe "+$suffix),name:"Smoke probe",email:("smoke-"+$suffix+"@example.com"),password:"Smoke-probe-password-7!",accept_terms:true}')
  invite_status=$(curl --silent --show-error --location --max-time "${STAGING_SMOKE_TIMEOUT_SECONDS:-20}" \
    -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' \
    --data "$invite_probe" "$base_url/api/auth/register")
  [[ "$invite_status" == "403" ]] || {
    echo "invite-only registration probe expected HTTP 403, got $invite_status" >&2
    exit 1
  }
  printf '%s\n' '/api/auth/register: invite-only guard ok'
fi

# The reverse proxy should serve the frontend.  Legal pages are usually hosted
# separately, so check the exact published URLs supplied by the release workflow
# instead of assuming that the app owns /terms and /privacy routes.
curl "${curl_args[@]}" -H 'Accept: text/html' "$base_url/" >/dev/null
for name in TERMS PRIVACY; do
  var="STAGING_${name}_URL"
  url=${!var:-}
  if [[ -n "$url" ]]; then
    case "$url" in https://*) ;; *) echo "$var must use HTTPS" >&2; exit 2;; esac
    curl "${curl_args[@]}" -o /dev/null "$url"
    printf '%s\n' "$var: ok"
  elif [[ "${STAGING_SMOKE_REQUIRE_LEGAL:-false}" == "true" ]]; then
    echo "set $var when STAGING_SMOKE_REQUIRE_LEGAL=true" >&2
    exit 2
  fi
done

# An invited pilot account can opt into an authenticated check.  Keep this
# disabled by default so the smoke can be run with no secret on the operator's
# workstation.  curl's cookie jar is temporary and removed on exit.
if [[ -n "${STAGING_SMOKE_EMAIL:-}" || -n "${STAGING_SMOKE_PASSWORD:-}" ]]; then
  [[ -n "${STAGING_SMOKE_EMAIL:-}" && -n "${STAGING_SMOKE_PASSWORD:-}" ]] || {
    echo "set both STAGING_SMOKE_EMAIL and STAGING_SMOKE_PASSWORD" >&2
    exit 2
  }
  cookie_jar=$(mktemp)
  trap 'rm -f "$cookie_jar"' EXIT
  login_payload=$(jq -cn --arg email "$STAGING_SMOKE_EMAIL" --arg password "$STAGING_SMOKE_PASSWORD" \
    '{email:$email,password:$password}')
  curl "${curl_args[@]}" -c "$cookie_jar" -b "$cookie_jar" \
    -H 'Content-Type: application/json' -H 'Accept: application/json' \
    --data "$login_payload" "$base_url/api/auth/login" >/dev/null
  curl "${curl_args[@]}" -b "$cookie_jar" -H 'Accept: application/json' \
    "$base_url/api/me" >/dev/null
  curl "${curl_args[@]}" -b "$cookie_jar" -H 'Accept: application/json' \
    "$base_url/api/projects" >/dev/null
  if [[ "${STAGING_SMOKE_REQUIRE_AI:-false}" == "true" ]]; then
    ai_payload=$(jq -cn '{message:"What should I work on today?"}')
    curl "${curl_args[@]}" -b "$cookie_jar" -H 'X-EnterAI-Client: staging-smoke' \
      -H 'Content-Type: application/json' -H 'Accept: application/json' \
      --data "$ai_payload" "$base_url/api/ai/plan" | jq -e 'has("reply") and (.actions | type == "array")' >/dev/null
    printf '%s\n' '/api/ai/plan (read-only): ok'
  fi
  curl "${curl_args[@]}" -b "$cookie_jar" -H 'X-EnterAI-Client: staging-smoke' \
    -X POST "$base_url/api/auth/logout" -o /dev/null
  printf '%s\n' '/api/auth/login -> /api/me -> /api/auth/logout: ok'
elif [[ "${STAGING_SMOKE_REQUIRE_AI:-false}" == "true" ]]; then
  echo 'STAGING_SMOKE_REQUIRE_AI=true requires pilot credentials' >&2
  exit 2
fi

echo "staging smoke passed: $base_url"
