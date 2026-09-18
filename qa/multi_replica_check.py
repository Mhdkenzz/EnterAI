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
import subprocess
import sys
import time
import urllib.error
import urllib.request
from uuid import uuid4

BASE = "http://127.0.0.1:8000"
COMPOSE = ["docker", "compose", "-f", "docker-compose.prod.yml"]

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {name}" + (f" -- {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def request(method: str, path: str, token: str | None = None, body=None, files=None):
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
    req = urllib.request.Request(f"{BASE}{path}", data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
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


def main() -> int:
    containers = replica_containers()
    both_replicas_take_traffic(containers)

    key = uuid4().hex
    status, _headers, account = request("POST", "/api/auth/register", body={
        "organization_name": f"Replica Check {key[:8]}", "name": "Replica Admin",
        "email": f"{key}@example.com", "password": "replica-check-password",
    })
    if status >= 400 or "token" not in account:
        check("registered an account to test with", False, f"{status} {account}")
        return 1
    token = account["token"]

    uploads_are_visible_from_either_replica(token, containers)
    exactly_once_agent_stepping()
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
