#!/usr/bin/env bash
# One-time preparation of a fresh Ubuntu 24.04 server for the platform.
# Run as root (or with sudo) from the repository's telegram_admin_bot folder:
#
#     sudo bash deploy/bootstrap_ubuntu24.sh
#
# What it does, and nothing else:
#   - security updates now, and automatically from now on
#   - Docker Engine + the compose plugin, from Docker's own apt repository
#   - a firewall (ufw): only SSH, HTTP and HTTPS reach the server
#   - fail2ban for SSH
#   - SSH: key login only (password login off), but ONLY if a key is already
#     installed for root or for $SUDO_USER, so it can't lock you out
#   - a 2 GB swap file on small servers (the image build needs the memory)
#   - `age`, for the encrypted backups (deploy/backup.sh)
#
# About the firewall and Docker: ports that Docker publishes bypass ufw.
# docker-compose.yml publishes only Caddy's 80/443 (meant to be public) and
# the panel's 8787 on 127.0.0.1 (not reachable from outside); Postgres and
# Valkey are not published at all. So ufw's rules and what is reachable
# agree. If you ever add a `ports:` entry, it is public whatever ufw says.
#
# It is safe to run again.

set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run as root: sudo bash deploy/bootstrap_ubuntu24.sh" >&2
  exit 1
fi
# shellcheck source=/dev/null
. /etc/os-release
if [[ "${VERSION_ID:-}" != "24.04" ]]; then
  echo "Warning: written for Ubuntu 24.04, this is ${PRETTY_NAME:-unknown}. Continuing." >&2
fi

export DEBIAN_FRONTEND=noninteractive
# Restart services after library updates without asking (needrestart would
# otherwise stop and wait for an answer).
export NEEDRESTART_MODE=a
# Keep a provider's edited config files (e.g. sshd_config) instead of
# stopping to ask which version to keep.
APT_OPTS=(-y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)

echo "==> Updates"
apt-get update -q
apt-get upgrade "${APT_OPTS[@]}"
apt-get install "${APT_OPTS[@]}" ca-certificates curl gnupg git ufw fail2ban python3-systemd unattended-upgrades
# `age` is in Ubuntu's "universe" section; a missing universe must not stop
# the rest of the setup, only the backups need it.
if ! apt-get install "${APT_OPTS[@]}" age; then
  echo "    WARNING: could not install age; deploy/backup.sh needs it (enable 'universe', then apt-get install age)." >&2
fi

echo "==> Automatic security updates"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
systemctl enable --now unattended-upgrades >/dev/null 2>&1 || true

echo "==> Docker"
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  arch=$(dpkg --print-architecture)
  echo "deb [arch=${arch} signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME:?} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install "${APT_OPTS[@]}" docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
# Container logs: capped, so a chatty container can't fill the disk.
if [[ ! -f /etc/docker/daemon.json ]]; then
  mkdir -p /etc/docker
  cat > /etc/docker/daemon.json <<'EOF'
{ "log-driver": "json-file", "log-opts": { "max-size": "10m", "max-file": "3" } }
EOF
  if systemctl is-active --quiet docker; then systemctl restart docker; fi
fi
systemctl enable --now docker
docker compose version
if [[ -n "${SUDO_USER:-}" ]] && [[ "${SUDO_USER}" != "root" ]]; then
  usermod -aG docker "${SUDO_USER}"
  echo "    ${SUDO_USER} can run docker after logging out and back in."
fi

echo "==> Firewall: SSH, HTTP, HTTPS only"
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 443/udp
ufw --force enable

echo "==> fail2ban (SSH)"
# Read SSH logins from the journal: works whether or not rsyslog (and so
# /var/log/auth.log) is installed.
cat > /etc/fail2ban/jail.d/10-sshd-journal.local <<'EOF'
[sshd]
enabled = true
backend = systemd
EOF
systemctl enable fail2ban >/dev/null 2>&1 || true
if ! systemctl restart fail2ban; then
  echo "    WARNING: fail2ban did not start; see: journalctl -u fail2ban -n 30" >&2
fi

echo "==> SSH: key login only"
has_key=0
for home in /root "/home/${SUDO_USER:-nobody}"; do
  if [[ -s "${home}/.ssh/authorized_keys" ]]; then has_key=1; fi
done
hardening=/etc/ssh/sshd_config.d/10-hardening.conf
if [[ "${has_key}" -eq 1 ]]; then
  # 10- sorts before a provider's 50-cloud-init.conf, and for sshd the first
  # value it reads wins, so these can't be overridden by that file.
  cat > "${hardening}" <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
  mkdir -p /run/sshd   # sshd -t needs it; with socket activation it may not exist yet
  if sshd -t; then
    systemctl reload ssh 2>/dev/null || systemctl restart ssh 2>/dev/null || true
    echo "    Password login is off. Keep your SSH key safe."
  else
    rm -f "${hardening}"
    echo "    WARNING: sshd rejected the new settings; left SSH as it was." >&2
  fi
else
  echo "    SKIPPED: no SSH key is installed yet, so password login stays on." >&2
  echo "    Add your public key to ~/.ssh/authorized_keys and run this script again." >&2
fi

echo "==> Swap"
mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
active_swap=$(swapon --show --noheadings || true)
if [[ "${mem_mb}" -lt 3000 ]] && [[ -z "${active_swap}" ]]; then
  if [[ ! -f /swapfile ]]; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
  fi
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "    2 GB swap added (${mem_mb} MB RAM)."
fi

echo
if [[ -f /var/run/reboot-required ]]; then
  echo "The updates want a reboot. Do it now (sudo reboot), log back in, then continue."
fi
echo "Done. Next: create .env (DEPLOY_TODAY.md, step 4), then: docker compose up -d --build"
