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
import socket
import subprocess
import sys
import threading
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
    for name in (APP_SERVICES | {"wa-gateway"}) - {"migrate"}:
        deps = SERVICES[name]["depends_on"]
        assert deps["postgres"]["condition"] == "service_healthy", name
        assert deps["valkey"]["condition"] == "service_healthy", name
        assert deps["migrate"]["condition"] == "service_completed_successfully", name
    assert SERVICES["migrate"]["depends_on"] == {"postgres": {"condition": "service_healthy"}}
    for name in ("postgres", "valkey", "panel", "wa-gateway"):
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


def test_wa_gateway_gets_only_the_variables_it_reads():
    # The gateway parses hostile network input (Baileys); it must not hold the
    # admin password, TOTP secret, SMTP/vision/DeepSeek keys it never reads.
    # USERBOT_MASTER_KEY still arrives: compose interpolates it from .env.
    gw = SERVICES["wa-gateway"]
    assert "env_file" not in gw
    assert set(gw["environment"]) == {"DATABASE_URL", "REDIS_URL", "USERBOT_MASTER_KEY", "WA_GATEWAY_LOG_LEVEL"}


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


# --- wa-gateway ------------------------------------------------------------------

WA_DIR = APP_DIR / "wa_gateway"


class _FakeValkey(threading.Thread):
    """Just enough RESP to answer the gateway's healthcheck: PUBSUB NUMSUB
    gets `numsub` for any channel, everything else +OK."""

    def __init__(self, numsub: int):
        super().__init__(daemon=True)
        self.numsub = numsub
        self.commands: list[list[str]] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()
        self.sock.close()

    @staticmethod
    def _parse(buf: bytes):
        if not buf.startswith(b"*"):
            return None, buf
        head, sep, rest = buf.partition(b"\r\n")
        if not sep:
            return None, buf
        parts = []
        for _ in range(int(head[1:])):
            size, sep, rest = rest.partition(b"\r\n")
            if not sep or not size.startswith(b"$"):
                return None, buf
            n = int(size[1:])
            if len(rest) < n + 2:
                return None, buf
            parts.append(rest[:n].decode())
            rest = rest[n + 2:]
        return parts, rest

    def _serve(self, conn):
        buf = b""
        with conn:
            while True:
                try:
                    data = conn.recv(4096)
                except OSError:
                    return
                if not data:
                    return
                buf += data
                while True:
                    cmd, buf = self._parse(buf)
                    if cmd is None:
                        break
                    self.commands.append(cmd)
                    if [c.upper() for c in cmd[:2]] == ["PUBSUB", "NUMSUB"]:
                        channel = cmd[2].encode()
                        conn.sendall(b"*2\r\n$%d\r\n%s\r\n:%d\r\n" % (len(channel), channel, self.numsub))
                    else:
                        conn.sendall(b"+OK\r\n")


def _gateway_healthcheck() -> list[str]:
    check = SERVICES["wa-gateway"]["healthcheck"]
    assert check["test"][:3] == ["CMD", "node", "-e"] and len(check["test"]) == 4
    assert check["start_period"] and check["retries"] >= 2
    return check["test"][1:]


def test_wa_gateway_healthcheck_asks_valkey_for_the_gateways_own_subscription():
    """The gateway has no HTTP, so its healthcheck asks Valkey whether the
    command channel has a subscriber (the gateway is the only one). Run the
    exact command from the compose file against a fake Valkey."""
    node = _tool("NODE_BIN", "node")
    if not node or not (WA_DIR / "node_modules" / "ioredis").is_dir():
        pytest.skip("node or wa_gateway/node_modules not available")
    # Same channel the gateway serves: cmd:<GATEWAY_ADDRESS> (config.ts).
    config = (WA_DIR / "src" / "config.ts").read_text(encoding="utf-8")
    address = re.search(r"GATEWAY_ADDRESS = '([^']+)'", config).group(1)
    assert "commandChannel: `cmd:${GATEWAY_ADDRESS}`" in config
    script = _gateway_healthcheck()[-1]
    assert f"'cmd:{address}'" in script

    def run(server: _FakeValkey | None) -> subprocess.CompletedProcess:
        port = server.port if server else 1
        env = dict(os.environ, REDIS_URL=f"redis://127.0.0.1:{port}/0")
        return subprocess.run([node, "-e", script], cwd=WA_DIR, env=env, capture_output=True, text=True, timeout=30)

    for numsub, expected in ((1, 0), (0, 1)):
        server = _FakeValkey(numsub)
        server.start()
        try:
            result = run(server)
        finally:
            server.stop.set()
            server.join(timeout=2)
        assert result.returncode == expected, (numsub, result.stdout, result.stderr)
        assert ["PUBSUB", "NUMSUB", f"cmd:{address}"] in [[c.upper() for c in cmd[:2]] + cmd[2:] for cmd in server.commands]
    assert run(None).returncode == 1  # Valkey unreachable: unhealthy, and quickly


