// ==UserScript==
// @name         OpenSea Mint Assist (manual wallet confirmation)
// @namespace    local.opensea-mint-assist
// @version      1.0.0
// @description  Watches one OpenSea drop page and optionally clicks its visible mint control once. Never handles keys or confirms a wallet transaction.
// @match        https://opensea.io/*
// @run-at       document-idle
// @grant        none
// @noframes
// ==/UserScript==

(function () {
  "use strict";

  // This helper deliberately does not call OpenSea APIs, read wallet data, or
  // press buttons inside MetaMask/Rabby. It only observes the current page.
  // Keep Auto-click off unless OpenSea has authorized your automation.
  var STORAGE_KEY = "local.opensea-mint-assist.v1";
  var saved = {};
  try {
    saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
  } catch (_) {}

  var state = {
    autoArm: saved.autoArm === true,
    armed: saved.autoArm === true,
    autoClick: saved.autoClick === true,
    freeOnly: saved.freeOnly !== false,
    clicked: false,
    lastMessage: "Disarmed — no page action will be taken.",
  };

  var panel;
  var statusLine;
  var armButton;
  var autoArmBox;
  var autoClickBox;
  var freeOnlyBox;
  var scanQueued = false;

  function savePreferences() {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      autoArm: state.autoArm,
      autoClick: state.autoClick,
      freeOnly: state.freeOnly,
    }));
  }

  function textOf(element) {
    return (element.innerText || element.textContent || "")
      .replace(/\s+/g, " ")
      .trim();
  }

  function visible(element) {
    if (!element || !element.isConnected) return false;
    var style = window.getComputedStyle(element);
    var rect = element.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" &&
      rect.width > 0 && rect.height > 0;
  }

  function freeEvidence() {
    var page = (document.body.innerText || "").replace(/\s+/g, " ").toLowerCase();
    return /\bfree\s+(mint|claim)\b/.test(page) ||
      /\bgas[- ]only\b/.test(page) ||
      /\b0(?:\.0+)?\s*(eth|matic|arb|op|avax|bnb|base)\b/.test(page);
  }

  function mintCandidate() {
    var elements = Array.prototype.slice.call(
      document.querySelectorAll("button, [role='button']")
    );
    var labels = /^(mint|mint now|claim|claim now|collect|collect now|start mint)$/i;
    for (var i = 0; i < elements.length; i += 1) {
      var element = elements[i];
      var label = textOf(element);
      if (!visible(element) || !label || !labels.test(label)) continue;
      if (panel && panel.contains(element)) continue;
      if (element.disabled || element.getAttribute("aria-disabled") === "true") continue;
      return element;
    }
    return null;
  }

  function setMessage(message) {
    state.lastMessage = message;
    if (statusLine) statusLine.textContent = message;
  }

  function scan() {
    if (!panel) return;
    var candidate = mintCandidate();
    var free = freeEvidence();
    var priceText = free ? "free/0-value evidence found" : "price not verified as free";

    if (!state.armed) {
      setMessage("Disarmed — watching only. " + priceText + ".");
      return;
    }
    if (!candidate) {
      setMessage("Armed — waiting for a visible Mint/Claim button. " + priceText + ".");
      return;
    }
    if (state.freeOnly && !free) {
      setMessage("Mint button found, but free-only protection blocked the click.");
      return;
    }
    if (!state.autoClick) {
      setMessage("Mint button found — click it yourself, then confirm the wallet prompt.");
      return;
    }
    if (state.clicked) return;

    state.clicked = true;
    state.armed = false;
    armButton.textContent = "Arm once";
    setMessage("Clicked the page button once. Review and confirm the wallet prompt manually.");
    candidate.click();
  }

  function scheduleScan() {
    if (scanQueued) return;
    scanQueued = true;
    window.requestAnimationFrame(function () {
      scanQueued = false;
      scan();
    });
  }

  function makePanel() {
    panel = document.createElement("section");
    panel.id = "local-opensea-mint-assist";
    panel.style.cssText = [
      "position:fixed", "z-index:2147483647", "right:16px", "bottom:16px",
      "width:310px", "padding:12px", "border:1px solid #5a6072",
      "border-radius:10px", "background:#17191f", "color:#f4f5f7",
      "font:12px/1.4 system-ui,sans-serif", "box-shadow:0 8px 28px #0008",
    ].join(";");

    var title = document.createElement("div");
    title.textContent = "OpenSea Mint Assist";
    title.style.cssText = "font-weight:700;font-size:14px;margin-bottom:5px";
    panel.appendChild(title);

    var warning = document.createElement("div");
    warning.textContent = "Page helper only — no keys, API calls, or wallet auto-confirmation.";
    warning.style.cssText = "color:#cbd0dc;margin-bottom:8px";
    panel.appendChild(warning);

    statusLine = document.createElement("div");
    statusLine.style.cssText = "min-height:34px;margin-bottom:8px";
    panel.appendChild(statusLine);

    var controls = document.createElement("div");
    controls.style.cssText = "display:flex;gap:6px;align-items:center;margin-bottom:8px";

    armButton = document.createElement("button");
    armButton.textContent = state.armed ? "Disarm" : "Arm once";
    armButton.style.cssText = "padding:6px 9px;border:0;border-radius:6px;background:#2081e2;color:white;cursor:pointer";
    armButton.addEventListener("click", function () {
      state.armed = !state.armed;
      state.clicked = false;
      armButton.textContent = state.armed ? "Disarm" : "Arm once";
      scan();
    });
    controls.appendChild(armButton);

    var closeButton = document.createElement("button");
    closeButton.textContent = "Hide";
    closeButton.style.cssText = "padding:6px 9px;border:1px solid #5a6072;border-radius:6px;background:transparent;color:#f4f5f7;cursor:pointer";
    closeButton.addEventListener("click", function () { panel.remove(); panel = null; });
    controls.appendChild(closeButton);
    panel.appendChild(controls);

    var autoArmLabel = document.createElement("label");
    autoArmBox = document.createElement("input");
    autoArmBox.type = "checkbox";
    autoArmBox.checked = state.autoArm;
    autoArmBox.addEventListener("change", function () {
      state.autoArm = autoArmBox.checked;
      if (state.autoArm) {
        state.armed = true;
        state.clicked = false;
        armButton.textContent = "Disarm";
      }
      savePreferences();
      scheduleScan();
    });
    autoArmLabel.appendChild(autoArmBox);
    autoArmLabel.appendChild(document.createTextNode(" Auto-arm on page load"));
    panel.appendChild(autoArmLabel);

    var autoLabel = document.createElement("label");
    autoClickBox = document.createElement("input");
    autoClickBox.type = "checkbox";
    autoClickBox.checked = state.autoClick;
    autoClickBox.addEventListener("change", function () {
      state.autoClick = autoClickBox.checked;
      savePreferences();
      scan();
    });
    autoLabel.appendChild(autoClickBox);
    autoLabel.appendChild(document.createTextNode(" Auto-click one page button"));
    panel.appendChild(autoLabel);

    var freeLabel = document.createElement("label");
    freeOnlyBox = document.createElement("input");
    freeOnlyBox.type = "checkbox";
    freeOnlyBox.checked = state.freeOnly;
    freeOnlyBox.addEventListener("change", function () {
      state.freeOnly = freeOnlyBox.checked;
      savePreferences();
      scan();
    });
    freeLabel.appendChild(freeOnlyBox);
    freeLabel.appendChild(document.createTextNode(" Require visible free/0-value evidence"));
    panel.appendChild(freeLabel);

    document.body.appendChild(panel);
    setMessage(state.lastMessage);
  }

  function start() {
    if (!document.body) return window.setTimeout(start, 250);
    makePanel();
    var observer = new MutationObserver(scheduleScan);
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
    window.setInterval(scheduleScan, 250);
    scheduleScan();
  }

  start();
}());
