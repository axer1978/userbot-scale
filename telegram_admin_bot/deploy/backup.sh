#!/usr/bin/env bash
# Encrypted backup: the database, the data folder (media) and .env, in one
# file encrypted with `age` to YOUR public key. The server can write backups
# but cannot read them: only the private key, which stays on your own
# computer, decrypts them.
#
# One-time setup, on your own computer:  age-keygen -o my-backup-key.txt
#   (it prints "Public key: age1..."). Put that public key on the server:
#       echo 'age1...' > deploy/age-recipient.txt
# Run by hand or nightly from cron (as a user that can run docker):
#       bash deploy/backup.sh
#       crontab -e   ->   30 3 * * * cd ~/userbot-scale/telegram_admin_bot && bash deploy/backup.sh >> backups/backup.log 2>&1
# Restore: age -d -i my-backup-key.txt userbot-<date>.tar.age | tar x
# Keeps the last 14 backups. Copy them off the server too.

set -euo pipefail
cd "$(dirname "$0")/.."

recipient_file=deploy/age-recipient.txt
if [ ! -s "$recipient_file" ]; then
  echo "No $recipient_file: put your age public key there first (see the top of this file)." >&2
  exit 1
fi
mkdir -p backups
stamp=$(date +%F-%H%M)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

docker compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' | gzip > "$work/database.sql.gz"
tar -cf "$work/files.tar" data .env 2>/dev/null || tar -cf "$work/files.tar" .env
tar -C "$work" -cf - database.sql.gz files.tar | age -R "$recipient_file" -o "backups/userbot-$stamp.tar.age"
chmod 600 "backups/userbot-$stamp.tar.age"
ls -1t backups/userbot-*.tar.age | tail -n +15 | xargs -r rm -f
echo "$(date -Is) backup written: backups/userbot-$stamp.tar.age"
