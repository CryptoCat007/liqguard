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
| `/risk` | Portfolio risk overview with metrics |
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
│   └── storage.py      # Encrypted SQLite storage
├── llm/                 # AI integration
│   └── meta_client.py  # Meta Muse Spark client + mock
├── telegram/            # Bot interface
│   ├── handlers.py     # Command handlers
│   └── monitor.py      # Background position monitor
└── main.py             # Entry point
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

## Testing

```bash
# Run all tests (67 tests)
python3 -m pytest liqguard/tests/ -v

# Run with coverage
python3 -m pytest --cov=liqguard

# Run specific test file
python3 -m pytest liqguard/tests/test_portfolio.py -v
```

### Test Coverage

- `test_risk_engine.py`: Liquidation distance, margin calculations, risk scoring
- `test_advice.py`: Actionable advice generation
- `test_portfolio.py`: Portfolio analysis and metrics
- `test_llm.py`: Mock mode and severity levels
- `test_storage.py`: Encryption key handling

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | Telegram Bot API token |
| `META_API_KEY` | No | Meta AI API key (uses mock if not set) |
| `META_MODEL` | No | Model name (default: muse-spark) |
| `LIQGUARD_ENCRYPTION_KEY` | Recommended | Fernet key for secrets (auto-generated if not set) |
| `LIQGUARD_DB_PATH` | No | SQLite database path (default: liqguard.db) |
| `LIQGUARD_POLL_INTERVAL` | No | Position check interval in seconds (default: 30) |

## Demo Video Script

For the hackathon demo (~3 minutes):

### Scene 1: Introduction (30s)
1. Show the bot in Telegram
2. Send `/start` - show welcome message
3. "Liquidations happen fast. LiqGuard warns you in time."

### Scene 2: Demo Mode + Simulation (60s)
1. `/demo` → Select "Liquidation Simulation"
2. Watch prices move, alerts fire at each tier
3. Show actionable advice with exact numbers
4. Tap an action button

### Scene 3: Portfolio Overview (30s)
1. `/risk` → Show portfolio risk overview
2. Highlight risk metrics, exposure analysis
3. Show riskiest position

### Scene 4: AI Features (30s)
1. `/ask What's my riskiest position?`
2. Show AI-powered analysis with exact numbers
3. Toggle to Chinese with `/language`

### Scene 5: Screenshot Import (20s)
1. Send a position screenshot
2. Show parsed data
3. Confirm to add to monitoring

### Scene 6: Security (10s)
1. Mention testnet-only execution
2. Show kill switch: `/killswitch`

## Roadmap

- [x] Binance USDⓈ-M Futures
- [x] Hyperliquid DEX
- [x] Mock mode for demo/testing
- [x] Portfolio risk overview
- [x] Protective actions Tier 1
- [ ] OKX and Bybit support
- [ ] Tier 2: Rules-based automation
- [ ] Tier 3: Full AI automation
- [ ] Web dashboard (separate project)
- [ ] Voice questions via Muse Voice

## License

MIT

## Disclaimer

LiqGuard is a monitoring and advisory tool. It does not provide financial advice. Trading perpetual futures involves significant risk of loss. Protective actions are testnet-only and not intended for production trading. Always do your own research and never risk more than you can afford to lose.
