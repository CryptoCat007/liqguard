"use strict";

const COLORS = {
  safe: "#34d399", warning: "#fbbf24", critical: "#fb923c", emergency: "#f43f5e", funding: "#a78bfa",
  grid: "rgba(148,163,184,0.08)", tick: "#5b667a", text: "#8a96ab",
};
const SYMBOL_COLORS = {
  BTCUSDT: "#f7931a", ETH: "#8b9cff", ETHUSDT: "#8b9cff", SOLUSDT: "#14f195", TSLAUSDT: "#e2e8f0",
  DOGEUSDT: "#d6b84a", AVAXUSDT: "#e84142",
};
const FALLBACK_COLORS = ["#38bdf8", "#f472b6", "#a3e635", "#facc15"];
const TIERS = ["warning", "critical", "emergency"];
const LEVEL_ICON = {
  warning: "⚠️", critical: "🚨", emergency: "🆘", funding: "⏳", stop: "🛑", adl: "⚡",
  triage: "🎯", sizing: "📏", correlation: "🧲",
};
const NEW_COUNTS = () => ({ warning: 0, critical: 0, emergency: 0, funding: 0, stop: 0, adl: 0, triage: 0, sizing: 0, correlation: 0 });
const STOP_LABEL = {
  ok: ["✅ Stop fires first", "safe"], partial: ["⚠️ Partial stop", "warning"], no_stop: ["🛑 No stop-loss", "critical"],
  too_close: ["⚠️ Stop too close to liq", "warning"], beyond_liquidation: ["🛑 Stop beyond liq", "emergency"],
};
const FIX_ICON = { move_stop: "🎯", add_margin: "💰", reduce: "✂️" };
const OPTION_META = {
  add_margin: { icon: "💰", label: "Add margin" },
  reduce: { icon: "✂️", label: "Reduce" },
  hedge: { icon: "🛡", label: "Hedge" },
};
const MODE = location.pathname.startsWith("/me") ? "me" : "demo";
const API = MODE === "me" ? "/api/me" : "/api/demo";
const CSRF_HEADERS = { "X-LiqGuard-CSRF": "1" };

const S = {
  history: {}, tierPrices: {}, thresholds: { warning: 20, critical: 10, emergency: 5 },
  totalSteps: 40, tick: 0.75, step: 0, state: "idle", order: [], view: "distance",
  overview: null, counts: NEW_COUNTS(), lastSeverity: {},
  minutesPerStep: null, wallets: {}, fundingEvents: [], fundingSteps: {}, rescue: null,
  protection: {}, session: null, escalation: null, pending: {}, stopMuse: {},
  insights: null, scenario: null, scenarios: [],
};
let chart = null;
let ws = null;
let retry = 0;

/* ---------------- helpers ---------------- */

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "style") Object.assign(node.style, v);
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}
const $ = (id) => document.getElementById(id);

