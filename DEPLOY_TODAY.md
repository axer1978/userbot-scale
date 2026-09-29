# Deploying on a new Ubuntu 24.04 server

What you get is one HTTPS address. Without a domain of your own it's a free name made from the server's IP address: a server at `203.0.113.7` becomes `https://203-0-113-7.sslip.io`. There's nothing to register: [sslip.io](https://sslip.io) answers every name like that with the IP in it, and Let's Encrypt issues a normal certificate for it.

- **The admin panel** (you): works on a computer, a phone or a tablet. It needs your password and a code from an authenticator app.
- **The client dashboard** (your clients): the same address plus `/owner/`. Each client logs in to their own master account and sees only their businesses.

Below, `SERVER_IP` is the server's address and `PANEL_ADDRESS` is your panel's name (`203-0-113-7.sslip.io`, or later `panel.yourdomain.com`). Nothing here needs to be sent to me.

## 1. Open the ports at your provider

Many providers have a firewall of their own in their web console (AWS "security group", Hetzner "Firewalls", Oracle "security list"...). If yours does, allow in:

- TCP 22 (SSH)
- TCP 80 and TCP 443 (HTTPS, and Let's Encrypt checking the certificate)
- UDP 443 (optional, faster HTTPS)

The server's own firewall is set up in step 3.

**Own domain instead of sslip.io (optional).** Add an A record `panel` → `SERVER_IP` at your DNS provider (on Cloudflare: **DNS only**, grey cloud), and `book` → `SERVER_IP` too if you want the booking pages. `nslookup panel.yourdomain.com` should answer with `SERVER_IP` before step 5. You can also switch later: see "Moving to your own domain" at the end.

## 2. Log in to the server

```bash
ssh root@SERVER_IP
```

If the provider gave you only a password, first add your SSH key from your own computer:

```bash
ls ~/.ssh/id_ed25519.pub || ssh-keygen -t ed25519
ssh-copy-id -i ~/.ssh/id_ed25519.pub root@SERVER_IP
```

`ssh-copy-id` asks for the server's password once. The bootstrap in step 3 only switches off password login once a key is installed.

If your provider logs you in as `ubuntu` (or another user) instead of `root`, that's fine: the commands below work the same. After step 3, log out and back in once so you can run `docker` without `sudo`.

## 3. Get the code and prepare the server

```bash
sudo apt-get update && sudo apt-get install -y git
```

**If the GitHub repository is public:**

```bash
git clone -b platform/phase-1 https://github.com/axer1978/userbot-scale.git
```

**If it is private** (GitHub no longer accepts passwords for `git clone`), use a read-only deploy key:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/github_deploy -N ""
cat ~/.ssh/github_deploy.pub
```

In GitHub: the repository → **Settings → Deploy keys → Add deploy key**, paste that line, leave "Allow write access" off. Then:

```bash
git clone -c core.sshCommand="ssh -i ~/.ssh/github_deploy -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new" \
  -b platform/phase-1 git@github.com:axer1978/userbot-scale.git
```

The `-c` part is saved in the clone, so later `git pull`s use the same key.

Then, either way:

```bash
cd userbot-scale/telegram_admin_bot
sudo bash deploy/bootstrap_ubuntu24.sh
```

The bootstrap installs:

- security updates, now and automatic from then on;
- Docker;
- a firewall that only lets SSH, HTTP and HTTPS in;
- fail2ban;
- key-only SSH;
- swap on small servers;
- `age` for encrypted backups.

It's safe to run again. If it ends with "The updates want a reboot", run `sudo reboot`, wait a minute, log in again and `cd userbot-scale/telegram_admin_bot`.

## 4. Create `.env` (the secrets are generated on the server and never leave it)

Still in `userbot-scale/telegram_admin_bot`. Paste the whole block:

```bash
umask 077
if [ -e .env ]; then echo "STOP: .env already exists. Keep it, it holds your keys."; else
python3 -c "import secrets,base64;print('USERBOT_MASTER_KEY='+base64.b64encode(secrets.token_bytes(32)).decode());print('ADMIN_PASSWORD='+secrets.token_urlsafe(24));print('POSTGRES_PASSWORD='+secrets.token_urlsafe(24));print('ADMIN_TOTP_SECRET='+base64.b32encode(secrets.token_bytes(20)).decode())" > .env
IP=$(curl -4 -fsS https://api.ipify.org)
echo "PANEL_DOMAIN=${IP//./-}.sslip.io" >> .env
echo "COMPOSE_PROFILES=public" >> .env
fi
grep -E '^(PANEL_DOMAIN|ADMIN_PASSWORD|ADMIN_TOTP_SECRET)=' .env
```

The last line shows your panel's address, admin password and authenticator key:

- `PANEL_DOMAIN` must be `SERVER_IP` with dashes plus `.sslip.io`. If it isn't, fix it with `nano .env`. With your own domain, put `panel.yourdomain.com` there instead.
- In your authenticator app (Google Authenticator, Microsoft Authenticator, Authy, 1Password…), choose **Enter a setup key**. Name: `Userbot panel`. Key: the `ADMIN_TOTP_SECRET` value. Type: time-based.
- Store the admin password in your password manager.
- **Back up `.env` off the server now**, for example in your password manager as a secure note (`cat .env` shows it all). It holds the master key. Without it, the stored Telegram logins can't be read.

The panel refuses to start on a public address without the authenticator key and a password of at least 14 characters. The block above satisfies both (the password is 32 characters).

Optional, same file (`nano .env`):

- **Booking pages** (the public calendar feed and booking page links for customers):

  ```bash
  D=$(grep '^PANEL_DOMAIN=' .env | cut -d= -f2)
  echo "BOOKING_DOMAIN=book.$D" >> .env
  echo "PUBLIC_BASE_URL=https://book.$D" >> .env
  sed -i 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=public,booking-pages/' .env
  ```

  With sslip.io, `book.203-0-113-7.sslip.io` works at once. With your own domain it needs the `book` A record from step 1.
- **E-mail** (alerts, digests, booking records): `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, `ALERT_EMAIL`.
- **Photo checks:** `VISION_API_URL`, `VISION_API_KEY`.
- **Ask AI:** `DEEPSEEK_PLATFORM_KEY`.

`.env.example` in the same folder explains every setting.

## 5. Start

```bash
docker compose up -d --build
docker compose ps -a
docker compose logs caddy 2>&1 | grep -iE "certificate obtained|error" | tail -5
```

The first build takes a few minutes. What you should see:

- `postgres`, `valkey`, `panel`, `manager`, `scheduler` and `caddy` all show `Up`; `panel` shows `(healthy)` after about half a minute.
- `migrate` shows `Exited (0)`.
- Caddy logs `certificate obtained successfully` for your `PANEL_DOMAIN`. If it reports an error instead, ports 80/443 aren't reachable yet (step 1) or, with your own domain, DNS isn't pointing here yet. Fix that, then `docker compose restart caddy`.

## 6. First login (on your phone)

Open `https://PANEL_ADDRESS` (the `PANEL_DOMAIN` value) and enter the password and the 6-digit code. The top bar folds into **☰ Menu** on a phone.

## 7. Move the Telegram account over

If this is the same Telegram number that runs on the AWS server, **stop the AWS copy first**, or both servers will answer every customer. From your own computer:

```bash
ssh -i ~/Downloads/gateway-key.pem ubuntu@56.228.9.106 "cd ~/userbot-scale/telegram_admin_bot && docker compose stop && docker compose ps -a"
```

Every service there should now show `Exited`. Its data stays on that server; `docker compose start` there would bring it back.

Then, in the new panel, open **New client** (on a phone: **☰ Menu → New client**), the onboarding wizard:

1. Sign in the Telegram account (API ID and hash, phone, the code Telegram sends). If you use a proxy for this account, fill in the **Proxy** field here, so the sign-in already goes through it (see "Proxy for the Telegram accounts" below).
2. Business name and industry.
3. Key settings: timezone, the owner's Telegram, bookings, auto-send, quiet hours.
4. Staging: enter your own test chat. Only it gets answers. Send a few test messages.
5. Check the prompt.
6. **Go live.**

Once the new server answers, you can end the old server's login in Telegram (**Settings → Devices**) and stop the AWS instance in the AWS console.

### Proxy for the Telegram accounts (optional)

Without a proxy, every account connects to Telegram from this server's datacenter address. That works, but Telegram sees a Latvian number, say, logging in from a German datacenter. A **residential or mobile proxy in the account's own country** makes the account connect from an ordinary address there instead.

- **What to buy:** a SOCKS5 proxy (HTTP also works), residential or mobile, **static or "sticky"** (the same address every time, not one that rotates per connection), in the country of the phone number. One per account. The provider gives you host, port, username and password.
- **Put it together as one line:** `socks5://USERNAME:PASSWORD@HOST:PORT`. If the password contains `@`, `:` or `/`, write them as `%40`, `%3A`, `%2F`.
- **New account:** paste it into the **Proxy** field when signing in (wizard step 1, or **+ Add account**). The sign-in and everything after it go through the proxy.
- **Existing account:** **Safety → This client → Telegram proxy**, paste it, **Use this proxy**. The server first checks that it can reach the proxy (a typo is refused), then the account reconnects through it within seconds. **Connect directly** removes it.

The password is stored encrypted like the Telegram login and is never shown again; the panel shows only host, port and user.

If the account shows **not connected** after adding a proxy, the proxy refused it (wrong user or password, or the proxy doesn't allow Telegram's ports). Check with the provider, or click **Connect directly** to go back.

### The web side is already behind a proxy

The panel is never reached directly: Caddy sits in front of it (HTTPS, certificates, and it hides the app). You can later put **Cloudflare** in front of Caddy to hide the server's address, but only with your own domain (Cloudflare can't proxy an sslip.io name), and it needs the real-visitor-address setup in Caddy first so login limits keep working. Not needed for now; ask me when you get there.

## 8. Give the client their login

Open **Client logins → New** (on a phone: **☰ Menu → Client logins**):

- set a username and a temporary password (at least 10 characters);
- tick their business, or several if they have more than one bot.

Send them `https://PANEL_ADDRESS/owner/` and the temporary password. They must change it on first login, and can turn on a 2-step code under Settings.

## 9. Encrypted nightly backup (optional, recommended)

On your own computer, install [age](https://github.com/FiloSottile/age/releases) and run `age-keygen -o my-backup-key.txt`. Keep that file private: it's the only thing that can open the backups. On the server, in `userbot-scale/telegram_admin_bot`, paste the **public** key it printed:

```bash
echo 'age1...your public key...' > deploy/age-recipient.txt
bash deploy/backup.sh
(crontab -l 2>/dev/null; echo "30 3 * * * cd $PWD && bash deploy/backup.sh >> backups/backup.log 2>&1") | crontab -
```

## 10. Checks

```bash
D=$(grep '^PANEL_DOMAIN=' .env | cut -d= -f2)
curl -sI "http://$D" | head -3                                   # 308 redirect to https
curl -sI "https://$D" | grep -iE "strict-transport|content-security"
sudo ss -tlnp | grep -vE '127\.0\.0\.|\[::1\]'                   # only 22, 80, 443
sudo ufw status
```

## If something goes wrong

See [RUNBOOK.md](RUNBOOK.md) for what to do when something breaks. The first two things to look at, and to send me if you need help (they contain no secrets):

```bash
docker compose ps -a
docker compose logs --tail 50 panel caddy migrate
```

## Updating later

```bash
cd ~/userbot-scale/telegram_admin_bot && git pull && docker compose up -d --build
```

## Moving to your own domain

Add the A record from step 1 and wait until `nslookup panel.yourdomain.com` answers with `SERVER_IP`. Then change `PANEL_DOMAIN` in `.env` (and `BOOKING_DOMAIN` / `PUBLIC_BASE_URL` if you use the booking pages) and run `docker compose up -d`. Caddy gets the new certificate by itself. Tell your clients the new `/owner/` address.

## What protects what

| Layer | What it does |
|---|---|
| HTTPS (Caddy, Let's Encrypt) | TLS 1.2/1.3 only, certificates renewed automatically, HTTP redirected to HTTPS, HSTS for 2 years |
| Browser headers | A strict Content-Security-Policy (no inline scripts, live connection to this host only), no framing, no referrer, no caching of API answers. Requests from another website are refused, which matters on a shared name like sslip.io |
| Admin login | Password of 14+ characters plus an authenticator code (each code works once); failed logins limited per address |
| Client logins | Passwords hashed with scrypt, optional authenticator code, sessions stored only as hashes, cookies Secure + HttpOnly + SameSite=Strict with the `__Host-` prefix (no other site can plant one), each client limited to their own businesses |
| Stored secrets | Telegram logins, API keys and authenticator keys AES-GCM encrypted with `USERBOT_MASTER_KEY` |
| Network | Only 22/80/443 open (ufw). Postgres and Valkey are reachable only inside Docker. The panel only through Caddy. The public booking pages get none of the secrets in `.env` |
| Server | Key-only SSH, fail2ban, automatic security updates |
| Backups | Encrypted to your own key; the server can't read them |