def test_wa_gateway_runs_unprivileged_and_keeps_nothing_on_disk():
    """The WhatsApp login state lives in Postgres (wa_auth_state), so the
    gateway needs no volume and no writable directory: the image drops to
    the `node` user and the sources never write a file (crypto.ts and the
    CLI tool only read the master-key file)."""
    dockerfile = (WA_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^USER node\s*$", dockerfile, re.M)
    assert dockerfile.index("USER node") < dockerfile.index("CMD [")
    assert "VOLUME" not in dockerfile
    assert "volumes" not in SERVICES["wa-gateway"]
    assert "ports" not in SERVICES["wa-gateway"]
    writers = re.compile(r"\b(?:writeFile|appendFile|createWriteStream|mkdir|mkdtemp|tmpdir|unlink|rename|rm)(?:Sync)?\(|\bfs\.")
    for path in sorted((WA_DIR / "src").glob("*.ts")):
        text = path.read_text(encoding="utf-8")
        assert not writers.search(text), f"{path.name} touches the filesystem"
        for imp in re.findall(r"import \{([^}]+)\} from '(?:node:)?fs(?:/promises)?'", text):
            assert {n.strip() for n in imp.split(",")} <= {"readFileSync"}, path.name
    # The data of record stays on Postgres's named volume across down/up/rebuild.
    assert "pgdata:/var/lib/postgresql/data" in SERVICES["postgres"]["volumes"]
    assert re.match(r"^postgres:16", SERVICES["postgres"]["image"])  # 17+ moves the data directory


# --- Environment variables ---------------------------------------------------------

# Read by the code but set by the compose file / the image / a test harness,
# never by the operator in .env.
INTERNAL_ENV = {
    "ADMIN_HOST": "Dockerfile ENV (0.0.0.0 inside the container)",
    "ADMIN_PORT": "fixed at 8787: compose publishes that port and Caddy proxies to it",
    "DATABASE_URL": "composed from POSTGRES_* by docker-compose.yml",
    "REDIS_URL": "the valkey service, fixed in docker-compose.yml",
    "DATA_DIR": "/app/data, fixed in docker-compose.yml",
    "PUBLIC_BIND": "booking-pages service, fixed in docker-compose.yml",
    "PUBLIC_PORT": "booking-pages service, fixed in docker-compose.yml",
    "PUBLIC_HOST": "booking-pages service, derived from BOOKING_DOMAIN",
    "SESSION_ID": "handed to a worker by manager.py",
    "USERBOT_MASTER_KEY_FILE": "an orchestrator secret file; this compose file requires USERBOT_MASTER_KEY",
    "PG_TEST_DSN": "tests only",
    "NODE_BIN": "tests only",
}


def _env_vars_read_by_the_code() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    python = re.compile(r"(?:os\.environ(?:\.get)?|os\.getenv|\benv(?:\.get)?)\s*[\(\[]\s*[\"']([A-Z][A-Z0-9_]+)[\"']")
    constants = re.compile(r"^_?ENV_[A-Z_]* = [\"']([A-Z][A-Z0-9_]+)[\"']", re.M)
    typescript = re.compile(r"\benv\.([A-Z][A-Z0-9_]+)\b")
    for path in [*APP_DIR.glob("*.py"), *(WA_DIR / "src").glob("*.ts")]:
        text = path.read_text(encoding="utf-8")
        names = set(python.findall(text)) | set(constants.findall(text)) | set(typescript.findall(text))
        for name in names:
            found.setdefault(name, set()).add(path.name)
    return found


def test_every_variable_the_code_reads_is_documented_for_the_operator():
    found = _env_vars_read_by_the_code()
    assert {"ADMIN_PASSWORD", "USERBOT_MASTER_KEY", "SMTP_HOST", "WA_GATEWAY_LOG_LEVEL", "LOG_LEVEL"} <= set(found)
    documented = {}
    for line in ENV_EXAMPLE.splitlines():
        match = re.match(r"^#?\s*([A-Z][A-Z0-9_]+)=", line)
        if match:
            documented[match.group(1)] = line
    undocumented = {name: files for name, files in found.items() if name not in documented and name not in INTERNAL_ENV}
    assert undocumented == {}, f"read by the code, not in .env.example: {undocumented}"
    assert set(INTERNAL_ENV) & set(documented) == set()
    # Everything .env.example lists is read by something (or by compose).
    compose_text = (APP_DIR / "docker-compose.yml").read_text(encoding="utf-8")
    compose_vars = set(re.findall(r"(?<!\$)\$\{([A-Z0-9_]+)", compose_text)) | {"COMPOSE_PROFILES"}
    stale = set(documented) - set(found) - compose_vars
    assert stale == set(), f"in .env.example but read by nothing: {stale}"
    # Each documented variable has an explanation right above it (or sits in
    # a commented group under one).
    lines = ENV_EXAMPLE.splitlines()
    for name, line in documented.items():
        assert lines[lines.index(line) - 1].startswith("#"), f"{name} has no comment above it"


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


LF_PATTERNS = ("*.sh", "Caddyfile*", "*.sql", "Dockerfile", ".dockerignore", "*.yml", "*.py", "*.ts", "*.json",
               ".env.example")


def test_files_used_on_linux_are_forced_to_lf():
    attributes = (REPO_DIR / ".gitattributes").read_text(encoding="utf-8")
    for pattern in LF_PATTERNS:
        assert re.search(rf"^{re.escape(pattern)}\s+text\s+eol=lf", attributes, re.M), pattern
    for path in [*(APP_DIR / "deploy").glob("*.sh"), *APP_DIR.glob("Caddyfile*")]:
        data = path.read_bytes()
        assert b"\r\n" not in data, f"{path.name} has CRLF line endings"
        if path.suffix == ".sh":
            assert data.startswith(b"#!/usr/bin/env bash\n")
            assert b"set -euo pipefail" in data


def test_git_checks_the_linux_files_out_with_lf():
    """What a clone on the server gets: git's own view (index content and
    the attribute it applies on checkout), not this checkout's bytes, which
    core.autocrlf may have rewritten on Windows."""
    git = shutil.which("git")
    if not git:
        pytest.skip("git not found")
    run = lambda *args, **kw: subprocess.run([git, *args], cwd=REPO_DIR, capture_output=True, text=True, check=True, **kw)
    if run("rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        pytest.skip("not a git checkout")
    linux = [
        "telegram_admin_bot/Dockerfile", "telegram_admin_bot/.dockerignore", "telegram_admin_bot/docker-compose.yml",
        "telegram_admin_bot/.env.example", "telegram_admin_bot/panel.py", "telegram_admin_bot/migrate_entrypoint.py",
        "telegram_admin_bot/migrations/0001_init.sql", "telegram_admin_bot/deploy/bootstrap_ubuntu24.sh",
        "telegram_admin_bot/Caddyfile.both", "telegram_admin_bot/wa_gateway/Dockerfile",
        "telegram_admin_bot/wa_gateway/.dockerignore", "telegram_admin_bot/wa_gateway/package.json",
        "telegram_admin_bot/wa_gateway/package-lock.json", "telegram_admin_bot/wa_gateway/tsconfig.json",
        "telegram_admin_bot/wa_gateway/src/main.ts",
    ]
    attrs = run("check-attr", "eol", "--", *linux).stdout
    for rel in linux:
        assert f"{rel}: eol: lf" in attrs, rel
    # And what is committed is LF already (a CRLF index would survive the
    # attribute until the file is next touched).
    eol = run("ls-files", "--eol", "--", *linux).stdout
    for line in eol.splitlines():
        assert line.startswith("i/lf"), line


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
    # The WhatsApp branch deploys its own guide: clones from it, and the
    # first-start checklist knows the gateway service exists.
    assert "-b platform/phase-1" not in doc
    assert doc.count("-b platform/whatsapp") == 2  # public and deploy-key clone
    start = doc[doc.index("## 5. Start"):doc.index("## 6.")]
    assert "`wa-gateway`" in start and "(healthy)" in start and "node:24-slim" in start
    assert "docker compose logs --tail 50 panel caddy migrate wa-gateway" in doc
    # The AWS server runs the older stack, which has no `scheduler` service:
    # naming one there would make `docker compose stop` fail and stop nothing.
    aws = next(line for line in doc.splitlines() if "56.228.9.106" in line)
    assert "docker compose stop" in aws and "scheduler" not in aws
    for label in ("New client", "Client logins", "☰ Menu"):
        assert label in (APP_DIR / "static" / "index.html").read_text(encoding="utf-8")