function symbolColor(sym) {
  if (SYMBOL_COLORS[sym]) return SYMBOL_COLORS[sym];
  const i = S.order.indexOf(sym);
  return FALLBACK_COLORS[(i < 0 ? 0 : i) % FALLBACK_COLORS.length];
}
function alpha(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
}
function money(v, opts = {}) {
  if (v == null || Number.isNaN(v)) return "—";
  const sign = opts.signed && v > 0 ? "+" : v < 0 ? "−" : "";
  return `${sign}$${Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: opts.dp ?? 0, minimumFractionDigits: opts.dp ?? 0 })}`;
}
function price(v) {
  if (v == null) return "—";
  const dp = v >= 1000 ? 1 : v >= 10 ? 2 : 4;
  return `$${v.toLocaleString(undefined, { minimumFractionDigits: dp, maximumFractionDigits: dp })}`;
}
function pct(v, dp = 1) { return v == null ? "—" : `${v.toFixed(dp)}%`; }
function clock(iso) {
  return new Date(iso).toLocaleTimeString(undefined, { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
function tiersOf(sym) { return S.protection[sym]?.thresholds?.effective || S.thresholds; }
function chartTiers() {
  const eff = Object.values(S.protection).map((p) => p.thresholds?.effective).filter(Boolean);
  if (!eff.length) return S.thresholds;
  return Object.fromEntries(TIERS.map((t) => [t, Math.min(...eff.map((e) => e[t]))]));
}
function tierFor(distance, sym) {
  const t = sym ? tiersOf(sym) : S.thresholds;
  if (distance == null) return "safe";
  if (distance <= t.emergency) return "emergency";
  if (distance <= t.critical) return "critical";
  if (distance <= t.warning) return "warning";
  return "safe";
}
function venueLabel(v) { return v === "hyperliquid" ? "Hyperliquid" : v === "binance" ? "Binance" : v === "demo" ? "Demo" : v; }
function duration(minutes) {
  if (minutes == null) return "—";
  const m = Math.max(0, Math.round(minutes));
  return m >= 60 ? `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m` : `${m}m`;
}
function distText(d, noLiq) { return noLiq || d == null ? "no liq" : pct(d); }
function empty(glyph, title, body) {
  return el("div", { class: "empty" }, el("div", { class: "glyph" }, glyph), el("b", {}, title), el("div", {}, body));
}

/* ---------------- snapshot & events ---------------- */

function applySnapshot(snap) {
  S.thresholds = snap.thresholds;
  S.totalSteps = snap.total_steps;
  S.tick = snap.tick_interval || 0.75;
  S.step = snap.step;
  S.state = snap.state;
  S.history = snap.history || {};
  S.tierPrices = snap.tier_prices || {};
  S.safetyTarget = snap.safety_target_pct;
  S.order = Object.keys(S.history);
  S.counts = NEW_COUNTS();
  S.lastSeverity = {};
  S.minutesPerStep = snap.minutes_per_step || null;
  S.wallets = snap.wallets || {};
  S.fundingEvents = snap.funding_events || [];
  S.fundingSteps = {};
  for (const [sym, pts] of Object.entries(S.history)) {
    S.fundingSteps[sym] = new Set(pts.filter((p) => p.funding_paid_usd != null).map((p) => p.step));
  }
  S.rescue = null;
  S.protection = snap.protection || {};
  S.session = snap.session || null;
  S.escalation = snap.escalation || null;
  S.pending = Object.fromEntries((snap.escalation?.pending || []).map((a) => [a.id, a]));
  S.stopMuse = {};
  S.insights = snap.insights || null;
  S.summary = snap.summary || null;
  S.scenario = snap.scenario || null;
  S.scenarios = snap.scenarios || [];
  renderScenarios();
  if (S.view !== "distance" && !S.order.includes(S.view)) S.view = "distance";

  $("musePill").textContent = snap.muse_mode === "live" ? "Muse · live" : "Muse · mock";
  $("musePill").classList.toggle("live", snap.muse_mode === "live");
  renderChannels(snap.channels || []);

  renderOverview(snap.overview);
  renderSummary(snap.summary);

  $("timeline").replaceChildren();
  (snap.alerts || []).forEach((a) => addAlert(a, { quiet: true }));
  (snap.escalation_log || []).forEach((e) => addEscalation(e, { quiet: true }));
  if (!snap.alerts?.length) $("timeline").append(empty("🔕", "No alerts yet", "Press Simulate market to walk prices toward liquidation."));
  updateCounts();

  $("advice").replaceChildren();
  (snap.advice || []).forEach((c) => addAdvice(c));
  if (!snap.advice?.length) $("advice").append(empty("✦", "No advice yet", "Muse explains each alert with exact margin and size numbers."));

  $("audit").replaceChildren();
  (snap.audit || []).forEach((e) => addAudit(e));
  if (!snap.audit?.length) $("audit").append(empty("📋", "Audit log is empty", "Alerts, suggestions and Telegram action steps are recorded here."));

  renderProtection();
  renderInsights();
  renderWake();
  buildTabs();
  buildChart();
  renderFunding();
  buildRescueTabs();
  renderRescue();
  setStatus({ state: snap.state, step: snap.step, total_steps: snap.total_steps });
}

function handle(msg) {
  const { type, data } = msg;
  switch (type) {
    case "snapshot":
    case "reset":
      applySnapshot(data);
      break;
    case "tick":
      onTick(data);
      break;
    case "alert":
      addAlert(data);
      break;
    case "advice":
      addAdvice(data);
      break;
    case "summary":
      if (data && data.step != null && S.step != null && data.step !== S.step) break;
      S.summary = data;
      renderSummary(data);
      break;
    case "audit":
      addAudit(data);
      break;
    case "funding":
      S.fundingEvents.push(data);
      renderFunding();
      break;
    case "status":
      setStatus(data);
      break;
    case "escalation":
      addEscalation(data);
      break;
    case "ack":
      onAck(data);
      break;
  }
}

function onTick(data) {
  S.step = data.step;
  for (const [sym, p] of Object.entries(data.prices)) {
    (S.history[sym] ||= []).push(p);
    S.tierPrices[sym] = p.tier_prices;
    if (p.funding_paid_usd != null) (S.fundingSteps[sym] ||= new Set()).add(p.step);
  }
  if (data.wallets) S.wallets = data.wallets;
  if (data.protection) S.protection = data.protection;
  if (data.session) S.session = data.session;
  if (data.insights) S.insights = data.insights;
  if (data.summary) S.summary = data.summary;
  if (data.escalation) {
    S.escalation = data.escalation;
    S.pending = Object.fromEntries((data.escalation.pending || []).map((a) => [a.id, { ...S.pending[a.id], ...a }]));
    renderWake();
  }
  renderProtection();
  renderOverview(data.overview);
  renderSummary(S.summary);
  renderInsights();
  refreshLiveToasts();
  renderFunding();
  updateChart(data);
  setProgress();
}

/* ---------------- overview ---------------- */

function renderChannels(channels) {
  const names = channels.map((c) => (c === "web" ? "Web" : c === "telegram" ? "Telegram" : c));
  $("channelsPill").textContent = `Alerts → ${names.length ? names.join(" + ") : "none"}`;
  $("channelsPill").classList.toggle("live", channels.includes("telegram"));
}

function renderOverview(ov) {
  if (!ov) return;
  S.overview = ov;
  const sum = ov.summary, rm = ov.risk_metrics, ex = ov.exposure;

  const level = rm.overall_risk_level;
  const banner = $("banner");
  banner.className = `banner card lvl-${level}`;
  $("riskLevel").textContent = level.toUpperCase();

  $("kpiNotional").textContent = money(sum.total_notional_value);
  $("kpiVenues").textContent = `${sum.total_positions} positions · ${(ov.venues || []).map(venueLabel).join(" + ")}`;
  $("kpiMargin").textContent = money(sum.total_margin_used);
  const eff = sum.total_margin_used ? sum.total_notional_value / sum.total_margin_used : 0;
  $("kpiLeverage").textContent = `${eff.toFixed(1)}× effective leverage`;
  const pnl = $("kpiPnl");
  pnl.textContent = money(sum.total_unrealized_pnl, { signed: true });
  pnl.className = `kpi-value ${sum.total_unrealized_pnl >= 0 ? "pos" : "neg"}`;
  $("kpiNet").textContent = money(ex.net_exposure, { signed: true });
  $("kpiLongShort").textContent = `L ${money(ex.long_exposure)} · S ${money(ex.short_exposure)}`;

  const closest = [...ov.positions].sort((a, b) => a.distance_to_liquidation_pct - b.distance_to_liquidation_pct)[0];
  const minD = rm.min_distance_to_liquidation;
  const md = $("kpiMinDist");
  md.textContent = pct(minD);
  md.className = `kpi-value c-${tierFor(minD, closest?.symbol)}`;
  $("kpiMinDistSym").textContent = closest ? `${closest.symbol} ${closest.side}` : "—";
  $("kpiSafety").textContent = money(rm.total_margin_needed_for_safety);
  $("kpiSafetySub").textContent = rm.total_margin_needed_for_safety > 0
    ? `to restore a ${S.safetyTarget ?? 30}% buffer` : "all above safety target";

  const riskiestSym = S.summaryRiskiest || S.insights?.focus?.symbol || ov.riskiest_position?.symbol;
  const rp = ov.positions.find((p) => p.symbol === riskiestSym);
  if (rp) setRiskiest(rp.symbol, rp.side, rp.distance_to_liquidation_pct);

  renderPositions(ov.positions);
}

function renderSummary(summary) {
  if (!summary) {
    S.summaryRiskiest = null;
    $("headline").textContent = "Muse is summarizing the portfolio…";
    $("summaryTag").hidden = true;
    return;
  }
  $("headline").textContent = summary.headline;
  $("summaryTag").hidden = !summary.mock;
  S.summaryRiskiest = summary.riskiest_symbol;
  const why = summary.priority_reason && summary.priority_basis !== "closest to liquidation" && summary.priority_basis !== "only position"
    ? summary.priority_reason : "";
  $("riskiestWhy").textContent = why;
  $("riskiestWhy").hidden = !why;
  if (S.overview) {
    const rp = S.overview.positions.find((p) => p.symbol === summary.riskiest_symbol);
    if (rp) setRiskiest(rp.symbol, rp.side, rp.distance_to_liquidation_pct);
  }
}

function setRiskiest(sym, side, dist) {
  const chip = $("riskiest");
  chip.replaceChildren(
    el("span", { style: { color: symbolColor(sym) } }, sym), " ",
    el("span", { class: `side side-${side}` }, side.toUpperCase()), " ",
    el("span", { class: `c-${tierFor(dist, sym)}` }, pct(dist)),
  );
}

function renderPositions(positions) {
  const bySym = Object.fromEntries(positions.map((p) => [p.symbol, p]));
  const order = S.order.length ? S.order.filter((s) => bySym[s]) : positions.map((p) => p.symbol);
  const box = $("positions");
  const scale = Math.max(35, S.safetyTarget ? S.safetyTarget + 5 : 35);

  box.replaceChildren(...order.map((sym) => {
    const p = bySym[sym];
    const d = p.distance_to_liquidation_pct;
    const tier = tierFor(d, sym);
    const color = COLORS[tier];
    const prev = S.lastSeverity[sym];
    S.lastSeverity[sym] = tier;
    const th = tiersOf(sym);

    const ticks = TIERS.map((t) => el("div", {
      class: "gauge-tick", title: `${t} ${th[t]}%`,
      style: { left: `${(Math.min(th[t], scale) / scale) * 100}%`, background: COLORS[t] },
    }));
    const labels = TIERS.map((t) => el("span", { style: { left: `${(Math.min(th[t], scale) / scale) * 100}%` } }, `${+th[t].toFixed(1)}`));

    const node = el("div", {
      class: `position${S.view === sym ? " selected" : ""}${prev && prev !== tier ? " flash" : ""}`,
      style: { "--flash": alpha(color, 0.5) },
      title: "Show price vs liquidation", onclick: () => setView(sym),
    },
      el("div", { class: "pos-top" },
        el("span", { class: "pos-sym", style: { color: symbolColor(sym) } }, sym),
        el("span", { class: `venue venue-${p.venue}` }, venueLabel(p.venue)),
        el("span", { class: `side side-${p.side}` }, p.side.toUpperCase()),
        el("span", { class: "lev" }, `${p.leverage}×`),
        el("span", { class: `pos-dist c-${tier}` }, pct(d)),
      ),
      el("div", { class: "gauge" },
        el("div", { class: "gauge-fill", style: { width: `${Math.min(d / scale, 1) * 100}%`, background: `linear-gradient(90deg, ${alpha(color, 0.55)}, ${color})` } }),
        ...ticks,
      ),
      el("div", { class: "gauge-labels" }, ...labels),
      el("div", { class: "pos-meta" },
        el("span", {}, `mark ${price(p.mark_price)}`),
        el("span", {}, `liq ${price(p.true_liquidation?.price ?? p.liquidation_price)}`),
        el("span", { class: p.unrealized_pnl >= 0 ? "pos" : "neg" }, money(p.unrealized_pnl, { signed: true })),
      ),
      liqDetail(p),
      fundingDetail(p.funding),
      protectionChips(S.protection[sym]),
      insightChips(sym),
      el("div", { class: "pos-actions" }, toolButtons(sym, p.venue, false)),
    );
    return node;
  }));
}

function liqDetail(p) {
  const t = p.true_liquidation;
  if (!t) return null;
  const parts = t.margin_mode === "cross"
    ? [`cross wallet ${money(t.wallet_balance)}`, `other PnL ${money(t.other_upnl, { signed: true })}`, `other maint. ${money(t.other_maintenance)}`]
    : [`isolated margin ${money(t.wallet_balance)}`];
  const ex = t.exchange_price, diff = ex && t.price ? Math.abs(ex - t.price) / ex : 0;
  if (diff > 0.0005) parts.push(`exchange ${price(ex)}`);
  return el("div", { class: "pos-detail" },
    el("span", { class: `mode mode-${t.margin_mode}` }, t.margin_mode.toUpperCase()),
    el("span", {}, `true liq ${t.price ? price(t.price) : "none"} · ${parts.join(" · ")}`),
  );
}

function fundingDetail(f) {
  if (!f) return null;
  const pays = f.next_payment_usd > 0;
  const drift = (f.drift || []).map((d) => `${d.horizon_hours}h → ${distText(d.distance_pct, d.liquidation_price == null)}`).join(" · ");
  return el("div", { class: "pos-detail funding-line" },
    el("span", { class: "mode mode-funding" }, `${f.rate_pct >= 0 ? "+" : ""}${f.rate_pct.toFixed(4)}%/${f.interval_hours}h`),
    el("span", {}, `${pays ? "pays" : "receives"} ${money(Math.abs(f.next_payment_usd), { dp: 2 })} in ${duration(f.minutes_to_settlement)} · drift ${drift} `,
      el("i", {}, f.label)),
  );
}

/* ---------------- timeline / advice / audit ---------------- */

function clearEmpty(box) { const e = box.querySelector(".empty"); if (e) e.remove(); }

function addFundingAlert(a, opts) {
  const f = a.funding;
  const box = $("timeline");
  box.prepend(el("li", { class: "tl-item tl-funding" },
    el("span", { class: "tl-node" }),
    el("div", { class: "tl-head" },
      el("span", { class: "lvl lvl-funding-b" }, "PRE-FUNDING"),
      el("span", { class: "tl-sym", style: { color: symbolColor(a.symbol) } }, a.symbol),
      el("span", { class: `side side-${a.side}` }, a.side.toUpperCase()),
      el("span", { class: "tl-time" }, clock(a.timestamp)),
    ),
    el("div", { class: "tl-body" },
      "Pays ", el("b", {}, money(f.payment_usd, { dp: 2 })), ` in ${duration(f.minutes_to_settlement)} (${f.rate_pct.toFixed(4)}%/${f.interval_hours}h) — distance `,
      el("b", {}, pct(f.distance_before_pct)), " → ", el("b", {}, pct(f.distance_after_pct)),
      ` · ${f.buffer_share_pct.toFixed(1)}% of the margin buffer`,
    ),
    el("div", { class: "chips" },
      ...(a.channels || []).map((c) => el("span", { class: `chip chip-${c}` }, c === "telegram" ? "Telegram ✓" : c === "web" ? "Web ✓" : c)),
      el("button", { class: "mini-btn", type: "button", onclick: () => loadRescue(a.symbol) }, "🛟 Rescue"),
    ),
  ));
  if (!opts.quiet) {
    toast("⏳", `PRE-FUNDING · ${a.symbol}`, `Pays ${money(f.payment_usd, { dp: 2 })} in ${duration(f.minutes_to_settlement)} → ${pct(f.distance_after_pct)} from liquidation`, COLORS.funding);
  }
}

function addAlert(a, opts = {}) {
  const box = $("timeline");
  clearEmpty(box);
  S.counts[a.level] = (S.counts[a.level] || 0) + 1;
  updateCounts();
  if (a.level === "funding") { addFundingAlert(a, opts); return; }
  if (a.level === "stop") { addStopAlert(a, opts); return; }
  if (a.level === "adl") { addAdlAlert(a, opts); return; }
  if (a.level === "triage") { addTriageAlert(a, opts); return; }
  if (a.level === "sizing") { addSizingAlert(a, opts); return; }
  if (a.level === "correlation") { addCorrelationAlert(a, opts); return; }
  if (a.ack_id && !opts.quiet && !S.pending[a.ack_id] && S.escalation?.enabled !== false) {
    S.pending[a.ack_id] = { id: a.ack_id, symbol: a.symbol, severity: a.level, reminders_sent: 0, max_reminders: S.escalation?.max_reminders ?? 5, distance_pct: a.distance_pct };
    renderWake();
  }
  const chips = (a.channels || []).map((c) => el("span", { class: `chip chip-${c}` }, c === "telegram" ? "Telegram ✓" : c === "web" ? "Web ✓" : c));
  box.prepend(el("li", { class: `tl-item tl-${a.level}` },
    el("span", { class: "tl-node" }),
    el("div", { class: "tl-head" },
      el("span", { class: `lvl lvl-${a.level}-b` }, a.level.toUpperCase()),
      el("span", { class: "tl-sym", style: { color: symbolColor(a.symbol) } }, a.symbol),
      el("span", { class: `side side-${a.side}` }, a.side.toUpperCase()),
      el("span", { class: "tl-time" }, clock(a.timestamp)),
    ),
    el("div", { class: "tl-body" },
      "Crossed ", el("b", {}, pct(a.threshold_pct, 2)),
      a.base_threshold_pct != null && Math.abs(a.base_threshold_pct - a.threshold_pct) > 0.01 ? el("span", { class: "faint" }, ` (base ${a.base_threshold_pct}%)`) : "",
      " — now ", el("b", {}, pct(a.distance_pct)),
      ` from liquidation · mark ${price(a.mark_price)} vs liq ${price(a.liquidation_price)}`,
      a.wicks?.max_wicks_away != null ? el("div", { class: "tl-sub" }, `🕯 ${a.wicks.max_wicks_away.toFixed(1)} max ${a.wicks.worst_interval} wicks from liquidation`) : null,
      a.adaptive_reasons?.length ? el("div", { class: "tl-sub" }, `Tiers widened: ${a.adaptive_reasons.join(" · ")}`) : null,
      a.liq_comparison?.gap_pct >= 1 ? el("div", { class: "tl-sub" },
        `Correlated liq ${price(a.liq_comparison.correlated_price)} is `, el("b", { class: "c-critical" }, `${a.liq_comparison.gap_pct.toFixed(1)} pts closer`),
        ` than the platform line`) : null,
    ),
    el("div", { class: "chips" }, priorityChip(a.priority), a.consolidated ? el("span", { class: "chip faint" }, "in one triage alert") : null,
      ...chips, a.ack_id ? ackButton(a.ack_id, a.symbol) : null),
  ));
  if (a.consolidated) opts = { ...opts, quiet: true };
  if (!opts.quiet) {
    const sent = (a.channels || []).map((c) => (c === "web" ? "Web" : c === "telegram" ? "Telegram" : c)).join(" + ");
    toast(LEVEL_ICON[a.level], `${a.level.toUpperCase()} · ${a.symbol}`,
      `${pct(a.distance_pct)} from liquidation — sent to ${sent || "dashboard"}`, COLORS[a.level], `tier:${a.symbol}`);
    if (a.level === "emergency") {
      document.body.classList.add("flash-emergency");
      setTimeout(() => document.body.classList.remove("flash-emergency"), 1400);
    }
  }
}

function updateCounts() {
  $("cntTriage").textContent = S.counts.triage;
  $("cntSizing").textContent = S.counts.sizing;
  $("cntCorr").textContent = S.counts.correlation;
  $("cntWarning").textContent = S.counts.warning;
  $("cntCritical").textContent = S.counts.critical;
  $("cntEmergency").textContent = S.counts.emergency;
  $("cntFunding").textContent = S.counts.funding;
  $("cntStop").textContent = S.counts.stop;
  $("cntAdl").textContent = S.counts.adl;
}

function tlHead(label, cls, a) {
  return el("div", { class: "tl-head" },
    el("span", { class: `lvl ${cls}` }, label),
    el("span", { class: "tl-sym", style: { color: symbolColor(a.symbol) } }, a.symbol),
    a.side ? el("span", { class: `side side-${a.side}` }, a.side.toUpperCase()) : null,
    el("span", { class: "tl-time" }, clock(a.timestamp)),
  );
}

function addStopAlert(a, opts) {
  const s = a.stop, m = a.explanation;
  S.stopMuse[a.symbol] = m;
  const [label] = STOP_LABEL[s.status] || [s.status];
  $("timeline").prepend(el("li", { class: "tl-item tl-stop" },
    el("span", { class: "tl-node" }),
    tlHead("STOP-LOSS", "lvl-stop-b", a),
    el("div", { class: "tl-body" },
      el("b", {}, label), s.stop_price ? ` · stop ${price(s.stop_price)}` : "", ` · liq ${price(s.liquidation_price)}`,
      m ? el("div", { class: "tl-muse" }, el("span", { class: "muse-glyph" }, "✦ "), m.explanation) : null,
    ),
    el("div", { class: "chips" }, ...(a.channels || []).map(channelChip),
      el("button", { class: "mini-btn", type: "button", onclick: () => loadRescue(a.symbol) }, "🛟 Rescue")),
  ));
  if (!opts.quiet) toast("🛑", `STOP-LOSS · ${a.symbol}`, m?.headline || label, COLORS.emergency);
  renderStops();
}

function addAdlAlert(a, opts) {
  const adl = a.adl;
  $("timeline").prepend(el("li", { class: "tl-item tl-adl" },
    el("span", { class: "tl-node" }),
    tlHead(`ADL ${adl.lights}/5`, "lvl-adl-b", a),
    el("div", { class: "tl-body" }, adlLights(adl.lights), " ",
      `Profitable (${money(a.unrealized_pnl, { signed: true })}) and near the front of the auto-deleverage queue — could be closed if a counterparty is liquidated past the insurance fund.`),
    el("div", { class: "chips" }, ...(a.channels || []).map(channelChip)),
  ));
  if (!opts.quiet) toast("⚡", `ADL ${adl.lights}/5 · ${a.symbol}`, `In profit ${money(a.unrealized_pnl, { signed: true })} — consider taking some off`, COLORS.funding);
}

function channelChip(c) {
  return el("span", { class: `chip chip-${c}` }, c === "telegram" ? "Telegram ✓" : c === "web" ? "Web ✓" : c);
}

function priorityChip(pr) {
  if (!pr || (pr.of || 0) < 2) return null;
  return pr.label
    ? el("span", { class: "chip chip-first", title: pr.reason }, `🎯 ${pr.label}`)
    : el("span", { class: "chip", title: pr.reason }, `priority #${pr.rank} of ${pr.of}`);
}

function addTriageAlert(a, opts) {
  const items = a.items || [];
  $("timeline").prepend(el("li", { class: `tl-item tl-triage tl-${a.severity || "warning"}` },
    el("span", { class: "tl-node" }),
    tlHead(`${items.length} AT RISK · HANDLE IN ORDER`, `lvl-triage-b`, a),
    el("ol", { class: "tl-order" }, ...items.map((t) => el("li", { class: t.label ? "first" : "" },
      el("b", { style: { color: symbolColor(t.symbol) } }, t.symbol), " ",
      el("span", { class: `c-${t.severity || "safe"}` }, `${(t.severity || "ok").toUpperCase()} ${pct(t.distance_pct)}`),
      el("span", { class: "faint" }, ` · ${minutesText(t.minutes_to_liq)}`),
      t.label ? el("span", { class: "first-tag" }, ` 🎯 ${t.label}`) : null,
    ))),
    el("div", { class: "chips" }, ...(a.channels || []).map(channelChip),
      a.ack_id ? ackButton(a.ack_id, a.symbol) : null,
      el("button", { class: "mini-btn", type: "button", onclick: () => loadRescue(a.symbol) }, `🛟 Rescue ${a.symbol}`)),
  ));
  if (a.ack_id && !opts.quiet && !S.pending[a.ack_id] && S.escalation?.enabled !== false) {
    S.pending[a.ack_id] = { id: a.ack_id, symbol: a.symbol, severity: a.severity, reminders_sent: 0, max_reminders: S.escalation?.max_reminders ?? 5, distance_pct: a.distance_pct };
    renderWake();
  }
  if (!opts.quiet) {
    const [title, body] = triageToastText(items.map((t) => t.symbol), a.focus);
    toast("🎯", title, body, COLORS[a.severity] || COLORS.critical, "triage");
    if (a.severity === "emergency") {
      document.body.classList.add("flash-emergency");
      setTimeout(() => document.body.classList.remove("flash-emergency"), 1400);
    }
  }
}

function addSizingAlert(a, opts) {
  const checks = a.sizing || [];
  $("timeline").prepend(el("li", { class: "tl-item tl-sizing" },
    el("span", { class: "tl-node" }),
    tlHead(`OVERSIZED · ${checks.length}`, "lvl-sizing-b", a),
    el("div", { class: "tl-body" },
      checks.map((c) => el("div", {},
        el("b", { style: { color: symbolColor(c.symbol) } }, c.symbol), ` loses ${pct(c.risk_pct)} of equity at its ${c.exit_kind} → suggest `,
        el("b", {}, `${fmtSize(c.suggested_size)}`), el("span", { class: "faint" }, ` (now ${fmtSize(c.size)})`)))),
    el("div", { class: "chips" }, ...(a.channels || []).map(channelChip)),
  ));
  if (!opts.quiet) toast("📏", "Position size too large", `${checks.map((c) => c.symbol).join(", ")}: over your ${checks[0]?.max_risk_pct}% risk per trade`, COLORS.warning, "sizing");
}

function addCorrelationAlert(a, opts) {
  const st = a.stress, conc = a.concentration || {};
  const move = st?.liq_move_down_pct;
  $("timeline").prepend(el("li", { class: `tl-item tl-corr tl-${a.severity || "warning"}` },
    el("span", { class: "tl-node" }),
    tlHead(`STRESS ${a.severity ? a.severity.toUpperCase() : "· CONCENTRATION"}`, "lvl-corr-b", a),
    el("div", { class: "tl-body" },
      move != null ? ["A ", el("b", { class: `c-${a.severity || "warning"}` }, pct(move)), ` correlated market drop liquidates the ${venueLabel(st.venue)} account`, el("span", { class: "faint" }, " (at alert time)")] : "",
      conc.flagged ? el("div", { class: "tl-sub" }, `🧲 ${conc.message}`) : null,
    ),
    el("div", { class: "chips" }, ...(a.channels || []).map(channelChip)),
  ));
  if (!opts.quiet) {
    toast("🧲", "Portfolio stress test", stressToastText() || conc.message, COLORS[a.severity] || COLORS.warning, "stress");
  }
}

/* ---------------- portfolio: triage / stress / liq comparison / sizing ---------------- */

function minutesText(m) {
  if (m == null) return "no liq";
  if (m < 120) return `~${Math.round(m)} min at typical vol`;
  if (m < 72 * 60) return `~${(m / 60).toFixed(1)} h at typical vol`;
  return `~${Math.round(m / 1440)} d at typical vol`;
}
function fmtSize(v) {
  if (v == null) return "—";
  return Math.abs(v) >= 1000 ? v.toLocaleString(undefined, { maximumFractionDigits: 0 }) : `${+v.toPrecision(4)}`;
}

function insightChips(sym) {
  const ins = S.insights;
  if (!ins) return null;
  const chips = [];
  const t = (ins.triage || []).find((x) => x.symbol === sym);
  if (t?.label) chips.push(el("span", { class: "pchip chip-first", title: t.reason }, `🎯 ${t.label}`));
  else if (t?.at_risk && t.of > 1) chips.push(el("span", { class: "pchip", title: t.reason }, `#${t.rank} of ${t.of}`));
  const c = (ins.comparisons || []).find((x) => x.symbol === sym);
  if (c?.gap_pct >= 1) chips.push(el("span", { class: "pchip c-critical", title: "LiqGuard correlated liquidation vs the platform's line" }, `🧲 real liq ${pct(c.correlated_distance_pct)} away`));
  const z = (ins.sizing || []).find((x) => x.symbol === sym);
  if (z?.status === "oversized") chips.push(el("span", { class: "pchip c-warning", title: (z.reasons || []).join(" · ") }, `📏 size → ${fmtSize(z.suggested_size)}`));
  return chips.length ? el("div", { class: "pchips" }, ...chips) : null;
}

function renderInsights() {
  renderConcentration();
  renderTriage();
  renderPortfolioStress();
  renderLiqComparison();
  renderSizing();
}

function renderConcentration() {
  const conc = S.insights?.concentration;
  const box = $("concBanner");
  box.hidden = !conc?.flagged;
  if (!conc?.flagged) return;
  const move = accountLiqMove();
  $("concTitle").textContent = `${pct(conc.dominant_share_pct, 0)} ${conc.dominant_side} across ${conc.dominant_count} correlated coins`;
  $("concSub").textContent = `${conc.symbols.join(" · ")} move together (avg correlation ${conc.avg_correlation.toFixed(2)}).`
    + (move ? ` A ${pct(move.pct)} market ${move.dir} liquidates the ${venueLabel(move.venue)} account.` : "");
  $("concBets").textContent = conc.effective_bets == null ? "—" : conc.effective_bets.toFixed(1);
}

function renderTriage() {
  const box = $("triage");
  const items = S.insights?.triage || [];
  const atRisk = items.filter((t) => t.at_risk).length;
  $("triageTag").textContent = atRisk ? `${atRisk} AT RISK` : "ALL CLEAR";
  $("triageTag").className = `tag${atRisk ? " tag-hot" : " tag-ro"}`;
  const focus = S.insights?.focus;
  const why = focus && items.length > 1 ? focus.why : "";
  $("triageWhy").hidden = !why;
  $("triageWhy").replaceChildren(...(why ? [el("b", {}, focus.label ? "🎯 Why first: " : "Ranked #1: "), why] : []));
  if (!items.length) { box.replaceChildren(empty("🎯", "No positions", "Positions are ranked here when you have more than one.")); return; }
  const maxScore = Math.max(...items.map((t) => t.score), 1);
  box.replaceChildren(...items.map((t) => el("li", { class: `tri${t.label ? " first" : ""}${t.at_risk ? "" : " calm"}` },
    el("div", { class: "tri-rank" }, t.rank),
    el("div", { class: "tri-main" },
      el("div", { class: "tri-head" },
        el("b", { style: { color: symbolColor(t.symbol) } }, t.symbol),
        el("span", { class: `side side-${t.side}` }, t.side.toUpperCase()),
        el("span", { class: `tri-sev c-${t.severity || "safe"}` }, t.severity ? t.severity.toUpperCase() : "OK"),
        t.label ? el("span", { class: "first-tag" }, `🎯 ${t.label}`) : null,
      ),
      el("div", { class: "tri-bar" }, el("i", { style: { width: `${(t.score / maxScore) * 100}%`, background: COLORS[t.severity || "safe"] } })),
      el("div", { class: "tri-meta" },
        el("span", {}, `${pct(t.distance_pct)} from liq`),
        el("span", {}, minutesText(t.minutes_to_liq)),
        el("span", { title: `σ5m ${pct(t.sigma_5m_pct, 2)}${t.sigma_source === "default" ? " (default)" : " (from 5m wicks)"}` },
          t.typical_moves == null ? "no liq" : `${t.typical_moves.toFixed(1)} typical 5m moves`),
        el("span", {}, `${money(t.notional)} notional`),
      ),
    ),
    el("div", { class: "tri-score", title: "Urgency score 0–100" }, t.score.toFixed(0)),
  )));
}

function renderPortfolioStress() {
  const box = $("pstress");
  const accounts = S.insights?.stress || [];
  if (!accounts.length) { box.replaceChildren(empty("📉", "No cross-margin account", "The stress test covers positions that share one cross wallet.")); return; }
  box.replaceChildren(...accounts.map((st) => {
    const down = st.shocks.filter((x) => x.market_move_pct < 0).sort((a, b) => b.market_move_pct - a.market_move_pct);
    const up = st.shocks.filter((x) => x.market_move_pct > 0);
    const move = st.liq_move_down_pct, uni = st.uniform_liq_move_down_pct;
    const band = move == null ? "safe" : move <= 2.5 ? "emergency" : move <= 5 ? "critical" : move <= 10 ? "warning" : "safe";
    const cell = (x) => el("div", { class: `ps-cell${x.liquidated ? " dead" : ""}` },
      el("div", { class: "ps-move" }, `${x.market_move_pct > 0 ? "+" : ""}${x.market_move_pct}%`),
      el("div", { class: "ps-out" }, x.liquidated ? "LIQUIDATED" : `${money(x.margin_left)} left`),
    );
    return el("div", { class: "ps-acct" },
      el("div", { class: "ps-head" },
        el("span", { class: `venue venue-${st.venue}` }, venueLabel(st.venue)),
        el("span", { class: "faint" }, `${st.symbols.length} cross positions · equity ${money(st.equity)} · maint. ${money(st.maintenance)}`),
      ),
      el("div", { class: `ps-big c-${band}` },
        el("div", { class: "ps-n" }, move == null ? "> 95%" : `−${pct(move)}`),
        el("div", {}, el("b", {}, "market move liquidates the account"),
          el("div", { class: "faint" }, `coins moving by their beta${uni != null ? ` · ${pct(uni)} if all move equally` : ""}`
            + (st.liq_move_up_pct != null ? ` · +${pct(st.liq_move_up_pct)} on the upside` : ""))),
      ),
      el("div", { class: "ps-grid" }, ...down.map(cell)),
      up.length ? el("div", { class: "ps-grid ps-up" }, ...up.map(cell)) : null,
    );
  }));
}

function renderLiqComparison() {
  const box = $("liqcmp");
  const comps = (S.insights?.comparisons || []).filter((c) => c.platform_price != null || c.correlated_price != null);
  if (!comps.length) { box.replaceChildren(empty("📐", "No liquidation prices", "Positions without a liquidation price have nothing to compare.")); return; }
  const scale = Math.max(...comps.map((c) => c.platform_distance_pct || 0), 10);
  box.replaceChildren(...comps.map((c) => {
    const gap = c.gap_pct;
    const hot = gap != null && gap >= 1;
    const bar = (d, cls) => el("div", { class: `lc-bar ${cls}` }, el("i", { style: { width: `${Math.min((d || 0) / scale, 1) * 100}%` } }), el("span", {}, pct(d)));
    return el("div", { class: `lc-row${hot ? " hot" : ""}` },
      el("div", { class: "lc-head" },
        el("b", { style: { color: symbolColor(c.symbol) } }, c.symbol),
        el("span", { class: `mode mode-${c.margin_mode}` }, c.margin_mode.toUpperCase()),
        hot ? el("span", { class: "lc-gap" }, `⚠️ ${gap.toFixed(1)} pts closer than shown`) : el("span", { class: "faint" }, "matches the platform"),
      ),
      el("div", { class: "lc-line" }, el("span", { class: "lc-label" }, "Platform shows"), el("span", { class: "lc-price" }, price(c.platform_price)), bar(c.platform_distance_pct, "plat")),
      el("div", { class: "lc-line" }, el("span", { class: "lc-label" }, "LiqGuard (together)"), el("span", { class: "lc-price" }, price(c.correlated_price)), bar(c.correlated_distance_pct, hot ? "real hot" : "real")),
      c.correlated_market_move_pct != null ? el("div", { class: "faint lc-note" }, `≈ a ${pct(Math.abs(c.correlated_market_move_pct))} market ${c.correlated_market_move_pct < 0 ? "drop" : "rise"}`) : null,
    );
  }));
}

function renderSizing() {
  const box = $("sizing");
  const checks = S.insights?.sizing || [];
  const over = checks.filter((c) => c.status === "oversized").length;
  $("sizingTag").textContent = checks.length ? (over ? `${over} OVERSIZED` : "SIZED OK") : "—";
  $("sizingTag").className = `tag${over ? " tag-hot" : " tag-ro"}`;
  if (checks.length) {
    const c0 = checks[0];
    $("sizingSub").textContent = `Equity ${money(c0.equity)} · limits ${c0.max_risk_pct}% loss per trade at the exit, ${c0.max_margin_pct}% margin per position${MODE === "me" ? " (/settings in Telegram)" : ""}`;
  }
  if (!checks.length) { box.replaceChildren(empty("📏", "No account equity", "Sizing needs the account's wallet balance.")); return; }
  box.replaceChildren(...checks.map((c) => el("div", { class: `sz-row${c.status === "oversized" ? " over" : ""}` },
    el("div", { class: "sz-head" },
      el("b", { style: { color: symbolColor(c.symbol) } }, c.symbol),
      el("span", { class: "faint" }, `${fmtSize(c.size)} · ${money(c.notional)} · ${c.notional_x_equity?.toFixed(1)}× equity`),
      el("span", { class: c.status === "oversized" ? "c-warning sz-status" : "c-safe sz-status" }, c.status === "oversized" ? `cut ${pct(c.reduce_pct, 0)}` : "fits"),
    ),
    el("div", { class: "sz-meters" },
      meter("Loss at exit", c.risk_pct, c.max_risk_pct, `${money(c.loss_at_exit_usd)} at ${c.exit_kind}`),
      meter("Margin share", c.margin_pct, c.max_margin_pct, `${money(c.margin)} margin`),
    ),
    c.status === "oversized" ? el("div", { class: "sz-suggest" }, "Suggested size ", el("b", {}, fmtSize(c.suggested_size)), ` (≈ ${money(c.suggested_notional)})`) : null,
  )));
}

function meter(label, value, limit, sub) {
  const ratio = value == null || !limit ? 0 : value / limit;
  const cls = ratio > 1 ? "over" : ratio > 0.75 ? "near" : "ok";
  return el("div", { class: `meter ${cls}` },
    el("div", { class: "meter-top" }, el("span", {}, label), el("b", {}, value == null ? "—" : `${pct(value)} / ${limit}%`)),
    el("div", { class: "meter-bar" }, el("i", { style: { width: `${Math.min(ratio, 1) * 100}%` } }), el("u", { style: { left: `${Math.min(1 / Math.max(ratio, 1), 1) * 100}%` } })),
    el("div", { class: "faint meter-sub" }, sub),
  );
}

function renderScenarios() {
  const sel = $("scenarioSelect");
  if (!S.scenarios.length) { sel.parentElement.hidden = true; return; }
  sel.parentElement.hidden = false;
  sel.replaceChildren(...S.scenarios.map((sc) => el("option", { value: sc.key, selected: sc.key === S.scenario, title: sc.description }, `🎬 ${sc.title}`)));
  sel.disabled = S.state === "running";
}

async function switchScenario(key) {
  const sel = $("scenarioSelect");
  sel.disabled = true;
  try {
    const r = await fetch(`${API}/scenario`, {
      method: "POST", credentials: "same-origin",
      headers: { ...CSRF_HEADERS, "Content-Type": "application/json" }, body: JSON.stringify({ scenario: key }),
    });
    if (r.status === 401) { toLogin(); return; }
    if (r.status === 409) toast("⏹", "Simulation running", "Stop the run before switching the demo book.", COLORS.warning);
    else if (!r.ok) throw new Error(`${r.status}`);
    else {
      const sc = S.scenarios.find((x) => x.key === key);
      toast("🎬", sc?.title || key, sc?.description || "", COLORS.safe);
    }
  } catch (err) {
    toast("⚠️", "Request failed", "Could not switch the scenario.", COLORS.critical);
  } finally {
    renderScenarios();
  }
}

/* ---------------- wake-up escalation ---------------- */

function ackButton(id, sym) {
  const pending = S.pending[id] ? !S.pending[id].acknowledged
    : Object.values(S.pending).some((p) => p.symbol === sym && !p.acknowledged);
  return el("button", {
    class: `mini-btn ack-btn${pending ? "" : " done"}`, type: "button", "data-ack": id, "data-sym": sym, disabled: !pending,
    onclick: (ev) => { ev.stopPropagation(); acknowledge(id); },
  }, pending ? "✅ I'm awake" : "✓ acknowledged");
}

function refreshAckButtons() {
  document.querySelectorAll("[data-ack]").forEach((b) => b.replaceWith(ackButton(b.dataset.ack, b.dataset.sym)));
}

async function acknowledge(id) {
  const res = await postJson(`${API}/ack`, { ack_id: id });
  if (!res) return;
  for (const p of Object.values(S.pending)) if (p.id === id || p.symbol === res.symbol) p.acknowledged = true;
  renderWake();
  refreshAckButtons();
}

function addEscalation(e, opts = {}) {
  const p = S.pending[e.ack_id];
  if (p) { p.reminders_sent = e.reminder; p.urgency = e.urgency; }
  else if (!opts.quiet) S.pending[e.ack_id] = { id: e.ack_id, symbol: e.symbol, severity: e.severity, reminders_sent: e.reminder, max_reminders: e.max_reminders, urgency: e.urgency, distance_pct: e.distance_pct };
  const box = $("timeline");
  clearEmpty(box);
  box.prepend(el("li", { class: `tl-item tl-esc tl-esc-${e.urgency}` },
    el("span", { class: "tl-node" }),
    tlHead(`${e.urgency === "final" ? "FINAL " : e.urgency === "urgent" ? "URGENT " : ""}REMINDER ${e.reminder}/${e.max_reminders}`, `lvl-esc-b lvl-esc-${e.urgency}`, e),
    el("div", { class: "tl-body" },
      `${e.severity.toUpperCase()} alert unacknowledged for ${Math.round(e.minutes_since_alert)} min · now `, el("b", {}, pct(e.distance_pct)), " from liquidation"),
    el("div", { class: "chips" }, ...(e.channels || []).map(channelChip), ackButton(e.ack_id, e.symbol)),
  ));
  if (!opts.quiet) {
    toast("⏰", `${e.urgency === "final" ? "FINAL WAKE-UP" : "WAKE UP"} · ${e.symbol}`, `Reminder ${e.reminder}/${e.max_reminders} — tap “I'm awake” to stop`, COLORS[e.severity] || COLORS.critical);
    const wake = $("wake");
    wake.classList.remove("ring");
    void wake.offsetWidth;
    wake.classList.add("ring");
    renderWake();
  }
}

function onAck(data) {
  for (const p of Object.values(S.pending)) if (p.id === data.ack_id || p.symbol === data.symbol) p.acknowledged = true;
  const box = $("timeline");
  box.prepend(el("li", { class: "tl-item tl-ack" },
    el("span", { class: "tl-node" }),
    tlHead("ACKNOWLEDGED", "lvl-ack-b", data),
    el("div", { class: "tl-body" }, `${data.severity} alert acknowledged from ${data.source} after ${data.reminders_sent} reminder(s) — reminders stopped.`),
  ));
  toast("✅", `Acknowledged · ${data.symbol}`, `From ${data.source} — no more reminders`, COLORS.safe);
  renderWake();
  refreshAckButtons();
}

function renderWake() {
  const items = Object.values(S.pending).filter((a) => !a.acknowledged);
  const wake = $("wake");
  wake.hidden = !items.length;
  if (!items.length) return;
  const esc = S.escalation || {};
  const worst = items.some((a) => a.severity === "emergency") ? "emergency" : "critical";
  wake.className = `wake card wake-${worst}${wake.classList.contains("ring") ? " ring" : ""}`;
  $("wakeTitle").textContent = `${items.length} alert${items.length > 1 ? "s" : ""} waiting for “I'm awake”`;
  $("wakeSub").textContent = esc.interval_minutes
    ? `Reminders every ${esc.interval_minutes} min (demo: ${esc.seconds_per_minute}s per minute), up to ${esc.max_reminders}, louder each time · same button in Telegram`
    : "Reminders repeat until acknowledged";
  $("wakeList").replaceChildren(...items.map((a) => el("div", { class: `wake-item c-${a.severity}` },
    el("b", { style: { color: symbolColor(a.symbol) } }, a.symbol),
    el("span", {}, `${a.severity.toUpperCase()} · ${pct(a.distance_pct)}`),
    el("span", { class: "faint" }, `${a.reminders_sent || 0}/${a.max_reminders} sent`),
    ackButton(a.id, a.symbol),
  )));
}

/* ---------------- wick stress / stops / ADL ---------------- */

function adlLights(n, max = 5) {
  return el("span", { class: `adl adl-${n}`, title: `ADL ${n}/${max}` },
    ...Array.from({ length: max }, (_, i) => el("i", { class: i < n ? "on" : "" })));
}

function wicksClass(n) {
  if (n == null) return "faint";
  return n <= 1 ? "c-emergency" : n <= 3 ? "c-critical" : n <= 6 ? "c-warning" : "c-safe";
}

function protectionChips(pr) {
  if (!pr) return null;
  const chips = [];
  if (pr.wicks?.max_wicks_away != null) {
    chips.push(el("span", { class: `pchip ${wicksClass(pr.wicks.max_wicks_away)}`, title: `Deepest ${pr.wicks.worst_interval} wick in 30 days` },
      `🕯 ${pr.wicks.max_wicks_away.toFixed(1)} max wicks`));
  }
  if (pr.stop) {
    const [label, tone] = STOP_LABEL[pr.stop.status] || [pr.stop.status, "muted"];
    chips.push(el("span", { class: `pchip c-${tone}` }, label));
  } else chips.push(el("span", { class: "pchip faint" }, "orders n/a"));
  if (pr.adl?.supported) {
    chips.push(el("span", { class: `pchip${pr.adl.warn ? " c-critical" : ""}`, title: pr.adl.note || "" }, "ADL ", adlLights(pr.adl.lights || 0)));
  } else if (pr.adl) chips.push(el("span", { class: "pchip faint", title: pr.adl.note || "" }, "ADL n/a"));
  if (pr.thresholds?.adapted) chips.push(el("span", { class: "pchip", title: pr.thresholds.reasons.join(" · ") }, "📈 adaptive"));
  return el("div", { class: "pchips" }, ...chips);
}

function renderSession() {
  const s = S.session;
  const box = $("session");
  if (!s) { box.replaceChildren(); return; }
  const when = s.sim_time ? new Date(s.sim_time) : null;
  const t = when ? `${s.weekday || ""} ${String(when.getUTCHours()).padStart(2, "0")}:${String(when.getUTCMinutes()).padStart(2, "0")} UTC` : "";
  const stock = Object.values(S.protection).some((p) => p.thresholds?.session?.stock_market_closed);
  box.replaceChildren(
    el("span", { class: "sess-clock" }, "🕒 ", t, el("span", { class: "faint" }, MODE === "me" || S.minutesPerStep ? " · demo clock" : "")),
    el("span", { class: `sess${s.weekend ? " on" : ""}` }, "Weekend ×1.15"),
    el("span", { class: `sess${s.low_liquidity ? " on" : ""}` }, "Thin hours ×1.10"),
    el("span", { class: `sess${stock ? " on" : ""}` }, "US stocks closed ×1.5"),
  );
}

function renderProtection() {
  renderSession();
  renderStress();
  renderStops();
}

function renderStress() {
  const box = $("stress");
  const entries = S.order.map((s) => S.protection[s]).filter(Boolean);
  if (!entries.length) { box.replaceChildren(empty("🕯", "No wick data yet", "Wick stats load from public 1m/5m candles for each open symbol.")); return; }
  const sources = new Set(entries.map((p) => p.wicks?.source).filter(Boolean));
  $("stressTag").textContent = sources.has("demo") ? "DEMO WICKS" : sources.size ? "LIVE KLINES" : "NO KLINES";
  box.replaceChildren(...entries.map((p) => {
    const w = p.wicks, th = p.thresholds;
    const byInt = Object.fromEntries((w?.exposures || []).map((e) => [e.interval, e]));
    const cell = (iv) => {
      const e = byInt[iv];
      if (!e) return el("div", { class: "st-cell faint" }, `${iv} —`);
      return el("div", { class: "st-cell" },
        el("div", { class: "st-iv" }, iv, e.window_days < 29 ? el("span", { class: "faint" }, ` · ${e.window_days.toFixed(1)}d`) : null),
        el("div", {}, el("b", { class: wicksClass(e.max_wicks_away) }, e.max_wicks_away == null ? "—" : `${e.max_wicks_away.toFixed(1)}×`), el("span", { class: "faint" }, ` max ${pct(e.max_pct, 2)}`)),
        el("div", {}, el("b", { class: wicksClass(e.typical_wicks_away / 4) }, e.typical_wicks_away == null ? "—" : `${e.typical_wicks_away.toFixed(0)}×`), el("span", { class: "faint" }, ` typical ${pct(e.typical_pct, 2)}`)),
      );
    };
    const tiers = th ? TIERS.map((t) => el("span", { class: `st-tier c-${t}` },
      `${t[0].toUpperCase()} ${th.base[t]}→`, el("b", {}, `${+th.effective[t].toFixed(1)}%`))) : [];
    return el("div", { class: `st-row${w?.one_max_wick_liquidates ? " danger" : ""}` },
      el("div", { class: "st-sym" },
        el("b", { style: { color: symbolColor(p.symbol) } }, p.symbol),
        el("span", { class: `venue venue-${p.venue}` }, venueLabel(p.venue)),
        el("span", { class: `c-${tierFor(p.distance_pct, p.symbol)}` }, pct(p.distance_pct)),
      ),
      el("div", { class: "st-big" },
        el("div", { class: `st-n ${wicksClass(w?.max_wicks_away)}` }, w?.max_wicks_away == null ? "—" : w.max_wicks_away.toFixed(1)),
        el("div", { class: "faint" }, w?.one_max_wick_liquidates ? "one wick liquidates!" : "max wicks away"),
      ),
      cell("1m"), cell("5m"),
      el("div", { class: "st-tiers" }, ...tiers,
        th?.reasons?.length ? el("div", { class: "st-why faint" }, th.reasons.join(" · ")) : el("div", { class: "st-why faint" }, "base tiers (no widening)")),
    );
  }));
}

function renderStops() {
  const box = $("stops");
  const entries = S.order.map((s) => S.protection[s]).filter(Boolean);
  if (!entries.length) { box.replaceChildren(empty("🛑", "No positions", "Stop-loss checks run against each position's liquidation price.")); return; }
  box.replaceChildren(...entries.map((p) => {
    const s = p.stop, adl = p.adl || {};
    const [label, tone] = s ? (STOP_LABEL[s.status] || [s.status, "muted"]) : ["Orders unavailable", "muted"];
    const muse = S.stopMuse[p.symbol];
    const fixes = s?.needs_warning ? (s.fixes || []) : [];
    return el("div", { class: `sp-row sp-${tone}` },
      el("div", { class: "sp-head" },
        el("b", { style: { color: symbolColor(p.symbol) } }, p.symbol),
        el("span", { class: `venue venue-${p.venue}` }, venueLabel(p.venue)),
        el("span", { class: `sp-status c-${tone}` }, label),
        el("span", { class: "sp-adl", title: adl.note || "" },
          adl.supported ? [adlLights(adl.lights || 0), el("span", { class: adl.warn ? "c-critical" : "faint" }, ` ${adl.lights}/5`)] : el("span", { class: "faint" }, "ADL n/a (Hyperliquid)")),
      ),
      s ? el("div", { class: "sp-line" },
        s.stop_price ? `stop ${price(s.stop_price)} · ` : "",
        `liq ${price(s.liquidation_price)}`,
        s.gap_pct != null ? ` · gap ${s.gap_pct >= 0 ? "" : "−"}${Math.abs(s.gap_pct).toFixed(2)}% (need ≥ ${s.required_gap_pct.toFixed(2)}%)` : ` · need ≥ ${s.required_gap_pct.toFixed(2)}% gap`,
      ) : null,
      fixes.length ? el("div", { class: "sp-fixes" }, ...fixes.map((f) => el("span", {
        class: `sp-fix${muse?.suggested_fix === f.kind ? " best" : ""}`,
      }, `${FIX_ICON[f.kind] || "•"} ${f.title}`, muse?.suggested_fix === f.kind ? el("span", { class: "star" }, " ★ Muse") : null))) : null,
      muse && s?.needs_warning ? el("div", { class: "sp-muse" }, el("span", { class: "muse-glyph" }, "✦ "), muse.explanation) : null,
    );
  }));
}

function addAdvice(c) {
  const box = $("advice");
  clearEmpty(box);
  const actions = [];
  if (c.margin_to_add_usd > 0) {
    actions.push(el("div", { class: "adv-act" },
      el("div", { class: "adv-act-label" }, "Add margin"),
      el("div", { class: "adv-act-value c-safe" }, money(c.margin_to_add_usd, { dp: 2 })),
      el("div", { class: "adv-act-sub" }, `new liq ${price(c.new_liquidation_price)} · ${c.target_distance_pct}% buffer`),
    ));
  }
  if (c.reduce_size_pct > 0) {
    actions.push(el("div", { class: "adv-act" },
      el("div", { class: "adv-act-label" }, "Or reduce size"),
      el("div", { class: "adv-act-value c-warning" }, `${c.reduce_size_pct.toFixed(1)}%`),
      el("div", { class: "adv-act-sub" }, `close ${c.reduce_size_by} ${c.symbol.replace(/USDT$/, "")}`),
    ));
  }
  box.prepend(el("div", { class: `adv adv-${c.level}` },
    el("div", { class: "adv-head" },
      el("span", { class: "adv-sym", style: { color: symbolColor(c.symbol) } }, c.symbol),
      el("span", { class: `lvl lvl-${c.level}-b` }, c.level.toUpperCase()),
      el("span", { class: "tag" }, c.mock ? "MUSE · MOCK" : "MUSE"),
    ),
    c.summary ? el("div", { class: "adv-summary" }, c.summary) : null,
    el("div", { class: "adv-text" }, c.explanation),
    actions.length ? el("div", { class: "adv-actions" }, ...actions) : null,
    c.rescue ? rescueMini(c) : null,
    c.ack_id ? el("div", { class: "chips" }, ackButton(c.ack_id, c.symbol)) : null,
    el("div", { class: "adv-foot" }, "Suggestion only · confirm in Telegram · testnet execution"),
  ));
  if (c.rescue && (!S.rescue || S.rescue.auto)) showRescue(c.symbol, { plan: c.rescue, explanation: c.rescue_explanation }, { auto: true });
}

function rescueMini(c) {
  const plan = c.rescue;
  return el("div", { class: "adv-rescue" },
    el("div", { class: "adv-act-label" }, "Rescue options · cost incl. fees + 24h funding"),
    ...plan.options.map((o) => el("div", { class: `rm-row${o.kind === plan.recommended ? " best" : ""}` },
      el("span", {}, `${OPTION_META[o.kind].icon} ${OPTION_META[o.kind].label}`),
      el("span", {}, distText(o.new_distance_pct, o.no_liquidation)),
      el("span", {}, money(o.costs.total_cost_usd, { dp: 2 })),
      o.kind === plan.recommended ? el("span", { class: "star" }, "★") : el("span", {}),
    )),
    el("button", { class: "mini-btn", type: "button", onclick: () => showRescue(c.symbol, { plan, explanation: c.rescue_explanation }) }, "Open in rescue simulator"),
  );
}

function addAudit(e) {
  const box = $("audit");
  clearEmpty(box);
  box.prepend(el("div", { class: "au" },
    el("div", { class: "au-time" }, clock(e.timestamp)),
    el("div", {},
      el("div", { class: `au-event ev-${e.event}` }, e.event.replace(/_/g, " "),
        e.symbol ? ` · ${e.symbol}` : "", el("span", { class: "src" }, e.source)),
      el("div", { class: "au-detail" }, e.detail),
    ),
  ));
  while (box.children.length > 120) box.lastChild.remove();
}

function triageToastText(order, focus) {
  const [first, ...rest] = order;
  const why = focus && focus.symbol === first && focus.basis && focus.basis !== "closest to liquidation" ? ` (${focus.basis})` : "";
  return [`${order.length} positions at risk`, `Handle ${first} first${why}${rest.length ? ` — then ${rest.join(", ")}` : ""}`];
}

function accountLiqMove() {
  const w = S.insights?.worst_account;
  if (!w || w.first_liquidation_move_pct == null) return null;
  return { pct: Math.abs(w.first_liquidation_move_pct), dir: w.first_liquidation_move_pct < 0 ? "drop" : "rise", venue: w.venue };
}

function stressToastText() {
  const m = accountLiqMove();
  return m ? `A ${pct(m.pct)} correlated market ${m.dir} liquidates the ${venueLabel(m.venue)} account` : null;
}

function refreshLiveToasts() {
  const box = $("toasts");
  const stress = box.querySelector('.toast[data-key="stress"] .toast-body');
  const st = stressToastText();
  if (stress && st) stress.textContent = st;
  const tri = box.querySelector('.toast[data-key="triage"]');
  const atRisk = (S.insights?.triage || []).filter((t) => t.at_risk).map((t) => t.symbol);
  if (tri && atRisk.length > 1) {
    const [title, body] = triageToastText(atRisk, S.insights.focus);
    tri.querySelector(".toast-title").textContent = title;
    tri.querySelector(".toast-body").textContent = body;
  }
}

function toast(icon, title, body, color, key) {
  const box = $("toasts");
  if (key) box.querySelectorAll(`.toast[data-key="${key}"]`).forEach((n) => n.remove());
  const t = el("div", { class: "toast", style: { "--t": color } },
    el("div", {}, icon),
    el("div", {}, el("div", { class: "toast-title" }, title), el("div", { class: "toast-body" }, body)),
  );
  if (key) t.dataset.key = key;
  box.append(t);
  while (box.children.length > 4) box.firstChild.remove();
  setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 320); }, 4200);
}

