/* ── Spendly — Shared JavaScript ─────────────────────────────────────────── */

"use strict";

/* ── Formatting helpers ──────────────────────────────────────────────────── */

/**
 * Format a number as Indian currency: ₹1,23,456
 */
function fmtINR(amount) {
  if (amount == null || isNaN(amount)) return "₹0";
  return "₹" + Number(amount).toLocaleString("en-IN", {
    maximumFractionDigits: 0,
  });
}

/**
 * Format a number as INR with paise (2 decimal places).
 */
function fmtINRFull(amount) {
  if (amount == null || isNaN(amount)) return "₹0.00";
  return "₹" + Number(amount).toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

/**
 * Format a YYYY-MM-DD date string to "9 Apr 2025".
 */
function fmtDate(iso) {
  if (!iso) return "—";
  try {
    const d = new Date(iso + "T00:00:00");
    return d.toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
  } catch (_) {
    return iso;
  }
}

/**
 * Return "today", "yesterday", or "9 Apr 2025".
 */
function fmtDateRelative(iso) {
  if (!iso) return "—";
  const today = new Date();
  today.setHours(0,0,0,0);
  const d = new Date(iso + "T00:00:00");
  const diff = Math.round((today - d) / 86400000);
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  return fmtDate(iso);
}

/**
 * Clamp a value between min and max.
 */
function clamp(val, min, max) {
  return Math.max(min, Math.min(max, val));
}

/* ── Category badge colour ───────────────────────────────────────────────── */

const CAT_CLASSES = {
  food:          "badge-food",
  transport:     "badge-transport",
  shopping:      "badge-shopping",
  bills:         "badge-bills",
  health:        "badge-health",
  entertainment: "badge-entertainment",
  education:     "badge-education",
};

/**
 * Return the CSS class for a category badge.
 */
function categoryClass(cat) {
  if (!cat) return "badge-default";
  return CAT_CLASSES[cat.toLowerCase()] || "badge-default";
}

/* ── API helpers ─────────────────────────────────────────────────────────── */

/**
 * Resolve a web API endpoint under the current Spendly app prefix.
 */
function _resolveApiUrl(endpoint) {
  if (!endpoint.startsWith("/")) return endpoint;
  if (endpoint.startsWith("/api/")) {
    return window.location.pathname.startsWith("/spendly")
      ? "/spendly" + endpoint
      : endpoint;
  }
  return endpoint;
}

/**
 * GET /api/<endpoint> and return parsed JSON.
 * Throws on network error or non-200 status.
 */
async function apiGet(endpoint) {
  const url = _resolveApiUrl(endpoint);
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} → ${res.status}`);
  return res.json();
}

/**
 * POST /api/<endpoint> with a JSON body.
 */
async function apiPost(endpoint, body) {
  const url = _resolveApiUrl(endpoint);
  const res = await fetch(url, {
    method:  "POST",
    headers: { "Content-Type": "application/json" },
    body:    JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${url} → ${res.status}`);
  return res.json();
}

/* ── Toast notifications ─────────────────────────────────────────────────── */

let _toastTimer = null;

/**
 * Show a brief toast message at the bottom of the screen.
 * @param {string} msg  Text to show
 * @param {string} type "info" | "success" | "error"
 * @param {number} ms   Duration in milliseconds (default 3000)
 */
function showToast(msg, type = "info", ms = 3000) {
  let el = document.getElementById("_spendly_toast");
  if (!el) {
    el = document.createElement("div");
    el.id = "_spendly_toast";
    el.style.cssText = [
      "position:fixed","bottom:1.5rem","right:1.5rem",
      "padding:0.65rem 1.1rem","border-radius:8px",
      "font-size:0.85rem","font-weight:600",
      "z-index:9999","transition:opacity 0.2s",
      "max-width:320px","box-shadow:0 4px 20px rgba(0,0,0,0.4)",
    ].join(";");
    document.body.appendChild(el);
  }

  const colours = {
    info:    "background:#1a1a24;color:#e8e8f0;border:1px solid #2a2a3a",
    success: "background:rgba(81,207,102,0.15);color:#51cf66;border:1px solid rgba(81,207,102,0.3)",
    error:   "background:rgba(255,107,107,0.15);color:#ff6b6b;border:1px solid rgba(255,107,107,0.3)",
  };
  el.style.cssText += ";" + (colours[type] || colours.info);
  el.textContent = msg;
  el.style.opacity = "1";

  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { el.style.opacity = "0"; }, ms);
}

/* ── Live search / filter utility ───────────────────────────────────────── */

/**
 * Attach a live text filter to a list of elements.
 *
 * @param {string} inputId   ID of the <input> element
 * @param {string} listSel   CSS selector for the items to filter
 * @param {string} dataProp  dataset property name to match against
 * @param {string} emptyId   ID of the "no results" element (optional)
 */
