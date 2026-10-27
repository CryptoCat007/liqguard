"use strict";

const ERRORS = {
  link: "That login link is invalid, already used, or expired. Send /dashboard to the bot for a new one.",
  widget: "Telegram sign-in could not be verified (or it is too old). Please try again.",
  session: "Your session expired or was revoked. Please sign in again.",
  disabled: "Sign-in is not enabled on this server. The public demo is still available.",
};

document.addEventListener("DOMContentLoaded", async () => {
  const error = new URLSearchParams(location.search).get("error");
  if (error && ERRORS[error]) {
    const box = document.getElementById("loginError");
    box.textContent = ERRORS[error];
    box.hidden = false;
  }

  let config;
  try {
    config = await (await fetch("/api/auth/config")).json();
  } catch (err) {
    return;
  }
  if (config.link_ttl_seconds) {
    const minutes = Math.max(1, Math.round(config.link_ttl_seconds / 60));
    document.getElementById("ttl").textContent = `${minutes} minute${minutes === 1 ? "" : "s"}`;
  }
  if (config.bot_username) {
    const link = document.createElement("a");
    link.href = `https://t.me/${encodeURIComponent(config.bot_username)}`;
    link.textContent = `@${config.bot_username}`;
    link.rel = "noopener";
    document.getElementById("botName").append(" ", link);
  }
  if (config.widget_enabled) {
    const script = document.createElement("script");
    script.async = true;
    script.src = "https://telegram.org/js/telegram-widget.js?22";
    script.dataset.telegramLogin = config.bot_username;
    script.dataset.size = "large";
    script.dataset.radius = "10";
    script.dataset.authUrl = `${location.origin}/auth/telegram`;
    document.getElementById("widget").append(script);
    document.getElementById("widgetBlock").hidden = false;
  }
});
