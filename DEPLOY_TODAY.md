# Deploying on a new Ubuntu 24.04 server

What you get is one HTTPS address, for example `https://panel.yourdomain.com`:

- **The admin panel** (you): works on a computer, a phone or a tablet. It needs your password and a code from an authenticator app.
- **The client dashboard** (your clients): at `https://panel.yourdomain.com/owner/`. Each client logs in to their own master account and sees only their businesses.

Replace `yourdomain.com` everywhere below with your domain, and `SERVER_IP` with the server's address. Nothing here needs to be sent to me.

## 1. The domain (do this first: DNS takes minutes to hours)

At your registrar or DNS provider:

| Type | Name | Value |
|---|---|---|
| A | `panel` | `SERVER_IP` |
| A | `book` (optional: public booking pages) | `SERVER_IP` |

On Cloudflare, set the record to **DNS only** (grey cloud). Check it from any computer:

```bash
nslookup panel.yourdomain.com
```

When it answers with `SERVER_IP`, you're ready for step 5.

## 2. Log in to the server

```bash
ssh root@SERVER_IP
```

If the provider gave you only a password, first add your SSH key from your own computer. On Windows PowerShell:

```powershell
type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh root@SERVER_IP "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys"
```

If `type` says the file is missing, run `ssh-keygen -t ed25519` first. The bootstrap in step 3 only switches off password login once a key is installed.

## 3. Get the code and prepare the server

```bash
apt-get update && apt-get install -y git
git clone -b platform/phase-1 https://github.com/axer1978/userbot-scale.git
cd userbot-scale/telegram_admin_bot
sudo bash deploy/bootstrap_ubuntu24.sh
```

If the GitHub repository is private, `git clone` asks for a login. Use a read-only deploy key instead:

- `ssh-keygen -t ed25519 -f ~/.ssh/github_deploy -N ""`
- Add the contents of `~/.ssh/github_deploy.pub` in GitHub: repo → Settings → Deploy keys (read access only).
- Clone with `GIT_SSH_COMMAND="ssh -i ~/.ssh/github_deploy" git clone -b platform/phase-1 git@github.com:axer1978/userbot-scale.git`.

The bootstrap installs:

- security updates, now and automatic from then on;
- Docker;
- a firewall that only lets SSH, HTTP and HTTPS in;
- fail2ban;
- key-only SSH;
- swap on small servers;
- `age` for encrypted backups.

## 4. Create `.env` (the secrets are generated on the server and never leave it)

Still in `userbot-scale/telegram_admin_bot`:

```bash
umask 077
python3 -c "import secrets,base64;print('USERBOT_MASTER_KEY='+base64.b64encode(secrets.token_bytes(32)).decode());print('ADMIN_PASSWORD='+secrets.token_urlsafe(24));print('POSTGRES_PASSWORD='+secrets.token_urlsafe(24));print('ADMIN_TOTP_SECRET='+base64.b32encode(secrets.token_bytes(20)).decode())" > .env
echo "PANEL_DOMAIN=panel.yourdomain.com" >> .env
echo "COMPOSE_PROFILES=public" >> .env
```

Then read your admin password and your authenticator key. You'll need both to log in:

```bash
grep -E '^(ADMIN_PASSWORD|ADMIN_TOTP_SECRET)=' .env
```

- In your authenticator app (Google Authenticator, Microsoft Authenticator, Authy, 1Password…), choose **Enter a setup key**. Name: `Userbot panel`. Key: the `ADMIN_TOTP_SECRET` value. Type: time-based.
- Store the admin password in your password manager.
- **Back up `.env` off the server now**, for example in your password manager as a secure note. It holds the master key. Without it, the stored Telegram logins can't be read.
- Don't run the `python3 -c` line again later: it would replace the keys.

The panel refuses to start on a public domain without the authenticator key and a password of at least 14 characters. The line above satisfies both.

Optional, same file (`nano .env`):

