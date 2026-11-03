# LiqGuard demo script (3:00)

A shot-by-shot plan for a three-minute screen recording. Every button label below is the real English label in the bot and dashboard.

**Before recording**

- Telegram open on the bot chat, language set to English (🌐 Language / 語言), no account bound, so everything runs on simulated positions.
- Browser open on <https://152-42-176-155.sslip.io>, logged in, English selected.
- `META_API_KEY` set on the server so the Muse pill reads **Muse · live**; without it the same flow runs with **Muse · mock** text.

| Time | Screen | Action | Voiceover (English) |
|---|---|---|---|
| 0:00–0:08 | Black title card, then a price chart wicking down through a liquidation line | Hold the frame | "It's 3 a.m. Bitcoin wicks four percent. Your exchange liquidates you in seconds, and the only notification is the one telling you it already happened." |
| 0:08–0:20 | Exchange app with five open cross-margin longs | Slow scroll over the positions | "Leveraged traders don't lose to bad ideas. They lose to the minutes they were asleep, and to the stop-loss they thought would save them." |
| 0:20–0:30 | Telegram, bot chat | Send `/start`; the inline menu appears | "LiqGuard is a Telegram bot that watches your Binance and Hyperliquid positions. Everything is one tap away from this menu." |
| 0:30–0:40 | Telegram, demo menu | Tap **🎬 Demo**, then **🎬 5 longs, one wallet** | "No account needed for the demo. This one puts five correlated longs in one cross-margin wallet and lets the market fall." |
| 0:40–0:55 | Telegram, alerts arriving | Let the tier alerts stream in; pause on one with **🚑 Handle this first**, **✅ I'm awake**, **😴 Snooze 30 min** | "As prices walk toward liquidation, alerts escalate by tier. Each one says how far you are from liquidation, and Meta Muse adds one line saying what to do first." |
| 0:55–1:05 | Telegram, same alert | Tap **✅ I'm awake** | "Critical alerts keep waking you up until you confirm. One tap stops the reminders, so the next message is the one that matters." |
| 1:05–1:20 | Telegram, risk overview | Tap **📊 Risk overview**; highlight the `💡 … · ✦ Muse` line | "The risk overview ranks every position by distance to liquidation. The numbers are computed by our own risk engine. The single recommendation line underneath is written by Muse, and it's tagged so you always know which is which." |
| 1:20–1:40 | Telegram, rescue calculator | Tap **🚑 Rescue {symbol} first** (or **🛟 Rescue sim** from a position) | "The rescue calculator compares adding margin, reducing size and hedging, with the dollar cost of each including fees and 24 hours of funding. Muse explains the trade-off in plain words." |
| 1:40–1:55 | Telegram, stop-loss check | Back to the menu, tap **🛡 Stop-loss check**; pause on **🛑 Stop price is beyond liquidation: it will never fire** | "And the check most traders never run: is your stop-loss actually in front of your liquidation price? Here it isn't. That stop would never fire." |
| 1:55–2:05 | Browser, web dashboard | Show the overview; point at the **Muse · live** pill | "The same engine powers a web dashboard. Same positions, same alerts, in real time over a websocket." |
| 2:05–2:20 | Browser, dashboard | Click **Simulate market**; watch a summary first appear tagged **✦ Muse (mock)**, then get replaced and tagged **✦ Muse** | "You see the rule-based text instantly. A few seconds later, the live Muse answer replaces it in place. You never wait on the model, and if it's slow or down, the rule text stays." |
| 2:20–2:35 | Browser, dashboard | Click **中文**; the whole UI, including the Muse line, switches to Traditional Chinese; click **EN** to switch back | "Every screen, alert and Muse answer is available in English and Traditional Chinese, in the bot and on the web." |
| 2:35–2:45 | README architecture diagram | Show the mermaid diagram | "Under the hood: one Python process runs the Telegram bot and the FastAPI dashboard. A deterministic risk engine computes every number. Meta Muse, model muse-spark-1.3, only turns them into words, with a ten-minute cache and a rule-based fallback." |
| 2:45–2:53 | Telegram, 🔑 Bind account screen | Show the read-only key / wallet-address prompt | "It only ever asks for a read-only Binance key or a Hyperliquid wallet address. Keys are envelope-encrypted per user and never echoed back. It cannot trade your mainnet account." |
| 2:53–3:00 | Title card: LiqGuard logo and dashboard URL | Hold | "LiqGuard: wake up before the liquidation, not after. Try the demo now." |
