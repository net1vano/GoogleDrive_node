/**
 * ComfyUI – Google Drive Upload node
 * Adds:
 *   • auth status banner (✅ authorized / ⚠ not authorized)
 *   • "Authorize" button  (oauth2 mode only)
 *   • "Revoke" button     (when authorized)
 *   • auto-hides credentials_json widget when already authorized in oauth2 mode
 *   • credentials_json uses password masking (dots) so it won't leak in screenshots
 */

import { app } from "../../scripts/app.js";

const NODE_TYPE = "GoogleDriveUpload";

// ── polling interval while oauth flow is in progress ──────────────────────
const POLL_INTERVAL_MS = 2000;

// ── small helpers ──────────────────────────────────────────────────────────

function getWidget(node, name) {
  return node.widgets?.find((w) => w.name === name);
}

async function fetchAuthStatus() {
  try {
    const r = await fetch("/gdrive/auth_status");
    return await r.json();          // { status, email, error }
  } catch {
    return { status: "unknown", email: "", error: "Server unreachable" };
  }
}

async function postAuthorize(credentials_json) {
  const r = await fetch("/gdrive/authorize", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ credentials_json }),
  });
  return await r.json();
}

async function postRevoke() {
  const r = await fetch("/gdrive/revoke", { method: "POST" });
  return await r.json();
}

// ── DOM helpers ────────────────────────────────────────────────────────────

function el(tag, attrs = {}, children = []) {
  const e = document.createElement(tag);
  Object.assign(e.style, attrs.style || {});
  delete attrs.style;
  Object.assign(e, attrs);
  children.forEach((c) => e.appendChild(c));
  return e;
}

// ── Main extension ─────────────────────────────────────────────────────────

