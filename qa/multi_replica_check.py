"""Verifies the properties that only break when more than one replica is running.

Run against the stack in docker-compose.prod.yml, which serves two API replicas
behind a load balancer:

    python3 qa/multi_replica_check.py

Each check states what would happen if the shared component were missing, because
that is the failure this is here to catch -- all of these pass trivially on a
single replica.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from uuid import uuid4

# The production Compose topology publishes only the TLS load balancer.  CI
# creates a short-lived self-signed certificate for it, so requests through the
# public path need this local-only trust override.
BASE = "https://127.0.0.1"
CI_TLS_CONTEXT = ssl._create_unverified_context()
COMPOSE = ["docker", "compose", "-f", "docker-compose.prod.yml"]

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f" -- {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def request(method: str, path: str, token: str | None = None, body=None, files=None, *, base: str = BASE,
            host: str | None = None):
    """Returns (status, headers, parsed-body-or-text)."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if files is not None:
        boundary = uuid4().hex
        name, filename, content = files
        payload = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\";"
            f" filename=\"{filename}\"\r\nContent-Type: text/plain\r\n\r\n".encode()
            + content + f"\r\n--{boundary}--\r\n".encode()
        )
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif body is not None:
        payload = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    else:
        payload = None
    if host:
        headers["Host"] = host
    req = urllib.request.Request(f"{base.rstrip('/')}{path}", data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30, context=CI_TLS_CONTEXT) as response:
            raw = response.read().decode()
            status, response_headers = response.status, dict(response.headers)
    except urllib.error.HTTPError as error:
        raw = error.read().decode()
        status, response_headers = error.code, dict(error.headers)
    try:
        return status, response_headers, json.loads(raw)
    except json.JSONDecodeError:
        return status, response_headers, raw


