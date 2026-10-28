"use strict";

const COLORS = {
  safe: "#34d399", warning: "#fbbf24", critical: "#fb923c", emergency: "#f43f5e", funding: "#a78bfa",
  grid: "rgba(148,163,184,0.08)", tick: "#5b667a", text: "#8a96ab",
};
const SYMBOL_COLORS = { BTCUSDT: "#f7931a", ETH: "#8b9cff", ETHUSDT: "#8b9cff", SOLUSDT: "#14f195" };
const FALLBACK_COLORS = ["#38bdf8", "#f472b6", "#a3e635", "#facc15"];
const TIERS = ["warning", "critical", "emergency"];
const LEVEL_ICON = { warning: "⚠️", critical: "🚨", emergency: "🆘", funding: "⏳" };
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
  overview: null, counts: { warning: 0, critical: 0, emergency: 0, funding: 0 }, lastSeverity: {},
  minutesPerStep: null, wallets: {}, fundingEvents: [], fundingSteps: {}, rescue: null,
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
function tierFor(distance) {
  const t = S.thresholds;
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
  S.counts = { warning: 0, critical: 0, emergency: 0, funding: 0 };
  S.lastSeverity = {};
  S.minutesPerStep = snap.minutes_per_step || null;
  S.wallets = snap.wallets || {};
  S.fundingEvents = snap.funding_events || [];
  S.fundingSteps = {};
  for (const [sym, pts] of Object.entries(S.history)) {
    S.fundingSteps[sym] = new Set(pts.filter((p) => p.funding_paid_usd != null).map((p) => p.step));
  }
  S.rescue = null;
  if (S.view !== "distance" && !S.order.includes(S.view)) S.view = "distance";

  $("musePill").textContent = snap.muse_mode === "live" ? "Muse · live" : "Muse · mock";
  $("musePill").classList.toggle("live", snap.muse_mode === "live");
  renderChannels(snap.channels || []);

  renderOverview(snap.overview);
  renderSummary(snap.summary);

  $("timeline").replaceChildren();
  (snap.alerts || []).forEach((a) => addAlert(a, { quiet: true }));
  if (!snap.alerts?.length) $("timeline").append(empty("🔕", "No alerts yet", "Press Simulate market to walk prices toward liquidation."));
  updateCounts();

  $("advice").replaceChildren();
  (snap.advice || []).forEach((c) => addAdvice(c));
  if (!snap.advice?.length) $("advice").append(empty("✦", "No advice yet", "Muse explains each alert with exact margin and size numbers."));

  $("audit").replaceChildren();
  (snap.audit || []).forEach((e) => addAudit(e));
  if (!snap.audit?.length) $("audit").append(empty("📋", "Audit log is empty", "Alerts, suggestions and Telegram action steps are recorded here."));

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
  renderOverview(data.overview);
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
  md.className = `kpi-value c-${tierFor(minD)}`;
  $("kpiMinDistSym").textContent = closest ? `${closest.symbol} ${closest.side}` : "—";
  $("kpiSafety").textContent = money(rm.total_margin_needed_for_safety);
  $("kpiSafetySub").textContent = rm.total_margin_needed_for_safety > 0
    ? `to restore a ${S.safetyTarget ?? 30}% buffer` : "all above safety target";

  if (!S.summaryRiskiest && ov.riskiest_position) {
    const rp = ov.riskiest_position;
    setRiskiest(rp.symbol, rp.side, rp.distance_to_liquidation_pct);
  } else if (S.summaryRiskiest) {
    const rp = ov.positions.find((p) => p.symbol === S.summaryRiskiest);
    if (rp) setRiskiest(rp.symbol, rp.side, rp.distance_to_liquidation_pct);
  }

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
    el("span", { class: `c-${tierFor(dist)}` }, pct(dist)),
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
    const tier = tierFor(d);
    const color = COLORS[tier];
    const prev = S.lastSeverity[sym];
    S.lastSeverity[sym] = tier;

    const ticks = TIERS.map((t) => el("div", {
      class: "gauge-tick", title: `${t} ${S.thresholds[t]}%`,
      style: { left: `${(S.thresholds[t] / scale) * 100}%`, background: COLORS[t] },
    }));
    const labels = TIERS.map((t) => el("span", { style: { left: `${(S.thresholds[t] / scale) * 100}%` } }, `${S.thresholds[t]}`));

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
      el("div", { class: "pos-actions" },
        el("button", {
          class: "mini-btn", type: "button", title: "Compare add margin, reduce and hedge",
          onclick: (ev) => { ev.stopPropagation(); loadRescue(sym); },
        }, "🛟 Rescue"),
      ),
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
      "Crossed ", el("b", {}, `${a.threshold_pct}%`), " — now ", el("b", {}, pct(a.distance_pct)),
      ` from liquidation · mark ${price(a.mark_price)} vs liq ${price(a.liquidation_price)}`,
    ),
    chips.length ? el("div", { class: "chips" }, ...chips) : null,
  ));
  if (!opts.quiet) {
    const sent = (a.channels || []).map((c) => (c === "web" ? "Web" : c === "telegram" ? "Telegram" : c)).join(" + ");
    toast(LEVEL_ICON[a.level], `${a.level.toUpperCase()} · ${a.symbol}`,
      `${pct(a.distance_pct)} from liquidation — sent to ${sent || "dashboard"}`, COLORS[a.level]);
    if (a.level === "emergency") {
      document.body.classList.add("flash-emergency");
      setTimeout(() => document.body.classList.remove("flash-emergency"), 1400);
    }
  }
}