/* ---------------- funding & wallets ---------------- */

function renderFunding() {
  const wallets = $("wallets");
  const entries = Object.entries(S.wallets || {});
  wallets.replaceChildren(...entries.map(([venue, w]) => el("div", { class: "wallet" },
    el("span", { class: `venue venue-${venue}` }, venueLabel(venue)),
    el("span", { class: "wallet-label" }, "cross wallet"),
    el("b", {}, money(w, { dp: 2 })),
  )));
  wallets.hidden = !entries.length;

  const box = $("funding");
  const positions = (S.overview?.positions || []).filter((p) => p.funding);
  const sources = new Set(positions.map((p) => p.funding.source));
  $("fundingTag").textContent = sources.has("live") ? "LIVE RATES" : "DEMO RATES";
  $("fundingTag").className = `tag${sources.has("live") ? " tag-ro" : ""}`;
  const rows = positions.map((p) => {
    const f = p.funding, pays = f.next_payment_usd > 0;
    return el("div", { class: "fund-row" },
      el("div", { class: "fund-top" },
        el("b", { style: { color: symbolColor(p.symbol) } }, p.symbol),
        el("span", { class: `venue venue-${p.venue}` }, venueLabel(p.venue)),
        el("span", { class: "fund-rate" }, `${f.rate_pct >= 0 ? "+" : ""}${f.rate_pct.toFixed(4)}% / ${f.interval_hours}h`),
        el("span", { class: "fund-apr" }, `${f.annualized_pct.toFixed(1)}% APR`),
      ),
      el("div", { class: "fund-meta" },
        `next settlement in ${duration(f.minutes_to_settlement)} · `,
        el("span", { class: pays ? "neg" : "pos" }, `${pays ? "pays" : "receives"} ${money(Math.abs(f.next_payment_usd), { dp: 2 })}`),
      ),
      el("div", { class: "fund-drift" },
        el("span", {}, "liq distance now ", el("b", {}, pct(p.distance_to_liquidation_pct))),
        ...(f.drift || []).map((d) => el("span", {}, `${d.horizon_hours}h `, el("b", {}, distText(d.distance_pct, d.liquidation_price == null)),
          ` (${d.settlements}× · pool ${money(d.pool_payment_usd, { signed: true })})`)),
        el("i", {}, f.label),
      ),
    );
  });
  const events = S.fundingEvents.slice(-6).reverse().map((e) => el("div", { class: "fund-ev" },
    el("span", { class: "fund-ev-sym", style: { color: symbolColor(e.symbol) } }, `▲ ${e.symbol}`),
    el("span", {}, `${e.payment_usd > 0 ? "paid" : "received"} ${money(Math.abs(e.payment_usd), { dp: 2 })}`),
    el("span", { class: "fund-ev-liq" }, `liq ${price(e.liquidation_before)} → ${price(e.liquidation_after)}`),
  ));
  box.replaceChildren(...rows);
  if (!rows.length) box.append(empty("💸", "No funding data", "Funding rates appear once positions have a rate."));
  if (events.length) box.append(el("div", { class: "account-label fund-ev-head" }, "Recent settlements"), ...events);
}

