# LiqGuard 🛡️

**Telegram bot that warns you before your perpetual futures positions get liquidated.**

Built for the [Meta Global AI Developer Hackathon](https://cerebralvalley.ai/e/meta-ai-developer-hackathon) (2026).

## Features

- 🔔 **Real-time Alerts**: Get notified before positions hit liquidation
- 📊 **Portfolio Risk Overview**: Comprehensive risk metrics at a glance
- 📈 **Multi-Exchange Support**: Binance USDⓈ-M Futures and Hyperliquid
- 🔒 **Security First**: Read-only API keys, encrypted storage, never trades on mainnet
- 🤖 **AI-Powered**: Risk explanations via Meta's Muse Spark (with mock fallback)
- 📸 **Screenshot Parsing**: Send a position screenshot, get it analyzed
- 🎮 **Demo Mode**: Simulated price paths with alert sequences
- 💰 **Actionable Advice**: Exact margin/reduction amounts, not estimates
- 🛡️ **Protective Actions**: User-confirmed risk reduction (testnet only)
- 🖥️ **Web Dashboard**: Public demo page plus a private per-user view behind Telegram login
- 👥 **Multi-user**: Every position, key, alert, action, audit entry and simulation is scoped to its owner

## Quick Start

### Prerequisites

- Python 3.11+
- A Telegram bot token (get from [@BotFather](https://t.me/BotFather))
- (Optional) Meta API key for live AI features

### Installation

```bash
# Clone the repository
git clone https://github.com/yourusername/liqguard.git
cd liqguard

# Install dependencies
pip install -e ".[dev]"

# Create .env file
cp .env.example .env
# Edit .env with your tokens
```

### Running the Bot

```bash
# Run directly
python -m liqguard.main

# Or use the console script
liqguard
```

### Using it as a user (end to end)

1. Open the bot in Telegram and send **/start**.
2. Send **/bind** and pick one:
   - **Binance**: create an API key with only *Enable Reading* ticked (no trading, no
     withdrawals; IP restriction recommended) and send the key, then the secret. Both
     messages are deleted right away. LiqGuard asks Binance what this key may do and
     rejects it if it can trade, withdraw or transfer.
   - **Hyperliquid**: send your wallet address (`0x…`). No key is needed.
3. **/positions** and **/risk** show your live positions. The monitor polls every 30 s and
   alerts you (and only you) as positions approach liquidation.
4. Send **/dashboard** in the private chat and open the one-time link to see your own
   dashboard: masked connections, live positions, audit log and a private simulation.
5. **/unbind** removes a connection; **/dashboard revoke** signs out every browser.

`test_e2e.py` runs exactly this flow for two users with only the exchange APIs faked.

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message and quick start |
| `/help` | Show all available commands |
| `/bind` | Connect an exchange: Binance (read-only key), Hyperliquid (wallet address) or Binance testnet (for protective actions) |
| `/unbind` | Disconnect an exchange |
| `/dashboard` | One-time login link to your private web dashboard (`/dashboard revoke` signs out everywhere) |
| `/positions` | View all positions with actionable advice |
| `/risk` | Portfolio risk overview across venues + one-line Muse summary naming the riskiest position |
| `/threshold` | Configure alert thresholds |
| `/demo` | Demo mode with price simulation |
| `/ask <question>` | Ask AI about your positions |
| `/actions` | View pending protective actions |
| `/killswitch` | Emergency stop for all actions |
| `/language` | Change language (English/繁體中文) |
| `/status` | View account status |

## How We Use Meta AI

LiqGuard leverages Meta's Muse Spark model with a unique architecture:

### Deterministic Numbers + AI Explanations

```
Position Data → Deterministic Calculations → Exact Numbers
                         ↓
                    Muse Spark → Natural Language Explanation
```

**Key principle**: The LLM never calculates numbers. All liquidation distances, margin requirements, and risk scores are computed deterministically. The LLM only explains these pre-calculated values.

### Tool Calling

The LLM has access to tools that return pre-computed values:

```python
tools = [
    "get_position_risk_metrics",   # Returns distance, risk score
    "get_margin_advice",            # Returns exact $ to add
    "get_reduction_advice",         # Returns exact % to reduce
]
```

### Structured Output

Risk assessments follow a strict schema:

```json
{
    "severity": "critical",
    "summary": "BTC position is 5.2% from liquidation",
    "explanation": "Add $1,234.56 margin to reach 30% safety",
    "recommended_action": {"action_type": "add_margin", "reason": "..."},
    "urgency": "high"
}
```

### Mock Mode for Demo/Testing

When `META_API_KEY` is not set, the client automatically switches to deterministic mock mode. All features work identically - the only difference is explanations are generated locally.

```python
client = MetaLLMClient()  # Auto-detects: mock if no key
client.is_mock  # True when running without API key
```

### Screenshot Understanding

Send a position screenshot → Muse extracts fields → User confirms → Monitoring starts

## Architecture

Business logic lives in framework-free packages. Telegram (`aiogram`) and the web dashboard
(`FastAPI`) are thin delivery layers on top of one shared `AppContext`; neither imports the
other, and `test_architecture.py` enforces both rules plus the absence of import cycles.

```
                 ┌──────────────┐          ┌──────────────┐
                 │  telegram/   │          │     web/     │   delivery layers
                 │  handlers,   │          │ FastAPI, WS, │   (only these import
                 │  sim_bridge  │          │ auth routes  │    aiogram / FastAPI)
                 └──────┬───────┘          └──────┬───────┘
                        └───────────┬─────────────┘
                                    ▼
                           ┌────────────────┐
                           │     core/      │  AppContext wiring, PositionService,
                           │                │  BindingActionExecutor
                           └───────┬────────┘
     ┌──────────┬──────────┬───────┼─────────┬────────────┬──────────┐
     ▼          ▼          ▼       ▼         ▼            ▼          ▼
 exchanges/   alerts/   protect/  simulation/ storage/    auth/      ai/
 adapters     monitor,  tier-1    per-user    envelope    sessions,  Muse client
 + key policy cooldowns guardrails registry   encryption, login      + mock
                                              repositories links,
                                                          widget
     └──────────┴──────────┴───────┬─────────┴────────────┴──────────┘
                                   ▼
                            risk/  (pure math: engine, advice, portfolio)
                                   ▼
                            models/ (dataclasses, no I/O)
```

| Package | Responsibility |
|---------|----------------|
| `models/` | Plain dataclasses: `Position`, `User`, `ExchangeBinding`, `Alert`, thresholds |
| `risk/` | Pure calculations: liquidation distance, exact margin/reduction advice, portfolio overview |
| `ai/` | `MetaLLMClient` (OpenAI-compatible Muse client) with deterministic mock fallback |
| `exchanges/` | `ExchangeAdapter` interface; Binance, Hyperliquid and demo adapters; Binance key-permission policy |
| `alerts/` | Per-user cooldown/dedup (`AlertManager`) and the polling `PositionMonitor` (delivers through a `Notifier`) |
| `protect/` | Tier-1 protective actions, guardrails, kill switch, per-user audit logger |
| `simulation/` | Deterministic market simulation and `SimulationRegistry` (one per user + one public demo) |
| `storage/` | SQLite schema, envelope encryption (`KeyManager`), per-user repositories |
| `auth/` | Signed session tokens, one-time login links, Telegram Login Widget verification |
| `core/` | `AppContext` (wires everything once), `PositionService`, testnet action executor |
| `telegram/` | aiogram handlers, Telegram notifier, owner-only simulation bridge |
| `web/` | Dashboard app, session/CSRF guards, static frontend |
| `config.py` · `logsafe.py` · `main.py` | Settings from env, log redaction, process entry point |

`risk/` is still the data layer for any future integration:

```python
from liqguard.risk import PortfolioAnalyzer, format_portfolio_overview_text

overview = PortfolioAnalyzer(user_threshold).analyze_portfolio(positions)
data = overview.to_dict()                                   # JSON for API/dashboard
text = format_portfolio_overview_text(overview, language="en")
```

### Multi-user isolation

Every piece of per-user state is keyed by the owner's Telegram user id, and lookups
never fall back to "any user":

| Data | Where it is scoped |
|------|--------------------|
| Exchange keys / wallets | `bindings` rows keyed by `telegram_id`, encrypted with that user's own data key; a ciphertext copied into another user's row fails to decrypt |
| Positions | `PositionService` builds adapters from the requesting user's bindings only; demo books are per user |
| Alerts | `AlertManager` keys cooldowns and history by `(user, venue, symbol)`; the monitor delivers only to `notifier.notify(owner_id, …)` |
| Protective actions | Stored per user; another user's action id is "not found" for confirm/execute/cancel; daily caps and kill switch per user |
| Audit log | `audit_log` rows and in-memory entries per user; subscribers receive only their user's entries |
| Simulations | `SimulationRegistry` gives each user a private simulation. Its Telegram bridge sends only to the owner (the old broadcast-to-every-chat path is gone). The public dashboard demo has no owner and never sends to Telegram |
| Dashboard | `/api/me/*` and `/ws/me` resolve the user from the session cookie only; there is no user id parameter to tamper with |

`test_isolation.py` checks each row with two users (Alice and Bob).

### Key handling

- **Envelope encryption.** A master key (KEK) from `LIQGUARD_MASTER_KEY` (or a mode-600 key
  file outside the repo) wraps a random per-user data key (DEK). Only wrapped DEKs are stored.
  API key, secret and wallet address are encrypted with the owner's DEK. Comma-separated master
  keys enable rotation (`EncryptedStorage.rotate_master_key()` re-wraps every DEK). SQLite runs
  with `secure_delete` so unbound keys are overwritten.
- **Read-only on mainnet.** On `/bind`, LiqGuard calls Binance
  `GET /sapi/v1/account/apiRestrictions` for *that key* and rejects any key with withdraw,
  transfer or trading permission (futures, spot/margin, options, portfolio margin, FIX). A key
  whose permissions cannot be verified is rejected too. The mainnet adapter additionally raises
  `ReadOnlyViolation` on any non-GET request.
- **Testnet trading only.** The separate "Binance testnet" binding may trade (tier-1 protective
  actions), but a withdraw permission is still rejected. Actions execute only for positions
  whose `network` is testnet.
- **Never logged.** Bindings, adapters, settings and the LLM client have redacted `repr`s; a
  logging filter on every handler scrubs bot tokens, 64-char API keys/signatures, Fernet
  ciphertexts and `token=`/`signature=` query values; uvicorn access logs are off. The bot
  deletes the message containing the secret after reading it.

### Dashboard login

1. Send `/dashboard` to the bot in a private chat. It replies with a one-time link
   (`/auth/login?token=…`, 256-bit random, stored only as a SHA-256 hash, expires after
   `LIQGUARD_LOGIN_LINK_TTL`, default 5 min). Group chats get no link.
2. Alternatively, sign in with the **Telegram Login Widget** on `/login` (needs
   `LIQGUARD_BOT_USERNAME` and BotFather `/setdomain`). The widget hash is verified with
   `SHA256(bot_token)` and must be fresh.
3. Either path sets `liqguard_session`: an HMAC-signed, expiring token
   (`LIQGUARD_SESSION_TTL`, default 12 h) in an `HttpOnly`, `SameSite=Lax` cookie (`Secure`
   when `LIQGUARD_PUBLIC_URL` is https). The token carries a session epoch;
   `/dashboard revoke` bumps it, which signs out every device and voids unused links.
4. State-changing requests need the `X-LiqGuard-CSRF` header and a same-origin `Origin`;
   `/ws/me` checks the Origin and the cookie and closes with code 1008 otherwise.

## Protective Actions (Tiers)

### Tier 0: Read-Only (Default)
- Monitor only, no execution capability
- Suitable for most users

### Tier 1: User Confirmation (Implemented)
- AI suggests risk-reducing actions
- User taps confirm button before execution
- **Testnet only**: needs `LIQGUARD_PROTECT_ENABLED=true` and a Binance testnet binding; there is no mainnet switch
- Guardrails:
  - Per-action amount cap ($1,000 default)
  - Daily total cap ($5,000 default)
  - Max position reduction (50% default)
  - Only risk-reducing actions (add margin, reduce size)
  - Never withdrawal permission
  - Audit log of every step
  - One-tap kill switch

### Tier 2: Rules-Based (Future Work)
- User defines conditions (e.g., "add margin if distance < 10%")
- Automatic execution when conditions met
- Still respects all guardrails

### Tier 3: Full Automation (Future Work)
- AI-driven decisions
- Portfolio-level optimization
- Requires extensive testing and user opt-in

## Demo Mode

Experience all features without real money:

```
/demo  →  Select scenario:
          • Normal Market
          • Volatile Market
          • Critical Risk
          • Safe Portfolio
          • 🎬 Liquidation Simulation
```

The **Liquidation Simulation** gradually moves prices toward liquidation, firing alerts at each threshold (20%, 10%, 5%) with action buttons.

## Security Model

Per-user isolation and per-user encryption are hard requirements. Details are in
[Multi-user isolation](#multi-user-isolation), [Key handling](#key-handling) and
[Dashboard login](#dashboard-login); this section is the summary.

### Guarantees

| # | Guarantee | Enforced by | Tested in |
|---|-----------|-------------|-----------|
| 1 | A user only ever sees, receives or acts on their own positions, keys, alerts, protective actions, audit entries and simulations | Owner id is part of every storage query, in-memory key and lookup; there is no "all users" read path outside the monitor loop, which handles one user at a time | `test_isolation.py`, `test_e2e.py` |
| 2 | Exchange credentials are encrypted per user | Envelope encryption: master key → per-user data key → API key, secret, wallet address | `test_storage.py`, `test_isolation.py`, `test_e2e.py` (raw DB bytes contain no key, secret or wallet) |
| 3 | Mainnet keys are read-only | Per-key `apiRestrictions` check at `/bind`; unverifiable keys are rejected; the mainnet adapter refuses non-GET requests | `test_exchanges.py`, `test_e2e.py` |
| 4 | No mainnet trading, ever | Protective actions run only for `network == testnet` positions through the user's own testnet binding | `test_isolation.py` |
| 5 | Dashboard data needs a valid session for that user | HMAC-signed, expiring cookie with a per-user epoch; one-time hashed login links; Telegram widget hash check; CSRF header and Origin checks | `test_auth.py`, `test_e2e.py` |
| 6 | Secrets never reach logs or API responses | Redacted reprs, log redaction filter, masked hints (`…ABCD`, `0x1234…abcd`) in the dashboard | `test_architecture.py`, `test_exchanges.py`, `test_e2e.py` |

### Threat model

- **Other LiqGuard users**: cannot read or influence your data via Telegram commands, callback
  data (action ids are random and owner-checked), dashboard routes (no user id in any URL) or
  WebSockets (cookie and Origin bound).
- **Database leak without the master key**: reveals only ciphertexts and wrapped data keys. Keep the
  master key outside the database host's backups (env var or secrets manager).
- **Stolen login link**: works once and for 5 minutes; `/dashboard revoke` kills all sessions.
- **Cross-site requests**: blocked by `SameSite=Lax`, the custom CSRF header and the Origin check.

### Not protected against (by design)

- **The operator.** Whoever holds the master key and the database can decrypt every user's
  credentials. That is inherent to a server that must call Binance on your behalf. Use
  read-only, IP-restricted keys.
- **Hyperliquid addresses are public on-chain.** They are encrypted anyway, so a database leak
  does not link a Telegram account to a wallet.
- **Telegram itself.** The bot deletes the messages containing your API key and secret right
  after reading them, but copies may remain on your other devices until they sync. During
  `/bind` the API key is held in memory only until you send the secret.

### Production checklist

1. Set `LIQGUARD_MASTER_KEY` from a secrets manager and back it up separately from the database.
2. Serve the dashboard behind HTTPS and set `LIQGUARD_PUBLIC_URL=https://…` (enables `Secure` cookies).
3. Set `LIQGUARD_BOT_USERNAME` and BotFather `/setdomain` if you want the Login Widget.
4. Keep `LIQGUARD_PROTECT_ENABLED=false` unless you want testnet protective actions.
5. Generate a master key with
   `python3 -c "from liqguard.storage import generate_key; print(generate_key())"`.
   If none is set, one is created once at `~/.liqguard/encryption.key` (mode 600) and reused across restarts.

## Web Dashboard

Two views on one page:

- **`/` – public demo**, clearly labelled **DEMO DATA**. It shows the demo portfolio that
  Telegram `/demo` uses (BTC + SOL on Binance, ETH on Hyperliquid). No login is needed, and
  nothing on it ever reaches Telegram.
- **`/me` – your private dashboard** after signing in with `/dashboard` (or the Telegram
  widget on `/login`). It shows only your bindings (masked), positions, audit log and your own
  simulation, whose alerts also arrive in your Telegram chat.

### Run it

```bash
pip install -e ".[dev]"

# Web only, no Telegram token needed
liqguard-dashboard --port 8765          # or: python3 -m liqguard.web.run --port 8765
liqguard-dashboard --public-only        # demo page only, login disabled

# Bot + dashboard in one process: alerts appear on the web and in Telegram at the same time
liqguard --dashboard --dashboard-port 8765   # or set LIQGUARD_DASHBOARD=true
```

Open http://127.0.0.1:8765 and click **Simulate market** for the public demo.

For the private view, run the combined mode, send `/dashboard` to the bot and open the link.
**Simulate market** on `/me` runs *your* simulation: alerts appear on the page and in your
Telegram chat only. Starting **🎬 Liquidation Simulation** from Telegram drives the same
private run. Set `LIQGUARD_PUBLIC_URL` to the address users open (https in production).

### What's on the page

| Panel | Contents |
|-------|----------|
| Muse summary banner | Overall risk level plus a one-line Muse summary naming the riskiest position (the same summary `/risk` shows) |
| KPIs | Total notional, margin used, unrealized PnL, net exposure, closest distance to liquidation, margin needed to restore the safety buffer |
| Chart | **Distance** tab: every position's distance to liquidation over time, with shaded warning/critical/emergency bands. **Per-symbol** tabs: mark price vs liquidation line, plus the price where each tier fires |
| Positions | Venue, side, leverage, distance gauge with tier markers, mark/liq price, PnL |
| Alert timeline | Three-tier alerts in order, showing which channels (Web, Telegram) received each one |
| Muse advice | Per-alert explanation, plus exact "add $X margin" and "reduce Y%" numbers computed in code |
| Audit log | Simulation start/stop, every alert, every advice card, and protective-action steps from Telegram (sanitized) |

### How the simulation works

`liqguard/simulation/market.py` (`MarketSimulation`) is a deterministic price path
(seeded, about 30 s); `SimulationRegistry` keeps one per user plus the public demo. Each position moves toward its liquidation price at a different pace, so the
run shows warning → critical → emergency in sequence. No tier is ever skipped, and each fires
once per position. All numbers come from `PortfolioAnalyzer` / `AdviceCalculator`; Muse
(or the mock) only writes the wording. The web dashboard and `TelegramSimulationBridge` both
subscribe to the owning simulation's event stream.

### API

The same simulation routes exist under `/api/demo/*` (public, demo data) and `/api/me/*`
(session cookie required, the signed-in user's data only).

| Endpoint | Method | Description |
|----------|--------|-------------|
| `…/snapshot` | GET | Full state: overview, summary, history, alerts, advice, audit |
| `…/portfolio` | GET | `/risk` data layer (`PortfolioRiskOverview.to_dict()`) + Muse summary |
| `…/alerts` · `…/advice` · `…/audit` | GET | Timeline, advice cards, audit log |
| `…/simulate` · `…/simulate/stop` · `…/simulate/reset` | POST | Simulation controls (CSRF header required) |
| `/api/me/session` · `/api/me/account` | GET | Session info; your bindings (masked), positions overview, audit log |
| `/api/health` · `/api/auth/config` | GET | Status; login options (no secrets) |
| `/auth/login?token=` · `/auth/telegram` | GET | One-time link / Telegram widget sign-in |
| `/auth/logout` | POST | Sign out (CSRF header required) |
| `/ws/demo` · `/ws/me` | WebSocket | Snapshot on connect, then `tick` / `alert` / `advice` / `summary` / `audit` / `status` events |

### Security

- **No trading from the web.** The only write endpoints are simulation controls and logout. Binding accounts and confirming actions stay in Telegram (tier 1, testnet only).
- **No secrets in the browser.** Responses carry no API keys, secrets or ciphertexts; bindings show a masked hint. Tests check this.
- **Hardened by default.** Binds to `127.0.0.1`, strict Content-Security-Policy, `X-Frame-Options: DENY`, `Cache-Control: no-store`. Chart.js (jsDelivr) and, on `/login`, the Telegram widget script are the only external scripts.

## Testing

```bash
python3 -m pytest liqguard/tests/ -q
```

| File | Covers |
|------|--------|
| `test_risk_engine.py` | Liquidation distance, margin calculations, risk scoring |
| `test_advice.py` | Actionable advice; added margin lands exactly on the target distance |
| `test_portfolio.py` | `/risk` portfolio aggregation and metrics |
| `test_llm.py` | Mock mode, severity levels, one-line portfolio summary (incl. live-response validation) |
| `test_simulation.py` | Deterministic path, tier ordering, advice/audit, Telegram bridge and handlers |
| `test_dashboard.py` | Endpoints, WebSocket, controls, no execution routes, no secret leakage |
| `test_storage.py` | Master key sources, envelope encryption, rotation, legacy migration |
| `test_isolation.py` | Two users: storage, alerts, monitor, simulations, actions, audit, demo books, dashboard |
| `test_auth.py` | Session tokens, login links, Telegram widget, cookies, CSRF, WebSocket auth, `/dashboard` |
| `test_exchanges.py` | Binance key-permission policy, read-only mainnet adapter, no secrets in reprs |
| `test_architecture.py` | No framework imports in business logic, no cycles, log redaction, startup key message |
| `test_e2e.py` | Two users from `/start` → `/bind` → monitor alerts → `/dashboard` login → private dashboard → `/unbind`, with faked exchange APIs |

246 tests at the time of writing.

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Bot only | Telegram Bot API token |
| `META_API_KEY` | No | Meta AI API key (Muse runs in deterministic mock mode if unset) |
| `META_MODEL` | No | Model name (default: muse-spark) |
| `LIQGUARD_MASTER_KEY` | Recommended | Master key(s), comma-separated for rotation. Legacy name `LIQGUARD_ENCRYPTION_KEY` still works |
| `LIQGUARD_KEY_FILE` | No | Key file used when no env key is set (default `~/.liqguard/encryption.key`, created mode 600) |
| `LIQGUARD_DB_PATH` | No | SQLite database path (default: liqguard.db) |
| `LIQGUARD_POLL_INTERVAL` | No | Position check interval in seconds (default: 30) |
| `LIQGUARD_DASHBOARD` | No | Start the dashboard with the bot (default: false) |
| `LIQGUARD_DASHBOARD_HOST` / `_PORT` | No | Dashboard bind address and port (default: 127.0.0.1:8765) |
| `LIQGUARD_PUBLIC_URL` | Production | Dashboard URL used in login links; https enables `Secure` cookies. If unset and the dashboard is off, `/dashboard` says it is unavailable |
| `LIQGUARD_BOT_USERNAME` | No | Enables the Telegram Login Widget (auto-detected when the bot starts) |
| `LIQGUARD_SESSION_SECRET` | No | Session signing secret, at least 32 chars (default: derived from the master key) |
| `LIQGUARD_SESSION_TTL` | No | Dashboard session lifetime in seconds (default: 43200) |
| `LIQGUARD_LOGIN_LINK_TTL` | No | One-time login link lifetime in seconds (default: 300) |
| `LIQGUARD_PROTECT_ENABLED` | No | Enable tier-1 protective actions on testnet bindings (default: false) |

## Demo Video Script

For the hackathon demo (~3 minutes):

### Scene 1: Introduction (20s)
1. Show the bot in Telegram
2. Send `/start` - show welcome message
3. "Liquidations happen fast. LiqGuard warns you in time."

### Scene 2: Web Dashboard + Simulation (60s)
Setup: `liqguard --dashboard`, send `/dashboard` to the bot and open the link; put the browser and Telegram side by side.
1. On your private dashboard, point out the Muse summary banner and the cross-venue KPIs
2. Click **Simulate market**
3. Distance chart: lines fall through the warning, critical and emergency bands
4. Alerts land in the timeline (tagged Web ✓ and Telegram ✓) and in Telegram at the same moment
5. Muse advice cards show exact "add $X margin" and "reduce Y%" numbers
6. Click the BTCUSDT tab: price closing in on the red liquidation line

### Scene 3: Telegram Bot Commands (40s)
1. `/risk` → Show portfolio risk overview in Telegram
2. `/positions` → Show all positions with actionable advice
3. `/ask What's my riskiest position?` → AI analysis

### Scene 4: Screenshot Import (30s)
1. Send a position screenshot to the bot
2. Show Muse parsing the image
3. Confirm to add to monitoring
4. See it appear in `/positions`

### Scene 5: Protective Actions (20s)
1. Show action button on alert
2. Mention testnet-only execution
3. Demo `/killswitch` for emergency stop

### Scene 6: Security Wrap-up (10s)
1. Read-only API keys
2. Encrypted storage
3. Per-user isolation and private dashboard login

## Roadmap

- [x] Binance USDⓈ-M Futures
- [x] Hyperliquid DEX
- [x] Mock mode for demo/testing
- [x] Portfolio risk overview
- [x] Protective actions Tier 1
- [x] Web dashboard for demo/judges
- [x] Multi-user isolation, envelope encryption, dashboard login
- [ ] Hyperliquid testnet execution and reduce-position execution
- [ ] Persist pending actions and kill switch across restarts
- [ ] OKX and Bybit support
- [ ] Tier 2: Rules-based automation
- [ ] Tier 3: Full AI automation
- [ ] Voice questions via Muse Voice

## License

MIT

## Disclaimer

LiqGuard is a monitoring and advisory tool. It does not provide financial advice. Trading perpetual futures involves significant risk of loss. Protective actions are testnet-only and not intended for production trading. Always do your own research and never risk more than you can afford to lose.
