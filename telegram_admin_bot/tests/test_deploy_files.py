"""The deployment files, checked without Docker: docker-compose.yml's shape
(profiles, ports, volumes, healthchecks, env), the Caddyfiles, the shell
scripts, .dockerignore, and the `.env` one-liner in DEPLOY_TODAY.md — run
for real and its output checked against what the panel and crypto.py accept.

`caddy` and `shellcheck` are optional: set CADDY_BIN / SHELLCHECK_BIN (or
have them on PATH) to also run `caddy validate` and shellcheck; otherwise
those two tests skip.
"""

from __future__ import annotations

import base64
import fnmatch
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import crypto
import totp

APP_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = APP_DIR.parent
COMPOSE = yaml.safe_load((APP_DIR / "docker-compose.yml").read_text(encoding="utf-8"))
SERVICES = COMPOSE["services"]
APP_SERVICES = {"migrate", "panel", "scheduler", "booking-pages", "manager"}
LONG_RUNNING = set(SERVICES) - {"migrate"}
DEPLOY_DOC = (REPO_DIR / "DEPLOY_TODAY.md").read_text(encoding="utf-8")
ENV_EXAMPLE = (APP_DIR / ".env.example").read_text(encoding="utf-8")


def _enabled(profiles: set[str]) -> set[str]:
    return {name for name, svc in SERVICES.items() if not svc.get("profiles") or set(svc["profiles"]) & profiles}


def _depends(svc: dict) -> set[str]:
    deps = svc.get("depends_on") or {}
    return set(deps) if isinstance(deps, (list, dict)) else set()


def _env_names(svc: dict) -> set[str]:
    env = svc.get("environment") or {}
    if isinstance(env, list):
        return {item.split("=", 1)[0] for item in env}
    return set(env)


# --- docker-compose.yml ------------------------------------------------------


def test_every_compose_variable_is_documented_in_env_example():
    text = (APP_DIR / "docker-compose.yml").read_text(encoding="utf-8")
    used = set(re.findall(r"(?<!\$)\$\{([A-Z0-9_]+)", text))
    assert used, "no ${VAR} references found — did the file format change?"
    documented = set(re.findall(r"^#?\s*([A-Z0-9_]+)=", ENV_EXAMPLE, re.M))
    assert used - documented == set()


@pytest.mark.parametrize(
    "profiles, expected, absent",
    [
        (set(), {"postgres", "valkey", "migrate", "panel", "manager", "scheduler"}, {"caddy", "caddy-booking", "booking-pages"}),
        ({"public"}, {"caddy", "panel"}, {"caddy-booking", "booking-pages"}),
        ({"public", "booking-pages"}, {"caddy", "booking-pages"}, {"caddy-booking"}),
        ({"booking-only"}, {"caddy-booking", "booking-pages"}, {"caddy"}),
    ],
)
def test_profiles_start_the_right_services_and_their_dependencies(profiles, expected, absent):
    enabled = _enabled(profiles)
    assert expected <= enabled
    assert not (absent & enabled)
    # Compose refuses to start a service whose dependency's profile is off.
    for name in enabled:
        missing = _depends(SERVICES[name]) - enabled
        assert not missing, f"{name} depends on {missing}, not enabled with profiles {profiles}"


def test_the_two_caddies_never_run_together():
    assert SERVICES["caddy"]["profiles"] == ["public"]
    assert SERVICES["caddy-booking"]["profiles"] == ["booking-only"]


def test_only_caddy_is_public_and_the_panel_is_loopback_only():
    for name, svc in SERVICES.items():
        ports = [str(p) for p in svc.get("ports") or []]
        if name in ("caddy", "caddy-booking"):
            assert sorted(p.split("/")[0] for p in ports) == ["443:443", "443:443", "80:80"]
        elif name == "panel":
            assert ports == ["127.0.0.1:8787:8787"]
        else:
            assert ports == [], f"{name} publishes {ports}"