/* ---------------- rescue simulator ---------------- */

function buildRescueTabs() {
  const tabs = $("rescueTabs");
  const active = S.rescue?.symbol;
  tabs.replaceChildren(...S.order.map((sym) => el("button", {
    class: `tab${active === sym && !S.rescue?.live ? " active" : ""}`, type: "button", role: "tab", onclick: () => loadRescue(sym),
  }, sym)));
}

async function loadRescue(symbol) {
  S.rescue = { symbol, loading: true };
  buildRescueTabs();
  renderRescue();
  $("rescueCard").scrollIntoView({ behavior: "smooth", block: "nearest" });
  try {
    const r = await fetch(`${API}/rescue?symbol=${encodeURIComponent(symbol)}`, { credentials: "same-origin" });
    if (r.status === 401) { toLogin(); return; }
    if (!r.ok) throw new Error(`${r.status}`);
    showRescue(symbol, await r.json());
  } catch (err) {
    S.rescue = { symbol, error: "Could not load rescue options. Try again in a moment." };
    renderRescue();
  }
}

async function loadLiveRescue(symbol, venue) {
  S.rescue = { symbol, loading: true, live: true };
  renderRescue();
  $("rescueCard").scrollIntoView({ behavior: "smooth", block: "nearest" });
  try {
    const r = await fetch(`/api/me/rescue/live?symbol=${encodeURIComponent(symbol)}&venue=${encodeURIComponent(venue)}`, { credentials: "same-origin" });
    if (r.status === 401) { toLogin(); return; }
    if (!r.ok) throw new Error(`${r.status}`);
    showRescue(symbol, await r.json(), { live: true });
  } catch (err) {
    S.rescue = { symbol, live: true, error: "Could not load rescue options for this position." };
    renderRescue();
  }
}