app.registerExtension({
  name: "GDriveUpload.CustomUI",

  async nodeCreated(node) {
    if (node.comfyClass !== NODE_TYPE) return;

    // Give ComfyUI a tick to finish wiring widgets
    await new Promise((r) => setTimeout(r, 0));

    // ── find widgets we care about ─────────────────────────────────────
    const authModeWidget   = getWidget(node, "auth_mode");
    const credsWidget      = getWidget(node, "credentials_json");

    if (!authModeWidget || !credsWidget) return;

    // ── make credentials textarea show dots (password mask) ───────────
    // ComfyUI renders STRING widgets as <textarea>; we overlay a mask layer.
    // The real value is stored in credsWidget.value (never shown in plain text).
    if (credsWidget.inputEl) {
      credsWidget.inputEl.style.webkitTextSecurity = "disc"; // Chrome/Safari
      credsWidget.inputEl.style.fontFamily = "monospace";
      // Firefox fallback: type=password doesn't work on textarea,
      // but -webkit-text-security works in most Electron/Chromium builds ComfyUI uses.
    }

    // ── inject a DOM section below the node's widget area ─────────────
    // We use node.addCustomWidget to inject a pure-DOM widget.
    let statusEl, authorizeBtn, revokeBtn, spinnerEl;
    let polling = false;

    const container = el("div", {
      style: {
        display: "flex",
        flexDirection: "column",
        gap: "6px",
        padding: "6px 8px",
        background: "rgba(0,0,0,0.25)",
        borderRadius: "6px",
        margin: "4px 0",
        fontSize: "12px",
        fontFamily: "monospace",
        minWidth: "0",
      },
    });

    // Status line
    statusEl = el("div", {
      style: { color: "#aaa", wordBreak: "break-all", lineHeight: "1.4" },
    });
    statusEl.textContent = "⏳ Checking auth status…";
    container.appendChild(statusEl);

    // Button row
    const btnRow = el("div", {
      style: { display: "flex", gap: "6px", flexWrap: "wrap" },
    });

    authorizeBtn = el("button", {
      textContent: "🔑 Authorize",
      style: {
        padding: "4px 10px",
        borderRadius: "4px",
        border: "none",
        background: "#4a90d9",
        color: "#fff",
        cursor: "pointer",
        fontSize: "12px",
        display: "none",
      },
    });

    revokeBtn = el("button", {
      textContent: "🚪 Revoke",
      style: {
        padding: "4px 10px",
        borderRadius: "4px",
        border: "none",
        background: "#c0392b",
        color: "#fff",
        cursor: "pointer",
        fontSize: "12px",
        display: "none",
      },
    });

    spinnerEl = el("span", {
      textContent: "⏳ Waiting for browser sign-in…",
      style: { color: "#f0c040", display: "none", alignSelf: "center" },
    });

    btnRow.appendChild(authorizeBtn);
    btnRow.appendChild(revokeBtn);
    btnRow.appendChild(spinnerEl);
    container.appendChild(btnRow);

    // ── helpers to update UI from status object ────────────────────────

    function applyStatus(s) {
      const isOAuth = authModeWidget.value === "oauth2";
      const authorized = s.status === "authorized";
      const errored = s.status === "error";

      // Status text
      if (authorized) {
        statusEl.textContent = s.email
          ? `✅ Authorized as ${s.email}`
          : "✅ Authorized";
        statusEl.style.color = "#5cb85c";
      } else if (errored) {
        statusEl.textContent = `❌ Error: ${s.error}`;
        statusEl.style.color = "#e74c3c";
      } else {
        if (isOAuth) {
          statusEl.textContent = "⚠️ Not authorized – paste client_secret JSON and click Authorize";
          statusEl.style.color = "#f0ad4e";
        } else {
          statusEl.textContent = "ℹ️ Service Account mode – paste JSON key above";
          statusEl.style.color = "#aaa";
        }
      }

      // Show/hide credentials widget
      // In OAuth2 mode: hide creds once authorized (token is cached on disk)
      // In service_account mode: always show
      if (isOAuth && authorized) {
        credsWidget.type = "converted-widget"; // hides it in ComfyUI
        if (credsWidget.inputEl) {
          credsWidget.inputEl.closest(".comfy-multiline-input")?.setAttribute("hidden", "");
          // also hide the label
        }
        setWidgetVisibility(credsWidget, false);
      } else {
        setWidgetVisibility(credsWidget, true);
      }

      // Buttons
      if (isOAuth) {
        authorizeBtn.style.display = authorized ? "none" : "inline-block";
        revokeBtn.style.display = authorized ? "inline-block" : "none";
      } else {
        authorizeBtn.style.display = "none";
        revokeBtn.style.display = "none";
      }

      node.setDirtyCanvas(true);
    }

    // ComfyUI widget visibility helper
    function setWidgetVisibility(widget, visible) {
      if (!widget) return;
      widget.hidden = !visible;
      if (widget.element) {
        widget.element.style.display = visible ? "" : "none";
      }
      // Recalculate node size
      node.setSize(node.computeSize());
      node.setDirtyCanvas(true);
    }

    // ── Authorize button ───────────────────────────────────────────────

    authorizeBtn.addEventListener("click", async () => {
      const creds = credsWidget.value?.trim();
      if (!creds) {
        statusEl.textContent = "❌ Paste client_secret JSON above first";
        statusEl.style.color = "#e74c3c";
        return;
      }

      authorizeBtn.disabled = true;
      authorizeBtn.textContent = "Opening browser…";
      spinnerEl.style.display = "inline";

      const res = await postAuthorize(creds);
      if (!res.ok) {
        statusEl.textContent = `❌ ${res.error}`;
        statusEl.style.color = "#e74c3c";
        authorizeBtn.disabled = false;
        authorizeBtn.textContent = "🔑 Authorize";
        spinnerEl.style.display = "none";
        return;
      }

      // Poll until authorized or error
      polling = true;
      const poll = setInterval(async () => {
        const s = await fetchAuthStatus();
        if (s.status === "authorized" || s.status === "error") {
          clearInterval(poll);
          polling = false;
          spinnerEl.style.display = "none";
          authorizeBtn.disabled = false;
          authorizeBtn.textContent = "🔑 Authorize";
          applyStatus(s);
        }
      }, POLL_INTERVAL_MS);
    });

    // ── Revoke button ──────────────────────────────────────────────────

    revokeBtn.addEventListener("click", async () => {
      revokeBtn.disabled = true;
      await postRevoke();
      revokeBtn.disabled = false;
      applyStatus({ status: "unknown", email: "", error: "" });
    });

    // ── React to auth_mode changes ─────────────────────────────────────

    const origAuthModeCallback = authModeWidget.callback;
    authModeWidget.callback = async function (...args) {
      origAuthModeCallback?.apply(this, args);
      const s = await fetchAuthStatus();
      applyStatus(s);
    };

    // ── Inject as custom DOM widget ────────────────────────────────────

    node.addDOMWidget("gdrive_auth_ui", "div", container, {
      getValue() { return null; },
      setValue() {},
    });

    // ── Initial status fetch ───────────────────────────────────────────

    const initial = await fetchAuthStatus();
    applyStatus(initial);
  },
});