def test_restart_policies():
    for name in LONG_RUNNING:
        assert SERVICES[name].get("restart") == "unless-stopped", name
    assert SERVICES["migrate"]["restart"] == "no"


def test_app_services_wait_for_a_healthy_database_and_a_finished_migration():
    for name in APP_SERVICES - {"migrate"}:
        deps = SERVICES[name]["depends_on"]
        assert deps["postgres"]["condition"] == "service_healthy", name
        assert deps["valkey"]["condition"] == "service_healthy", name
        assert deps["migrate"]["condition"] == "service_completed_successfully", name
    assert SERVICES["migrate"]["depends_on"] == {"postgres": {"condition": "service_healthy"}}
    for name in ("postgres", "valkey", "panel"):
        assert SERVICES[name].get("healthcheck", {}).get("test"), f"{name} has no healthcheck"


def test_panel_healthcheck_hits_a_real_unauthenticated_route():
    check = " ".join(SERVICES["panel"]["healthcheck"]["test"])
    assert "http://127.0.0.1:8787/api/login-options" in check
    assert '@app.get("/api/login-options")' in (APP_DIR / "panel.py").read_text(encoding="utf-8")


def test_volumes_keep_the_database_and_the_certificates():
    assert "pgdata:/var/lib/postgresql/data" in SERVICES["postgres"]["volumes"]
    assert "caddy_data:/data" in SERVICES["caddy"]["volumes"]
    assert "caddy_booking_data:/data" in SERVICES["caddy-booking"]["volumes"]
    for volume in ("pgdata", "caddy_data", "caddy_config", "caddy_booking_data", "caddy_booking_config"):
        assert volume in COMPOSE["volumes"]
    for name in APP_SERVICES:
        assert "./data:/app/data" in SERVICES[name]["volumes"], name
    # Valkey is only a message bus: nothing to keep, so nothing written.
    assert SERVICES["valkey"]["command"] == ["valkey-server", "--save", "", "--appendonly", "no"]


def test_every_service_has_capped_logs():
    for name, svc in SERVICES.items():
        logging = svc.get("logging") or {}
        assert logging.get("driver") == "json-file", name
        assert logging["options"]["max-size"] and logging["options"]["max-file"], name


def test_app_services_get_env_file_except_the_public_booking_pages():
    # panel.py reads PANEL_DOMAIN / ADMIN_TOTP_SECRET only from .env; the
    # public-mode refusal depends on it arriving.
    for name in APP_SERVICES - {"booking-pages"}:
        assert SERVICES[name]["env_file"] == [{"path": ".env", "required": False}], name
    assert SERVICES["booking-pages"]["env_file"] == []


def test_each_app_service_gets_what_it_reads_at_boot():
    needs = {
        "migrate": {"DATABASE_URL", "USERBOT_MASTER_KEY"},
        "panel": {"DATABASE_URL", "REDIS_URL", "ADMIN_PASSWORD", "USERBOT_MASTER_KEY", "DATA_DIR"},
        "manager": {"DATABASE_URL", "REDIS_URL", "USERBOT_MASTER_KEY", "DATA_DIR"},
        "scheduler": {"DATABASE_URL", "REDIS_URL", "USERBOT_MASTER_KEY", "DATA_DIR"},
        "booking-pages": {"DATABASE_URL", "REDIS_URL", "PUBLIC_BIND", "PUBLIC_PORT", "PUBLIC_HOST"},
    }
    for name, names in needs.items():
        assert names <= _env_names(SERVICES[name]), f"{name} lacks {names - _env_names(SERVICES[name])}"
    for name in APP_SERVICES:
        url = SERVICES[name]["environment"]["DATABASE_URL"]
        assert url.startswith("postgresql://") and "@postgres:5432/" in url
    assert SERVICES["booking-pages"]["environment"]["PUBLIC_BIND"] == "0.0.0.0"