function showRescue(symbol, data, opts = {}) {
  S.rescue = { symbol, plan: data.plan, explanation: data.explanation, auto: !!opts.auto, live: !!opts.live };
  buildRescueTabs();
  renderRescue();
}

function renderRescue() {
  const body = $("rescueBody");
  const r = S.rescue;
  if (!r) {
    body.replaceChildren(empty("🛟", "Pick a position", "Choose a symbol above, tap 🛟 on a position, or wait for the first alert — its rescue plan opens here."));
    return;
  }
  if (r.loading) { body.replaceChildren(empty("⏳", `Simulating ${r.symbol}…`, "Computing add margin, reduce and hedge.")); return; }
  if (r.error) { body.replaceChildren(empty("⚠️", "Rescue unavailable", r.error)); return; }

  const plan = r.plan, cur = plan.current, ex = r.explanation;
  $("rescueSub").textContent = `${r.live ? "Your account · live data" : "Demo position"} · target ${plan.target_distance_pct}% from liquidation · funding ${plan.funding_label}`;
  const trade = Object.fromEntries((ex?.trade_offs || []).map((t) => [t.option, t]));

  const head = el("div", { class: "rs-head" },
    el("span", { class: "pos-sym", style: { color: symbolColor(plan.symbol) } }, plan.symbol),
    el("span", { class: `venue venue-${plan.venue}` }, venueLabel(plan.venue)),
    el("span", { class: `side side-${plan.side}` }, plan.side.toUpperCase()),
    el("span", { class: `mode mode-${plan.margin_mode}` }, plan.margin_mode.toUpperCase()),
    el("span", { class: "rs-now" }, `mark ${price(plan.mark_price)} · liq ${price(cur.price)} · `,
      el("b", { class: `c-${tierFor(cur.distance_pct)}` }, distText(cur.distance_pct, cur.price == null)), " away"),
  );

  const cols = plan.options.map((o) => {
    const best = o.kind === plan.recommended;
    const c = o.costs, t = trade[o.kind];
    return el("div", { class: `rs-opt${best ? " best" : ""}${o.protects_liquidation ? "" : " weak"}` },
      el("div", { class: "rs-opt-head" },
        el("span", { class: "rs-kind" }, `${OPTION_META[o.kind].icon} ${OPTION_META[o.kind].label}`),
        best ? el("span", { class: "tag tag-best" }, "RECOMMENDED") : null,
        !o.protects_liquidation ? el("span", { class: "tag" }, "DOESN'T MOVE LIQ") : null,
      ),
      el("div", { class: "rs-title" }, o.title),
      el("div", { class: "rs-liq" },
        el("div", {}, el("div", { class: "adv-act-label" }, "New liquidation"), el("div", { class: "rs-big" }, o.new_liquidation_price ? price(o.new_liquidation_price) : "none")),
        el("div", {}, el("div", { class: "adv-act-label" }, "Distance"), el("div", { class: `rs-big c-${o.reaches_target ? "safe" : tierFor(o.new_distance_pct)}` }, distText(o.new_distance_pct, o.no_liquidation))),
      ),
      el("dl", { class: "rs-costs" },
        el("dt", {}, "Fees"), el("dd", {}, money(c.fees_usd, { dp: 2 })),
        el("dt", {}, "Realized loss"), el("dd", {}, money(c.realized_loss_usd, { dp: 2 })),
        el("dt", {}, "Funding 24h"), el("dd", {}, money(c.funding_24h_usd, { dp: 2, signed: true })),
        el("dt", { class: "total" }, "Total cost"), el("dd", { class: "total" }, money(c.total_cost_usd, { dp: 2 })),
        el("dt", {}, "Capital needed"), el("dd", {}, money(c.capital_required_usd, { dp: 2 })),
      ),
      t ? el("ul", { class: "rs-pc" },
        ...t.pros.map((x) => el("li", { class: "pro" }, x)),
        ...t.cons.map((x) => el("li", { class: "con" }, x)),
      ) : null,
      o.notes.length ? el("div", { class: "rs-notes" }, o.notes.join(" ")) : null,
    );
  });

  body.replaceChildren(
    head,
    ex ? el("div", { class: "rs-muse" },
      el("span", { class: "muse-glyph" }, "✦"),
      el("div", {}, el("b", {}, ex.headline), el("div", { class: "rs-caveat" }, ex.caveat)),
      el("span", { class: "tag" }, ex.mock ? "MUSE · MOCK" : "MUSE"),
    ) : null,
    plan.already_safe
      ? empty("✅", `Already beyond the ${plan.target_distance_pct}% target`, "No rescue needed right now. Run the simulation to see the options once this position is at risk.")
      : el("div", { class: "rs-grid" }, ...cols),
    el("div", { class: "adv-foot" }, "Simulation only · costs use taker fees and funding at the current rate · nothing is executed from the dashboard"),
  );
}

