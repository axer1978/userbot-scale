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
# Docker publishes Caddy's 80/443 itself; the panel's 8787 is published on
# 127.0.0.1 only, and Postgres/Valkey are not published at all.
# It is safe to run again.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo bash deploy/bootstrap_ubuntu24.sh" >&2
  exit 1
fi
. /etc/os-release
if [ "${VERSION_ID:-}" != "24.04" ]; then
  echo "Warning: written for Ubuntu 24.04, this is ${PRETTY_NAME:-unknown}. Continuing." >&2
fi

export DEBIAN_FRONTEND=noninteractive
echo "==> Updates"
apt-get update -q
apt-get upgrade -y -q
apt-get install -y -q ca-certificates curl gnupg git ufw fail2ban unattended-upgrades age

echo "==> Automatic security updates"
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

echo "==> Docker"
if ! command -v docker >/dev/null 2>&1; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -q
  apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker
if [ -n "${SUDO_USER:-}" ] && [ "${SUDO_USER}" != "root" ]; then
  usermod -aG docker "$SUDO_USER"
  echo "    $SUDO_USER can run docker after logging out and back in."
fi
# Container logs: capped, so a chatty container can't fill the disk.
if [ ! -f /etc/docker/daemon.json ]; then
  cat > /etc/docker/daemon.json <<'EOF'
{ "log-driver": "json-file", "log-opts": { "max-size": "10m", "max-file": "3" } }
EOF
  systemctl restart docker
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
systemctl enable --now fail2ban

echo "==> SSH: key login only"
has_key=0
for home in /root "/home/${SUDO_USER:-nobody}"; do
  if [ -s "$home/.ssh/authorized_keys" ]; then has_key=1; fi
done
if [ "$has_key" -eq 1 ]; then
  cat > /etc/ssh/sshd_config.d/10-hardening.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
  systemctl reload ssh || systemctl reload sshd || true
  echo "    Password login is off. Keep your SSH key safe."
else
  echo "    SKIPPED: no SSH key is installed yet, so password login stays on." >&2
  echo "    Add your public key to ~/.ssh/authorized_keys and run this script again." >&2
fi

echo "==> Swap"
mem_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if [ "$mem_mb" -lt 3000 ] && ! swapon --show | grep -q .; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "    2 GB swap added (${mem_mb} MB RAM)."
fi

echo
echo "Done. Next: create .env (DEPLOY_TODAY.md, step 4), then: docker compose up -d --build"
