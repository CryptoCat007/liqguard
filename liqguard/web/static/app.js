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
const STOP_TONE = { ok: "safe", partial: "warning", no_stop: "critical", too_close: "warning", beyond_liquidation: "emergency" };
const FIX_ICON = { move_stop: "🎯", add_margin: "💰", reduce: "✂️" };
const OPTION_ICON = { add_margin: "💰", reduce: "✂️", hedge: "🛡" };
const MODE = location.pathname.startsWith("/me") ? "me" : "demo";
const API = MODE === "me" ? "/api/me" : "/api/demo";
const CSRF_HEADERS = { "X-LiqGuard-CSRF": "1" };
const LANG_KEY = "liqguard.lang";
const LANG_COOKIE = "liqguard_lang";
const I18N = { lang: "en", strings: {} };

function L(key, params) {
  const template = I18N.strings[key];
  if (template == null) return key;
  return template.replace(/\{(\w+)\}/g, (m, name) => (params && name in params ? String(params[name]) : m));
}
const hasL = (key) => key in I18N.strings;

/** A demo API url; the public demo runs one simulation per language, picked by `lang`. */
function api(path, params = {}) {
  const q = new URLSearchParams(params);
  if (MODE === "demo") q.set("lang", I18N.lang);
  const s = q.toString();
  return `${API}${path}${s ? `?${s}` : ""}`;
}

async function loadStrings(lang) {
  const r = await fetch(`/api/i18n/${encodeURIComponent(lang)}`);
  if (!r.ok) throw new Error(`${r.status}`);
  const data = await r.json();
  I18N.lang = data.lang;
  I18N.strings = data.strings;
  document.documentElement.lang = data.lang === "zh-Hant" ? "zh-Hant-TW" : "en";
  for (const node of document.querySelectorAll("[data-i18n]")) node.textContent = L(node.dataset.i18n);
  for (const node of document.querySelectorAll("[data-i18n-title]")) node.title = L(node.dataset.i18nTitle);
  for (const node of document.querySelectorAll("[data-i18n-aria]")) node.setAttribute("aria-label", L(node.dataset.i18nAria));
  for (const node of document.querySelectorAll("[data-i18n-placeholder]")) node.placeholder = L(node.dataset.i18nPlaceholder);
  $("langBtn").lang = data.lang === "zh-Hant" ? "en" : "zh-Hant-TW";
}

/** Shared with the server-rendered login pages, which read the cookie. */
function rememberLanguage(lang) {
  localStorage.setItem(LANG_KEY, lang);
  document.cookie = `${LANG_COOKIE}=${encodeURIComponent(lang)}; path=/; max-age=31536000; samesite=lax`;
}
function cookieLanguage() {
  const hit = document.cookie.split("; ").find((c) => c.startsWith(`${LANG_COOKIE}=`));
  return hit ? decodeURIComponent(hit.slice(LANG_COOKIE.length + 1)) : null;
}

async function initialLanguage() {
  const fromUrl = new URLSearchParams(location.search).get("lang");
  if (fromUrl) {
    rememberLanguage(fromUrl);
    return fromUrl;
  }
  if (MODE === "me") {
    try {
      const r = await fetch("/api/me/language", { credentials: "same-origin" });
      if (r.ok) return (await r.json()).language;
    } catch (err) { /* fall back to the browser choice */ }
  }
  return cookieLanguage() || localStorage.getItem(LANG_KEY) || "en";
}