def test_caddy_mounts_what_its_config_imports():
    mounts = SERVICES["caddy"]["volumes"]
    for name in ("Caddyfile", "Caddyfile.booking", "Caddyfile.both"):
        assert f"./{name}:/etc/caddy/{name}:ro" in mounts
    both = (APP_DIR / "Caddyfile.both").read_text(encoding="utf-8")
    imports = re.findall(r"^import\s+(\S+)", both, re.M)
    assert imports == ["Caddyfile", "Caddyfile.booking"]  # relative to Caddyfile.both itself
    assert "Caddyfile.both" in " ".join(SERVICES["caddy"]["command"])


def test_caddyfiles_proxy_to_the_compose_services():
    panel = (APP_DIR / "Caddyfile").read_text(encoding="utf-8")
    booking = (APP_DIR / "Caddyfile.booking").read_text(encoding="utf-8")
    assert "{$PANEL_DOMAIN} {" in panel and "reverse_proxy panel:8787" in panel
    assert "{$BOOKING_DOMAIN} {" in booking
    port = SERVICES["booking-pages"]["environment"]["PUBLIC_PORT"]
    assert f"reverse_proxy booking-pages:{port}" in booking


# --- Caddy and shell scripts, with the real tools when available ----------------


def _tool(env_var: str, name: str) -> str | None:
    return os.environ.get(env_var) or shutil.which(name)