/* ---------------- controls ---------------- */

function setStatus(st) {
  S.state = st.state;
  const running = st.state === "running";
  const sim = $("simulateBtn");
  sim.disabled = running;
  sim.classList.toggle("running", running);
  sim.lastChild.textContent = running ? " Simulating…" : st.state === "finished" || st.state === "stopped" ? " Replay simulation" : " Simulate market";
  $("stopBtn").disabled = !running;
  $("scenarioSelect").disabled = running;
  setProgress();
}

function setProgress() {
  $("progressFill").style.width = S.state === "idle" ? "0" : `${(S.step / S.totalSteps) * 100}%`;
}

function toLogin() { location.assign("/login?error=session"); }

async function post(path) {
  try {
    const r = await fetch(path, { method: "POST", headers: CSRF_HEADERS, credentials: "same-origin" });
    if (r.status === 401) { toLogin(); return null; }
    if (!r.ok) throw new Error(`${r.status}`);
    return await r.json();
  } catch (err) {
    toast("⚠️", "Request failed", `Could not reach ${path}. Is the server running?`, COLORS.critical);
    console.error(path, err);
    return null;
  }
}

async function postJson(path, body) {
  try {
    const r = await fetch(path, {
      method: "POST", credentials: "same-origin",
      headers: { ...CSRF_HEADERS, "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (r.status === 401) { toLogin(); return null; }
    if (r.status === 404) { toast("ℹ️", "Already handled", "That reminder was already acknowledged or has expired.", COLORS.text); return null; }
    if (!r.ok) throw new Error(`${r.status}`);
    return await r.json();
  } catch (err) {
    toast("⚠️", "Request failed", `Could not reach ${path}. Is the server running?`, COLORS.critical);
    return null;
  }
}

/* ---------------- chart ---------------- */

const tierBands = {
  id: "tierBands",
  beforeDatasetsDraw(c) {
    if (S.view !== "distance") return;
    const { ctx, chartArea: a, scales: { y } } = c;
    const t = chartTiers();
    const bands = [[0, t.emergency, COLORS.emergency, 0.12], [t.emergency, t.critical, COLORS.critical, 0.08], [t.critical, t.warning, COLORS.warning, 0.05]];
    ctx.save();
    for (const [lo, hi, col, al] of bands) {
      const top = y.getPixelForValue(hi), bot = y.getPixelForValue(lo);
      ctx.fillStyle = alpha(col, al);
      ctx.fillRect(a.left, top, a.right - a.left, bot - top);
      ctx.strokeStyle = alpha(col, 0.5);
      ctx.setLineDash([4, 4]);
      ctx.beginPath(); ctx.moveTo(a.left, top); ctx.lineTo(a.right, top); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = alpha(col, 0.9);
      ctx.font = "600 10px Inter, ui-sans-serif, sans-serif";
      ctx.textAlign = "right";
      ctx.fillText(`${Object.keys(t).find((k) => t[k] === hi).toUpperCase()} ${+hi.toFixed(1)}%`, a.right - 6, top - 4);
    }
    ctx.restore();
  },
};

function buildTabs() {
  const tabs = $("chartTabs");
  const mk = (id, label) => el("button", {
    class: `tab${S.view === id ? " active" : ""}`, type: "button", role: "tab", onclick: () => setView(id),
  }, label);
  tabs.replaceChildren(mk("distance", "Distance"), ...S.order.map((s) => mk(s, s)));
}

function setView(v) {
  S.view = v;
  buildTabs();
  buildChart();
  if (S.overview) renderPositions(S.overview.positions);
}

function labels() {
  if (S.minutesPerStep) {
    return Array.from({ length: S.totalSteps + 1 }, (_, i) => {
      const m = i * S.minutesPerStep;
      return `+${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}`;
    });
  }
  return Array.from({ length: S.totalSteps + 1 }, (_, i) => `${Math.round(i * S.tick)}s`);
}
function fundingPoint(sym, size) {
  return (ctx) => (S.fundingSteps[sym]?.has(ctx.dataIndex) ? size : 0);
}
function series(sym, key) {
  const arr = new Array(S.totalSteps + 1).fill(null);
  for (const p of S.history[sym] || []) arr[p.step] = p[key];
  return arr;
}

function buildChart() {
  if (typeof Chart === "undefined") { $("chartEmpty").hidden = false; return; }
  if (chart) chart.destroy();
  const legend = $("chartLegend");
  const common = {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: "rgba(11,16,25,0.95)", borderColor: "rgba(148,163,184,0.2)", borderWidth: 1,
        titleColor: "#e6eaf2", bodyColor: "#c3cad6", padding: 10, boxPadding: 4,
        filter: (item) => item.raw != null,
      },
    },
    scales: {
      x: { grid: { color: COLORS.grid }, ticks: { color: COLORS.tick, maxTicksLimit: 9, font: { size: 10 } } },
      y: { grid: { color: COLORS.grid }, ticks: { color: COLORS.tick, font: { size: 10 } } },
    },
  };

  if (S.view === "distance") {
    $("chartTitle").textContent = "Distance to liquidation";
    $("chartSub").textContent = S.minutesPerStep
      ? `All positions · tier bands · ▲ funding settlement · simulated clock (${S.minutesPerStep} min per step)`
      : "All positions · shaded bands are the warning / critical / emergency tiers";
    if (S.session?.reasons?.length) $("chartSub").textContent += ` · tiers widened (${S.session.reasons.join(", ")})`;
    const datasets = S.order.map((sym) => ({
      label: sym, data: series(sym, "distance_pct"), borderColor: symbolColor(sym),
      backgroundColor: symbolColor(sym), borderWidth: 2.5, tension: 0.35, spanGaps: true,
      pointRadius: fundingPoint(sym, 5), pointStyle: "triangle", pointBackgroundColor: COLORS.funding, pointBorderColor: COLORS.funding,
    }));
    const maxStart = Math.max(30, ...S.order.flatMap((s) => (S.history[s] || []).map((p) => p.distance_pct)));
    common.scales.y.min = 0;
    common.scales.y.max = Math.ceil((maxStart + 3) / 5) * 5;
    common.scales.y.ticks.callback = (v) => `${v}%`;
    common.plugins.tooltip.callbacks = { label: (i) => ` ${i.dataset.label}: ${i.raw?.toFixed(2)}%` };
    chart = new Chart($("chart"), { type: "line", data: { labels: labels(), datasets }, options: common, plugins: [tierBands] });
    legend.replaceChildren(
      ...S.order.map((s) => el("span", {}, el("i", { style: { background: symbolColor(s) } }), s)),
      el("span", { style: { color: COLORS.funding } }, "▲ funding paid"),
    );
    return;
  }

  const sym = S.view;
  const col = symbolColor(sym);
  const h = S.history[sym] || [];
  const last = h[h.length - 1];
  const liq = last ? last.liquidation_price : null;
  const tp = S.tierPrices[sym] || {};
  const flat = (v) => new Array(S.totalSteps + 1).fill(v);
  $("chartTitle").textContent = `${sym} · price vs liquidation`;
  $("chartSub").textContent = last ? `True liquidation now ${price(liq)} (moves with the shared wallet and funding) · ▲ settlement` : "";

  const ctx = $("chart").getContext("2d");
  const grad = ctx.createLinearGradient(0, 0, 0, 380);
  grad.addColorStop(0, alpha(col, 0.28));
  grad.addColorStop(1, alpha(col, 0.02));

  const datasets = [
    { label: "Mark price", data: series(sym, "mark_price"), borderColor: col, backgroundColor: grad, fill: { target: 1 }, borderWidth: 2.5, pointRadius: 0, tension: 0.35, spanGaps: true },
    { label: "True liquidation", data: series(sym, "liquidation_price"), borderColor: COLORS.emergency, borderWidth: 2, borderDash: [6, 5], fill: false, spanGaps: true, stepped: true,
      pointRadius: fundingPoint(sym, 6), pointStyle: "triangle", pointBackgroundColor: COLORS.funding, pointBorderColor: COLORS.funding },
    ...TIERS.map((t) => ({ label: `${t[0].toUpperCase() + t.slice(1)} (${+tiersOf(sym)[t].toFixed(1)}%)`, data: flat(tp[t]), borderColor: alpha(COLORS[t], 0.55), borderWidth: 1, borderDash: [3, 4], pointRadius: 0, fill: false })),
  ];
  const vals = [...h.map((p) => p.mark_price), ...h.map((p) => p.liquidation_price)].filter((v) => v != null);
  const lo = Math.min(...vals), hi = Math.max(...vals), pad = (hi - lo) * 0.08 || hi * 0.02;
  common.scales.y.min = lo - pad;
  common.scales.y.max = hi + pad;
  common.scales.y.ticks.callback = (v) => price(v);
  common.plugins.tooltip.callbacks = { label: (i) => ` ${i.dataset.label}: ${price(i.raw)}` };
  chart = new Chart($("chart"), { type: "line", data: { labels: labels(), datasets }, options: common });
  legend.replaceChildren(
    el("span", {}, el("i", { style: { background: col } }), "Mark price"),
    el("span", { style: { color: COLORS.emergency } }, el("i", { class: "dashed" }), "True liquidation"),
    el("span", { style: { color: COLORS.funding } }, "▲ funding paid"),
    ...TIERS.map((t) => el("span", { style: { color: COLORS[t] } }, el("i", { class: "dashed" }), `${t} tier`)),
  );
}

function updateChart(tick) {
  if (!chart) return;
  if (S.view === "distance") {
    chart.data.datasets.forEach((ds) => {
      const p = tick.prices[ds.label];
      if (p) ds.data[p.step] = p.distance_pct;
    });
  } else {
    const p = tick.prices[S.view];
    if (!p) return;
    chart.data.datasets[0].data[p.step] = p.mark_price;
    chart.data.datasets[1].data[p.step] = p.liquidation_price;
    const y = chart.options.scales.y;
    const out = (v) => v > y.max || v < y.min;
    if (out(p.mark_price) || out(p.liquidation_price)) { buildChart(); return; }
  }
  chart.update("none");
}

/* ---------------- websocket ---------------- */

function connect() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${proto}//${location.host}/ws/${MODE}`);
  ws.onopen = () => {
    retry = 0;
    $("connPill").classList.add("connected");
    $("connText").textContent = "Live";
  };
  ws.onclose = (ev) => {
    if (ev.code === 1008 && MODE === "me") { toLogin(); return; }
    $("connPill").classList.remove("connected");
    $("connText").textContent = "Reconnecting";
    setTimeout(connect, Math.min(1000 * 2 ** retry++, 8000));
  };
  ws.onmessage = (ev) => {
    try { handle(JSON.parse(ev.data)); } catch (err) { console.error("bad message", err); }
  };
}