function attachLiveFilter(inputId, listSel, dataProp = "name", emptyId = null) {
  const input = document.getElementById(inputId);
  if (!input) return;

  input.addEventListener("input", () => {
    const q = input.value.toLowerCase().trim();
    let visible = 0;
    document.querySelectorAll(listSel).forEach(el => {
      const val = (el.dataset[dataProp] || el.textContent).toLowerCase();
      const show = !q || val.includes(q);
      el.style.display = show ? "" : "none";
      if (show) visible++;
    });
    if (emptyId) {
      const noRes = document.getElementById(emptyId);
      if (noRes) noRes.style.display = visible === 0 ? "block" : "none";
    }
  });
}

/* ── Chart.js defaults ──────────────────────────────────────────────────── */

/**
 * Apply Spendly's dark-theme defaults to Chart.js.
 * Call once at page load if you use charts on that page.
 */
function applyChartDefaults() {
  if (typeof Chart === "undefined") return;
  Chart.defaults.color            = "#8888a0";
  Chart.defaults.borderColor      = "#2a2a3a";
  Chart.defaults.backgroundColor  = "rgba(124,106,247,0.15)";
  Chart.defaults.plugins.legend.display = false;
  Chart.defaults.plugins.tooltip.backgroundColor = "#1a1a24";
  Chart.defaults.plugins.tooltip.titleColor       = "#e8e8f0";
  Chart.defaults.plugins.tooltip.bodyColor        = "#8888a0";
  Chart.defaults.plugins.tooltip.borderColor      = "#2a2a3a";
  Chart.defaults.plugins.tooltip.borderWidth      = 1;
  Chart.defaults.plugins.tooltip.padding          = 10;
}

/* ── URL param helpers ───────────────────────────────────────────────────── */

/**
 * Read a query param from the current URL.
 */
function getParam(name) {
  return new URLSearchParams(window.location.search).get(name);
}

/**
 * Navigate to the same page with updated query params.
 */
function setParams(updates) {
  const params = new URLSearchParams(window.location.search);
  Object.entries(updates).forEach(([k, v]) => {
    if (v == null || v === "") params.delete(k);
    else params.set(k, v);
  });
  window.location.search = params.toString();
}

/* ── Copy to clipboard ───────────────────────────────────────────────────── */

/**
 * Apply dynamic fill styles from data attributes.
 */
function applyDynamicFillStyles() {
  document.querySelectorAll('[data-fill-width]').forEach(el => {
    const width = el.dataset.fillWidth;
    if (width != null && width !== "") {
      el.style.width = width.endsWith("%") ? width : `${width}%`;
    }
  });

  document.querySelectorAll('[data-fill-height]').forEach(el => {
    const height = el.dataset.fillHeight;
    if (height != null && height !== "") {
      el.style.height = height.endsWith("%") ? height : `${height}%`;
    }
  });

  document.querySelectorAll('[data-fill-color]').forEach(el => {
    const color = el.dataset.fillColor;
    if (color) el.style.background = color;
  });
}

window.addEventListener('DOMContentLoaded', applyDynamicFillStyles);

/**
 * Copy text to clipboard and show a toast.
 */
function copyText(text, label = "Copied!") {
  navigator.clipboard.writeText(text)
    .then(() => showToast(label, "success"))
    .catch(() => showToast("Copy failed", "error"));
}

/* ── On-page init ────────────────────────────────────────────────────────── */

document.addEventListener("DOMContentLoaded", () => {
  // Mark current nav link active based on pathname
  const path = window.location.pathname;
  document.querySelectorAll(".sidebar nav a").forEach(a => {
    const href = a.getAttribute("href") || "";
    const isActive = href !== "/" && path.startsWith(href);
    if (isActive) a.classList.add("active");
    else a.classList.remove("active");
  });

  const menuToggle = document.getElementById("menuToggle");
  const closeMenu = document.querySelector(".close-menu");
  const sidebar = document.getElementById("sidebar");
  const backdrop = document.getElementById("navBackdrop");

  function toggleNav() {
    const isOpen = sidebar.classList.contains("open");
    if (isOpen) {
      sidebar.classList.remove("open");
      backdrop.classList.remove("open");
      document.body.style.overflow = "";
    } else {
      sidebar.classList.add("open");
      backdrop.classList.add("open");
      document.body.style.overflow = "hidden";
    }
  }

  function closeNav() {
    sidebar.classList.remove("open");
    backdrop.classList.remove("open");
    document.body.style.overflow = "";
  }

  menuToggle?.addEventListener("click", toggleNav);
  closeMenu?.addEventListener("click", closeNav);
  backdrop?.addEventListener("click", closeNav);

  // Close nav on escape key
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && sidebar.classList.contains("open")) {
      closeNav();
    }
  });
});