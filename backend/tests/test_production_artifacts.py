"""Static release checks for container and hosted-stack entrypoints.

These are intentionally small: they catch the class of regressions that only
appears after an image is built or a hosted Compose stack starts, without needing
Docker-in-Docker in the unit-test job.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_backend_healthcheck_uses_dockerfile_cmd_and_runtime_host_expansion():
    dockerfile = (ROOT / "backend" / "Dockerfile").read_text()

    assert "CMD-SHELL" not in dockerfile
    assert 'CMD ["sh", "-c",' in dockerfile
    assert 'host=\\"${HEALTHCHECK_HOST:-localhost}\\"' in dockerfile
    assert 'host=\\"${host%%,*}\\"' in dockerfile
    assert 'curl -fsS -H \\"Host: $host\\" http://127.0.0.1:8000/health' in dockerfile


def test_production_backup_runs_bash_script_with_bash_image_and_entrypoint():
    compose = (ROOT / "docker-compose.prod.yml").read_text()
    backup = compose.split("\n  backup:\n", 1)[1].split("\n  # Mirrors uploaded files", 1)[0]

    assert "image: postgres:16\n" in backup
    assert 'entrypoint: ["/bin/bash", "-c"]' in backup
    assert "/bin/bash /backup.sh" in backup
    assert "; sh /backup.sh" not in backup


def test_multi_replica_ci_exercises_invite_only_and_terms_consent():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    qa = (ROOT / "qa" / "multi_replica_check.py").read_text()

    assert 'INVITE_ONLY_REGISTRATION: "true"' in ci
    assert '"accept_terms": True' in qa
    assert "/api/auth/invites" in qa
    assert "/api/auth/accept-invite" in qa
    assert "SEED_ADMIN_EMAIL" in qa and "SEED_ADMIN_PASSWORD" in qa