function updateCounts() {
  $("cntWarning").textContent = S.counts.warning;
  $("cntCritical").textContent = S.counts.critical;
  $("cntEmergency").textContent = S.counts.emergency;
  $("cntFunding").textContent = S.counts.funding;
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

function toast(icon, title, body, color) {
  const box = $("toasts");
  const t = el("div", { class: "toast", style: { "--t": color } },
    el("div", {}, icon),
    el("div", {}, el("div", { class: "toast-title" }, title), el("div", { class: "toast-body" }, body)),
  );
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
    el("div", { class: "rs-grid" }, ...cols),
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

/* ---------------- chart ---------------- */

const tierBands = {
  id: "tierBands",
  beforeDatasetsDraw(c) {
    if (S.view !== "distance") return;
    const { ctx, chartArea: a, scales: { y } } = c;
    const t = S.thresholds;
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
      ctx.fillText(`${Object.keys(t).find((k) => t[k] === hi).toUpperCase()} ${hi}%`, a.right - 6, top - 4);
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
    ...TIERS.map((t) => ({ label: `${t[0].toUpperCase() + t.slice(1)} (${S.thresholds[t]}%)`, data: flat(tp[t]), borderColor: alpha(COLORS[t], 0.55), borderWidth: 1, borderDash: [3, 4], pointRadius: 0, fill: false })),
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
  if (!data.bindings?.length) binds.append(empty("🔌", "No exchanges connected", "Use /bind in Telegram (read-only Binance key or Hyperliquid wallet)."));

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
    el("button", { class: "mini-btn", type: "button", onclick: () => loadLiveRescue(p.symbol, p.venue) }, "🛟"),
    p.funding ? el("span", { class: "acct-fund" },
      `funding ${p.funding.rate_pct >= 0 ? "+" : ""}${p.funding.rate_pct.toFixed(4)}%/${p.funding.interval_hours}h · next ${duration(p.funding.minutes_to_settlement)} · `
      + (p.funding.drift || []).map((d) => `${d.horizon_hours}h → ${distText(d.distance_pct, d.liquidation_price == null)}`).join(" · ")
      + ` (${p.funding.label})`) : null,
  )));
  if (!positions.length) box.append(empty("📭", "No open positions", data.label ? "Nothing open right now." : "Connect an exchange or enable /demo in Telegram."));

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
  $("resetBtn").addEventListener("click", () => post(`${API}/simulate/reset`));
  $("signOutBtn").addEventListener("click", async () => {
    await fetch("/auth/logout", { method: "POST", headers: CSRF_HEADERS, credentials: "same-origin" });
    location.assign("/");
  });
  setInterval(() => { if (ws && ws.readyState === 1) ws.send("ping"); }, 25000);
  bootstrap();
});