@pytest.mark.parametrize("config", ["Caddyfile", "Caddyfile.booking", "Caddyfile.both"])
def test_caddy_validates_each_config(config, tmp_path):
    caddy = _tool("CADDY_BIN", "caddy")
    if not caddy:
        pytest.skip("caddy not found (set CADDY_BIN)")
    env = dict(os.environ, PANEL_DOMAIN="1-2-3-4.sslip.io", BOOKING_DOMAIN="book.1-2-3-4.sslip.io")
    result = subprocess.run(
        [caddy, "validate", "--config", str(APP_DIR / config), "--adapter", "caddyfile"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Valid configuration" in result.stdout + result.stderr


def test_shell_scripts_pass_shellcheck():
    shellcheck = _tool("SHELLCHECK_BIN", "shellcheck")
    if not shellcheck:
        pytest.skip("shellcheck not found (set SHELLCHECK_BIN)")
    scripts = [str(APP_DIR / "deploy" / n) for n in ("bootstrap_ubuntu24.sh", "backup.sh")]
    result = subprocess.run([shellcheck, "-s", "bash", *scripts], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


def test_files_used_on_linux_are_forced_to_lf():
    attributes = (REPO_DIR / ".gitattributes").read_text(encoding="utf-8")
    for pattern in ("*.sh", "Caddyfile*", "*.sql"):
        assert re.search(rf"^{re.escape(pattern)}\s+text\s+eol=lf", attributes, re.M), pattern
    for path in [*(APP_DIR / "deploy").glob("*.sh"), *APP_DIR.glob("Caddyfile*")]:
        data = path.read_bytes()
        assert b"\r\n" not in data, f"{path.name} has CRLF line endings"
        if path.suffix == ".sh":
            assert data.startswith(b"#!/usr/bin/env bash\n")
            assert b"set -euo pipefail" in data


# --- .dockerignore -------------------------------------------------------------


def _dockerignored(rel: str, patterns: list[str]) -> bool:
    ignored = False
    for pattern in patterns:
        negate = pattern.startswith("!")
        pat = pattern[1:] if negate else pattern
        pat = pat.rstrip("/")
        parts = rel.split("/")
        hit = any(fnmatch.fnmatch("/".join(parts[:i]), pat) for i in range(1, len(parts) + 1))
        if hit:
            ignored = not negate
    return ignored


def test_dockerignore_keeps_the_code_and_drops_the_secrets():
    patterns = [
        line.strip() for line in (APP_DIR / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    keep = [
        "panel.py", "manager.py", "scheduler.py", "public_app.py", "migrate_entrypoint.py", "pg.py",
        "requirements.txt", "migrations/0001_init.sql", "static/index.html", "static/owner/index.html",
        "static/owner/owner.js",
    ]
    for rel in keep:
        assert (APP_DIR / rel).exists(), rel
        assert not _dockerignored(rel, patterns), f"{rel} would be left out of the image"
    for rel in (".env", ".env.bak", "data/x.session", "backups/userbot-1.tar.age", "deploy/age-recipient.txt"):
        assert _dockerignored(rel, patterns), f"{rel} would be copied into the image"


def test_dockerfile_installs_requirements_and_binds_for_compose():
    dockerfile = (APP_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^FROM python:3\.\d+-slim$", dockerfile, re.M)
    assert "pip install -r requirements.txt" in dockerfile
    assert "ADMIN_HOST=0.0.0.0" in dockerfile


# --- DEPLOY_TODAY.md -------------------------------------------------------------


def _one_liner(text: str) -> str:
    match = re.search(r'^\s*#?\s*python3 -c "(import secrets,base64;[^"]+)" > \.env$', text, re.M)
    assert match, "the .env one-liner was not found"
    return match.group(1)


def _panel_min_password() -> int:
    return int(re.search(r"^PUBLIC_MIN_PASSWORD = (\d+)$", (APP_DIR / "panel.py").read_text(encoding="utf-8"), re.M).group(1))


@pytest.mark.parametrize("source", ["DEPLOY_TODAY.md", ".env.example"])
def test_env_one_liner_makes_values_the_stack_accepts(source, monkeypatch):
    code = _one_liner(DEPLOY_DOC if source == "DEPLOY_TODAY.md" else ENV_EXAMPLE)
    for _ in range(5):  # random output: try it a few times
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
        values = dict(line.split("=", 1) for line in out.splitlines())
        assert set(values) == {"USERBOT_MASTER_KEY", "ADMIN_PASSWORD", "POSTGRES_PASSWORD", "ADMIN_TOTP_SECRET"}

        # crypto.py loads it exactly as the containers do.
        monkeypatch.setenv("USERBOT_MASTER_KEY", values["USERBOT_MASTER_KEY"])
        monkeypatch.delenv("USERBOT_MASTER_KEY_FILE", raising=False)
        crypto.reset_cache()
        keyring = crypto.load_keyring()
        assert len(keyring.active_key()) == 32
        blob = crypto.encrypt(b"x", aad=b"s:f")
        assert crypto.decrypt(blob, aad=b"s:f") == b"x"

        totp.validate_secret(values["ADMIN_TOTP_SECRET"])
        assert len(base64.b32decode(values["ADMIN_TOTP_SECRET"])) == 20
        assert len(values["ADMIN_PASSWORD"]) >= _panel_min_password()

        # All of them go through compose's ${...} interpolation, and the
        # Postgres one into a URL: no `$`, nothing URL-special.
        for key, value in values.items():
            assert "$" not in value and "\n" not in value, key
        assert re.fullmatch(r"[A-Za-z0-9_-]+", values["POSTGRES_PASSWORD"])
        assert re.fullmatch(r"[A-Za-z0-9_-]+", values["ADMIN_PASSWORD"])
    crypto.reset_cache()


def test_deploy_doc_defaults_to_sslip_and_covers_the_whole_path():
    doc = DEPLOY_DOC
    assert 'echo "PANEL_DOMAIN=${IP//./-}.sslip.io" >> .env' in doc
    assert 'echo "COMPOSE_PROFILES=public" >> .env' in doc
    assert "COMPOSE_PROFILES=public,booking-pages" in doc
    assert "sudo bash deploy/bootstrap_ubuntu24.sh" in doc
    assert "git clone -c core.sshCommand=" in doc  # private repo: pulls keep working
    assert "docker compose up -d --build" in doc
    assert "RUNBOOK.md" in doc
    # The AWS server runs the older stack, which has no `scheduler` service:
    # naming one there would make `docker compose stop` fail and stop nothing.
    aws = next(line for line in doc.splitlines() if "56.228.9.106" in line)
    assert "docker compose stop" in aws and "scheduler" not in aws
    for label in ("New client", "Client logins", "☰ Menu"):
        assert label in (APP_DIR / "static" / "index.html").read_text(encoding="utf-8")