/* ---------------- scope (public demo vs signed-in) ---------------- */

async function setupScope() {
  if (MODE !== "me") return true;
  const r = await fetch("/api/me/session", { credentials: "same-origin" });
  if (r.status === 401) { toLogin(); return false; }
  const me = await r.json();
  const who = me.username ? `@${me.username}` : `user ${me.user_id}`;
  document.title = `LiqGuard · ${who}`;
  const ribbon = $("ribbon");
  ribbon.classList.add("private");
  ribbon.querySelector("b").textContent = "PRIVATE";
  $("ribbonText").textContent = `Signed in as ${who}. Everything below is scoped to your account; the simulation is your own demo run and alerts go only to your Telegram chat.`;
  $("ribbonLink").hidden = true;
  const pill = $("scopePill");
  pill.textContent = `DEMO SIM · ${who}`;
  pill.className = "pill pill-private";
  pill.title = "Your private simulation (demo prices); your real positions are in 'Your account'";
  $("signInBtn").hidden = true;
  $("signOutBtn").hidden = false;
  $("footScope").textContent = `Signed in as ${who}.`;
  $("account").hidden = false;
  await refreshAccount();
  setInterval(refreshAccount, 30000);
  setupAccountPanels();
  return true;
}

function venueTitle(b) {
  return `${venueLabel(b.venue)}${b.network === "testnet" ? " testnet" : ""}`;
}

async function refreshAccount() {
  let data;
  try {
    const r = await fetch("/api/me/account", { credentials: "same-origin" });
    if (r.status === 401) { toLogin(); return; }
    data = await r.json();
  } catch (err) {
    $("accountSub").textContent = "Could not load account data — retrying in 30 s";
    return;
  }
  $("accountSub").textContent = "Live data from your own exchange connections · refreshed every 30 s";

  const binds = $("accountBindings");
  binds.replaceChildren(...(data.bindings || []).map((b) => el("div", { class: "binding" },
    el("span", { class: `venue venue-${b.venue}` }, venueTitle(b)),
    el("span", {}, b.binding_type === "wallet" ? "wallet" : (b.permissions || []).join(" · ") || "api key"),
    el("span", { class: "hint" }, b.api_key || b.wallet_address || ""),
  )));
  if (!data.bindings?.length) binds.append(empty("🔌", "No exchanges connected", "在下方「🔑 綁定帳號」或 Telegram 主選單綁定（Binance 唯讀金鑰或 Hyperliquid 錢包）。"));

  $("accountPositionsLabel").textContent = data.label ? data.label.replace(/^📊\s*/, "") : "Positions";
  const box = $("accountPositions");
  const positions = data.positions?.positions || [];
  box.replaceChildren(...positions.map((p) => el("div", { class: "acct-pos" },
    el("span", {}, el("b", {}, p.symbol), " ", el("span", { class: `side side-${p.side}` }, p.side.toUpperCase()), " ",
      el("span", { class: `venue venue-${p.venue}` }, venueLabel(p.venue)),
      p.margin_mode ? el("span", { class: `mode mode-${p.margin_mode}` }, p.margin_mode.toUpperCase()) : null),
    el("span", {}, `mark ${price(p.mark_price)} · liq ${price(p.liquidation_price)}`,
      p.true_liquidation?.price && Math.abs(p.true_liquidation.price - p.liquidation_price) / (p.liquidation_price || 1) > 0.0005
        ? ` · true ${price(p.true_liquidation.price)}` : ""),
    el("span", { class: `d c-${tierFor(p.distance_to_liquidation_pct)}` }, pct(p.distance_to_liquidation_pct)),
    el("button", { class: "mini-btn", type: "button", title: "Rescue simulator", onclick: () => loadLiveRescue(p.symbol, p.venue) }, "🛟"),
    toolButtons(p.symbol, p.venue, true),
    protectionChips((data.protection || []).find((x) => x.symbol === p.symbol && x.venue === p.venue)),
    p.funding ? el("span", { class: "acct-fund" },
      `funding ${p.funding.rate_pct >= 0 ? "+" : ""}${p.funding.rate_pct.toFixed(4)}%/${p.funding.interval_hours}h · next ${duration(p.funding.minutes_to_settlement)} · `
      + (p.funding.drift || []).map((d) => `${d.horizon_hours}h → ${distText(d.distance_pct, d.liquidation_price == null)}`).join(" · ")
      + ` (${p.funding.label})`) : null,
  )));
  if (!positions.length) box.append(empty("📭", "No open positions", data.label ? "Nothing open right now." : "Connect an exchange or enable /demo in Telegram."));
  const live = accountInsights(data.insights);
  if (live) box.append(live);
  for (const a of data.escalations || []) {
    box.prepend(el("div", { class: `acct-wake c-${a.severity}` }, `⏰ ${a.symbol} ${a.severity.toUpperCase()} · ${a.reminders_sent}/${a.max_reminders} reminders `,
      el("button", { class: "mini-btn ack-btn", type: "button", onclick: async () => {
        if (await postJson("/api/me/live/ack", { ack_id: a.id })) refreshAccount();
      } }, "✅ 我醒了"),
      el("button", { class: "mini-btn", type: "button", onclick: async () => {
        const res = await postJson("/api/me/live/snooze", { ack_id: a.id });
        if (res) { toast("😴", "暫停提醒 30 分鐘", `${res.symbol} 的提醒暫停 30 分鐘。`, COLORS.text, `snooze-${a.id}`); refreshAccount(); }
      } }, "😴 暫停提醒 30 分鐘")));
  }

  const acts = $("accountActions");
  acts.replaceChildren(...(data.actions || []).slice().reverse().map((a) => el("div", { class: "au" },
    el("div", { class: "au-time" }, clock(a.timestamp)),
    el("div", {},
      el("div", { class: "au-event" }, `${(a.action_type || "").replace(/_/g, " ")} · ${a.symbol}`, el("span", { class: "src" }, a.status)),
      el("div", { class: "au-detail" }, [a.amount_usd ? money(a.amount_usd, { dp: 2 }) : null, a.details?.reason || a.details?.result].filter(Boolean).join(" · ")),
    ),
  )));
  if (!data.actions?.length) acts.append(empty("📋", "No protective actions yet", data.protect_enabled ? "Proposals and confirmations from Telegram appear here." : "Protective actions are disabled on this server."));
}

