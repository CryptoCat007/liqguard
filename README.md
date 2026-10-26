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
- 🖥️ **Web Dashboard**: Read-only live view for demos; same alerts as Telegram

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

## Bot Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message and quick start |
| `/help` | Show all available commands |
| `/bind` | Connect an exchange (Binance or Hyperliquid) |
| `/unbind` | Disconnect an exchange |
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

```
liqguard/
├── connectors/          # Exchange API connectors
│   ├── binance.py      # Binance USDⓈ-M Futures
│   ├── hyperliquid.py  # Hyperliquid DEX
│   └── demo.py         # Demo mode with price simulation
├── models/              # Data models
│   ├── position.py     # Normalized position format
│   ├── alert.py        # Alert and threshold models
│   └── user.py         # User and binding models
├── services/            # Business logic
│   ├── advice.py       # Deterministic actionable advice
│   ├── portfolio.py    # Portfolio analysis & data layer
│   ├── risk_engine.py  # Liquidation distance calculations
│   ├── alert_manager.py # Alert cooldowns and deduplication
│   ├── protective_actions.py # Tier 1 actions with guardrails
│   ├── simulation.py   # Shared demo price path + event bus (web + Telegram)
│   └── storage.py      # Encrypted SQLite storage
├── llm/                 # AI integration
│   └── meta_client.py  # Meta Muse Spark client + mock
├── telegram/            # Bot interface
│   ├── handlers.py     # Command handlers
│   ├── monitor.py      # Background position monitor
│   └── sim_bridge.py   # Mirrors simulation alerts to Telegram chats
├── dashboard/           # Read-only web dashboard
│   ├── server.py       # FastAPI + WebSocket server
│   ├── run.py          # Standalone runner (liqguard-dashboard)
│   └── static/         # index.html, app.css, app.js (Chart.js)
└── main.py             # Entry point (bot + optional dashboard)
```

### Data Layer API

The `services/portfolio.py` module exposes a clean API for future integrations:

```python
from liqguard.services import PortfolioAnalyzer, format_portfolio_overview_text

analyzer = PortfolioAnalyzer(user_threshold)
overview = analyzer.analyze_portfolio(positions)

# JSON-serializable for API/dashboard
data = overview.to_dict()

# Formatted for display
text = format_portfolio_overview_text(overview, language="en")
```

## Protective Actions (Tiers)

### Tier 0: Read-Only (Default)
- Monitor only, no execution capability
- Suitable for most users

### Tier 1: User Confirmation (Implemented)
- AI suggests risk-reducing actions
- User taps confirm button before execution
- **Testnet only** - mainnet disabled by config
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

### API Key Protection

1. **Read-Only Enforcement**: Binance keys with withdraw permission are rejected
2. **Encryption at Rest**: Secrets encrypted with Fernet (AES-128-CBC)
3. **Testnet Execution**: Protective actions only on testnet
4. **Never Mainnet**: Production trading disabled by config
5. **Audit Logging**: Every action logged with timestamp

### Encryption Key Management

```bash
# Generate a key
python -c "from liqguard.services import EncryptedStorage; print(EncryptedStorage.generate_key())"

# Set via environment (recommended for production)
export LIQGUARD_ENCRYPTION_KEY="your-key-here"

# Or auto-generated and saved to ~/.liqguard/encryption.key
```

## Web Dashboard

A single-page, read-only dashboard for the demo video and judges. It shows the same
demo portfolio that Telegram `/demo` uses (BTC + SOL on Binance, ETH on Hyperliquid).

### Run it

```bash
pip install -e ".[dev]"

# Web only, no Telegram token needed
liqguard-dashboard --port 8765          # or: python -m liqguard.dashboard.run --port 8765

# Bot + dashboard in one process: alerts appear on the web and in Telegram at the same time
liqguard --dashboard --dashboard-port 8765   # or set LIQGUARD_DASHBOARD=true
```

Open http://127.0.0.1:8765 and click **Simulate market**.

To get the web-triggered alerts in Telegram, run the combined mode and either send `/demo`
to the bot once (any scenario subscribes your chat) or preset
`LIQGUARD_DEMO_CHAT_IDS=<your chat id>`. Starting **🎬 Liquidation Simulation** from Telegram
drives the same run, so the dashboard updates as well.

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