- **Booking pages:** `BOOKING_DOMAIN=book.yourdomain.com` and `PUBLIC_BASE_URL=https://book.yourdomain.com`, then change the profiles line to `COMPOSE_PROFILES=public,booking-pages`.
- **E-mail** (alerts, digests, booking records): `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `ALERT_EMAIL`.
- **Photo checks:** `VISION_API_URL`, `VISION_API_KEY`.
- **Ask AI:** `DEEPSEEK_PLATFORM_KEY`.

## 5. Start

```bash
docker compose up -d --build
docker compose ps -a
docker compose logs caddy | grep -iE "certificate obtained|error" | tail -5
```

What you should see:

- `postgres`, `valkey`, `panel`, `manager`, `scheduler` and `caddy` all show `Up`.
- `migrate` shows `Exited (0)`.
- Caddy logs `certificate obtained successfully` for `panel.yourdomain.com`. If it reports an error, DNS isn't pointing here yet. Wait, then `docker compose restart caddy`.

The first build takes a few minutes.

## 6. First login (on your phone)

Open `https://panel.yourdomain.com` and enter the password and the 6-digit code. The top bar folds into **☰ Menu** on a phone.

## 7. Move the Telegram account over

If this is the same Telegram number that runs on the AWS server, **stop it there first**, or both servers will answer every customer:

```bash
ssh -i ~/Downloads/gateway-key.pem ubuntu@56.228.9.106 "cd ~/userbot-scale/telegram_admin_bot && docker compose stop manager scheduler"
```

Then, in the new panel, go to **☰ Menu → New client** (the onboarding wizard):

1. Sign in the Telegram account (API ID and hash, phone, the code Telegram sends).
2. Business name and industry.
3. Key settings: timezone, the owner's Telegram, bookings, auto-send, quiet hours.
4. Staging: enter your own test chat. Only it gets answers. Send a few test messages.
5. Check the prompt.
6. **Go live.**

## 8. Give the client their login

Go to **☰ Menu → Client logins → New**:

- set a username and a temporary password (at least 10 characters);
- tick their business, or several if they have more than one bot.

Send them `https://panel.yourdomain.com/owner/` and the temporary password. They must change it on first login, and can turn on a 2-step code under Settings.

## 9. Encrypted nightly backup (optional, recommended)

On your own computer, install [age](https://github.com/FiloSottile/age/releases) and run `age-keygen -o my-backup-key.txt`. Keep that file private: it's the only thing that can open the backups. On the server, paste the **public** key it printed:

```bash
echo 'age1...your public key...' > deploy/age-recipient.txt
bash deploy/backup.sh
(crontab -l 2>/dev/null; echo "30 3 * * * cd $PWD && bash deploy/backup.sh >> backups/backup.log 2>&1") | crontab -
```

## 10. Checks

```bash
curl -sI http://panel.yourdomain.com | head -3          # 308 redirect to https
curl -sI https://panel.yourdomain.com | grep -iE "strict-transport|content-security"
sudo ss -tlnp | grep -vE "127.0.0.1|::1"                 # only 22, 80, 443
sudo ufw status
```

If there is a problem, send me the output of `docker compose ps -a` and `docker compose logs --tail 50 panel caddy`. They contain no secrets.

## Updating later

```bash
cd ~/userbot-scale/telegram_admin_bot && git pull && docker compose up -d --build
```

## What protects what

| Layer | What it does |
|---|---|
| HTTPS (Caddy, Let's Encrypt) | TLS 1.2/1.3 only, certificates renewed automatically, HTTP redirected to HTTPS, HSTS for 2 years |
| Browser headers | A strict Content-Security-Policy (no inline scripts), no framing, no referrer, no caching of API answers |
| Admin login | Password of 14+ characters plus an authenticator code (each code works once); failed logins limited per address |
| Client logins | Passwords hashed with scrypt, optional authenticator code, sessions stored only as hashes, cookies Secure + HttpOnly + SameSite=Strict, each client limited to their own businesses |
| Stored secrets | Telegram logins, API keys and authenticator keys AES-GCM encrypted with `USERBOT_MASTER_KEY` |
| Network | Only 22/80/443 open (ufw). Postgres and Valkey are reachable only inside Docker. The panel only through Caddy |
| Server | Key-only SSH, fail2ban, automatic security updates |
| Backups | Encrypted to your own key; the server can't read them |