function accountInsights(ins) {
  if (!ins) return null;
  const parts = [];
  const atRisk = (ins.triage || []).filter((t) => t.at_risk);
  if (atRisk.length) {
    parts.push(el("div", { class: "acct-ins" }, el("b", {}, "🎯 Handle in order: "),
      atRisk.map((t, i) => el("span", { class: `c-${t.severity}` }, `${i ? " → " : ""}${t.symbol} ${pct(t.distance_pct)}`))));
  }
  for (const st of ins.stress || []) {
    if (st.liq_move_down_pct == null && st.symbols.length < 2) continue;
    const shocks = st.shocks.filter((x) => x.market_move_pct < 0).map((x) => `${x.market_move_pct}%: ${x.liquidated ? "LIQUIDATED" : "ok"}`).join(" · ");
    parts.push(el("div", { class: "acct-ins" }, el("b", {}, `📉 ${venueLabel(st.venue)} cross: `),
      st.liq_move_down_pct != null ? `a ${pct(st.liq_move_down_pct)} correlated drop liquidates the account · ` : "", shocks));
  }
  for (const c of (ins.comparisons || []).filter((x) => x.gap_pct >= 1)) {
    parts.push(el("div", { class: "acct-ins" }, el("b", {}, `🧲 ${c.symbol}: `),
      `platform liq ${price(c.platform_price)} (${pct(c.platform_distance_pct)}) vs LiqGuard ${price(c.correlated_price)} (${pct(c.correlated_distance_pct)}) — `,
      el("b", { class: "c-critical" }, `${c.gap_pct.toFixed(1)} pts closer`)));
  }
  if (ins.concentration?.flagged) parts.push(el("div", { class: "acct-ins c-warning" }, `🧲 ${ins.concentration.message}`));
  for (const z of (ins.sizing || []).filter((x) => x.status === "oversized")) {
    parts.push(el("div", { class: "acct-ins" }, el("b", {}, `📏 ${z.symbol}: `), `${pct(z.risk_pct)} of equity at its ${z.exit_kind} → suggested ${fmtSize(z.suggested_size)}`));
  }
  return parts.length ? el("div", { class: "acct-insights" }, ...parts) : null;
}

/* ---------------- quick actions (same reports as the Telegram buttons) ---------------- */

const TOOLS = [["rescue", "🛟 救倉試算"], ["wick", "🕯 插針壓測"], ["stop", "🛑 止損檢查"], ["funding", "💸 資金費影響"]];

function toolButtons(symbol, venue, live) {
  return el("div", { class: "tool-row", lang: "zh-Hant-TW" }, ...TOOLS.map(([tool, label]) => el("button", {
    class: "mini-btn tool-btn", type: "button", "data-tool": tool,
    onclick: (ev) => { ev.stopPropagation(); openTool(tool, symbol, venue, live); },
  }, label)));
}

function toolLineClass(line) {
  if (/^(⚠️|🛑|⛔)/.test(line)) return "tool-line warn";
  if (/^(✅|💡)/.test(line)) return "tool-line good";
  if (/^\s{2,}/.test(line)) return "tool-line indent";
  return "tool-line";
}

async function openTool(tool, symbol, venue, live) {
  const dlg = $("toolDialog");
  const label = Object.fromEntries(TOOLS)[tool];
  $("toolTitle").textContent = `${label} · ${symbol}`;
  $("toolTabs").replaceChildren(...TOOLS.map(([t, l]) => el("button", {
    class: `mini-btn${t === tool ? " active" : ""}`, type: "button", onclick: () => openTool(t, symbol, venue, live),
  }, l)));
  $("toolBody").replaceChildren(el("div", { class: "tool-line muted" }, "載入中…"));
  if (!dlg.open) dlg.showModal();
  const q = `symbol=${encodeURIComponent(symbol)}&tool=${tool}`;
  const url = live ? `/api/me/position-tool/live?${q}&venue=${encodeURIComponent(venue)}` : `${API}/position-tool?${q}`;
  try {
    const r = await fetch(url, { credentials: "same-origin" });
    if (r.status === 401) { toLogin(); return; }
    if (r.status === 404) {
      $("toolBody").replaceChildren(el("div", { class: "tool-line warn" }, "這個倉位已不存在（可能已平倉或模擬已結束）。"));
      return;
    }
    if (!r.ok) throw new Error(`${r.status}`);
    const data = await r.json();
    $("toolTitle").textContent = data.title;
    const body = data.lines.map((line) => line ? el("div", { class: toolLineClass(line) }, line) : el("div", { class: "tool-gap" }));
    if (tool === "rescue") {
      body.push(el("button", { class: "btn btn-ghost tool-more", type: "button", onclick: () => {
        dlg.close();
        if (live) loadLiveRescue(symbol, venue); else loadRescue(symbol);
      } }, "📊 在救倉模擬器中比較"));
    }
    $("toolBody").replaceChildren(...body);
  } catch (err) {
    $("toolBody").replaceChildren(el("div", { class: "tool-line warn" }, "暫時無法載入，請稍後再試。"));
  }
}

/* ---------------- 🔔 通知設定 & 🔑 綁定帳號 (signed-in only) ---------------- */

const SETTING_META = {
  warning: ["⚠️ 警告門檻", "%"], critical: ["🚨 嚴重門檻", "%"], emergency: ["🆘 緊急門檻", "%"],
  interval: ["⏰ 提醒間隔", " 分鐘"], max: ["🔁 最多提醒", " 次"], quiet: ["🌙 勿擾時段", ""],
};
const TOGGLE_META = { escalation: "⏰ 喚醒提醒", adaptive: "📈 自適應門檻", stops: "🛑 止損檢查" };

function quietText(v) {
  if (!v || v === "off") return "關閉";
  const [a, b] = String(v).split("-").map(Number);
  return `${String(a).padStart(2, "0")}:00–${String(b).padStart(2, "0")}:00`;
}

async function sendJson(path, body) {
  try {
    const r = await fetch(path, {
      method: "POST", credentials: "same-origin",
      headers: { ...CSRF_HEADERS, "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (r.status === 401) { toLogin(); return { ok: false }; }
    const data = await r.json().catch(() => ({}));
    return { ok: r.ok, status: r.status, data };
  } catch (err) {
    return { ok: false, status: 0, data: { detail: "無法連線到伺服器，請稍後再試。" } };
  }
}

function panelMsg(id, text, ok) {
  const box = $(id);
  box.textContent = text || "";
  box.className = `panel-msg${text ? (ok ? " ok" : " err") : ""}`;
}

async function refreshSettings() {
  try {
    const r = await fetch("/api/me/settings", { credentials: "same-origin" });
    if (r.status === 401) { toLogin(); return; }
    renderSettings(await r.json());
  } catch (err) {
    panelMsg("settingsMsg", "無法載入通知設定，請稍後再試。", false);
  }
}

function renderSettings(cur) {
  const rows = Object.entries(SETTING_META).map(([key, [label, unit]]) => {
    const current = key === "quiet" ? (cur.quiet || "off") : cur[key];
    const options = [...new Set([...(cur.presets[key] || []).map(String), String(current)])];
    const select = el("select", { class: "field", "data-setting": key, "aria-label": label,
      onchange: (ev) => saveSetting(key, ev.target.value) },
      ...options.map((v) => {
        const o = el("option", { value: v }, key === "quiet" ? quietText(v) : `${v}${unit}`);
        if (v === String(current)) o.selected = true;
        return o;
      }));
    return el("label", { class: "setting-row" }, el("span", {}, label), select);
  });
  const toggles = Object.entries(TOGGLE_META).map(([key, label]) => {
    const box = el("input", { type: "checkbox", "data-setting": key, onchange: (ev) => saveSetting(key, ev.target.checked ? "on" : "off") });
    box.checked = !!cur[key];
    return el("label", { class: "toggle-row" }, box, el("span", {}, label));
  });
  $("settingsForm").replaceChildren(...rows, el("div", { class: "toggles" }, ...toggles),
    el("div", { class: "panel-sub" }, `勿擾時段以 UTC${cur.quiet_utc_offset_hours >= 0 ? "+" : ""}${cur.quiet_utc_offset_hours}（台北時間）計算。`));
}

async function saveSetting(key, value) {
  const res = await sendJson("/api/me/settings", { key, value });
  if (res.ok) {
    renderSettings(res.data);
    panelMsg("settingsMsg", "✅ 已儲存，Telegram 也會套用同樣設定。", true);
  } else {
    panelMsg("settingsMsg", `❌ ${res.data?.detail || "無法套用這個設定"}`, false);
    refreshSettings();
  }
}

function bindingLabel(b) {
  const venue = b.venue === "binance" ? "🟡 Binance" : "🟢 Hyperliquid";
  return `${venue}${b.network === "testnet" ? " 測試網" : ""}`;
}

async function refreshBindings() {
  let data;
  try {
    const r = await fetch("/api/me/bindings", { credentials: "same-origin" });
    if (r.status === 401) { toLogin(); return; }
    data = await r.json();
  } catch (err) {
    panelMsg("bindMsg", "無法載入綁定狀態，請稍後再試。", false);
    return;
  }
  $("securityNote").textContent = data.security_note;
  const list = $("bindList");
  list.replaceChildren(...data.bindings.map((b) => el("div", { class: "binding" },
    el("span", {}, bindingLabel(b)),
    el("span", { class: "hint" }, b.api_key || b.wallet_address || ""),
    el("button", { class: "mini-btn danger", type: "button", onclick: () => confirmUnbind(b) }, "🗑 解除綁定"),
  )));
  if (!data.bindings.length) list.append(empty("🔌", "尚未綁定", "選擇下方交易所開始綁定。"));
}

function confirmUnbind(b) {
  const dlg = $("confirmDialog");
  $("confirmText").textContent = `確定要解除 ${bindingLabel(b)}（${b.api_key || b.wallet_address || ""}）？解除後會刪除加密保存的金鑰／地址，並停止監控這個帳號。`;
  $("confirmYes").onclick = async () => {
    dlg.close();
    const res = await sendJson("/api/me/bindings/unbind", { venue: b.venue, network: b.network || "mainnet", confirm: true });
    panelMsg("bindMsg", res.ok ? `✅ 已解除 ${bindingLabel(b)} 的綁定，金鑰已刪除。` : `❌ ${res.data?.detail || "解除失敗"}`, res.ok);
    refreshBindings();
    refreshAccount();
  };
  $("confirmNo").onclick = () => dlg.close();
  dlg.showModal();
}

function setupAccountPanels() {
  document.querySelectorAll(".bind-tab").forEach((tab) => tab.addEventListener("click", () => {
    document.querySelectorAll(".bind-tab").forEach((t) => {
      const on = t === tab;
      t.classList.toggle("active", on);
      t.setAttribute("aria-selected", on ? "true" : "false");
    });
    $("bindBinance").hidden = tab.dataset.venue !== "binance";
    $("bindHyperliquid").hidden = tab.dataset.venue !== "hyperliquid";
    panelMsg("bindMsg", "");
  }));
  const submit = (formId, path, fields) => $(formId).addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const form = ev.target;
    const body = Object.fromEntries(fields.map((f) => [f, form.elements[f].value.trim()]));
    form.reset();
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    panelMsg("bindMsg", "🔄 驗證中…", true);
    const res = await sendJson(path, body);
    button.disabled = false;
    panelMsg("bindMsg", res.data?.message || res.data?.detail || (res.ok ? "✅ 已綁定" : "❌ 綁定失敗"), res.ok);
    if (res.ok) { refreshBindings(); refreshAccount(); }
  });
  submit("bindBinance", "/api/me/bindings/binance", ["api_key", "api_secret"]);
  submit("bindHyperliquid", "/api/me/bindings/hyperliquid", ["address"]);
  refreshSettings();
  refreshBindings();
}

async function bootstrap() {
  if (!(await setupScope())) return;
  try {
    const r = await fetch(`${API}/snapshot`, { credentials: "same-origin" });
    if (r.status === 401) { toLogin(); return; }
    if (r.ok) applySnapshot(await r.json());
  } catch (err) {
    $("headline").textContent = "Dashboard server unreachable — retrying…";
  }
  connect();
}

document.addEventListener("DOMContentLoaded", () => {
  $("simulateBtn").addEventListener("click", async () => {
    $("simulateBtn").disabled = true;
    const res = await post(`${API}/simulate`);
    if (!res) $("simulateBtn").disabled = false;
  });
  $("stopBtn").addEventListener("click", () => post(`${API}/simulate/stop`));
  $("scenarioSelect").addEventListener("change", (ev) => switchScenario(ev.target.value));
  $("resetBtn").addEventListener("click", () => post(`${API}/simulate/reset`));
  $("signOutBtn").addEventListener("click", async () => {
    await fetch("/auth/logout", { method: "POST", headers: CSRF_HEADERS, credentials: "same-origin" });
    location.assign("/");
  });
  $("toolClose").addEventListener("click", () => $("toolDialog").close());
  for (const id of ["toolDialog", "confirmDialog"]) {
    $(id).addEventListener("click", (ev) => { if (ev.target === ev.currentTarget) ev.currentTarget.close(); });
  }
  setInterval(() => { if (ws && ws.readyState === 1) ws.send("ping"); }, 25000);
  bootstrap();
});