const S = {
  history: {}, tierPrices: {}, thresholds: { warning: 20, critical: 10, emergency: 5 },
  totalSteps: 40, tick: 0.75, step: 0, state: "idle", order: [], view: "distance",
  overview: null, counts: NEW_COUNTS(), lastSeverity: {},
  minutesPerStep: null, wallets: {}, fundingEvents: [], fundingSteps: {}, rescue: null,
  protection: {}, session: null, escalation: null, pending: {}, stopMuse: {},
  insights: null, scenario: null, scenarios: [], openSym: null, adviceBySym: {}, adviceCount: 0, auditCount: 0,
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
function signedRate(f) { return `${f.rate_pct >= 0 ? "+" : ""}${f.rate_pct.toFixed(4)}%/${f.interval_hours}h`; }
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
/** SAFE / WARNING / DANGER for first-time readers: critical and emergency are both "act now". */
function statusOf(level) {
  if (level === "critical" || level === "emergency") return "danger";
  return level === "warning" ? "warning" : "safe";
}
const tierLabel = (t) => L(`tier_${t || "safe"}`);
const sideLabel = (s) => (s ? L(`side_${s}`) : "");
const modeLabel = (m) => L(`mode_${m}`);
const stopLabel = (status) => (STOP_TONE[status] ? L(`stop_${status}`) : status);
const optionLabel = (kind) => `${OPTION_ICON[kind] || "•"} ${L(`opt_${kind}`)}`;
function venueLabel(v) {
  if (v === "hyperliquid") return "Hyperliquid";
  if (v === "binance") return "Binance";
  return v === "demo" ? L("venue_demo") : v;
}
function channelName(c) {
  if (c === "telegram") return "Telegram";
  return c === "web" ? L("channel_web") : c;
}
function sourceName(s) { return hasL(`src_${s}`) ? L(`src_${s}`) : s; }
function duration(minutes) {
  if (minutes == null) return "—";
  const m = Math.max(0, Math.round(minutes));
  return m >= 60 ? `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m` : `${m}m`;
}
function distText(d, noLiq) { return noLiq || d == null ? L("no_liq") : pct(d); }
function empty(glyph, title, body) {
  return el("div", { class: "empty" }, el("div", { class: "glyph" }, glyph), el("b", {}, title), el("div", {}, body));
}
function sideTag(side) { return side ? el("span", { class: `side side-${side}` }, sideLabel(side)) : null; }
function venueTag(venue) { return el("span", { class: `venue venue-${venue}` }, venueLabel(venue)); }
function symTag(sym, tag = "b") { return el(tag, { class: "sym", style: { color: symbolColor(sym) } }, sym); }
function setTag(id, text, cls = "") {
  const tag = $(id);
  if (!tag) return;
  tag.textContent = text;
  tag.className = `tag${cls ? ` ${cls}` : ""}`;
  tag.hidden = !text;
}
function openFold(id, scroll = true) {
  const fold = $(id);
  if (!fold) return;
  fold.open = true;
  if (scroll) fold.scrollIntoView({ behavior: "smooth", block: "start" });
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
  S.adviceBySym = {};
  S.adviceCount = 0;
  S.auditCount = 0;
  if (S.openSym && !S.order.includes(S.openSym)) S.openSym = null;
  renderScenarios();
  if (S.view !== "distance" && !S.order.includes(S.view)) S.view = "distance";

  renderMuse(snap.muse_mode);
  renderChannels(snap.channels || []);

  $("timeline").replaceChildren();
  (snap.alerts || []).forEach((a) => addAlert(a, { quiet: true }));
  (snap.escalation_log || []).forEach((e) => addEscalation(e, { quiet: true }));
  if (!snap.alerts?.length) $("timeline").append(empty("🔕", L("empty_alerts"), L("empty_alerts_hint")));
  updateCounts();

  $("advice").replaceChildren();
  (snap.advice || []).forEach((c) => addAdvice(c));
  if (!snap.advice?.length) $("advice").append(empty("✦", L("empty_advice"), L("empty_advice_hint")));

  $("audit").replaceChildren();
  (snap.audit || []).forEach((e) => addAudit(e));
  if (!snap.audit?.length) $("audit").append(empty("📋", L("empty_audit"), L("empty_audit_hint")));
  updateHistoryCounts();

  renderOverview(snap.overview);
  renderSummary(snap.summary);
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

/** The badge says "live" only after Muse really answered; otherwise the rule-based text is what users see. */
function renderMuse(mode) {
  const known = ["live", "pending", "fallback", "mock"].includes(mode) ? mode : "mock";
  const pill = $("musePill");
  pill.textContent = L(`muse_${known}`);
  pill.title = L(`muse_title_${known}`);
  pill.classList.toggle("live", known === "live");
  pill.classList.toggle("degraded", known === "fallback");
  pill.dataset.mode = known;
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
      renderHero();
      break;
    case "advice_update":
      updateAdvice(data);
      renderHero();
      break;
    case "alert_update":
      if (data.level === "stop") updateStopAlert(data);
      break;
    case "muse":
      renderMuse(data.mode);
      break;
    case "account_muse":
      updateAccountMuse(data);
      break;
    case "rescue_muse":
      updateRescueMuse(data);
      break;
    case "report_muse":
      updateToolReport(data);
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

/* ---------------- overview: hero, KPIs, positions ---------------- */

function renderChannels(channels) {
  const names = channels.map(channelName);
  $("channelsPill").textContent = L("channels", { names: names.length ? names.join(" + ") : L("channels_none") });
  $("channelsPill").classList.toggle("live", channels.includes("telegram"));
}

function renderOverview(ov) {
  if (!ov) return;
  S.overview = ov;
  const sum = ov.summary, rm = ov.risk_metrics, ex = ov.exposure;

  $("kpiNotional").textContent = money(sum.total_notional_value);
  $("kpiVenues").textContent = L("kpi_positions_venues", { n: sum.total_positions, venues: (ov.venues || []).map(venueLabel).join(" + ") });
  const pnl = $("kpiPnl");
  pnl.textContent = money(sum.total_unrealized_pnl, { signed: true });
  pnl.className = `kpi-value ${sum.total_unrealized_pnl >= 0 ? "pos" : "neg"}`;

  const closest = [...ov.positions].sort((a, b) => a.distance_to_liquidation_pct - b.distance_to_liquidation_pct)[0];
  const minD = rm.min_distance_to_liquidation;
  const md = $("kpiMinDist");
  md.textContent = pct(minD);
  md.className = `kpi-value c-${tierFor(minD, closest?.symbol)}`;
  $("kpiMinDistSym").textContent = closest ? `${closest.symbol} · ${sideLabel(closest.side)}` : "—";
  $("kpiSafety").textContent = money(rm.total_margin_needed_for_safety);
  $("kpiSafetySub").textContent = rm.total_margin_needed_for_safety > 0
    ? L("kpi_safety_sub", { target: S.safetyTarget ?? 30 }) : L("kpi_safety_ok");

  const eff = sum.total_margin_used ? sum.total_notional_value / sum.total_margin_used : 0;
  $("exposureLine").textContent = L("exposure_line", {
    margin: money(sum.total_margin_used), lev: eff.toFixed(1), net: money(ex.net_exposure, { signed: true }),
    long: money(ex.long_exposure), short: money(ex.short_exposure),
  });

  renderPositions(ov.positions);
  renderHero();
}

function renderSummary(summary) {
  if (!summary) {
    $("headline").textContent = L("muse_summarizing");
    $("summaryTag").hidden = true;
    renderHero();
    return;
  }
  $("headline").textContent = summary.headline;
  const summarySource = museSource(summary);
  $("summaryTag").textContent = summarySource === "rules" ? "" : L(summarySource === "muse" ? "muse_mark" : "muse_mark_mock");
  $("summaryTag").hidden = summarySource === "rules";
  renderHero();
}

/** Triage #1 (the same position Telegram and the Muse summary name), else the closest one. */
function riskiestPosition() {
  const ov = S.overview;
  if (!ov?.positions?.length) return null;
  const sym = S.insights?.focus?.symbol || S.summary?.riskiest_symbol || ov.riskiest_position?.symbol;
  return ov.positions.find((p) => p.symbol === sym) || ov.riskiest_position || ov.positions[0];
}

function renderHero() {
  const ov = S.overview;
  const hero = $("banner");
  if (!ov) return;
  const rp = riskiestPosition();
  const counts = { warning: 0, danger: 0 };
  for (const p of ov.positions) {
    const st = statusOf(tierFor(p.distance_to_liquidation_pct, p.symbol));
    if (st !== "safe") counts[st] += 1;
  }
  const status = counts.danger ? "danger" : counts.warning ? "warning" : "safe";
  hero.className = `hero card status-${status}`;
  $("riskLevel").textContent = L(`status_${status}`);
  $("statusSub").textContent = counts.danger ? L("status_danger_sub", { n: counts.danger })
    : counts.warning ? L("status_warning_sub", { n: counts.warning }) : L("status_safe_sub", { n: ov.positions.length });

  if (!rp) {
    $("riskiest").textContent = L("no_positions");
    $("heroDist").textContent = "—";
    $("riskiestWhy").hidden = true;
    $("heroAction").textContent = L("action_none");
    $("heroActionSub").textContent = "";
    $("heroRescueBtn").hidden = true;
    $("heroDetailBtn").hidden = true;
    return;
  }
  const tier = tierFor(rp.distance_to_liquidation_pct, rp.symbol);
  $("riskiest").replaceChildren(symTag(rp.symbol, "span"), " ", venueTag(rp.venue), " ", sideTag(rp.side),
    el("span", { class: "lev" }, ` ${rp.leverage}×`));
  const dist = $("heroDist");
  dist.textContent = pct(rp.distance_to_liquidation_pct);
  dist.className = `c-${tier}`;
  const focus = S.insights?.focus;
  const explain = focus && focus.symbol === rp.symbol && (S.insights?.triage || []).length > 1
    && !["closest", "only_position"].includes(focus.basis_code);
  $("riskiestWhy").textContent = explain ? focus.why : "";
  $("riskiestWhy").hidden = !explain;

  const advice = S.adviceBySym[rp.symbol];
  const plan = advice?.rescue;
  const best = plan?.options?.find((o) => o.kind === plan.recommended);
  const margin = rp.margin_advice, reduce = rp.reduction_advice;
  let title, sub;
  if (status === "safe") {
    title = L("action_none");
    sub = margin?.margin_to_add_usd > 0
      ? L("action_optional", { amount: money(margin.margin_to_add_usd), symbol: rp.symbol, target: margin.target_distance_pct })
      : L("action_watching");
  } else if (best && !plan.already_safe) {
    title = L("action_cheapest", { title: best.title });
    sub = L("action_cheapest_sub", { cost: money(best.costs.total_cost_usd, { dp: 2 }), dist: distText(best.new_distance_pct, best.no_liquidation) });
  } else if (margin?.margin_to_add_usd > 0) {
    title = L("action_add_margin", { amount: money(margin.margin_to_add_usd), symbol: rp.symbol });
    sub = reduce?.reduce_size_pct
      ? L("action_or_reduce", { pct: pct(reduce.reduce_size_pct), target: margin.target_distance_pct })
      : L("action_target", { target: margin.target_distance_pct });
  } else {
    title = L("action_review", { symbol: rp.symbol });
    sub = L("action_watching");
  }
  $("heroAction").textContent = title;
  $("heroActionSub").textContent = sub;
  $("heroRescueBtn").hidden = false;
  $("heroDetailBtn").hidden = false;
  $("heroRescueBtn").onclick = () => loadRescue(rp.symbol);
  $("heroDetailBtn").onclick = () => openPosition(rp.symbol);
}

function positionOrder(positions) {
  const triage = S.insights?.triage || [];
  const rank = Object.fromEntries(triage.map((t) => [t.symbol, t.rank]));
  return [...positions].sort((a, b) => (rank[a.symbol] ?? 99) - (rank[b.symbol] ?? 99)
    || a.distance_to_liquidation_pct - b.distance_to_liquidation_pct);
}

function openPosition(sym) {
  S.openSym = sym;
  if (S.overview) renderPositions(S.overview.positions);
  const row = document.querySelector(`[data-pos="${CSS.escape(sym)}"]`);
  if (row) row.scrollIntoView({ behavior: "smooth", block: "center" });
}

function togglePosition(sym) {
  S.openSym = S.openSym === sym ? null : sym;
  if (S.overview) renderPositions(S.overview.positions);
}

function renderPositions(positions) {
  const box = $("positions");
  if (!positions.length) {
    box.replaceChildren(empty("📭", L("no_positions"), L("no_positions_open")));
    return;
  }
  const scale = Math.max(35, S.safetyTarget ? S.safetyTarget + 5 : 35);
  box.replaceChildren(...positionOrder(positions).map((p) => {
    const sym = p.symbol;
    const d = p.distance_to_liquidation_pct;
    const tier = tierFor(d, sym);
    const prev = S.lastSeverity[sym];
    S.lastSeverity[sym] = tier;
    const open = S.openSym === sym;
    const row = el("div", {
      class: `prow tier-${tier}${open ? " open" : ""}${prev && prev !== tier ? " flash" : ""}`,
      style: { "--flash": alpha(COLORS[tier], 0.5) }, role: "button", tabindex: "0", "aria-expanded": open ? "true" : "false",
      "data-pos": sym, title: L("position_toggle"),
      onclick: () => togglePosition(sym),
      onkeydown: (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); togglePosition(sym); } },
    },
      el("span", { class: "prow-sym" }, el("i", { class: "prow-dot" }), symTag(sym, "span")),
      el("span", { class: "prow-venue" }, venueTag(p.venue)),
      el("span", { class: "prow-side" }, sideTag(p.side), el("span", { class: "lev" }, ` ${p.leverage}×`)),
      el("span", { class: "prow-dist" },
        el("span", { class: "prow-bar" }, el("i", { style: { width: `${Math.min(d / scale, 1) * 100}%`, background: COLORS[tier] } })),
        el("b", { class: `c-${tier}` }, pct(d))),
      el("span", { class: `prow-risk risk-${tier}` }, tierLabel(tier)),
      el("span", { class: "prow-chev", "aria-hidden": "true" }, "›"),
    );
    return open ? el("div", { class: "pgroup open" }, row, positionDetail(p, tier, scale)) : el("div", { class: "pgroup" }, row);
  }));
}

function positionDetail(p, tier, scale) {
  const sym = p.symbol;
  const d = p.distance_to_liquidation_pct;
  const color = COLORS[tier];
  const th = tiersOf(sym);
  const ticks = TIERS.map((t) => el("div", {
    class: "gauge-tick", title: `${tierLabel(t)} ${th[t]}%`,
    style: { left: `${(Math.min(th[t], scale) / scale) * 100}%`, background: COLORS[t] },
  }));
  const labels = TIERS.map((t) => el("span", { style: { left: `${(Math.min(th[t], scale) / scale) * 100}%` } }, `${+th[t].toFixed(1)}`));
  return el("div", { class: "pdetail" },
    el("div", { class: "gauge" },
      el("div", { class: "gauge-fill", style: { width: `${Math.min(d / scale, 1) * 100}%`, background: `linear-gradient(90deg, ${alpha(color, 0.55)}, ${color})` } }),
      ...ticks,
    ),
    el("div", { class: "gauge-labels" }, ...labels),
    el("div", { class: "pos-meta" },
      el("span", {}, L("mark_at", { price: price(p.mark_price) })),
      el("span", {}, L("liq_at", { price: price(p.true_liquidation?.price ?? p.liquidation_price) })),
      el("span", { class: p.unrealized_pnl >= 0 ? "pos" : "neg" }, L("pnl_at", { pnl: money(p.unrealized_pnl, { signed: true }) })),
    ),
    liqDetail(p),
    fundingDetail(p.funding),
    protectionChips(S.protection[sym]),
    insightChips(sym),
    el("div", { class: "pos-actions" },
      el("button", { class: "mini-btn primary", type: "button", onclick: (ev) => { ev.stopPropagation(); loadRescue(sym); } }, L("btn_rescue_sim")),
      el("button", { class: "mini-btn", type: "button", onclick: (ev) => { ev.stopPropagation(); showOnChart(sym); } }, L("btn_chart")),
      toolButtons(sym, p.venue, false),
    ),
  );
}

function showOnChart(sym) {
  openFold("foldChart");
  setView(sym);
}

function liqDetail(p) {
  const t = p.true_liquidation;
  if (!t) return null;
  const parts = t.margin_mode === "cross"
    ? [L("liq_cross_wallet", { amount: money(t.wallet_balance) }), L("liq_other_pnl", { amount: money(t.other_upnl, { signed: true }) }),
      L("liq_other_maint", { amount: money(t.other_maintenance) })]
    : [L("liq_isolated", { amount: money(t.wallet_balance) })];
  const ex = t.exchange_price, diff = ex && t.price ? Math.abs(ex - t.price) / ex : 0;
  if (diff > 0.0005) parts.push(L("liq_exchange", { price: price(ex) }));
  return el("div", { class: "pos-detail" },
    el("span", { class: `mode mode-${t.margin_mode}` }, modeLabel(t.margin_mode)),
    el("span", {}, `${L("true_liq", { price: t.price ? price(t.price) : L("none") })} · ${parts.join(" · ")}`),
  );
}

function driftText(f) {
  return (f.drift || []).map((d) => `${d.horizon_hours}h → ${distText(d.distance_pct, d.liquidation_price == null)}`).join(" · ");
}

function fundingDetail(f) {
  if (!f) return null;
  const pays = f.next_payment_usd > 0;
  return el("div", { class: "pos-detail funding-line" },
    el("span", { class: "mode mode-funding" }, signedRate(f)),
    el("span", {}, L(pays ? "funding_pays_in" : "funding_receives_in", { amount: money(Math.abs(f.next_payment_usd), { dp: 2 }), when: duration(f.minutes_to_settlement) }),
      ` · ${L("funding_drift", { drift: driftText(f) })} `, el("i", {}, f.label)),
  );
}

/* ---------------- alert timeline ---------------- */

function clearEmpty(box) { const e = box.querySelector(".empty"); if (e) e.remove(); }

function channelChip(c) {
  return el("span", { class: `chip chip-${c}` }, `${channelName(c)} ✓`);
}

function rescueButton(sym, label) {
  return el("button", { class: "mini-btn", type: "button", onclick: () => loadRescue(sym) }, label || L("btn_rescue"));
}

function tlHead(label, cls, a) {
  return el("div", { class: "tl-head" },
    el("span", { class: `lvl ${cls}` }, label),
    a.symbol ? el("span", { class: "tl-sym", style: { color: symbolColor(a.symbol) } }, a.symbol) : null,
    sideTag(a.side),
    el("span", { class: "tl-time" }, clock(a.timestamp)),
  );
}

function trackPending(a, severity) {
  if (a.ack_id && !S.pending[a.ack_id] && S.escalation?.enabled !== false) {
    S.pending[a.ack_id] = { id: a.ack_id, symbol: a.symbol, severity, reminders_sent: 0, max_reminders: S.escalation?.max_reminders ?? 5, distance_pct: a.distance_pct };
    renderWake();
  }
}

function flashEmergency() {
  document.body.classList.add("flash-emergency");
  setTimeout(() => document.body.classList.remove("flash-emergency"), 1400);
}

function addFundingAlert(a, opts) {
  const f = a.funding;
  $("timeline").prepend(el("li", { class: "tl-item tl-funding" },
    el("span", { class: "tl-node" }),
    tlHead(L("lvl_prefunding"), "lvl-funding-b", a),
    el("div", { class: "tl-body" },
      L("prefunding_pays", { amount: money(f.payment_usd, { dp: 2 }), when: duration(f.minutes_to_settlement), rate: `${f.rate_pct.toFixed(4)}%/${f.interval_hours}h` }), " ",
      el("b", {}, pct(f.distance_before_pct)), " → ", el("b", {}, pct(f.distance_after_pct)),
      ` · ${L("prefunding_buffer", { share: pct(f.buffer_share_pct) })}`,
    ),
    el("div", { class: "chips" }, ...(a.channels || []).map(channelChip), rescueButton(a.symbol)),
  ));
  if (!opts.quiet) {
    toast("⏳", `${L("lvl_prefunding")} · ${a.symbol}`,
      L("prefunding_toast", { amount: money(f.payment_usd, { dp: 2 }), when: duration(f.minutes_to_settlement), dist: pct(f.distance_after_pct) }), COLORS.funding);
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
  if (!opts.quiet) trackPending(a, a.level);
  const widened = a.base_threshold_pct != null && Math.abs(a.base_threshold_pct - a.threshold_pct) > 0.01;
  box.prepend(el("li", { class: `tl-item tl-${a.level}` },
    el("span", { class: "tl-node" }),
    tlHead(tierLabel(a.level), `lvl-${a.level}-b`, a),
    el("div", { class: "tl-body" },
      L("alert_crossed", { tier: pct(a.threshold_pct, 2) }),
      widened ? el("span", { class: "faint" }, ` ${L("alert_base", { base: `${a.base_threshold_pct}%` })}`) : "",
      " · ", L("alert_now", { dist: pct(a.distance_pct) }),
      ` · ${L("alert_prices", { mark: price(a.mark_price), liq: price(a.liquidation_price) })}`,
      a.wicks?.max_wicks_away != null ? el("div", { class: "tl-sub" }, `🕯 ${L("alert_wicks", { n: a.wicks.max_wicks_away.toFixed(1), interval: a.wicks.worst_interval })}`) : null,
      a.adaptive_reasons?.length ? el("div", { class: "tl-sub" }, L("alert_widened", { reasons: a.adaptive_reasons.join(" · ") })) : null,
      a.liq_comparison?.gap_pct >= 1 ? el("div", { class: "tl-sub" },
        L("alert_corr_liq", { price: price(a.liq_comparison.correlated_price) }), " ",
        el("b", { class: "c-critical" }, L("pts_closer", { n: a.liq_comparison.gap_pct.toFixed(1) }))) : null,
    ),
    el("div", { class: "chips" }, priorityChip(a.priority), a.consolidated ? el("span", { class: "chip faint" }, L("in_triage_alert")) : null,
      ...(a.channels || []).map(channelChip), a.ack_id ? ackButton(a.ack_id, a.symbol) : null),
  ));
  if (a.consolidated) opts = { ...opts, quiet: true };
  if (!opts.quiet) {
    const sent = (a.channels || []).map(channelName).join(" + ");
    toast(LEVEL_ICON[a.level], `${tierLabel(a.level)} · ${a.symbol}`,
      L("alert_toast", { dist: pct(a.distance_pct), sent: sent || L("channel_web") }), COLORS[a.level], `tier:${a.symbol}`);
    if (a.level === "emergency") flashEmergency();
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

function addStopAlert(a, opts) {
  const m = a.explanation;
  S.stopMuse[a.symbol] = m;
  $("timeline").prepend(stopAlertNode(a));
  if (!opts.quiet) toast("🛑", `${L("lvl_stop")} · ${a.symbol}`, m?.headline || stopLabel(a.stop.status), COLORS.emergency);
  renderStops();
}

function updateStopAlert(a) {
  S.stopMuse[a.symbol] = a.explanation;
  document.querySelector(`#timeline [data-alert="${CSS.escape(a.id)}"]`)?.replaceWith(stopAlertNode(a));
  renderStops();
}

function stopAlertNode(a) {
  const s = a.stop, m = a.explanation;
  const label = stopLabel(s.status);
  return el("li", { class: "tl-item tl-stop", "data-alert": a.id },
    el("span", { class: "tl-node" }),
    tlHead(L("lvl_stop"), "lvl-stop-b", a),
    el("div", { class: "tl-body" },
      el("b", {}, label), s.stop_price ? ` · ${L("stop_at", { price: price(s.stop_price) })}` : "", ` · ${L("liq_at", { price: price(s.liquidation_price) })}`,
      m ? el("div", { class: "tl-muse" }, el("span", { class: "muse-glyph" }, "✦ "), m.explanation) : null,
    ),
    el("div", { class: "chips" }, ...(a.channels || []).map(channelChip), rescueButton(a.symbol)),
  );
}

function addAdlAlert(a, opts) {
  const adl = a.adl;
  $("timeline").prepend(el("li", { class: "tl-item tl-adl" },
    el("span", { class: "tl-node" }),
    tlHead(`ADL ${adl.lights}/5`, "lvl-adl-b", a),
    el("div", { class: "tl-body" }, adlLights(adl.lights), " ", L("adl_alert_body", { pnl: money(a.unrealized_pnl, { signed: true }) })),
    el("div", { class: "chips" }, ...(a.channels || []).map(channelChip)),
  ));
  if (!opts.quiet) toast("⚡", `ADL ${adl.lights}/5 · ${a.symbol}`, L("adl_toast", { pnl: money(a.unrealized_pnl, { signed: true }) }), COLORS.funding);
}

function priorityChip(pr) {
  if (!pr || (pr.of || 0) < 2) return null;
  return pr.label
    ? el("span", { class: "chip chip-first", title: pr.reason }, `🎯 ${pr.label}`)
    : el("span", { class: "chip", title: pr.reason }, L("priority_rank", { rank: pr.rank, of: pr.of }));
}

function addTriageAlert(a, opts) {
  const items = a.items || [];
  $("timeline").prepend(el("li", { class: `tl-item tl-triage tl-${a.severity || "warning"}` },
    el("span", { class: "tl-node" }),
    tlHead(L("lvl_triage", { n: items.length }), "lvl-triage-b", a),
    el("ol", { class: "tl-order" }, ...items.map((t) => el("li", { class: t.label ? "first" : "" },
      el("b", { style: { color: symbolColor(t.symbol) } }, t.symbol), " ",
      el("span", { class: `c-${t.severity || "safe"}` }, `${tierLabel(t.severity)} ${pct(t.distance_pct)}`),
      el("span", { class: "faint" }, ` · ${minutesText(t.minutes_to_liq)}`),
      t.label ? el("span", { class: "first-tag" }, ` 🎯 ${t.label}`) : null,
    ))),
    el("div", { class: "chips" }, ...(a.channels || []).map(channelChip),
      a.ack_id ? ackButton(a.ack_id, a.symbol) : null,
      rescueButton(a.symbol, L("btn_rescue_sym", { symbol: a.symbol }))),
  ));
  if (!opts.quiet) trackPending(a, a.severity);
  if (!opts.quiet) {
    const [title, body] = triageToastText(items.map((t) => t.symbol), a.focus);
    toast("🎯", title, body, COLORS[a.severity] || COLORS.critical, "triage");
    if (a.severity === "emergency") flashEmergency();
  }
}

function addSizingAlert(a, opts) {
  const checks = a.sizing || [];
  $("timeline").prepend(el("li", { class: "tl-item tl-sizing" },
    el("span", { class: "tl-node" }),
    tlHead(L("lvl_sizing", { n: checks.length }), "lvl-sizing-b", a),
    el("div", { class: "tl-body" },
      checks.map((c) => el("div", {},
        el("b", { style: { color: symbolColor(c.symbol) } }, c.symbol), " ",
        L("sizing_loses", { risk: pct(c.risk_pct), exit: c.exit_label || c.exit_kind }), " ",
        el("b", {}, fmtSize(c.suggested_size)), el("span", { class: "faint" }, ` ${L("sizing_now", { size: fmtSize(c.size) })}`)))),
    el("div", { class: "chips" }, ...(a.channels || []).map(channelChip)),
  ));
  if (!opts.quiet) {
    toast("📏", L("sizing_toast_title"), L("sizing_toast", { symbols: checks.map((c) => c.symbol).join(", "), limit: checks[0]?.max_risk_pct }), COLORS.warning, "sizing");
  }
}

function addCorrelationAlert(a, opts) {
  const st = a.stress, conc = a.concentration || {};
  const move = st?.liq_move_down_pct;
  $("timeline").prepend(el("li", { class: `tl-item tl-corr tl-${a.severity || "warning"}` },
    el("span", { class: "tl-node" }),
    tlHead(a.severity ? L("lvl_stress", { tier: tierLabel(a.severity) }) : L("lvl_concentration"), "lvl-corr-b", a),
    el("div", { class: "tl-body" },
      move != null ? [L("stress_drop_liquidates", { move: pct(move), venue: venueLabel(st.venue) }), el("span", { class: "faint" }, ` ${L("at_alert_time")}`)] : "",
      conc.flagged ? el("div", { class: "tl-sub" }, `🧲 ${conc.message}`) : null,
    ),
    el("div", { class: "chips" }, ...(a.channels || []).map(channelChip)),
  ));
  if (!opts.quiet) {
    toast("🧲", L("pstress_title"), stressToastText() || conc.message, COLORS[a.severity] || COLORS.warning, "stress");
  }
}

/* ---------------- portfolio: triage / stress / liq comparison / sizing ---------------- */

function minutesText(m) {
  if (m == null) return L("no_liq");
  if (m < 120) return L("typical_min", { n: Math.round(m) });
  if (m < 72 * 60) return L("typical_h", { n: (m / 60).toFixed(1) });
  return L("typical_d", { n: Math.round(m / 1440) });
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
  else if (t?.at_risk && t.of > 1) chips.push(el("span", { class: "pchip", title: t.reason }, L("rank_of", { rank: t.rank, of: t.of })));
  const c = (ins.comparisons || []).find((x) => x.symbol === sym);
  if (c?.gap_pct >= 1) chips.push(el("span", { class: "pchip c-critical", title: L("real_liq_title") }, `🧲 ${L("real_liq_away", { dist: pct(c.correlated_distance_pct) })}`));
  const z = (ins.sizing || []).find((x) => x.symbol === sym);
  if (z?.status === "oversized") chips.push(el("span", { class: "pchip c-warning", title: (z.reasons || []).join(" · ") }, `📏 ${L("size_to", { size: fmtSize(z.suggested_size) })}`));
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
  $("concTitle").textContent = L("conc_headline", { share: pct(conc.dominant_share_pct, 0), side: sideLabel(conc.dominant_side), n: conc.dominant_count });
  $("concSub").textContent = L("conc_body", { symbols: conc.symbols.join(" · "), corr: conc.avg_correlation.toFixed(2) })
    + (move ? ` ${L(move.down ? "move_drop_liquidates" : "move_rise_liquidates", { move: pct(move.pct), venue: venueLabel(move.venue) })}` : "");
  $("concBets").textContent = conc.effective_bets == null ? "—" : conc.effective_bets.toFixed(1);
}

function renderTriage() {
  const box = $("triage");
  const items = S.insights?.triage || [];
  const atRisk = items.filter((t) => t.at_risk).length;
  setTag("triageTag", atRisk ? L("tag_at_risk", { n: atRisk }) : L("tag_all_clear"), atRisk ? "tag-hot" : "tag-ro");
  const focus = S.insights?.focus;
  const why = focus && items.length > 1 ? focus.why : "";
  $("triageWhy").hidden = !why;
  $("triageWhy").replaceChildren(...(why ? [el("b", {}, focus.label ? L("why_first") : L("ranked_first")), " ", why] : []));
  if (!items.length) { box.replaceChildren(empty("🎯", L("no_positions"), L("triage_empty_hint"))); return; }
  const maxScore = Math.max(...items.map((t) => t.score), 1);
  box.replaceChildren(...items.map((t) => el("li", { class: `tri${t.label ? " first" : ""}${t.at_risk ? "" : " calm"}` },
    el("div", { class: "tri-rank" }, t.rank),
    el("div", { class: "tri-main" },
      el("div", { class: "tri-head" },
        el("b", { style: { color: symbolColor(t.symbol) } }, t.symbol),
        sideTag(t.side),
        el("span", { class: `tri-sev c-${t.severity || "safe"}` }, tierLabel(t.severity)),
        t.label ? el("span", { class: "first-tag" }, `🎯 ${t.label}`) : null,
      ),
      el("div", { class: "tri-bar" }, el("i", { style: { width: `${(t.score / maxScore) * 100}%`, background: COLORS[t.severity || "safe"] } })),
      el("div", { class: "tri-meta" },
        el("span", {}, L("from_liq", { dist: pct(t.distance_pct) })),
        el("span", {}, minutesText(t.minutes_to_liq)),
        el("span", { title: `σ5m ${pct(t.sigma_5m_pct, 2)} ${L(t.sigma_source === "default" ? "sigma_default" : "sigma_wicks")}` },
          t.typical_moves == null ? L("no_liq") : L("typical_moves", { n: t.typical_moves.toFixed(1) })),
        el("span", {}, L("notional_of", { amount: money(t.notional) })),
      ),
    ),
    el("div", { class: "tri-score", title: L("score_title") }, t.score.toFixed(0)),
  )));
}

function renderPortfolioStress() {
  const box = $("pstress");
  const accounts = S.insights?.stress || [];
  const worst = accountLiqMove();
  setTag("pstressTag", worst?.down ? L("tag_liq_move", { move: pct(worst.pct) }) : accounts.length ? L("tag_no_liq_95") : "",
    worst?.down && worst.pct <= 10 ? "tag-hot" : "tag-ro");
  if (!accounts.length) { box.replaceChildren(empty("📉", L("pstress_empty"), L("pstress_empty_hint"))); return; }
  box.replaceChildren(...accounts.map((st) => {
    const down = st.shocks.filter((x) => x.market_move_pct < 0).sort((a, b) => b.market_move_pct - a.market_move_pct);
    const up = st.shocks.filter((x) => x.market_move_pct > 0);
    const move = st.liq_move_down_pct, uni = st.uniform_liq_move_down_pct;
    const band = move == null ? "safe" : move <= 2.5 ? "emergency" : move <= 5 ? "critical" : move <= 10 ? "warning" : "safe";
    const cell = (x) => el("div", { class: `ps-cell${x.liquidated ? " dead" : ""}` },
      el("div", { class: "ps-move" }, `${x.market_move_pct > 0 ? "+" : ""}${x.market_move_pct}%`),
      el("div", { class: "ps-out" }, x.liquidated ? L("liquidated") : L("margin_left", { amount: money(x.margin_left) })),
    );
    const notes = [L("by_beta")];
    if (uni != null) notes.push(L("if_equal", { move: pct(uni) }));
    if (st.liq_move_up_pct != null) notes.push(L("on_upside", { move: `+${pct(st.liq_move_up_pct)}` }));
    return el("div", { class: "ps-acct" },
      el("div", { class: "ps-head" },
        venueTag(st.venue),
        el("span", { class: "faint" }, L("ps_account", { n: st.symbols.length, equity: money(st.equity), maint: money(st.maintenance) })),
      ),
      el("div", { class: `ps-big c-${band}` },
        el("div", { class: "ps-n" }, move == null ? "> 95%" : `−${pct(move)}`),
        el("div", {}, el("b", {}, L("ps_liquidates")), el("div", { class: "faint" }, notes.join(" · "))),
      ),
      el("div", { class: "ps-grid" }, ...down.map(cell)),
      up.length ? el("div", { class: "ps-grid ps-up" }, ...up.map(cell)) : null,
    );
  }));
}

function renderLiqComparison() {
  const box = $("liqcmp");
  const comps = (S.insights?.comparisons || []).filter((c) => c.platform_price != null || c.correlated_price != null);
  const hotCount = comps.filter((c) => c.gap_pct != null && c.gap_pct >= 1).length;
  setTag("liqcmpTag", comps.length ? (hotCount ? L("tag_closer", { n: hotCount }) : L("tag_matches")) : "", hotCount ? "tag-hot" : "tag-ro");
  if (!comps.length) { box.replaceChildren(empty("📐", L("liqcmp_empty"), L("liqcmp_empty_hint"))); return; }
  const scale = Math.max(...comps.map((c) => c.platform_distance_pct || 0), 10);
  box.replaceChildren(...comps.map((c) => {
    const gap = c.gap_pct;
    const hot = gap != null && gap >= 1;
    const bar = (d, cls) => el("div", { class: `lc-bar ${cls}` }, el("i", { style: { width: `${Math.min((d || 0) / scale, 1) * 100}%` } }), el("span", {}, pct(d)));
    const move = c.correlated_market_move_pct;
    return el("div", { class: `lc-row${hot ? " hot" : ""}` },
      el("div", { class: "lc-head" },
        el("b", { style: { color: symbolColor(c.symbol) } }, c.symbol),
        el("span", { class: `mode mode-${c.margin_mode}` }, modeLabel(c.margin_mode)),
        hot ? el("span", { class: "lc-gap" }, `⚠️ ${L("pts_closer_shown", { n: gap.toFixed(1) })}`) : el("span", { class: "faint" }, L("matches_platform")),
      ),
      el("div", { class: "lc-line" }, el("span", { class: "lc-label" }, L("platform_shows")), el("span", { class: "lc-price" }, price(c.platform_price)), bar(c.platform_distance_pct, "plat")),
      el("div", { class: "lc-line" }, el("span", { class: "lc-label" }, L("liqguard_together")), el("span", { class: "lc-price" }, price(c.correlated_price)), bar(c.correlated_distance_pct, hot ? "real hot" : "real")),
      move != null ? el("div", { class: "faint lc-note" }, L(move < 0 ? "approx_drop" : "approx_rise", { move: pct(Math.abs(move)) })) : null,
    );
  }));
}

function renderSizing() {
  const box = $("sizing");
  const checks = S.insights?.sizing || [];
  const over = checks.filter((c) => c.status === "oversized").length;
  setTag("sizingTag", checks.length ? (over ? L("tag_oversized", { n: over }) : L("tag_sized_ok")) : "", over ? "tag-hot" : "tag-ro");
  if (checks.length) {
    const c0 = checks[0];
    $("sizingSub").textContent = L("sizing_limits", { equity: money(c0.equity), risk: c0.max_risk_pct, margin: c0.max_margin_pct })
      + (MODE === "me" ? ` ${L("sizing_settings_hint")}` : "");
  }
  if (!checks.length) { box.replaceChildren(empty("📏", L("sizing_empty"), L("sizing_empty_hint"))); return; }
  box.replaceChildren(...checks.map((c) => el("div", { class: `sz-row${c.status === "oversized" ? " over" : ""}` },
    el("div", { class: "sz-head" },
      el("b", { style: { color: symbolColor(c.symbol) } }, c.symbol),
      el("span", { class: "faint" }, L("sizing_row", { size: fmtSize(c.size), notional: money(c.notional), x: c.notional_x_equity?.toFixed(1) })),
      el("span", { class: c.status === "oversized" ? "c-warning sz-status" : "c-safe sz-status" }, c.status === "oversized" ? L("sizing_cut", { pct: pct(c.reduce_pct, 0) }) : L("sizing_fits")),
    ),
    el("div", { class: "sz-meters" },
      meter(L("meter_loss"), c.risk_pct, c.max_risk_pct, L("meter_loss_sub", { amount: money(c.loss_at_exit_usd), exit: c.exit_label || c.exit_kind })),
      meter(L("meter_margin"), c.margin_pct, c.max_margin_pct, L("meter_margin_sub", { amount: money(c.margin) })),
    ),
    c.status === "oversized" ? el("div", { class: "sz-suggest" }, L("suggested_size"), " ", el("b", {}, fmtSize(c.suggested_size)), ` (≈ ${money(c.suggested_notional)})`) : null,
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
    const r = await fetch(api("/scenario"), {
      method: "POST", credentials: "same-origin",
      headers: { ...CSRF_HEADERS, "Content-Type": "application/json" }, body: JSON.stringify({ scenario: key }),
    });
    if (r.status === 401) { toLogin(); return; }
    if (r.status === 409) toast("⏹", L("sim_running_title"), L("sim_running_body"), COLORS.warning);
    else if (!r.ok) throw new Error(`${r.status}`);
    else {
      const sc = S.scenarios.find((x) => x.key === key);
      toast("🎬", sc?.title || key, sc?.description || "", COLORS.safe);
    }
  } catch (err) {
    toast("⚠️", L("request_failed"), L("scenario_failed"), COLORS.critical);
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
  }, pending ? L("awake") : L("acknowledged"));
}

function refreshAckButtons() {
  document.querySelectorAll("[data-ack]").forEach((b) => b.replaceWith(ackButton(b.dataset.ack, b.dataset.sym)));
}

async function acknowledge(id) {
  const res = await postJson(api("/ack"), { ack_id: id });
  if (!res) return;
  for (const p of Object.values(S.pending)) if (p.id === id || p.symbol === res.symbol) p.acknowledged = true;
  renderWake();
  refreshAckButtons();
}

function reminderLabel(e) {
  return L(`reminder_${e.urgency}`, { n: e.reminder, max: e.max_reminders });
}

function addEscalation(e, opts = {}) {
  const p = S.pending[e.ack_id];
  if (p) { p.reminders_sent = e.reminder; p.urgency = e.urgency; }
  else if (!opts.quiet) S.pending[e.ack_id] = { id: e.ack_id, symbol: e.symbol, severity: e.severity, reminders_sent: e.reminder, max_reminders: e.max_reminders, urgency: e.urgency, distance_pct: e.distance_pct };
  const box = $("timeline");
  clearEmpty(box);
  box.prepend(el("li", { class: `tl-item tl-esc tl-esc-${e.urgency}` },
    el("span", { class: "tl-node" }),
    tlHead(reminderLabel(e), `lvl-esc-b lvl-esc-${e.urgency}`, e),
    el("div", { class: "tl-body" },
      L("reminder_body", { tier: tierLabel(e.severity), minutes: Math.round(e.minutes_since_alert) }), " ", el("b", {}, pct(e.distance_pct))),
    el("div", { class: "chips" }, ...(e.channels || []).map(channelChip), ackButton(e.ack_id, e.symbol)),
  ));
  if (!opts.quiet) {
    toast("⏰", `${L(e.urgency === "final" ? "wake_final" : "wake_up")} · ${e.symbol}`, L("wake_toast", { n: e.reminder, max: e.max_reminders }), COLORS[e.severity] || COLORS.critical);
    const wake = $("wake");
    wake.classList.remove("ring");
    void wake.offsetWidth;
    wake.classList.add("ring");
    renderWake();
  }
}

function onAck(data) {
  for (const p of Object.values(S.pending)) if (p.id === data.ack_id || p.symbol === data.symbol) p.acknowledged = true;
  $("timeline").prepend(el("li", { class: "tl-item tl-ack" },
    el("span", { class: "tl-node" }),
    tlHead(L("lvl_ack"), "lvl-ack-b", data),
    el("div", { class: "tl-body" }, L("ack_body", { tier: tierLabel(data.severity), source: sourceName(data.source), n: data.reminders_sent })),
  ));
  toast("✅", `${L("lvl_ack")} · ${data.symbol}`, L("ack_toast", { source: sourceName(data.source) }), COLORS.safe);
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
  $("wakeTitle").textContent = L("wake_waiting", { n: items.length });
  $("wakeSub").textContent = esc.interval_minutes
    ? L("wake_sub", { interval: esc.interval_minutes, seconds: esc.seconds_per_minute, max: esc.max_reminders })
    : L("wake_sub_plain");
  $("wakeList").replaceChildren(...items.map((a) => el("div", { class: `wake-item c-${a.severity}` },
    el("b", { style: { color: symbolColor(a.symbol) } }, a.symbol),
    el("span", {}, `${tierLabel(a.severity)} · ${pct(a.distance_pct)}`),
    el("span", { class: "faint" }, L("wake_sent", { n: a.reminders_sent || 0, max: a.max_reminders })),
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
    chips.push(el("span", { class: `pchip ${wicksClass(pr.wicks.max_wicks_away)}`, title: L("deepest_wick", { interval: pr.wicks.worst_interval }) },
      `🕯 ${L("max_wicks", { n: pr.wicks.max_wicks_away.toFixed(1) })}`));
  }
  if (pr.stop) chips.push(el("span", { class: `pchip c-${STOP_TONE[pr.stop.status] || "muted"}` }, stopLabel(pr.stop.status)));
  else chips.push(el("span", { class: "pchip faint" }, L("orders_na")));
  if (pr.adl?.supported) {
    chips.push(el("span", { class: `pchip${pr.adl.warn ? " c-critical" : ""}`, title: pr.adl.note || "" }, "ADL ", adlLights(pr.adl.lights || 0)));
  } else if (pr.adl) chips.push(el("span", { class: "pchip faint", title: pr.adl.note || "" }, L("adl_na")));
  if (pr.thresholds?.adapted) chips.push(el("span", { class: "pchip", title: pr.thresholds.reasons.join(" · ") }, `📈 ${L("adaptive")}`));
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
    el("span", { class: "sess-clock" }, "🕒 ", t, el("span", { class: "faint" }, MODE === "me" || S.minutesPerStep ? ` · ${L("demo_clock")}` : "")),
    el("span", { class: `sess${s.weekend ? " on" : ""}` }, `${L("sess_weekend")} ×1.15`),
    el("span", { class: `sess${s.low_liquidity ? " on" : ""}` }, `${L("sess_thin")} ×1.10`),
    el("span", { class: `sess${stock ? " on" : ""}` }, `${L("sess_stocks")} ×1.5`),
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
  if (!entries.length) {
    setTag("stressTag", "");
    box.replaceChildren(empty("🕯", L("stress_empty"), L("stress_empty_hint")));
    return;
  }
  const sources = new Set(entries.map((p) => p.wicks?.source).filter(Boolean));
  const deadly = entries.filter((p) => p.wicks?.one_max_wick_liquidates).length;
  setTag("stressTag", deadly ? L("tag_one_wick", { n: deadly }) : sources.has("demo") ? L("tag_demo_wicks") : sources.size ? L("tag_live_klines") : L("tag_no_klines"),
    deadly ? "tag-hot" : "");
  box.replaceChildren(...entries.map((p) => {
    const w = p.wicks, th = p.thresholds;
    const byInt = Object.fromEntries((w?.exposures || []).map((e) => [e.interval, e]));
    const cell = (iv) => {
      const e = byInt[iv];
      if (!e) return el("div", { class: "st-cell faint" }, `${iv} —`);
      return el("div", { class: "st-cell" },
        el("div", { class: "st-iv" }, iv, e.window_days < 29 ? el("span", { class: "faint" }, ` · ${e.window_days.toFixed(1)}d`) : null),
        el("div", {}, el("b", { class: wicksClass(e.max_wicks_away) }, e.max_wicks_away == null ? "—" : `${e.max_wicks_away.toFixed(1)}×`), el("span", { class: "faint" }, ` ${L("wick_max", { pct: pct(e.max_pct, 2) })}`)),
        el("div", {}, el("b", { class: wicksClass(e.typical_wicks_away / 4) }, e.typical_wicks_away == null ? "—" : `${e.typical_wicks_away.toFixed(0)}×`), el("span", { class: "faint" }, ` ${L("wick_typical", { pct: pct(e.typical_pct, 2) })}`)),
      );
    };
    const tiers = th ? TIERS.map((t) => el("span", { class: `st-tier c-${t}` },
      `${tierLabel(t)} ${th.base[t]}→`, el("b", {}, `${+th.effective[t].toFixed(1)}%`))) : [];
    return el("div", { class: `st-row${w?.one_max_wick_liquidates ? " danger" : ""}` },
      el("div", { class: "st-sym" },
        el("b", { style: { color: symbolColor(p.symbol) } }, p.symbol),
        venueTag(p.venue),
        el("span", { class: `c-${tierFor(p.distance_pct, p.symbol)}` }, pct(p.distance_pct)),
      ),
      el("div", { class: "st-big" },
        el("div", { class: `st-n ${wicksClass(w?.max_wicks_away)}` }, w?.max_wicks_away == null ? "—" : w.max_wicks_away.toFixed(1)),
        el("div", { class: "faint" }, w?.one_max_wick_liquidates ? L("one_wick_liquidates") : L("max_wicks_away")),
      ),
      cell("1m"), cell("5m"),
      el("div", { class: "st-tiers" }, ...tiers,
        el("div", { class: "st-why faint" }, th?.reasons?.length ? th.reasons.join(" · ") : L("base_tiers"))),
    );
  }));
}

function renderStops() {
  const box = $("stops");
  const entries = S.order.map((s) => S.protection[s]).filter(Boolean);
  const bad = entries.filter((p) => p.stop?.needs_warning).length;
  setTag("stopsTag", entries.length ? (bad ? L("tag_needs_attention", { n: bad }) : L("tag_stops_ok")) : "", bad ? "tag-hot" : "tag-ro");
  if (!entries.length) { box.replaceChildren(empty("🛑", L("no_positions"), L("stops_empty_hint"))); return; }
  box.replaceChildren(...entries.map((p) => {
    const s = p.stop, adl = p.adl || {};
    const tone = s ? (STOP_TONE[s.status] || "muted") : "muted";
    const label = s ? stopLabel(s.status) : L("orders_unavailable");
    const muse = S.stopMuse[p.symbol];
    const fixes = s?.needs_warning ? (s.fixes || []) : [];
    const gap = s?.gap_pct != null
      ? L("stop_gap", { gap: `${s.gap_pct >= 0 ? "" : "−"}${Math.abs(s.gap_pct).toFixed(2)}%`, need: `${s.required_gap_pct.toFixed(2)}%` })
      : s ? L("stop_need", { need: `${s.required_gap_pct.toFixed(2)}%` }) : "";
    return el("div", { class: `sp-row sp-${tone}` },
      el("div", { class: "sp-head" },
        el("b", { style: { color: symbolColor(p.symbol) } }, p.symbol),
        venueTag(p.venue),
        el("span", { class: `sp-status c-${tone}` }, label),
        el("span", { class: "sp-adl", title: adl.note || "" },
          adl.supported ? [adlLights(adl.lights || 0), el("span", { class: adl.warn ? "c-critical" : "faint" }, ` ${adl.lights}/5`)] : el("span", { class: "faint" }, L("adl_na_hl"))),
      ),
      s ? el("div", { class: "sp-line" },
        s.stop_price ? `${L("stop_at", { price: price(s.stop_price) })} · ` : "",
        L("liq_at", { price: price(s.liquidation_price) }), ` · ${gap}`,
      ) : null,
      fixes.length ? el("div", { class: "sp-fixes" }, ...fixes.map((f) => el("span", {
        class: `sp-fix${muse?.suggested_fix === f.kind ? " best" : ""}`,
      }, `${FIX_ICON[f.kind] || "•"} ${f.title}`, muse?.suggested_fix === f.kind ? el("span", { class: "star" }, ` ★ ${L("muse_pick")}`) : null))) : null,
      muse && s?.needs_warning ? el("div", { class: "sp-muse" }, el("span", { class: "muse-glyph" }, "✦ "), muse.explanation) : null,
    );
  }));
}

/* ---------------- Muse advice & audit ---------------- */

function updateHistoryCounts() {
  setTag("adviceCount", String(S.adviceCount));
  setTag("auditCount", String(S.auditCount));
}

function addAdvice(c) {
  const box = $("advice");
  clearEmpty(box);
  S.adviceBySym[c.symbol] = c;
  S.adviceCount += 1;
  updateHistoryCounts();
  box.prepend(adviceNode(c));
  if (c.rescue && (!S.rescue || S.rescue.auto)) showRescue(c.symbol, { plan: c.rescue, explanation: c.rescue_explanation }, { auto: true });
}

/** Muse's text arrived for a card already shown with the rule-based text: swap it in place. */
function updateAdvice(c) {
  const old = document.querySelector(`#advice [data-advice="${CSS.escape(c.id)}"]`);
  if (!old) return;
  old.replaceWith(adviceNode(c));
  if (S.adviceBySym[c.symbol]?.id === c.id) S.adviceBySym[c.symbol] = c;
  if (c.rescue && S.rescue?.auto && S.rescue.symbol === c.symbol) {
    showRescue(c.symbol, { plan: c.rescue, explanation: c.rescue_explanation }, { auto: true });
  }
}

/**
 * Who wrote an explanation: "muse" (Muse answered), "rules" (a key is configured and Muse's answer is
 * pending or failed) or "mock" (no key). Only the no-key demo text is ever labelled "(mock)".
 */
function museSource(x) {
  if (!x) return "mock";
  if (x.mock === false || x.live === true) return "muse";
  return x.source === "rules" ? "rules" : "mock";
}

/** The one recommendation a card or panel leads with: Muse's wording once it answered, else the rule-based text. */
function recLine(text, source, attrs = {}) {
  const src = source === true ? "muse" : source === false ? "mock" : source;
  const live = src === "muse";
  return el("div", { class: `rec-line${live ? " live" : ""}`, "data-live": String(live), "data-source": src, ...attrs },
    el("span", { class: "rec-text" }, `💡 ${text}`),
    src === "rules" ? null : " ",
    src === "rules" ? null : el("span", { class: "muse-mark" }, L(live ? "muse_mark" : "muse_mark_mock")));
}

function adviceNode(c) {
  const actions = [];
  const rescueLed = c.rescue && !c.rescue.already_safe;
  if (!rescueLed && c.margin_to_add_usd > 0) {
    actions.push(el("div", { class: "adv-act" },
      el("div", { class: "adv-act-label" }, L("opt_add_margin")),
      el("div", { class: "adv-act-value c-safe" }, money(c.margin_to_add_usd, { dp: 2 })),
      el("div", { class: "adv-act-sub" }, L("adv_new_liq", { price: price(c.new_liquidation_price), target: c.target_distance_pct })),
    ));
  }
  if (!rescueLed && c.reduce_size_pct > 0) {
    actions.push(el("div", { class: "adv-act" },
      el("div", { class: "adv-act-label" }, L("adv_or_reduce")),
      el("div", { class: "adv-act-value c-warning" }, `${c.reduce_size_pct.toFixed(1)}%`),
      el("div", { class: "adv-act-sub" }, L("adv_close", { size: c.reduce_size_by, coin: c.symbol.replace(/USDT$/, "") })),
    ));
  }
  return el("div", { class: `adv adv-${c.level}`, "data-advice": c.id },
    el("div", { class: "adv-head" },
      el("span", { class: "adv-sym", style: { color: symbolColor(c.symbol) } }, c.symbol),
      el("span", { class: `lvl lvl-${c.level}-b` }, tierLabel(c.level)),
    ),
    recLine(c.recommendation?.text ?? c.explanation, c.recommendation?.source ?? !!c.recommendation?.live, { "data-rec": "advice" }),
    actions.length ? el("div", { class: "adv-actions" }, ...actions) : null,
    c.rescue ? rescueMini(c) : null,
    c.ack_id ? el("div", { class: "chips" }, ackButton(c.ack_id, c.symbol)) : null,
    el("div", { class: "adv-foot" }, L("adv_foot")),
  );
}

function rescueMini(c) {
  const plan = c.rescue;
  return el("div", { class: "adv-rescue" },
    el("div", { class: "adv-act-label" }, L("rescue_mini_title")),
    ...plan.options.map((o) => el("div", { class: `rm-row${o.kind === plan.recommended ? " best" : ""}` },
      el("span", {}, optionLabel(o.kind)),
      el("span", {}, distText(o.new_distance_pct, o.no_liquidation)),
      el("span", {}, money(o.costs.total_cost_usd, { dp: 2 })),
      o.kind === plan.recommended ? el("span", { class: "star" }, "★") : el("span", {}),
    )),
    el("button", { class: "mini-btn", type: "button", onclick: () => { showRescue(c.symbol, { plan, explanation: c.rescue_explanation }); openFold("rescueCard"); } }, L("open_in_simulator")),
  );
}

function auditEventLabel(event) {
  return hasL(`ev_${event}`) ? L(`ev_${event}`) : event.replace(/_/g, " ");
}

function addAudit(e) {
  const box = $("audit");
  clearEmpty(box);
  S.auditCount += 1;
  updateHistoryCounts();
  box.prepend(el("div", { class: "au" },
    el("div", { class: "au-time" }, clock(e.timestamp)),
    el("div", {},
      el("div", { class: `au-event ev-${e.event}` }, auditEventLabel(e.event),
        e.symbol ? ` · ${e.symbol}` : "", el("span", { class: "src" }, sourceName(e.source))),
      el("div", { class: "au-detail" }, e.detail),
    ),
  ));
  while (box.children.length > 120) box.lastChild.remove();
}

function triageToastText(order, focus) {
  const [first, ...rest] = order;
  const why = focus && focus.symbol === first && focus.basis && focus.basis_code !== "closest" ? ` (${focus.basis})` : "";
  const body = L("triage_toast", { first, why }) + (rest.length ? ` ${L("triage_toast_then", { rest: rest.join(", ") })}` : "");
  return [L("triage_toast_title", { n: order.length }), body];
}

function accountLiqMove() {
  const w = S.insights?.worst_account;
  if (!w || w.first_liquidation_move_pct == null) return null;
  return { pct: Math.abs(w.first_liquidation_move_pct), down: w.first_liquidation_move_pct < 0, venue: w.venue };
}

function stressToastText() {
  const m = accountLiqMove();
  return m ? L(m.down ? "move_drop_liquidates" : "move_rise_liquidates", { move: pct(m.pct), venue: venueLabel(m.venue) }) : null;
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
    venueTag(venue),
    el("span", { class: "wallet-label" }, L("cross_wallet")),
    el("b", {}, money(w, { dp: 2 })),
  )));
  wallets.hidden = !entries.length;

  const box = $("funding");
  const positions = (S.overview?.positions || []).filter((p) => p.funding);
  const sources = new Set(positions.map((p) => p.funding.source));
  setTag("fundingTag", positions.length ? L(sources.has("live") ? "tag_live_rates" : "tag_demo_rates") : "", sources.has("live") ? "tag-ro" : "");
  const rows = positions.map((p) => {
    const f = p.funding, pays = f.next_payment_usd > 0;
    return el("div", { class: "fund-row" },
      el("div", { class: "fund-top" },
        el("b", { style: { color: symbolColor(p.symbol) } }, p.symbol),
        venueTag(p.venue),
        el("span", { class: "fund-rate" }, signedRate(f)),
        el("span", { class: "fund-apr" }, `${f.annualized_pct.toFixed(1)}% APR`),
      ),
      el("div", { class: "fund-meta" },
        `${L("next_settlement", { when: duration(f.minutes_to_settlement) })} · `,
        el("span", { class: pays ? "neg" : "pos" }, L(pays ? "funding_pays" : "funding_receives", { amount: money(Math.abs(f.next_payment_usd), { dp: 2 }) })),
      ),
      el("div", { class: "fund-drift" },
        el("span", {}, `${L("liq_distance_now")} `, el("b", {}, pct(p.distance_to_liquidation_pct))),
        ...(f.drift || []).map((d) => el("span", {}, `${d.horizon_hours}h `, el("b", {}, distText(d.distance_pct, d.liquidation_price == null)),
          ` ${L("drift_detail", { n: d.settlements, pool: money(d.pool_payment_usd, { signed: true }) })}`)),
        el("i", {}, f.label),
      ),
    );
  });
  const events = S.fundingEvents.slice(-6).reverse().map((e) => el("div", { class: "fund-ev" },
    el("span", { class: "fund-ev-sym", style: { color: symbolColor(e.symbol) } }, `▲ ${e.symbol}`),
    el("span", {}, L(e.payment_usd > 0 ? "funding_paid" : "funding_received", { amount: money(Math.abs(e.payment_usd), { dp: 2 }) })),
    el("span", { class: "fund-ev-liq" }, L("liq_moved", { before: price(e.liquidation_before), after: price(e.liquidation_after) })),
  ));
  box.replaceChildren(...rows);
  if (!rows.length) box.append(empty("💸", L("funding_empty"), L("funding_empty_hint")));
  if (events.length) box.append(el("div", { class: "account-label fund-ev-head" }, L("recent_settlements")), ...events);
}

/* ---------------- rescue simulator ---------------- */

function buildRescueTabs() {
  const tabs = $("rescueTabs");
  const active = S.rescue?.symbol;
  tabs.replaceChildren(...S.order.map((sym) => el("button", {
    class: `tab${active === sym && !S.rescue?.live ? " active" : ""}`, type: "button", role: "tab", onclick: () => loadRescue(sym, { scroll: false }),
  }, sym)));
  setTag("rescueTag", active || "");
}

async function loadRescue(symbol, opts = {}) {
  S.rescue = { symbol, loading: true };
  buildRescueTabs();
  renderRescue();
  openFold("rescueCard", opts.scroll !== false);
  try {
    const r = await fetch(api("/rescue", { symbol }), { credentials: "same-origin" });
    if (r.status === 401) { toLogin(); return; }
    if (!r.ok) throw new Error(`${r.status}`);
    const data = await r.json();
    showRescue(symbol, data);
    if (data.muse_pending && !socketOpen()) awaitMuseRescue(api("/rescue", { symbol, wait: 1 }), symbol, {});
  } catch (err) {
    S.rescue = { symbol, error: L("rescue_failed") };
    renderRescue();
  }
}

async function loadLiveRescue(symbol, venue) {
  S.rescue = { symbol, loading: true, live: true };
  renderRescue();
  openFold("rescueCard");
  try {
    const url = `/api/me/rescue/live?${new URLSearchParams({ symbol, venue })}`;
    const r = await fetch(url, { credentials: "same-origin" });
    if (r.status === 401) { toLogin(); return; }
    if (!r.ok) throw new Error(`${r.status}`);
    const data = await r.json();
    showRescue(symbol, data, { live: true });
    if (data.muse_pending && !socketOpen()) awaitMuseRescue(`${url}&wait=1`, symbol, { live: true });
  } catch (err) {
    S.rescue = { symbol, live: true, error: L("rescue_live_failed") };
    renderRescue();
  }
}

const socketOpen = () => ws?.readyState === WebSocket.OPEN;

/** Muse's trade-off text, pushed over the WebSocket, replaces the rule-based one if that rescue is still open. */
function updateRescueMuse(data) {
  const r = S.rescue;
  if (!r || r.loading || r.error || !r.plan || r.symbol !== data.symbol) return;
  if ((r.live ? "live" : "sim") !== data.scope || r.plan.venue !== data.venue) return;
  showRescue(r.symbol, { plan: r.plan, explanation: data.explanation }, { live: r.live, auto: r.auto });
}

/** Without a socket (reconnecting), ask again and wait for Muse instead. */
async function awaitMuseRescue(url, symbol, opts) {
  try {
    const r = await fetch(url, { credentials: "same-origin" });
    if (!r.ok) return;
    const data = await r.json();
    if (S.rescue?.symbol === symbol && !S.rescue.loading && !!S.rescue.live === !!opts.live && data.explanation && !data.explanation.mock) {
      showRescue(symbol, data, opts);
    }
  } catch (err) { /* keep the rule-based text */ }
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
    body.replaceChildren(empty("🛟", L("rescue_pick"), L("rescue_pick_hint")));
    return;
  }
  if (r.loading) { body.replaceChildren(empty("⏳", L("rescue_loading", { symbol: r.symbol }), L("rescue_loading_hint"))); return; }
  if (r.error) { body.replaceChildren(empty("⚠️", L("rescue_unavailable"), r.error)); return; }

  const plan = r.plan, cur = plan.current, ex = r.explanation;
  $("rescueSub").textContent = L("rescue_sub_plan", { scope: L(r.live ? "rescue_scope_live" : "rescue_scope_demo"), target: plan.target_distance_pct, label: plan.funding_label });
  const trade = Object.fromEntries((ex?.trade_offs || []).map((t) => [t.option, t]));

  const head = el("div", { class: "rs-head" },
    el("span", { class: "pos-sym", style: { color: symbolColor(plan.symbol) } }, plan.symbol),
    venueTag(plan.venue),
    sideTag(plan.side),
    el("span", { class: `mode mode-${plan.margin_mode}` }, modeLabel(plan.margin_mode)),
    el("span", { class: "rs-now" }, `${L("mark_at", { price: price(plan.mark_price) })} · ${L("liq_at", { price: price(cur.price) })} · `,
      el("b", { class: `c-${tierFor(cur.distance_pct)}` }, distText(cur.distance_pct, cur.price == null)), ` ${L("away")}`),
  );

  const cols = plan.options.map((o) => {
    const best = o.kind === plan.recommended;
    const c = o.costs, t = trade[o.kind];
    return el("div", { class: `rs-opt${best ? " best" : ""}${o.protects_liquidation ? "" : " weak"}` },
      el("div", { class: "rs-opt-head" },
        el("span", { class: "rs-kind" }, optionLabel(o.kind)),
        best ? el("span", { class: "tag tag-best" }, L("recommended")) : null,
        !o.protects_liquidation ? el("span", { class: "tag" }, L("no_liq_move")) : null,
      ),
      el("div", { class: "rs-title" }, o.title),
      el("div", { class: "rs-liq" },
        el("div", {}, el("div", { class: "adv-act-label" }, L("new_liq")), el("div", { class: "rs-big" }, o.new_liquidation_price ? price(o.new_liquidation_price) : L("none"))),
        el("div", {}, el("div", { class: "adv-act-label" }, L("distance")), el("div", { class: `rs-big c-${o.reaches_target ? "safe" : tierFor(o.new_distance_pct)}` }, distText(o.new_distance_pct, o.no_liquidation))),
      ),
      el("dl", { class: "rs-costs" },
        el("dt", {}, L("cost_fees")), el("dd", {}, money(c.fees_usd, { dp: 2 })),
        el("dt", {}, L("cost_realized")), el("dd", {}, money(c.realized_loss_usd, { dp: 2 })),
        el("dt", {}, L("cost_funding")), el("dd", {}, money(c.funding_24h_usd, { dp: 2, signed: true })),
        el("dt", { class: "total" }, L("cost_total")), el("dd", { class: "total" }, money(c.total_cost_usd, { dp: 2 })),
        el("dt", {}, L("cost_capital")), el("dd", {}, money(c.capital_required_usd, { dp: 2 })),
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
    ex ? recLine(ex.headline, museSource(ex), { "data-rec": "rescue" }) : null,
    plan.already_safe
      ? empty("✅", L("rescue_safe", { target: plan.target_distance_pct }), L("rescue_safe_hint"))
      : el("div", { class: "rs-grid" }, ...cols),
    ex?.caveat ? el("div", { class: "rs-caveat" }, ex.caveat) : null,
    el("div", { class: "adv-foot" }, L("rescue_foot")),
  );
}

/* ---------------- controls ---------------- */

function setStatus(st) {
  S.state = st.state;
  const running = st.state === "running";
  const sim = $("simulateBtn");
  sim.disabled = running;
  sim.classList.toggle("running", running);
  $("simulateLabel").textContent = L(running ? "simulating" : st.state === "finished" || st.state === "stopped" ? "replay" : "simulate");
  $("stopBtn").disabled = !running;
  $("scenarioSelect").disabled = running;
  setProgress();
}

function setProgress() {
  $("progressFill").style.width = S.state === "idle" ? "0" : `${(S.step / S.totalSteps) * 100}%`;
}

function toLogin() { location.assign("/login?error=session"); }

function requestFailed(path) {
  toast("⚠️", L("request_failed"), L("request_failed_body", { path: path.split("?")[0] }), COLORS.critical);
}

async function post(path) {
  try {
    const r = await fetch(path, { method: "POST", headers: CSRF_HEADERS, credentials: "same-origin" });
    if (r.status === 401) { toLogin(); return null; }
    if (!r.ok) throw new Error(`${r.status}`);
    return await r.json();
  } catch (err) {
    requestFailed(path);
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
    if (r.status === 404) { toast("ℹ️", L("already_handled"), L("already_handled_body"), COLORS.text); return null; }
    if (!r.ok) throw new Error(`${r.status}`);
    return await r.json();
  } catch (err) {
    requestFailed(path);
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
    const bands = [[0, "emergency", 0.12], [t.emergency, "critical", 0.08], [t.critical, "warning", 0.05]];
    ctx.save();
    for (const [lo, tier, al] of bands) {
      const hi = t[tier], col = COLORS[tier];
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
      ctx.fillText(`${tierLabel(tier)} ${+hi.toFixed(1)}%`, a.right - 6, top - 4);
    }
    ctx.restore();
  },
};

function buildTabs() {
  const tabs = $("chartTabs");
  const mk = (id, label) => el("button", {
    class: `tab${S.view === id ? " active" : ""}`, type: "button", role: "tab", onclick: () => setView(id),
  }, label);
  tabs.replaceChildren(mk("distance", L("tab_distance")), ...S.order.map((s) => mk(s, s)));
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
  if (chart) { chart.destroy(); chart = null; }
  if (!$("foldChart").open) return;
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
    $("chartTitle").textContent = L("chart_title");
    $("chartSub").textContent = S.minutesPerStep ? L("chart_sub_clock", { minutes: S.minutesPerStep }) : L("chart_sub");
    if (S.session?.reasons?.length) $("chartSub").textContent += ` · ${L("chart_widened", { reasons: S.session.reasons.join(", ") })}`;
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
      el("span", { style: { color: COLORS.funding } }, `▲ ${L("legend_funding")}`),
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
  $("chartTitle").textContent = L("chart_symbol_title", { symbol: sym });
  $("chartSub").textContent = last ? L("chart_symbol_sub", { price: price(liq) }) : "";

  const ctx = $("chart").getContext("2d");
  const grad = ctx.createLinearGradient(0, 0, 0, 380);
  grad.addColorStop(0, alpha(col, 0.28));
  grad.addColorStop(1, alpha(col, 0.02));

  const datasets = [
    { label: L("legend_mark"), data: series(sym, "mark_price"), borderColor: col, backgroundColor: grad, fill: { target: 1 }, borderWidth: 2.5, pointRadius: 0, tension: 0.35, spanGaps: true },
    { label: L("legend_true_liq"), data: series(sym, "liquidation_price"), borderColor: COLORS.emergency, borderWidth: 2, borderDash: [6, 5], fill: false, spanGaps: true, stepped: true,
      pointRadius: fundingPoint(sym, 6), pointStyle: "triangle", pointBackgroundColor: COLORS.funding, pointBorderColor: COLORS.funding },
    ...TIERS.map((t) => ({ label: `${tierLabel(t)} (${+tiersOf(sym)[t].toFixed(1)}%)`, data: flat(tp[t]), borderColor: alpha(COLORS[t], 0.55), borderWidth: 1, borderDash: [3, 4], pointRadius: 0, fill: false })),
  ];
  const vals = [...h.map((p) => p.mark_price), ...h.map((p) => p.liquidation_price)].filter((v) => v != null);
  const lo = Math.min(...vals), hi = Math.max(...vals), pad = (hi - lo) * 0.08 || hi * 0.02;
  common.scales.y.min = lo - pad;
  common.scales.y.max = hi + pad;
  common.scales.y.ticks.callback = (v) => price(v);
  common.plugins.tooltip.callbacks = { label: (i) => ` ${i.dataset.label}: ${price(i.raw)}` };
  chart = new Chart($("chart"), { type: "line", data: { labels: labels(), datasets }, options: common });
  legend.replaceChildren(
    el("span", {}, el("i", { style: { background: col } }), L("legend_mark")),
    el("span", { style: { color: COLORS.emergency } }, el("i", { class: "dashed" }), L("legend_true_liq")),
    el("span", { style: { color: COLORS.funding } }, `▲ ${L("legend_funding")}`),
    ...TIERS.map((t) => el("span", { style: { color: COLORS[t] } }, el("i", { class: "dashed" }), L("legend_tier", { tier: tierLabel(t) }))),
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
  const q = MODE === "demo" ? `?lang=${encodeURIComponent(I18N.lang)}` : "";
  ws = new WebSocket(`${proto}//${location.host}/ws/${MODE}${q}`);
  ws.onopen = () => {
    retry = 0;
    $("connPill").classList.add("connected");
    $("connText").textContent = L("live");
  };
  ws.onclose = (ev) => {
    if (ev.code === 1008 && MODE === "me") { toLogin(); return; }
    $("connPill").classList.remove("connected");
    $("connText").textContent = L("reconnecting");
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
  const who = me.username ? `@${me.username}` : L("user_label", { id: me.user_id });
  document.title = `LiqGuard · ${who}`;
  $("ribbon").classList.add("private");
  $("ribbonTag").textContent = L("ribbon_private_tag");
  $("ribbonText").textContent = L("ribbon_private", { who });
  $("ribbonLink").hidden = true;
  const pill = $("scopePill");
  pill.textContent = L("scope_private", { who });
  pill.className = "pill pill-sm pill-private";
  pill.title = L("scope_private_title");
  $("signInBtn").hidden = true;
  $("signOutBtn").hidden = false;
  $("footScope").textContent = L("foot_signed_in", { who });
  $("account").hidden = false;
  await refreshAccount();
  setInterval(refreshAccount, 30000);
  setupAccountPanels();
  return true;
}

function venueTitle(b) {
  return `${venueLabel(b.venue)}${b.network === "testnet" ? L("testnet") : ""}`;
}

function actionTypeLabel(type) {
  const key = `action_type_${type}`;
  return hasL(key) ? L(key) : (type || "").replace(/_/g, " ");
}
function actionStatusLabel(status) {
  const key = `action_status_${status}`;
  return hasL(key) ? L(key) : status;
}

async function refreshAccount() {
  let data;
  try {
    const r = await fetch("/api/me/account", { credentials: "same-origin" });
    if (r.status === 401) { toLogin(); return; }
    data = await r.json();
  } catch (err) {
    $("accountSub").textContent = L("account_sub_error");
    return;
  }
  $("accountSub").textContent = L("account_sub");

  const binds = $("accountBindings");
  binds.replaceChildren(...(data.bindings || []).map((b) => el("div", { class: "binding" },
    el("span", { class: `venue venue-${b.venue}` }, venueTitle(b)),
    el("span", {}, b.binding_type === "wallet" ? L("binding_wallet") : L("binding_readonly")),
    el("span", { class: "hint" }, b.api_key || b.wallet_address || ""),
  )));
  if (!data.bindings?.length) binds.append(empty("🔌", L("no_exchanges"), L("no_exchanges_hint")));

  $("accountPositionsLabel").textContent = data.label ? data.label.replace(/^📊\s*/, "") : L("positions");
  const box = $("accountPositions");
  const positions = data.positions?.positions || [];
  box.replaceChildren(...positions.map((p) => el("div", { class: "acct-pos" },
    el("span", {}, el("b", {}, p.symbol), " ", sideTag(p.side), " ", venueTag(p.venue),
      p.margin_mode ? el("span", { class: `mode mode-${p.margin_mode}` }, modeLabel(p.margin_mode)) : null),
    el("span", {}, `${L("mark_at", { price: price(p.mark_price) })} · ${L("liq_at", { price: price(p.liquidation_price) })}`,
      p.true_liquidation?.price && Math.abs(p.true_liquidation.price - p.liquidation_price) / (p.liquidation_price || 1) > 0.0005
        ? ` · ${L("true_liq", { price: price(p.true_liquidation.price) })}` : ""),
    el("span", { class: `d c-${tierFor(p.distance_to_liquidation_pct)}` }, pct(p.distance_to_liquidation_pct)),
    el("button", { class: "mini-btn", type: "button", title: L("rescue_sim"), onclick: () => loadLiveRescue(p.symbol, p.venue) }, "🛟"),
    toolButtons(p.symbol, p.venue, true),
    protectionChips((data.protection || []).find((x) => x.symbol === p.symbol && x.venue === p.venue)),
    p.funding ? el("span", { class: "acct-fund" },
      `${L("funding_rate_line", { rate: signedRate(p.funding), next: duration(p.funding.minutes_to_settlement) })} · ${driftText(p.funding)} (${p.funding.label})`) : null,
  )));
  if (!positions.length) box.append(empty("📭", L("no_positions"), L(data.label ? "no_positions_open" : "no_positions_hint")));
  S.accountMuse = data.muse || null;
  const muse = accountMuseBlock(S.accountMuse);
  if (muse) box.prepend(muse);
  const live = accountInsights(data.insights);
  if (live) box.append(live);
  for (const a of data.escalations || []) {
    box.prepend(el("div", { class: `acct-wake c-${a.severity}` }, `⏰ ${a.symbol} ${tierLabel(a.severity)} · ${L("reminders", { sent: a.reminders_sent, max: a.max_reminders })} `,
      el("button", { class: "mini-btn ack-btn", type: "button", onclick: async () => {
        if (await postJson("/api/me/live/ack", { ack_id: a.id })) refreshAccount();
      } }, L("awake")),
      el("button", { class: "mini-btn", type: "button", onclick: async () => {
        const res = await postJson("/api/me/live/snooze", { ack_id: a.id });
        if (res) { toast("😴", L("snoozed_title"), L("snoozed_body", { symbol: res.symbol }), COLORS.text, `snooze-${a.id}`); refreshAccount(); }
      } }, L("snooze"))));
  }

  const acts = $("accountActions");
  acts.replaceChildren(...(data.actions || []).slice().reverse().map((a) => el("div", { class: "au" },
    el("div", { class: "au-time" }, clock(a.timestamp)),
    el("div", {},
      el("div", { class: "au-event" }, `${actionTypeLabel(a.action_type)} · ${a.symbol}`, el("span", { class: "src" }, actionStatusLabel(a.status))),
      el("div", { class: "au-detail" }, [a.amount_usd ? money(a.amount_usd, { dp: 2 }) : null, a.details?.reason || a.details?.result].filter(Boolean).join(" · ")),
    ),
  )));
  if (!data.actions?.length) acts.append(empty("📋", L("no_actions"), L(data.protect_enabled ? "no_actions_hint" : "actions_disabled")));
}

/** Muse's one recommendation for the signed-in book: same service, inputs and cache as Telegram /risk. */
function accountMuseBlock(m) {
  if (!m || (!m.summary && !m.suggestion)) return null;
  const s = m.summary, g = m.suggestion;
  const [text, mock, kind] = s ? [s.headline, s.mock, "summary"] : [g.explanation, g.mock, "suggestion"];
  return recLine(text, museSource(s || g), { id: "accountMuse", class: `rec-line acct-muse${mock ? "" : " live"}`, "data-view": m.view, "data-muse": kind, "data-mock": String(!!mock) });
}

function updateAccountMuse(data) {
  const m = S.accountMuse;
  if (!m || data.view !== m.view) return;
  if (data.summary) { m.summary = data.summary; m.summary_pending = false; }
  if (data.suggestion) { m.suggestion = data.suggestion; m.suggestion_pending = false; }
  const node = $("accountMuse"), next = accountMuseBlock(m);
  if (node && next) node.replaceWith(next);
}

function accountInsights(ins) {
  if (!ins) return null;
  const parts = [];
  const atRisk = (ins.triage || []).filter((t) => t.at_risk);
  if (atRisk.length) {
    parts.push(el("div", { class: "acct-ins" }, el("b", {}, `🎯 ${L("ins_handle_order")} `),
      atRisk.map((t, i) => el("span", { class: `c-${t.severity}` }, `${i ? " → " : ""}${t.symbol} ${pct(t.distance_pct)}`))));
  }
  for (const st of ins.stress || []) {
    if (st.liq_move_down_pct == null && st.symbols.length < 2) continue;
    const shocks = st.shocks.filter((x) => x.market_move_pct < 0)
      .map((x) => `${x.market_move_pct}%: ${L(x.liquidated ? "shock_liquidated" : "shock_ok")}`).join(" · ");
    parts.push(el("div", { class: "acct-ins" }, el("b", {}, `📉 ${L("ins_cross", { venue: venueLabel(st.venue) })} `),
      st.liq_move_down_pct != null ? `${L("ins_drop_liquidates", { pct: pct(st.liq_move_down_pct) })} · ` : "", shocks));
  }
  for (const c of (ins.comparisons || []).filter((x) => x.gap_pct >= 1)) {
    parts.push(el("div", { class: "acct-ins" }, el("b", {}, `🧲 ${c.symbol}: `),
      `${L("ins_liq_compare", {
        platform: price(c.platform_price), platform_pct: pct(c.platform_distance_pct),
        ours: price(c.correlated_price), ours_pct: pct(c.correlated_distance_pct),
      })} — `,
      el("b", { class: "c-critical" }, L("ins_pts_closer", { pts: c.gap_pct.toFixed(1) }))));
  }
  if (ins.concentration?.flagged) parts.push(el("div", { class: "acct-ins c-warning" }, `🧲 ${ins.concentration.message}`));
  for (const z of (ins.sizing || []).filter((x) => x.status === "oversized")) {
    parts.push(el("div", { class: "acct-ins" }, el("b", {}, `📏 ${z.symbol}: `),
      L("ins_oversized", { pct: pct(z.risk_pct), exit: z.exit_label || z.exit_kind, size: fmtSize(z.suggested_size) })));
  }
  return parts.length ? el("div", { class: "acct-insights" }, ...parts) : null;
}

/* ---------------- quick actions (same reports as the Telegram buttons) ---------------- */

const TOOLS = ["rescue", "wick", "stop", "funding"];
const toolLabel = (tool) => L(`tool_${tool}`);

function toolButtons(symbol, venue, live) {
  return el("div", { class: "tool-row" }, ...TOOLS.map((tool) => el("button", {
    class: "mini-btn tool-btn", type: "button", "data-tool": tool,
    onclick: (ev) => { ev.stopPropagation(); openTool(tool, symbol, venue, live); },
  }, toolLabel(tool))));
}

const MUSE_TAIL = / · (✦ Muse.*)$/;

function toolLine(line) {
  const tail = line.match(MUSE_TAIL);
  if (!tail) return el("div", { class: toolLineClass(line) }, line);
  return el("div", { class: "tool-line muse rec-line" },
    el("span", { class: "rec-text" }, line.slice(0, tail.index)), " ", el("span", { class: "muse-mark" }, tail[1]));
}

function toolLineClass(line) {
  if (/^(⚠️|🛑|⛔)/.test(line)) return "tool-line warn";
  if (/^(✅|💡)/.test(line)) return "tool-line good";
  if (/^\s{2,}/.test(line)) return "tool-line indent";
  return "tool-line";
}

async function openTool(tool, symbol, venue, live) {
  const dlg = $("toolDialog");
  $("toolTitle").textContent = `${toolLabel(tool)} · ${symbol}`;
  $("toolTabs").replaceChildren(...TOOLS.map((t) => el("button", {
    class: `mini-btn${t === tool ? " active" : ""}`, type: "button", onclick: () => openTool(t, symbol, venue, live),
  }, toolLabel(t))));
  $("toolBody").replaceChildren(el("div", { class: "tool-line muted" }, L("tool_loading")));
  if (!dlg.open) dlg.showModal();
  const shown = S.tool = { tool, symbol, venue, scope: live ? "live" : "sim", loaded: false };
  const url = live
    ? `/api/me/position-tool/live?${new URLSearchParams({ symbol, tool, venue })}`
    : api("/position-tool", { symbol, tool });
  try {
    const r = await fetch(url, { credentials: "same-origin" });
    if (r.status === 401) { toLogin(); return; }
    if (r.status === 404) {
      $("toolBody").replaceChildren(el("div", { class: "tool-line warn" }, L("tool_gone")));
      return;
    }
    if (!r.ok) throw new Error(`${r.status}`);
    const data = await r.json();
    if (S.tool !== shown) return;
    shown.loaded = true;
    renderToolReport(data, tool, symbol, venue, live);
  } catch (err) {
    $("toolBody").replaceChildren(el("div", { class: "tool-line warn" }, L("tool_failed")));
  }
}

function renderToolReport(data, tool, symbol, venue, live) {
  $("toolTitle").textContent = data.title;
  const body = data.lines.map((line) => line ? toolLine(line) : el("div", { class: "tool-gap" }));
  if (tool === "rescue") {
    body.push(el("button", { class: "btn btn-ghost tool-more", type: "button", onclick: () => {
      $("toolDialog").close();
      if (live) loadLiveRescue(symbol, venue); else loadRescue(symbol);
    } }, L("tool_compare")));
  }
  $("toolBody").replaceChildren(...body);
}

/** Muse's line for the open quick report arrived over the WebSocket. */
function updateToolReport(data) {
  const t = S.tool;
  if (!t || !t.loaded || !$("toolDialog").open || t.tool !== data.tool || t.symbol !== data.symbol || t.scope !== data.scope) return;
  if (t.venue && t.venue !== data.venue) return;
  renderToolReport(data.report, t.tool, t.symbol, t.venue, t.scope === "live");
}

/* ---------------- 🔔 alert settings & 🔑 exchange connections (signed-in only) ---------------- */

const SETTING_UNITS = { warning: "%", critical: "%", emergency: "%", interval: "unit_minutes", max: "unit_times", quiet: "" };
const TOGGLES = ["escalation", "adaptive", "stops"];
const unitText = (unit) => (unit.startsWith("unit_") ? L(unit) : unit);

function quietText(v) {
  if (!v || v === "off") return L("off");
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
    return { ok: false, status: 0, data: { detail: L("offline") } };
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
    panelMsg("settingsMsg", L("settings_load_failed"), false);
  }
}

function renderSettings(cur) {
  const rows = Object.entries(SETTING_UNITS).map(([key, unit]) => {
    const label = L(`setting_${key}`);
    const current = key === "quiet" ? (cur.quiet || "off") : cur[key];
    const options = [...new Set([...(cur.presets[key] || []).map(String), String(current)])];
    const select = el("select", { class: "field", "data-setting": key, "aria-label": label,
      onchange: (ev) => saveSetting(key, ev.target.value) },
      ...options.map((v) => {
        const o = el("option", { value: v }, key === "quiet" ? quietText(v) : `${v}${unitText(unit)}`);
        if (v === String(current)) o.selected = true;
        return o;
      }));
    return el("label", { class: "setting-row" }, el("span", {}, label), select);
  });
  const toggles = TOGGLES.map((key) => {
    const label = L(`toggle_${key}`);
    const box = el("input", { type: "checkbox", "data-setting": key, onchange: (ev) => saveSetting(key, ev.target.checked ? "on" : "off") });
    box.checked = !!cur[key];
    return el("label", { class: "toggle-row" }, box, el("span", {}, label));
  });
  $("settingsForm").replaceChildren(...rows, el("div", { class: "toggles" }, ...toggles),
    el("div", { class: "panel-sub" }, L("quiet_note", { offset: `${cur.quiet_utc_offset_hours >= 0 ? "+" : ""}${cur.quiet_utc_offset_hours}` })));
}

async function saveSetting(key, value) {
  const res = await sendJson("/api/me/settings", { key, value });
  if (res.ok) {
    renderSettings(res.data);
    panelMsg("settingsMsg", L("settings_saved"), true);
  } else {
    panelMsg("settingsMsg", `❌ ${res.data?.detail || L("settings_failed")}`, false);
    refreshSettings();
  }
}

function bindingLabel(b) {
  const venue = b.venue === "binance" ? "🟡 Binance" : "🟢 Hyperliquid";
  return `${venue}${b.network === "testnet" ? L("testnet") : ""}`;
}

async function refreshBindings() {
  let data;
  try {
    const r = await fetch("/api/me/bindings", { credentials: "same-origin" });
    if (r.status === 401) { toLogin(); return; }
    data = await r.json();
  } catch (err) {
    panelMsg("bindMsg", L("bind_load_failed"), false);
    return;
  }
  $("securityNote").textContent = data.security_note;
  const list = $("bindList");
  list.replaceChildren(...data.bindings.map((b) => el("div", { class: "binding" },
    el("span", {}, bindingLabel(b)),
    el("span", { class: "hint" }, b.api_key || b.wallet_address || ""),
    el("button", { class: "mini-btn danger", type: "button", onclick: () => confirmUnbind(b) }, L("unbind")),
  )));
  if (!data.bindings.length) list.append(empty("🔌", L("not_bound"), L("not_bound_hint")));
}

function confirmUnbind(b) {
  const dlg = $("confirmDialog");
  $("confirmText").textContent = L("confirm_unbind", { label: bindingLabel(b), hint: b.api_key || b.wallet_address || "" });
  $("confirmYes").onclick = async () => {
    dlg.close();
    const res = await sendJson("/api/me/bindings/unbind", { venue: b.venue, network: b.network || "mainnet", confirm: true });
    panelMsg("bindMsg", res.ok ? L("unbound", { label: bindingLabel(b) }) : `❌ ${res.data?.detail || L("unbind_failed")}`, res.ok);
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
    panelMsg("bindMsg", L("verifying"), true);
    const res = await sendJson(path, body);
    button.disabled = false;
    panelMsg("bindMsg", res.data?.message || res.data?.detail || L(res.ok ? "bound" : "bind_failed"), res.ok);
    if (res.ok) { refreshBindings(); refreshAccount(); }
  });
  submit("bindBinance", "/api/me/bindings/binance", ["api_key", "api_secret"]);
  submit("bindHyperliquid", "/api/me/bindings/hyperliquid", ["address"]);
  refreshSettings();
  refreshBindings();
}

async function switchLanguage() {
  const next = I18N.lang === "zh-Hant" ? "en" : "zh-Hant";
  rememberLanguage(next);
  if (MODE === "me") await sendJson("/api/me/language", { language: next });
  const url = new URL(location.href);
  if (url.searchParams.has("lang")) url.searchParams.set("lang", next);
  location.assign(url.toString());
}

async function bootstrap() {
  try {
    await loadStrings(await initialLanguage());
    rememberLanguage(I18N.lang);
  } catch (err) { /* keep the built-in English text */ }
  if (!(await setupScope())) return;
  try {
    const r = await fetch(api("/snapshot"), { credentials: "same-origin" });
    if (r.status === 401) { toLogin(); return; }
    if (r.ok) applySnapshot(await r.json());
  } catch (err) {
    $("headline").textContent = L("unreachable");
  }
  connect();
}

document.addEventListener("DOMContentLoaded", () => {
  $("simulateBtn").addEventListener("click", async () => {
    $("simulateBtn").disabled = true;
    const res = await post(api("/simulate"));
    if (!res) $("simulateBtn").disabled = false;
  });
  $("stopBtn").addEventListener("click", () => post(api("/simulate/stop")));
  $("scenarioSelect").addEventListener("change", (ev) => switchScenario(ev.target.value));
  $("resetBtn").addEventListener("click", () => post(api("/simulate/reset")));
  $("foldChart").addEventListener("toggle", () => { if ($("foldChart").open) { buildTabs(); buildChart(); } });
  $("signOutBtn").addEventListener("click", async () => {
    await fetch("/auth/logout", { method: "POST", headers: CSRF_HEADERS, credentials: "same-origin" });
    location.assign("/login?notice=signed_out");
  });
  $("toolClose").addEventListener("click", () => $("toolDialog").close());
  $("langBtn").addEventListener("click", switchLanguage);
  for (const id of ["toolDialog", "confirmDialog"]) {
    $(id).addEventListener("click", (ev) => { if (ev.target === ev.currentTarget) ev.currentTarget.close(); });
  }
  setInterval(() => { if (ws && ws.readyState === 1) ws.send("ping"); }, 25000);
  bootstrap();
});
