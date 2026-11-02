# Deploying LiqGuard on a VPS (Docker + Caddy)

One container runs the Telegram bot (long polling) **and** the dashboard in a single process.
All state (SQLite database, master key, audit log) lives on the named volume `liqguard-data`
mounted at `/data`. Caddy terminates https in front of it with an automatic Let's Encrypt
certificate.

```
Telegram ⇄ liqguard (polls getUpdates)          browser ── https ──▶ Caddy :443 ──▶ liqguard :8765
                └── /data (volume: liqguard.db, encryption.key, audit.log)
```

## 1. Server

Any small VPS works (1 vCPU / 1 GB, e.g. a DigitalOcean Droplet with Ubuntu 24.04).

1. Install Docker Engine with the compose plugin: <https://docs.docker.com/engine/install/ubuntu/>.
2. Point a DNS **A record** (e.g. `liqguard.example.com`) at the server's IP.
3. Allow only SSH, HTTP and HTTPS in, e.g. with a DigitalOcean Cloud Firewall or
   `ufw allow OpenSSH && ufw allow 80,443/tcp && ufw enable`. Port 8765 is published on
   127.0.0.1 only, so it is never reachable from outside.

## 2. Configure

```bash
git clone <your repo url> liqguard && cd liqguard
cp .env.example .env && chmod 600 .env
```

Set at least these keys in `.env`:

```bash
TELEGRAM_BOT_TOKEN=...                           # from @BotFather
LIQGUARD_DOMAIN=liqguard.example.com             # the DNS name from step 1
LIQGUARD_PUBLIC_URL=https://liqguard.example.com # used in /dashboard login links; https => Secure cookies
META_API_KEY=                                    # optional; Muse runs in mock mode without it
```

With `META_API_KEY` set, Muse uses `muse-spark-1.3` by default. If the logs say `model_not_found`,
set `META_MODEL` to a model `GET https://api.meta.ai/v1/models` lists. `META_API_BASE`,
`META_MAX_TOKENS` (2000), `META_TIMEOUT` (15 s) and `META_REASONING_EFFORT` (`low`) are optional;
see the README's environment table. Startup logs one `Muse: live API · model … at …` line (never the key).

Never commit `.env`. `docker-compose.yml` pins the data paths, bind address and port, so the
`LIQGUARD_DB_PATH` / `LIQGUARD_DASHBOARD_HOST` lines from `.env.example` cannot move state out of
the volume.

## 3. Start

```bash
docker compose --profile https up -d --build
docker compose logs -f liqguard        # first boot: "No master key configured: generated a new one and saved it to /data/encryption.key"
                                       # then "Dashboard: http://0.0.0.0:8765 ..." and "Start polling"
curl https://liqguard.example.com/healthz   # {"status":"ok"}
```

On first boot LiqGuard creates `/data/encryption.key` (mode 600) and reuses it on every
restart. Then open the bot in Telegram, send `/start`, and `/dashboard` for a login link.

If you would rather inject the master key than generate it, set `LIQGUARD_MASTER_KEY` in `.env`.
Generate one with `docker compose run --rm liqguard python -c "from liqguard.storage import generate_key; print(generate_key())"`.
With that set, no key file is written.

### Telegram Login Widget (optional)

`/login` always works with the `/dashboard` one-time link. It can also show Telegram's
"Log in with Telegram" button, which only works on the domain registered for your bot:

1. In Telegram, open [@BotFather](https://t.me/BotFather), send `/setdomain`, pick your bot and
   send the bare domain, for example `liqguard.example.com` (no scheme, no path).
2. Make sure `.env` has `LIQGUARD_PUBLIC_URL=https://liqguard.example.com` (the same domain), then
   `docker compose up -d`. The bot username is detected from the token at startup;
   `LIQGUARD_BOT_USERNAME` (without `@`) overrides it.

LiqGuard renders the widget and its "or" divider only when `LIQGUARD_PUBLIC_URL` is `https://`
and its host is not `localhost`, `127.0.0.1`, `0.0.0.0`, `::1` or `*.localhost`. Telegram rejects
those with "Bot domain invalid", so local development shows the one-time link steps only.

### Caddy on the host instead of in compose

If Caddy (or nginx) already runs on the server, start only the app with
`docker compose up -d --build` and proxy to the loopback port:

```
liqguard.example.com {
	encode zstd gzip
	reverse_proxy 127.0.0.1:8765
}
```

WebSockets (`/ws/demo`, `/ws/me`) pass through `reverse_proxy` without extra config.

## 4. One poller per bot token (important)

Telegram hands each update to exactly **one** `getUpdates` caller. If two processes poll the
same token, each gets only some of the messages and button presses, and the rest disappear
without an error on your side. Telegram may also log
`409 Conflict: terminated by other getUpdates request`. So:

- run exactly one `liqguard` container: never `--scale liqguard=2`, and never a second checkout
  with the same `.env`;
- don't run `liqguard` on your laptop with the production token. Create a separate bot in
  BotFather for development;
- `docker compose up -d` recreates the container by stopping the old one first, and the bot
  stops polling on SIGTERM within about a second, so updates never overlap two pollers.

If a user reports that a button did nothing, check the logs for
`Callback received: ... action=...`. If that line is missing, the update went to another
poller.

## 5. Operations

| Task | Command |
|------|---------|
| Logs | `docker compose logs -f liqguard` |
| Health | `docker compose ps` (the image `HEALTHCHECK` calls `/healthz`) |
| Update | `git pull && docker compose --profile https up -d --build` |
| Restart | `docker compose restart liqguard` |
| Stop | `docker compose --profile https down` (the volume and its data are kept) |

**Backups.** Back up the database and the master key, and store them **separately**. Without
the key, the stored exchange credentials can't be decrypted; with both, anyone can decrypt them.

```bash
docker compose exec liqguard python -c "import sqlite3; s=sqlite3.connect('/data/liqguard.db'); d=sqlite3.connect('/data/backup.db'); s.backup(d); d.close()"
docker compose cp liqguard:/data/backup.db ./liqguard-$(date +%F).db
docker compose cp liqguard:/data/encryption.key ./encryption.key   # once; keep it offline
docker compose exec liqguard rm /data/backup.db
```

**Never** run `docker compose down -v` on the server: `-v` deletes the volume, including the
master key.

## Environment reference

All settings are environment variables; see `.env.example` and the README's
*Environment Variables* table. These are the deployment-related ones:

| Variable | In the container |
|----------|------------------|
| `LIQGUARD_DATA_DIR` | `/data` (DB, key and audit log default into it) |
| `LIQGUARD_DB_PATH`, `LIQGUARD_KEY_FILE`, `LIQGUARD_AUDIT_LOG` | empty, so they derive from the data dir; set them only outside Docker |
| `LIQGUARD_DASHBOARD` / `LIQGUARD_DASHBOARD_HOST` / `PORT` | `true` / `0.0.0.0` / `8765` |
| `LIQGUARD_PUBLIC_URL` | `https://<your domain>` (also the domain for BotFather `/setdomain`) |
| `LIQGUARD_BOT_USERNAME` | optional override (without `@`); detected from the token at startup, shown on `/login` and used by the Login Widget |
| `TELEGRAM_BOT_TOKEN`, `META_API_KEY`, `LIQGUARD_MASTER_KEY` | secrets: only in `.env`, never in the image or the repo |