`liqguard/services/simulation.py` (`MarketSimulation`) is one shared, deterministic price path
(seeded, about 30 s). Each position moves toward its liquidation price at a different pace, so the
run shows warning → critical → emergency in sequence. No tier is ever skipped, and each fires
once per position. All numbers come from `PortfolioAnalyzer` / `AdviceCalculator`; Muse
(or the mock) only writes the wording. The web dashboard and `TelegramSimulationBridge` both
subscribe to the same event stream.

### API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/snapshot` | GET | Full state: overview, summary, history, alerts, advice, audit |
| `/api/portfolio` | GET | `/risk` data layer (`PortfolioRiskOverview.to_dict()`) + Muse summary |
| `/api/alerts` · `/api/advice` · `/api/audit` | GET | Timeline, advice cards, audit log |
| `/api/health` | GET | Status, Muse mode, simulation state |
| `/api/simulate` · `/api/simulate/stop` · `/api/simulate/reset` | POST | Demo simulation controls |
| `/ws` | WebSocket | Snapshot on connect, then `tick` / `alert` / `advice` / `summary` / `audit` / `status` events |

### Security

- **Read-only.** The only write endpoints are the demo-simulation controls. Nothing binds accounts or proposes, confirms, or executes actions; that stays in Telegram (Tier 1, testnet only).
- **No secrets in the browser.** Responses carry no API keys, wallet secrets, or Telegram user IDs, and tests check this.
- **Demo data by default.** Binds to `127.0.0.1` by default and sends a strict Content-Security-Policy. Chart.js is the only external script (jsDelivr CDN).

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
| `test_storage.py` | Encryption key handling |

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Bot only | Telegram Bot API token (not needed for `liqguard-dashboard`) |
| `META_API_KEY` | No | Meta AI API key (Muse runs in deterministic mock mode if unset) |
| `META_MODEL` | No | Model name (default: muse-spark) |
| `LIQGUARD_ENCRYPTION_KEY` | Recommended | Fernet key for secrets (auto-generated to `~/.liqguard/encryption.key` if not set) |
| `LIQGUARD_DB_PATH` | No | SQLite database path (default: liqguard.db) |
| `LIQGUARD_POLL_INTERVAL` | No | Position check interval in seconds (default: 30) |
| `LIQGUARD_DASHBOARD` | No | Start the dashboard with the bot (default: false) |
| `LIQGUARD_DASHBOARD_HOST` | No | Dashboard bind address (default: 127.0.0.1) |
| `LIQGUARD_DASHBOARD_PORT` | No | Dashboard port (default: 8765) |
| `LIQGUARD_DEMO_CHAT_IDS` | No | Comma-separated chat IDs that receive simulation alerts without running `/demo` first |

## Demo Video Script

For the hackathon demo (~3 minutes):

### Scene 1: Introduction (20s)
1. Show the bot in Telegram
2. Send `/start` - show welcome message
3. "Liquidations happen fast. LiqGuard warns you in time."

### Scene 2: Web Dashboard + Simulation (60s)
Setup: `liqguard --dashboard`, send `/demo` to the bot once, put the browser and Telegram side by side.
1. Open http://127.0.0.1:8765 and point out the Muse summary banner and the cross-venue KPIs
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
3. No secrets in dashboard

## Roadmap

- [x] Binance USDⓈ-M Futures
- [x] Hyperliquid DEX
- [x] Mock mode for demo/testing
- [x] Portfolio risk overview
- [x] Protective actions Tier 1
- [x] Web dashboard for demo/judges
- [ ] OKX and Bybit support
- [ ] Tier 2: Rules-based automation
- [ ] Tier 3: Full AI automation
- [ ] Voice questions via Muse Voice

## License

MIT

## Disclaimer

LiqGuard is a monitoring and advisory tool. It does not provide financial advice. Trading perpetual futures involves significant risk of loss. Protective actions are testnet-only and not intended for production trading. Always do your own research and never risk more than you can afford to lose.
