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
- 📐 **True Liquidation Price**: Cross-margin aware (other positions' PnL and maintenance), Binance maintenance tiers and the Hyperliquid margin model
- 💸 **Funding & Fees**: Live funding rates (refreshed every minute), liquidation drift over 8 h / 24 h at the current rate, pre-funding warnings, taker fees in every cost
- 🛟 **Rescue Simulator**: Add margin vs reduce X% vs hedge, each with new liquidation price, distance and full cost; Muse explains the trade-offs
- 🕯 **Wick Stress Test**: Deepest adverse 1m / 5m wicks over 30 days from public klines, shown as "X wicks away" from liquidation
- 📈 **Adaptive Alert Tiers**: Tiers widen for each symbol's wick history, weekends, low-liquidity hours and stock perps while the US market is closed
- 🛑 **Stop-Loss Sanity Check**: Reads open orders and warns when there is no stop, or the liquidation price is hit before the stop triggers; Muse suggests a stop or size fix
- ⏰ **Wake-up Escalation**: Critical/emergency alerts repeat every N minutes, louder each time, until you tap **✅ I'm awake**
- ⚡ **ADL Indicator**: Binance auto-deleverage rank as 1–5 lights, with a warning when a profitable position reaches 4–5
- 🎯 **Priority Triage**: With several positions in trouble, one consolidated alert ranks them by urgency (distance, volatility-adjusted time to liquidation, size) and labels the top one **Handle this first**
- 📏 **Position Sizing Check**: Warns when a position's loss at its exit or its margin is too large for the account (configurable risk % per trade) and suggests a size
- 📉 **Portfolio Stress Test**: "If everything moves −5 / −10 / −20 % together, is the account liquidated?" Correlated coins move by their beta; shows the account-level liquidation move
- 🧲 **Concentration Warning & Real Liq Line**: Flags one-directional exposure across correlated coins, and shows the platform's displayed liquidation price next to LiqGuard's correlated cross-margin liquidation, with the gap highlighted
- 🛡️ **Protective Actions**: User-confirmed risk reduction (testnet only)
- 🖥️ **Web Dashboard**: Public demo page plus a private per-user view behind Telegram login
- 👥 **Multi-user**: Every position, key, alert, action, audit entry and simulation is scoped to its owner

## Real user feedback

Several features come straight from traders' posts on Threads (paraphrased, no usernames):

| What traders said | What LiqGuard does about it |
|-------------------|-----------------------------|
| "With several positions open, in a fast move I can't tell which one to deal with first." | **Priority triage.** Positions are ranked by urgency and one consolidated alert lists them in order, with **🎯 Handle this first** on the top one. The same ranking appears in `/risk`, `/positions` and the dashboard. |
| "People open positions far too big for their account." | **Position sizing check.** Each position's loss at its exit (stop, else liquidation) and its margin are compared with account equity. Your limits default to 2 % risk per trade and 25 % margin per position (`/settings risk 2`, `/settings maxmargin 25`). Oversized positions get a suggested size. |
| "Cross margin, five coins, all long. They were liquidated together in one fast move." | **Portfolio stress test** (−5 / −10 / −20 % with correlated coins moving together) and the **account-level liquidation move**. A **concentration warning** fires when most exposure points one way across coins that move together. |
| "The real liquidation was nowhere near the liquidation line the platform showed." | **Platform liq vs LiqGuard liq.** The exchange's line assumes your other positions stand still. LiqGuard also computes the liquidation price when they fall together, and highlights the gap in points of distance. |

Try it with `/demo` → **🎬 5 longs, one wallet**, or pick that scenario on the dashboard.

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
| `/positions` | All positions: exchange vs true liquidation price, correlated liquidation when it is closer, priority rank, sizing, funding, 8 h / 24 h drift, wicks away, adaptive tiers, stop-loss status, ADL lights, 🛟 rescue buttons |
| `/risk` | Portfolio risk overview + Muse summary; 🎯 priority triage (handle in this order); 📉 portfolio stress test per cross account with platform vs correlated liquidation; 🧲 concentration; 📏 oversized positions with suggested sizes; protection checks (no stop, stop beyond liq, one wick from liq, ADL 4–5); liquidation, funding and wick stress test for the riskiest position |
| `/threshold` | Configure alert thresholds (base tiers; adaptive tiers widen from these) |
| `/settings` | Wake-up escalation (interval 1/2/5/10 min or `/settings interval 3`, max reminders, on/off), adaptive tiers on/off, stop-loss checks on/off, sizing limits (`/settings risk 2`, `/settings maxmargin 25`) |
| `/demo` | Demo mode with price simulation: the mixed book or **🎬 5 longs, one wallet** |
| `/ask <question>` | Ask AI about your positions |
| `/actions` | View pending protective actions |
| `/killswitch` | Emergency stop for all actions |
| `/language` | Change language (English/繁體中文) |
| `/status` | View account status |

## Liquidation, Funding & Rescue

All of this is computed in `liqguard/risk/` (pure Python, no I/O). Muse only explains it.

### True liquidation price (`risk/liquidation.py`)

Binance's published one-way formula, solved per maintenance tier:

```
        WB − TMM₁ + UPNL₁ + cum − s·Q·E
LP  =  ─────────────────────────────────         s = +1 long, −1 short
              Q·MMR − s·Q
```

- **WB** is the wallet: the cross wallet balance (Binance `crossWalletBalance`; Hyperliquid
  `accountValue` minus cross uPnL) or the isolated wallet.
- **UPNL₁ / TMM₁** are the unrealized PnL and maintenance margin of every *other* cross
  position in the same account. This is why a losing SOL position moves BTC's liquidation price.
- **MMR / cum** come from the tier that contains Q·LP. Binance tiers are read per account from
  `GET /fapi/v1/leverageBracket`. Built-in defaults are only used, and flagged, when the account
  can't be read. Hyperliquid uses MMR = 1 / (2 × max leverage) from `meta`, with cum = 0.
- `true_liquidation(position, account)` returns the price, the distance, and what went into it.
  Alerts use the **closer** of the exchange-reported and computed distances. A cross position
  whose account couldn't be fetched falls back to the exchange's figure.
- The tests check hand-derived examples and that equity equals maintenance at the computed
  price for random books. They also check that the result matches Hyperliquid's documented
  formula exactly and that bisection agrees with the closed form.

### Funding (`exchanges/funding.py`, `core/market.py`, `risk/funding.py`)

- **Live rates.** Rates come from public endpoints, refreshed every `LIQGUARD_FUNDING_REFRESH`
  seconds, and nothing is hardcoded.
  - **Binance:** `GET /fapi/v1/premiumIndex` gives `lastFundingRate` and `nextFundingTime`, and
    `GET /fapi/v1/fundingInfo` gives the per-symbol `fundingIntervalHours` (4 h or 8 h, default 8).
  - **Hyperliquid:** `POST /info {"type":"metaAndAssetCtxs"}` gives the hourly `funding`; it
    settles on the hour.
- **Refresh failures.** A failed refresh keeps the last good data and reports the error in
  `/api/health`. For example, Binance returns HTTP 451 from restricted regions.
- **Drift.** Each payment (`s · Q · mark · rate`) comes out of the margin pool, so in cross margin
  every position's funding moves every liquidation price. `project_funding` shows the 8 h and
  24 h drift, labelled **"at current rate"** because the rate changes every settlement.
- **Pre-funding warning.** Sent when all of these hold:
  - A settlement is within `LIQGUARD_PREFUNDING_LEAD_MINUTES`.
  - The position pays.
  - It is within the warning distance.
  - The pool's payment is at least 5% of the remaining margin buffer, or would push it into a
    worse alert tier.

  It is sent once per position per settlement and comes with a 🛟 button.
- **Fees.** Taker fees default to 0.05% on Binance and 0.045% on Hyperliquid, and can be
  overridden with `LIQGUARD_TAKER_FEE_*`.

### Rescue simulator (`risk/rescue.py`)

For one at-risk position, each option is solved exactly for the safety target (default 30%):

| Option | How it is computed | Cost reported |
|--------|--------------------|---------------|
| 💰 Add margin | Exact wallet amount whose liquidation price sits at the target | 24 h funding; capital required |
| ✂️ Reduce X% | Bisection on the fraction. Cross: realized PnL and fee go to the shared wallet. Isolated: margin is released pro rata, so only a full close helps (and the plan says so) | Taker fee, realized loss, 24 h funding on what is left |
| 🛡 Hedge | Binance cross: opposite leg in Hedge Mode in the same wallet, size solved by bisection. Hyperliquid or isolated: a cross-venue hedge, marked as **not** moving this liquidation price | Open + close fees, 24 h funding on net exposure, capital |

`recommended` is the cheapest option that reaches the target and protects the position.
`MetaLLMClient.explain_rescue(plan)` returns structured output (`headline`, `recommended_option`,
per-option `pros` / `cons`, `caveat`). A live answer is thrown away for the deterministic mock if it
recommends an option the plan doesn't contain or quotes a dollar amount that isn't in the plan.

Where it shows up:
- **Telegram alerts:** 🛟 *Rescue options*, then one button per option for details.
- **Telegram commands:** the same buttons on `/positions` and `/risk`.
- **Dashboard:** the Rescue simulator panel.
- **Simulation:** every advice card in `/demo` and the dashboard simulation.

## Wicks, Stops, ADL & Wake-up

Built from recurring pain points traders post on X and Reddit: liquidated by a wick that
recovered a minute later, slept through the alert, auto-deleveraged on a winning trade, a stop
that sat *beyond* the liquidation price. As elsewhere, the numbers come from `liqguard/risk/`
and Muse only explains them.

### Wick stress test (`exchanges/klines.py`, `core/wicks.py`, `risk/wicks.py`)

- **Data.** Public klines only, no keys: Binance `GET /fapi/v1/klines` (1m, 1500 per page,
  5m built from 1m in code) and Hyperliquid `candleSnapshot` (1m and 5m natively).
- **Wick.** The adverse intra-candle move from the open: open → low for a long, open → high
  for a short. *Typical* is the 95th percentile and *max* the deepest in the window.
- **X wicks away.** Distance to (true) liquidation ÷ wick size, for 1m and 5m, typical and max.
  "One max wick liquidates" is flagged when a single max wick is enough.
- **Cache and rate limits.** `WickService` caches candles per (venue, symbol, interval) and
  refreshes incrementally every `LIQGUARD_WICK_REFRESH` seconds, never blocking a request.
  A sliding-window `RateLimiter` spends request weight against a per-venue budget
  (Binance 1200/min of the 2400 IP limit, weight 1/2/5/10 by `limit`; Hyperliquid 600/min of
  1200, weight 20 + candles/60). If a refresh fails, the last data is kept and the error
  shows in `/api/health`.
- **Coverage.** Hyperliquid serves only the latest 5000 candles per interval (about 3.5 days
  of 1m and 17 days of 5m). Each stat reports its real window, and the cache grows toward 30 days.

### Adaptive tiers (`risk/sessions.py`, `risk/adaptive.py`)

Your `/threshold` tiers are the base. For each position:

1. **Wick floors.** Emergency ≥ max 1m wick, critical ≥ max 5m wick, warning ≥ 2 × max 5m
   wick, so a routine bad wick can't jump straight past a tier.
2. **Session factor.** ×1.15 on weekends (UTC), ×1.10 in low-liquidity hours
   (`LIQGUARD_LOW_LIQUIDITY_HOURS_UTC`, default 21–01 UTC), ×1.5 for stock perps (e.g.
   `TSLAUSDT`, Hyperliquid `xyz:` markets) while NYSE is closed (9:30–16:00 New York,
   Mon–Fri; holidays are not modelled). The combined factor is capped at 1.5.
3. **Caps.** Tiers stay at least 1 pt apart and are capped at 25 / 40 / 60 %.

Alerts show the effective tier, the base tier and the reasons. You can switch this off in `/settings`.

### Stop-loss sanity check (`risk/stops.py`, `ai/stops.py`)

- **Orders.** Binance read-only `GET /fapi/v1/openOrders`, plus `openAlgoOrders` where the
  account has conditional orders. Hyperliquid `frontendOpenOrders` by wallet address.
  Venues whose orders could not be read are reported as unknown, not "no stop".
- **Statuses.** For each position, LiqGuard finds the stop that triggers first:
  - **no_stop:** no stop-loss at all.
  - **beyond_liquidation:** the exchange liquidates first and the stop never fires. This is critical.
  - **too_close:** the gap is smaller than max(0.5 %, the typical 5m wick), so a stop-market
    fill can slip past liquidation in a wick.
  - **partial:** the stops cover only part of the size.
  - **ok:** the stop fires first.
- **Fixes, computed in code:**
  - Move the stop to a safe price.
  - Keep the stop and add $X margin.
  - Keep the stop and reduce Y % (via the rescue simulator, so liquidation moves beyond the stop).
- **Muse.** Muse picks and explains one fix. Its answer is rejected unless the fix is one of
  those computed and every $ amount matches the computed numbers.
- **Alerts.** Warnings fire immediately and again only when the status or stop price changes.

### ADL indicator (`risk/adl.py`)

- **Binance.** `adlQuantile` from position risk (or `GET /fapi/v1/adlQuantile`), shown as 1–5
  lights (quantile + 1) in `/positions`, `/risk` and the dashboard. A warning fires when a
  profitable position reaches 4–5 lights, because profitable, high-leverage positions are
  closed first if a liquidation exceeds the insurance fund.
- **Hyperliquid.** Shown as *unsupported*: it publishes no per-position ADL rank.

### Wake-up escalation (`alerts/escalation.py`)

- **Reminders.** Critical and emergency alerts carry a **✅ I'm awake** button. If it isn't
  tapped, the alert is resent every N minutes (default 2) up to a cap (default 5). The
  wording escalates: reminder → URGENT → FINAL WAKE-UP CALL.
- **Restarts and recovery.** A worse severity restarts the count, and an older alert's button
  still acknowledges it. Recovering above the critical tier stops reminders.
- **Scope.** Acknowledgements are owner-scoped: someone else's id does nothing.
- **Where to acknowledge.** In Telegram or the dashboard; each side confirms the other.
- **Settings.** Each user sets the interval, cap and on/off with `/settings`. Server defaults
  come from `LIQGUARD_ESCALATION_INTERVAL_MINUTES` and `LIQGUARD_ESCALATION_MAX_REMINDERS`.

> **Make sure the bot can wake you.** Reminders only help if the phone makes a sound:
> - **Telegram:** open the LiqGuard chat → tap the bot name → **Notifications** → turn sound
>   on, pick a loud tone, and don't mute the chat. On iOS, *Settings → Notifications and
>   Sounds → Exceptions* lets this one chat keep sound when other private chats are quiet.
> - **Phone:** in your Focus / Do Not Disturb settings, add Telegram to the allowed apps, or
>   allow notifications from the LiqGuard chat if your system supports per-contact exceptions.
>
> `/settings` shows this tip too.

## Triage, Sizing & Portfolio Stress

All four checks are pure functions in `risk/` and run on every position check, simulation
step and dashboard refresh. `risk/insights.py` (`portfolio_insights`) combines them for one
user's book.

### Priority triage (`risk/triage.py`)

Each position gets an urgency score from 0 to 100:

| Weight | Input | How |
|--------|-------|-----|
| 55 % | Time to liquidation at typical volatility | σ₅ₘ = the symbol's typical 5m adverse wick ÷ 1.645 (a default per asset class when no klines are cached). A random walk needs about (distance ÷ σ₅ₘ)² five-minute steps, so a volatile coin 6 % away can be more urgent than a calm one 4 % away. u = 1 ÷ (1 + minutes ÷ 720) |
| 30 % | Distance against the position's own adaptive warning tier | u = 1 − distance ÷ warning tier, clamped to 0…1 |
| 15 % | Size | Notional relative to the largest position. In one cross wallet every position's loss at liquidation is the same shared buffer, so notional is what tells them apart |

Positions inside a tier rank above those outside. The top one is labelled **🎯 Handle this
first**. When two or more positions are due an alert in the same check, the monitor sends
**one** message listing them in order. Only the top position starts wake-up escalation and
carries the 🛟 rescue button. A single tier alert still shows its rank, e.g.
"Priority #2 of 3 — handle SOLUSDT first".

### Position sizing check (`risk/sizing.py`)

- **Equity** is the venue account's cross wallet plus cross unrealized PnL, plus isolated margin and PnL.
- **Exit** is the protective stop if it fires before liquidation (see the stop-loss check).
  Otherwise it is the liquidation price. For cross positions that is the correlated
  liquidation below, because that is where a fast move really ends the position.
- **Risk** is size × |mark − exit| ÷ equity. **Margin share** is margin ÷ equity.
- **Suggested size** is min(risk % × equity ÷ |mark − exit|, max margin % × equity ÷ margin per unit).

Both limits are per user (`/settings risk 0.1–20`, `/settings maxmargin 1–100`). New users
get `LIQGUARD_RISK_PER_TRADE_PCT` and `LIQGUARD_MAX_MARGIN_PCT` (defaults 2 and 25). One
consolidated 📏 alert lists newly oversized positions.

### Portfolio stress test and correlated liquidation (`risk/correlation.py`, `risk/stress.py`)

- **Factors.** Each coin has a beta to BTC and a correlation to the market (`FACTORS`; crypto
  and stock-perp defaults for unknown symbols). Two positions on the same underlying, e.g.
  `ETHUSDT` and Hyperliquid `ETH`, have correlation 1.
- **Shock.** The market moves m %, and every cross position in the account moves β × m %. The
  account is liquidated when wallet + PnL ≤ the maintenance margin of the shocked positions
  (Binance tiers / Hyperliquid model, as in `true_liquidation`).
- **Shocks shown.** −5 / −10 / −20 % and +5 / +10 / +20 %, each with "margin left" or
  **LIQUIDATED**.
- **Account liquidation move.** Found by scan plus bisection, both down and up. It is shown
  with coins moving by their beta and with "all move equally" for comparison.
- **Platform vs LiqGuard liquidation.** For each cross position, LiqGuard shows the price at the
  account's adverse liquidation move next to the exchange's displayed price (others frozen).
  The gap is shown in points of distance; isolated positions keep their own price.
- **Alerts.** A 🧲 stress alert fires when the first liquidation move drops to ≤ 10 % (warning),
  ≤ 5 % (critical) or ≤ 2.5 % (emergency), each band once. It also fires when the
  concentration flag first appears.

### Concentration (`risk/stress.py`)

A book is flagged when ≥ 75 % of notional points one way across ≥ 2 positions whose average
pairwise correlation is ≥ 0.6. It also reports **effective independent bets**,
(Σ|wᵢ|)² ÷ ΣΣ wᵢ wⱼ ρᵢⱼ: five correlated longs behave like about 1.4 bets.

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
| `models/` | Plain dataclasses: `Position` (incl. margin mode), `MarginAccount`, `MaintenanceBracket`, `FundingInfo`, `User`, `ExchangeBinding`, `Alert`, thresholds |
| `risk/` | Pure calculations: true liquidation (`liquidation.py`), funding drift and pre-funding warnings (`funding.py`), taker fees (`fees.py`), rescue simulator (`rescue.py`), `RiskContext`, advice, portfolio overview |
| `ai/` | `MetaLLMClient` (OpenAI-compatible Muse client) with deterministic mock fallback; validated rescue explanations (`ai/rescue.py`) |
| `exchanges/` | `ExchangeAdapter` interface (`get_positions`, `get_account`); Binance, Hyperliquid and demo adapters; public funding feeds; Binance key-permission policy |
| `alerts/` | Per-user cooldown/dedup (`AlertManager`) and the polling `PositionMonitor` (delivers through a `Notifier`) |
| `protect/` | Tier-1 protective actions, guardrails, kill switch, per-user audit logger |
| `simulation/` | Deterministic market simulation and `SimulationRegistry` (one per user + one public demo) |
| `storage/` | SQLite schema, envelope encryption (`KeyManager`), per-user repositories |
| `auth/` | Signed session tokens, one-time login links, Telegram Login Widget verification |
| `core/` | `AppContext` (wires everything once), `PositionService.snapshot_for(user)` (positions + accounts + funding), `MarketDataService` (shared public funding cache), testnet action executor |
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
          • 🎬 5 longs, one wallet (cross margin)
```

The **Liquidation Simulation** gradually moves prices toward liquidation, firing alerts at each threshold (20%, 10%, 5%) with action buttons.
It runs a cross-margined book:
- BTC and SOL share one Binance cross wallet, so SOL is dragged toward liquidation by BTC's losses.
- ETH sits in a Hyperliquid cross account.
- Demo funding settles on a simulated clock (12 min per step, 8 h per run): BTC 8 h, SOL 4 h, ETH hourly.
- A ⏳ pre-funding warning fires before BTC's large settlement.
- Every alert carries a 🛟 rescue plan.
- The demo clock starts on **Saturday 20:00 UTC** and runs through the low-liquidity hours,
  so tiers widen (weekend ×1.15, thin hours ×1.10) from each symbol's demo wick profile.
  SOL has a fat-wick profile, so its tiers widen most.
- Stop-loss checks run from the first step:
  - BTC's stop sits beyond liquidation (🛑 critical).
  - ETH has no stop.
  - SOL's stop turns *too close* and then *beyond liquidation* as the shared wallet drains.
  - Each comes with Muse's fix.
- `TSLAUSDT` is a stock perp held over the weekend, so its tiers widen ×1.5 because NYSE is
  closed. It is profitable while its ADL rank climbs to 4/5 and then 5/5 (⚡ warnings).
- Critical and emergency alerts start wake-up reminders (one demo minute is 1.5 s), so you see
  reminder → URGENT → FINAL unless you tap **✅ I'm awake**.

**🎬 5 longs, one wallet** is the scenario from the [user feedback](#real-user-feedback):

- **The book.** BTC, ETH, SOL, DOGE and AVAX are 10× longs (~$76k notional) sharing one
  $9,000 Binance cross wallet. Over the run each falls by its beta × an 8 % market drop.
- **At the start.** Every platform liquidation line looks 50 %+ away. The stress test already
  shows that −10 % liquidates the account, and that a ~8.8 % correlated drop is enough. 🧲
  Concentration ("100 % long across 5 coins… like 1.4 independent bets"), 📏 sizing (all five
  oversized, with suggested sizes) and 🛑 stop-loss alerts (four have no stop) arrive first.
- **Stress bands.** The 🧲 stress alert escalates warning → critical → emergency as the account
  liquidation move shrinks below 10 / 5 / 2.5 %. The platform lines still show 10 %+.
- **Tier crossings.** Positions cross tiers within a step or two of each other. Each batch
  arrives as one 🎯 **N POSITIONS AT RISK — HANDLE IN THIS ORDER** alert. SOL ranks first: its
  wicks are fattest, so its time to liquidation at typical volatility is shortest.
- **BTC's stop.** BTC's stop at $55,500 starts far above its displayed liquidation price. As
  the wallet drains, the liquidation price climbs through it (🛑 *beyond liquidation*).
- **End of the run.** It stops just short of account liquidation (< 1 % correlated move left).

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

Two views on one page. The **scenario switch** next to *Simulate market* picks the demo book
(*Mixed book* or *5 longs, one wallet*); it is disabled while a run is in progress.

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
| Chart | **Distance** tab: every position's distance to liquidation over simulated time, with shaded warning/critical/emergency bands and ▲ funding settlements. **Per-symbol** tabs: mark price vs the true liquidation line (which steps when the shared wallet pays funding), plus the price where each tier fires |
| Positions | Venue, side, leverage, distance gauge with tier markers, mark and **true** liquidation price (cross wallet, other positions' PnL and maintenance), funding rate, next payment and 8 h / 24 h drift "at current rate", 🛟 Rescue button |
| Rescue simulator | Add margin vs reduce vs hedge side by side: new liquidation, distance, fees, realized loss, 24 h funding, capital, recommended option, Muse trade-offs. Opens automatically on each alert |
| Funding & wallets | Cross wallet per venue, each position's rate and next settlement, drift, recent settlements |
| Concentration banner | Appears when exposure is concentrated one way across correlated coins: share, coins, average correlation, effective independent bets, and the market move that liquidates the account |
| Priority triage | Every position ranked by urgency score with a bar, severity, distance, time to liquidation at typical volatility, σ₅ₘ and notional; the top at-risk row is highlighted **🎯 Handle this first** |
| Portfolio stress test | Per cross account: the market move that liquidates it (beta-weighted, and "all move equally"), and −5 / −10 / −20 % (and upside) cells showing margin left or **LIQUIDATED** |
| Platform liq vs real liq | For each position: the platform's displayed liquidation price and distance next to LiqGuard's correlated liquidation price and distance, as two bars, with a red "N pts closer than shown" badge |
| Position sizing | Equity and your limits; for each position, loss at exit vs risk limit and margin share vs margin limit as meters, "cut N %" and the suggested size |
| Wake-up banner | Unacknowledged critical/emergency alerts, reminders sent / cap, **✅ I'm awake** buttons; shakes on each reminder |
| Wick stress & adaptive tiers | Session strip (demo clock, weekend / thin hours / US stocks closed). For each position: max wicks away, 1m and 5m max and typical wicks, base → effective tiers with reasons |
| Stop-loss sanity & ADL | For each position: stop status, stop vs liquidation price, gap vs required gap, the computed fixes with Muse's pick ★ and explanation, and 1–5 ADL lights (Hyperliquid: n/a) |
| Alert timeline | 🎯 Consolidated triage alerts (positions in order), 🧲 stress/concentration, 📏 sizing, adaptive-tier alerts (with priority rank) (effective vs base tier, wicks away, reasons), 🛑 stop-loss, ⚡ ADL, ⏳ pre-funding, ⏰ reminders and ✅ acknowledgements, showing which channels (Web, Telegram) received each one |
| Muse advice | Per-alert explanation, exact "add $X margin" / "reduce Y%", and a rescue mini-comparison |
| Audit log | Simulation start/stop, every alert, every advice card, and protective-action steps from Telegram (sanitized) |

### How the simulation works

`liqguard/simulation/market.py` (`MarketSimulation`) is a deterministic price path
(seeded, about 30 s of wall time, 8 simulated hours); `SimulationRegistry` keeps one per user plus the public demo.

- **Cross accounts.** Positions sit in cross accounts, so their distances are coupled. One
  position per account follows a distance path, and the others follow a price path; their
  distance is whatever the shared wallet implies.
- **Liquidation each step.** Liquidation prices are computed every step with `true_liquidation`.
- **Funding.** Demo funding settlements, labelled *demo* (live rates are used for real
  accounts), are deducted from the wallets. They emit `funding` events and ⏳ pre-funding alerts.
- **Alert order.** The run shows warning → critical → emergency in sequence. No tier is skipped,
  and each fires once per position.
- **Rescue plans.** Every alert gets a rescue plan.

All numbers come from `risk/`; Muse (or the mock) only writes the wording. The web dashboard and `TelegramSimulationBridge` both
subscribe to the owning simulation's event stream.

### API

The same simulation routes exist under `/api/demo/*` (public, demo data) and `/api/me/*`
(session cookie required, the signed-in user's data only).

| Endpoint | Method | Description |
|----------|--------|-------------|
| `…/snapshot` | GET | Full state: overview, summary, history, alerts, advice, audit |
| `…/portfolio` | GET | `/risk` data layer (`PortfolioRiskOverview.to_dict()`) + Muse summary |
| `…/alerts` · `…/advice` · `…/audit` | GET | Timeline, advice cards, audit log |
| `…/rescue?symbol=` | GET | Rescue plan for a simulated position + Muse explanation |
| `/api/me/rescue/live?symbol=&venue=` | GET | Rescue plan for one of *your* live (or demo-book) positions |
| `…/simulate` · `…/simulate/stop` · `…/simulate/reset` | POST | Simulation controls (CSRF header required) |
| `…/ack` | POST | `{"ack_id": …}`: "I'm awake" for one of this simulation's alerts (CSRF header required) |
| `…/insights` | GET | Priority triage, per-account stress test, platform vs correlated liquidation, concentration and sizing for this simulation |
| `…/scenario` | POST | `{"scenario": "mixed" \| "cross_longs"}`: switch the demo book (CSRF header required; 409 while running) |
| `/api/me/session` · `/api/me/account` | GET | Session info; your bindings (masked), positions overview, per-position protection (wicks, tiers, stop, ADL), portfolio insights (triage, stress, liq comparison, concentration, sizing), pending live reminders, audit log |
| `/api/me/live/ack` | POST | Acknowledge one of *your* live monitor's reminders (CSRF header required) |
| `/api/health` · `/api/auth/config` | GET | Status (incl. funding and wick feed freshness/errors); login options (no secrets) |
| `/auth/login?token=` · `/auth/telegram` | GET | One-time link / Telegram widget sign-in |
| `/auth/logout` | POST | Sign out (CSRF header required) |
| `/ws/demo` · `/ws/me` | WebSocket | Snapshot on connect, then `tick` / `alert` / `advice` / `funding` / `summary` / `audit` / `status` / `escalation` / `ack` events |

### Security

- **No trading from the web.** The only write endpoints are simulation controls (including the scenario switch), alert acknowledgements and logout. Binding accounts and confirming actions stay in Telegram (tier 1, testnet only).
- **No secrets in the browser.** Responses carry no API keys, secrets or ciphertexts; bindings show a masked hint. Tests check this.
- **Hardened by default.** Binds to `127.0.0.1`, strict Content-Security-Policy, `X-Frame-Options: DENY`, `Cache-Control: no-store`. Chart.js (jsDelivr) and, on `/login`, the Telegram widget script are the only external scripts.

## Testing

```bash
python3 -m pytest liqguard/tests/ -q
```

| File | Covers |
|------|--------|
| `test_liquidation.py` | Hand-derived liquidation prices, Binance tiers (`cum` continuity, equity = maintenance at LP), cross-margin effects, Hyperliquid formula equivalence, closed form = bisection |
| `test_funding.py` | Binance/Hyperliquid feeds with mocked HTTP (4 h/8 h intervals, failures), settlements, drift, pre-funding warnings, refresh service, adapter accounts |
| `test_rescue.py` | Add margin / reduce / hedge math and costs, recommendation, Muse validation, sim funding + rescue, Telegram buttons, monitor, dashboard endpoints and isolation |
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
| `test_protection.py` | Wick stats and 5m aggregation, kline pagination and request weights, rate limiter, wick cache (incremental refresh, errors), sessions and stock-market hours, adaptive floors and caps, stop-loss statuses and fixes, Muse fix validation, order parsers, open-orders / ADL adapters, ADL lights, escalation tracker and service, monitor integration |
| `test_telegram_protection.py` | `/positions` and `/risk` protection lines, `/settings` (buttons, text args, validation, per-user), "I'm awake" for live and simulated alerts (owner only) |
| `test_dashboard_protection.py` | Snapshot protection data, stop/ADL/escalation events, `…/ack` (CSRF, id validation, once only), live ack and cross-user isolation, account protection payload |
| `test_portfolio_risk.py` | Correlation factors, account surplus and liquidation move, ±5/10/20 % shocks, platform vs correlated liquidation gap, concentration and effective bets, sizing (equity, stop vs liquidation exit, both limits), triage ordering and labels, consolidated monitor alert and dedupe, the 5-long scenario, Telegram `/risk` / `/positions` / `/settings` / `/demo`, dashboard insights and scenario endpoints, per-user isolation, env defaults |

532 tests at the time of writing.

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
| `LIQGUARD_TAKER_FEE_BINANCE` / `_HYPERLIQUID` | No | Taker fee rates used in rescue costs (default: 0.0005 / 0.00045) |
| `LIQGUARD_FUNDING_REFRESH` | No | Seconds between funding-rate refreshes (default: 60, minimum 10) |
| `LIQGUARD_PREFUNDING_LEAD_MINUTES` | No | How long before settlement pre-funding warnings fire (default: 30) |
| `LIQGUARD_WICK_DAYS` | No | Wick stress-test window in days (default: 30, max 30) |
| `LIQGUARD_WICK_REFRESH` | No | Seconds between kline refreshes per symbol (default: 3600, minimum 300) |
| `LIQGUARD_LOW_LIQUIDITY_HOURS_UTC` | No | Low-liquidity window, `start-end` in UTC hours (default: `21-1`) |
| `LIQGUARD_STOP_BUFFER_PCT` | No | Minimum gap between stop and liquidation price, % (default: 0.5; the typical 5m wick is used when larger) |
| `LIQGUARD_ESCALATION_INTERVAL_MINUTES` | No | Default reminder interval for new users (default: 2, minimum 0.5) |
| `LIQGUARD_ESCALATION_MAX_REMINDERS` | No | Default reminder cap for new users (default: 5) |
| `LIQGUARD_RISK_PER_TRADE_PCT` | No | Default max loss per position at its exit, % of equity, for new users (default: 2, range 0.1–20) |
| `LIQGUARD_MAX_MARGIN_PCT` | No | Default max margin per position, % of equity, for new users (default: 25, range 1–100) |

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
6. Positions panel: BTC's **true** liquidation rises as SOL loses, because they share a Binance cross wallet
7. Around +7h: ⏳ **pre-funding** alert (BTC is about to pay a large funding payment); the Funding card shows the 8 h / 24 h drift "at current rate"
8. Rescue simulator panel: add margin vs reduce vs hedge with new liquidation, distance and full cost; Muse explains the trade-offs
9. Click the BTCUSDT tab: price closing in on the true liquidation line, which steps up at the ▲ settlement
10. In Telegram, tap 🛟 on the alert, then *Hedge*, to show the per-option detail

### Scene 2b: Wicks, Stops, ADL, Wake-up (45s)
1. Before pressing Simulate: the **Wick stress** card shows "Saturday 20:00 UTC · Weekend ×1.15" and each position's "X max wicks away". TSLA's tiers are widened because US stocks are closed
2. The first timeline entries are 🛑 **STOP-LOSS**: BTC's stop is beyond liquidation, and the Stop-loss card shows Muse's ★ fix ("keep the stop, reduce 3.5 %" or "move the stop to $…"). ETH has no stop. The same messages arrive in Telegram
3. Mid-run: ⚡ **ADL 4/5**, then **5/5**, on the profitable TSLA position
4. BTC turns critical. Don't tap anything: ⏰ reminders arrive in Telegram and the dashboard banner (1/5 → URGENT → FINAL)
5. Tap **✅ I'm awake** in Telegram. The dashboard shows *acknowledged from telegram* and the reminders stop
6. `/settings`: change the interval to 5 min and point at the notification-exception tip

### Scene 2c: Five longs, one wallet (45s)
1. Switch the dashboard scenario to **5 longs, one wallet** (or `/demo` → 🎬 5 longs, one wallet)
2. Before pressing Simulate: the 🧲 banner reads "100 % long across 5 correlated coins". **Platform liq vs real liq** shows BTC's line 52 % away, but LiqGuard's correlated line only ~9 % away. The stress test shows −10 % = LIQUIDATED
3. Position sizing: all five are oversized for a $9k wallet, each with a suggested size
4. Simulate: the 🧲 stress alert escalates while the platform lines still look far away. Then one 🎯 alert in Telegram lists the five positions in the order to handle them, with **Handle this first** on SOL
5. `/risk` in Telegram shows the same triage, stress table and gap

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
- [x] Cross-margin true liquidation price, live funding, rescue simulator
- [x] Wick stress test, adaptive tiers, stop-loss sanity check, ADL lights, wake-up escalation
- [x] Priority triage, position sizing, correlated portfolio stress test, concentration warning
- [ ] Rolling correlations and betas from klines instead of fixed factors
- [ ] Exchange holiday calendars for stock perps; Hyperliquid ADL once an API exposes it
- [ ] Portfolio-margin / multi-assets mode and Binance Hedge Mode positions as input
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