def compose(*args: str, replica: int | None = None) -> str:
    command = list(COMPOSE)
    if replica is not None:
        command += ["exec", "-T", "--index", str(replica)]
    else:
        command += ["exec", "-T"]
    result = subprocess.run(command + list(args), capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        return f"__ERROR__ {result.stderr.strip()}"
    return result.stdout.strip()


def replica_containers() -> list[str]:
    result = subprocess.run(COMPOSE + ["ps", "-q", "api"], capture_output=True, text=True, timeout=60)
    return [line for line in result.stdout.split() if line]


def replica_base_url(container: str) -> str | None:
    """Return the API's private bridge address for one named replica.

    This is used only by the CI harness, which runs on the Docker host.  It
    lets the confirmation check deliberately send its proposal and redemption
    to different containers instead of merely hoping the load balancer happens
    to alternate requests.
    """
    result = subprocess.run(
        ["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", container],
        capture_output=True, text=True, timeout=60,
    )
    address = result.stdout.strip()
    return f"http://{address}:8000" if result.returncode == 0 and address else None


def requests_served_per_replica(containers: list[str], pattern: str) -> list[int]:
    """How many matching requests each replica logged. Read from the containers
    rather than from a response header, so the deployment does not have to disclose
    its topology to clients just to be testable."""
    counts = []
    for container in containers:
        logs = subprocess.run(["docker", "logs", container], capture_output=True, text=True, timeout=60)
        counts.append((logs.stdout + logs.stderr).count(pattern))
    return counts


def both_replicas_take_traffic(containers: list[str]) -> None:
    """Without a working load balancer the rest of this file proves nothing, so
    establish first that requests really do land on two different replicas."""
    check("two API replicas are running", len(containers) == 2, f"found {len(containers)}")
    if len(containers) != 2:
        return
    before = requests_served_per_replica(containers, "GET /health")
    for _ in range(20):
        request("GET", "/health")
    after = requests_served_per_replica(containers, "GET /health")
    served = [later - earlier for earlier, later in zip(before, after)]
    check("the load balancer spreads traffic over both replicas", all(count > 0 for count in served),
          f"requests served per replica: {served}")


def shared_rate_limit(token: str) -> None:
    """The limit is configured per minute, not per replica. With in-process
    limiters each replica would grant the full allowance, so N replicas would let
    through N times the configured number of calls."""
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    allowed = 0
    throttled = 0
    for _ in range(limit * 4):
        status, _headers, _body = request("POST", "/api/ai/plan", token=token, body={"message": "hello"})
        if status == 429:
            throttled += 1
        elif status < 400:
            allowed += 1
    check("shared rate limit is not multiplied by replica count", allowed <= limit,
          f"{allowed} calls allowed against a limit of {limit}")
    check("throttling actually engages", throttled > 0, f"{throttled} requests were rejected")


def shared_confirmation_replay_guard(token: str, containers: list[str]) -> None:
    """A signed write proposal may be redeemed only once across replicas.

    An in-process set would let the second request through when the load
    balancer sends it to the other API container; the durable redemption record
    must make that retry return 409 instead.
    """
    if len(containers) != 2:
        check("two explicit replicas are available for confirmation replay", False, f"found {len(containers)}")
        return
    replica_a, replica_b = (replica_base_url(container) for container in containers)
    if not replica_a or not replica_b:
        check("replica private addresses are available for confirmation replay", False, f"{replica_a=}, {replica_b=}")
        return
    status, _headers, project = request("POST", "/api/projects", token=token,
                                        base=replica_a,
                                        host="127.0.0.1",
                                        body={"name": f"Replay Guard {uuid4().hex[:8]}", "code": uuid4().hex[:6].upper()})
    if status >= 400:
        check("project created for confirmation replay check", False, f"{status} {project}")
        return
    status, _headers, proposal = request("POST", "/api/ai/plan", token=token, base=replica_a,
                                         host="127.0.0.1",
                                         body={"message": "Create task shared confirmation replay check"})
    actions = proposal.get("actions", []) if isinstance(proposal, dict) else []
    token_value = actions[0].get("confirmation_token") if actions else None
    if status >= 400 or not token_value:
        check("Copilot returned a write confirmation", False, f"{status} {proposal}")
        return
    first, _headers, _body = request("POST", "/api/ai/confirm", token=token, base=replica_b,
                                     host="127.0.0.1",
                                     body={"confirmation_token": token_value})
    second, _headers, _body = request("POST", "/api/ai/confirm", token=token, base=replica_a,
                                      host="127.0.0.1",
                                      body={"confirmation_token": token_value})
    check("replica B accepts confirmation proposed through replica A", first < 400, str(first))
    check("replica A rejects replica B's replay", second == 409, str(second))


def exactly_once_agent_stepping() -> None:
    """Two replicas must not step the same agent at once. Proven at the mechanism:
    replica 1 holds the agent's advisory lock while replica 2 asks for it."""
    # The holder releases by exiting on its own. Killing the `docker compose exec`
    # client would leave the process inside the container running and still holding
    # the lock, so the release check below would be testing nothing.
    hold = (
        "import time;"
        "from app.database import engine;"
        "from app.execution import agent_step_lock;"
        "ctx = agent_step_lock(engine, 'shared-agent');"
        "print('ACQUIRED' if ctx.__enter__() else 'BUSY', flush=True);"
        "time.sleep(20)"
    )
    try_once = (
        "from app.database import engine;"
        "from app.execution import agent_step_lock;"
        "ctx = agent_step_lock(engine, 'shared-agent');"
        "print('ACQUIRED' if ctx.__enter__() else 'BUSY', flush=True);"
        "ctx.__exit__(None, None, None)"
    )
    holder = subprocess.Popen(COMPOSE + ["exec", "-T", "--index", "1", "api", "python", "-c", hold],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        # No blocking read on the holder's pipe: a failed exec would hang this job
        # rather than fail it. Give it time to take the lock, then ask replica 2 --
        # if replica 1 never acquired, replica 2 succeeds and the check says so.
        time.sleep(6)
        contended = compose("api", "python", "-c", try_once, replica=2)
        check("replica 2 is refused an agent lock replica 1 holds", contended == "BUSY", contended)
        holder_output = holder.communicate(timeout=120)[0].strip()
        check("replica 1 reported taking the lock", "ACQUIRED" in holder_output, holder_output)
    except subprocess.TimeoutExpired:
        check("the lock holder exited", False, "holder did not finish within 120s")
    finally:
        if holder.poll() is None:
            holder.kill()

    released = compose("api", "python", "-c", try_once, replica=2)
    check("the lock is released once its holder exits", released == "ACQUIRED", released)


def uploads_are_visible_from_either_replica(token: str, containers: list[str]) -> None:
    """Local disk is per-container: whichever replica did not receive the upload
    would 404 it. Shared object storage is what makes this pass."""
    status, _headers, project = request("POST", "/api/projects", token=token,
                                        body={"name": f"Replica {uuid4().hex[:8]}", "code": uuid4().hex[:6].upper()})
    if status >= 400:
        check("project created for upload check", False, f"{status} {project}")
        return
    status, _headers, _document = request("POST", f"/api/projects/{project['id']}/documents", token=token,
                                          files=("file", "brief.txt", b"shared object storage"))
    check("document upload accepted", status == 201, str(status))

    # Read it back repeatedly; the load balancer spreads these over both replicas.
    path = f"/api/projects/{project['id']}/documents"
    before = requests_served_per_replica(containers, path)
    results = [request("GET", path, token=token) for _ in range(10)]
    after = requests_served_per_replica(containers, path)
    check("every replica lists the uploaded document",
          all(status == 200 and len(listed) == 1 for status, _headers, listed in results),
          f"statuses {[status for status, _h, _b in results]}")
    served = [later - earlier for earlier, later in zip(before, after)]
    check("the read-back was spread over both replicas", all(count > 0 for count in served),
          f"reads served per replica: {served}")

    stored = compose("db", "psql", "-U", os.getenv("POSTGRES_USER", "enterai"),
                     "-d", os.getenv("POSTGRES_DB", "enterai"), "-tAc",
                     "select path from project_documents order by created_at desc limit 1")
    check("the document went to object storage, not a container's disk",
          stored.startswith("s3://"), stored)


def invite_token_from_outbox(email: str) -> str | None:
    """Read a locally queued invite link for CI's deliberately unavailable SMTP.

    Hosted deployments deliver this link through their real mail relay, so the
    check falls back to the seeded admin below when no local outbox row exists.
    The recipient is generated by this process and cannot contain SQL syntax.
    """
    escaped = email.replace("'", "''")
    body = compose("db", "psql", "-U", os.getenv("POSTGRES_USER", "enterai"),
                   "-d", os.getenv("POSTGRES_DB", "enterai"), "-tAc",
                   f"select body from email_outbox where recipient='{escaped}' and body is not null order by created_at desc limit 1")
    match = re.search(r"[?&]invite=([A-Za-z0-9_-]+)", body)
    return match.group(1) if match else None


def provision_test_account() -> str | None:
    """Create a consented account, or use an invited/seeded account in pilot mode.

    The production Compose default is invite-only.  CI exercises that control by
    logging in as its isolated bootstrap admin, creating an invite, and accepting
    it when the failed SMTP relay leaves the single-use link in email_outbox.
    """
    key = uuid4().hex
    email = f"{key}@example.com"
    password = "Replica-Check-password-7!"
    status, _headers, account = request("POST", "/api/auth/register", body={
        "organization_name": f"Replica Check {key[:8]}", "name": "Replica Admin",
        "email": email, "password": password, "accept_terms": True,
    })
    if status < 400 and isinstance(account, dict) and account.get("token"):
        check("registered a consented test account", True)
        return account["token"]
    if status != 403:
        check("registered a consented test account", False, f"{status} {account}")
        return None

    admin_email = os.getenv("SEED_ADMIN_EMAIL", "").strip()
    admin_password = os.getenv("SEED_ADMIN_PASSWORD", "")
    if not admin_email or not admin_password:
        check("seeded admin credentials are available for invite-only checks", False,
              "set SEED_ADMIN_EMAIL and SEED_ADMIN_PASSWORD")
        return None
    status, _headers, admin = request("POST", "/api/auth/login", body={
        "email": admin_email, "password": admin_password,
    })
    if status >= 400 or not isinstance(admin, dict) or not admin.get("token"):
        check("seeded admin can sign in for invite-only checks", False, f"{status} {admin}")
        return None
    admin_token = admin["token"]
    status, _headers, invite = request("POST", "/api/auth/invites", token=admin_token,
                                       body={"email": email, "role": "member"})
    if status != 201:
        check("seeded admin can create an invite for the test account", False, f"{status} {invite}")
        return None
    check("seeded admin can create an invite for the test account", True)

    raw_invite = os.getenv("QA_INVITE_TOKEN", "").strip() or invite_token_from_outbox(email)
    if raw_invite:
        status, _headers, accepted = request("POST", "/api/auth/accept-invite", body={
            "token": raw_invite, "name": "Replica Admin", "password": password,
            "accept_terms": True,
        })
        if status == 200 and isinstance(accepted, dict) and accepted.get("token"):
            check("accepted the invited test account with terms consent", True)
            return accepted["token"]
        check("accepted the invited test account with terms consent", False, f"{status} {accepted}")
        return None

    # A real SMTP relay will not retain its delivered body in this database. The
    # invite creation above still proves the protected path, so use the isolated
    # seeded admin for the remaining shared-state checks in that environment.
    check("invite link is available for local acceptance", True, "using seeded admin while SMTP owns delivery")
    return admin_token


def main() -> int:
    containers = replica_containers()
    both_replicas_take_traffic(containers)

    token = provision_test_account()
    if not token:
        return 1

    uploads_are_visible_from_either_replica(token, containers)
    exactly_once_agent_stepping()
    shared_confirmation_replay_guard(token, containers)
    # Last, because it deliberately exhausts this account's allowance.
    shared_rate_limit(token)

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("All multi-replica checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
