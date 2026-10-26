# LiqGuard 🛡️

**Telegram bot that warns you before your perpetual futures positions get liquidated.**

Built for the [Meta Global AI Developer Hackathon](https://cerebralvalley.ai/e/meta-ai-developer-hackathon) (2026).

## Features

- 🔔 **Real-time Alerts**: Get notified before positions hit liquidation
- 📊 **Multi-Exchange Support**: Binance USDⓈ-M Futures and Hyperliquid
- 🔒 **Security First**: Read-only API keys, encrypted storage, never trades
- 🤖 **AI-Powered**: Risk explanations in English and Traditional Chinese via Meta's Muse Spark
- 📸 **Screenshot Parsing**: Send a position screenshot, get it analyzed
- 🎮 **Demo Mode**: Try everything without real money

## Quick Start

### Prerequisites

- Python 3.11+
- A Telegram bot token (get from [@BotFather](https://t.me/BotFather))
- (Optional) Meta API key for AI features

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

### Configuration

Create a `.env` file:

```env
# Required
TELEGRAM_BOT_TOKEN=your_telegram_bot_token

# Optional - enables AI risk explanations and screenshot parsing
META_API_KEY=your_meta_api_key

# Optional - for persistent encrypted storage (generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
LIQGUARD_ENCRYPTION_KEY=your_fernet_key

# Optional - database path
LIQGUARD_DB_PATH=liqguard.db

# Optional - position check interval in seconds (default: 30)
LIQGUARD_POLL_INTERVAL=30
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
| `/positions` | View all positions with risk assessment |
| `/threshold` | Configure alert thresholds |
| `/demo` | Toggle demo mode with simulated positions |
| `/ask <question>` | Ask AI about your positions |
| `/language` | Change language (English/繁體中文) |
| `/status` | View account status |

## How We Use Meta AI

LiqGuard leverages Meta's Muse Spark model for several key features:

### 1. Tool Calling for Risk Calculations
```python
# The LLM uses tools to perform precise calculations
tools = [
    "calculate_liquidation_distance",
    "calculate_margin_needed",
    "get_risk_level"
]
```

### 2. Structured Output for Risk Assessment
```json
{
    "severity": "critical",
    "reason": "Position is 5.2% from liquidation",
    "actions": ["Add $500 margin", "Reduce position size"],
    "urgency": "high"
}
```

### 3. Multilingual Risk Explanations
- Clear, jargon-free explanations in English and Traditional Chinese
- Contextual advice based on position severity

### 4. Screenshot Understanding
- Send a position screenshot from any exchange
- AI extracts: symbol, size, entry price, mark price, liquidation price, leverage
- Works even with partial or noisy screenshots

## Architecture

```
liqguard/
├── connectors/          # Exchange API connectors
│   ├── binance.py      # Binance USDⓈ-M Futures
│   ├── hyperliquid.py  # Hyperliquid DEX
│   └── demo.py         # Demo mode simulator
├── models/              # Data models
│   ├── position.py     # Normalized position format
│   ├── alert.py        # Alert and threshold models
│   └── user.py         # User and binding models
├── services/            # Business logic
│   ├── risk_engine.py  # Liquidation distance calculations
│   ├── alert_manager.py # Alert cooldowns and deduplication
│   └── storage.py      # Encrypted SQLite storage
├── llm/                 # AI integration
│   └── meta_client.py  # Meta Muse Spark client
├── telegram/            # Bot interface
│   ├── handlers.py     # Command handlers
│   └── monitor.py      # Background position monitor
└── main.py             # Entry point
```

### Data Flow

```
Exchange APIs → Connectors → Normalizer → Risk Engine → Alert Manager → Telegram
                                              ↓
                                         Meta LLM (explanations)
```

## Security Model

### API Key Protection

1. **Read-Only Enforcement**: Binance keys are validated; keys with withdraw permission are rejected
2. **Encryption at Rest**: API secrets are encrypted with Fernet (AES-128-CBC) before storage
3. **No Trading**: The bot never places orders or initiates withdrawals
4. **Minimal Permissions**: Hyperliquid uses public API only (wallet address, no private key)

### Key Validation Flow

```python
# Binance key validation
is_valid, msg, permissions = await connector.validate_credentials()

if "WITHDRAW" in permissions:
    # REJECTED - too dangerous
    raise SecurityError("Key has withdraw permission")

if "FUTURES_TRADE" in permissions:
    # WARNING - recommend read-only
    warn_user()
```

### Storage Security

- SQLite database with encrypted secrets
- Encryption key from environment variable
- Ephemeral key warning for development mode

## Demo Mode

Demo mode lets you experience all features without connecting real accounts:

```
/demo  →  Select scenario:
          • Normal Market (typical conditions)
          • Volatile Market (positions at risk)
          • Critical Risk (near liquidation)
          • Safe Portfolio (all positions safe)
```

Demo includes simulated positions:
- BTC/USDT Long (10x leverage)
- ETH/USDT Short (20x leverage)
- SOL/USDT Long (5x leverage)

## Demo Video Script

For recording the hackathon demo video (~3 minutes):

### Scene 1: Introduction (30s)
1. Show the bot in Telegram
2. Send `/start` - show welcome message
3. Explain the problem: "Liquidations happen fast. LiqGuard warns you in time."

### Scene 2: Demo Mode (45s)
1. Send `/demo` → Select "Critical Risk" scenario
2. Send `/positions` → Show positions near liquidation
3. Show the detailed risk breakdown and margin suggestions

### Scene 3: AI Features (45s)
1. Send `/ask What is my riskiest position?`
2. Show AI-powered analysis
3. Send a screenshot → Show AI parsing
4. Toggle to Chinese with `/language`

### Scene 4: Real Exchange Binding (30s)
1. Send `/bind` → Show Binance flow
2. Explain read-only key validation
3. Show security warnings for dangerous permissions

### Scene 5: Alerts (30s)
1. Show an alert notification
2. Explain distance to liquidation
3. Show margin calculation: "Add $X to reach 30% safety"

### Scene 6: Wrap-up (15s)
1. Send `/status` - show account overview
2. Mention: works with Hyperliquid too (no key needed!)

## Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=liqguard

# Run specific test file
pytest liqguard/tests/test_risk_engine.py -v
```

### Test Coverage

- `test_risk_engine.py`: Liquidation distance calculations, margin calculations, risk scoring

## Development

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run linter
ruff check liqguard/

# Format code
ruff format liqguard/
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | Telegram Bot API token |
| `META_API_KEY` | No | Meta AI API key for LLM features |
| `LIQGUARD_ENCRYPTION_KEY` | No | Fernet key for secret encryption |
| `LIQGUARD_DB_PATH` | No | SQLite database path (default: liqguard.db) |
| `LIQGUARD_POLL_INTERVAL` | No | Position check interval in seconds (default: 30) |

## Supported Exchanges

### Binance USDⓈ-M Futures
- Requires API key and secret
- Must be READ-ONLY (no trade, no withdraw)
- Uses `/fapi/v2/` endpoints

### Hyperliquid
- Wallet address only (no private key)
- Uses public `clearinghouseState` endpoint
- Fully trustless - we can't do anything bad

## Roadmap

- [ ] OKX and Bybit support
- [ ] Voice questions via Muse Voice
- [ ] Daily digest summaries
- [ ] Muse Image risk visualization cards
- [ ] Stop-loss suggestions

## License

MIT

## Disclaimer

LiqGuard is a monitoring tool only. It does not provide financial advice. Trading perpetual futures involves significant risk of loss. Always do your own research and never risk more than you can afford to lose.
