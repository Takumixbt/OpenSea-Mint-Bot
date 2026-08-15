// ==UserScript==
// @name         OpenSea Mint Assist
// @namespace    opensea-mint-bot
// @version      2.0.0
// @description  Watch one OpenSea page and optionally click one visible mint control. Wallet confirmation stays manual.
// @match        https://opensea.io/*
// @run-at       document-idle
// @grant        none
// @noframes
// ==/UserScript==

(function () {
  "use strict";

  // This script intentionally has no API access, wallet access, or external
  // dependencies. It observes the current page and never signs a transaction.
  const STORAGE_KEY = "opensea-mint-bot.browser-helper.v2";
  const saved = readSaved();
  const state = {
    autoArm: saved.autoArm === true,
    autoClick: saved.autoClick === true,
    freeOnly: saved.freeOnly !== false,
    paidAcknowledged: saved.paidAcknowledged === true,
    maxPrice: saved.maxPrice || "0",
    quantity: saved.quantity || "1",
    targetTime: saved.targetTime || "",
    armed: saved.autoArm === true,
    clicked: false,
    lastUrl: location.href,
    message: "Watching only. Arm the helper when ready.",
  };

  let panel;
  let statusLine;
  let detailsLine;
  let armButton;
  let autoArmBox;
  let autoClickBox;
  let freeOnlyBox;
  let paidBox;
  let maxPriceInput;
  let quantityInput;
  let targetTimeInput;
  let scanQueued = false;

  function readSaved() {
    try {
      return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
    } catch (_) {
      return {};
    }
  }

  function savePreferences() {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      autoArm: state.autoArm,
      autoClick: state.autoClick,
      freeOnly: state.freeOnly,
      paidAcknowledged: state.paidAcknowledged,
      maxPrice: state.maxPrice,
      quantity: state.quantity,
      targetTime: state.targetTime,
    }));
  }

  function textOf(element) {
    return (element && (element.innerText || element.textContent || element.getAttribute("aria-label")) || "")
      .replace(/\s+/g, " ")
      .trim();
  }

  function visible(element) {
    if (!element || !element.isConnected) return false;
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" &&
      style.opacity !== "0" && rect.width > 0 && rect.height > 0;
  }

  function cleanText(text) {
    return String(text || "").replace(/\s+/g, " ").trim();
  }

  function nearbyContext(element) {
    let current = element;
    let result = textOf(element);
    for (let depth = 0; current && depth < 5; depth += 1) {
      current = current.parentElement;
      const candidate = cleanText(textOf(current));
      if (candidate.length > result.length && candidate.length < 1200) {
        result = candidate;
      }
    }
    return result.slice(0, 1600);
  }

  function freeEvidence(text) {
    const normalized = cleanText(text).toLowerCase();
    return /\bfree\s+(?:mint|claim|collect)\b/.test(normalized) ||
      /\bgas[- ]only\b/.test(normalized) ||
      /(?:^|\s)0(?:\.0+)?\s*(?:eth|weth|matic|pol|arb|op|avax|bnb|base)\b/.test(normalized);
  }

  function parseVisiblePrice(text) {
    const matches = [];
    const pattern = /(?:^|\s)(\d+(?:\.\d+)?)\s*(ETH|WETH|MATIC|POL|ARB|OP|AVAX|BNB|BASE)\b/ig;
    let match;
    while ((match = pattern.exec(text)) !== null) {
      const amount = Number(match[1]);
      if (Number.isFinite(amount)) {
        matches.push({ amount: amount, unit: match[2].toUpperCase(), raw: match[0].trim() });
      }
    }
    return matches.length ? matches[0] : null;
  }

  function buttonLabelMatches(label) {
    return /^(?:mint|claim|collect)(?:\s+(?:now|nft|nfts|token|tokens))?(?:\s+\d+)?$/i.test(label);
  }

  function findMintButton() {
    const elements = Array.prototype.slice.call(
      document.querySelectorAll("button, [role='button']")
    );
    for (let index = 0; index < elements.length; index += 1) {
      const element = elements[index];
      const label = cleanText(textOf(element));
      if (!visible(element) || !buttonLabelMatches(label)) continue;
      if (panel && panel.contains(element)) continue;
      if (element.disabled || element.getAttribute("aria-disabled") === "true") continue;
      const context = nearbyContext(element);
      return {
        element: element,
        label: label,
        context: context,
        free: freeEvidence(context),
        price: parseVisiblePrice(context),
      };
    }
    return null;
  }

  function pageLooksLikeOpenSeaMintPage() {
    return /\/collection\/|\/drops\//i.test(location.pathname);
  }

  function targetTimeMs() {
    if (!state.targetTime) return 0;
    const timestamp = new Date(state.targetTime).getTime();
    return Number.isFinite(timestamp) ? timestamp : 0;
  }

  function setMessage(message) {
    state.message = message;
    if (statusLine) statusLine.textContent = message;
  }

  function updateDetails(candidate) {
    if (!detailsLine) return;
    if (!pageLooksLikeOpenSeaMintPage()) {
      detailsLine.textContent = "Open an OpenSea collection or drop page.";
      return;
    }
    if (!candidate) {
      detailsLine.textContent = "No visible Mint/Claim/Collect control yet.";
      return;
    }
    const price = candidate.price ? candidate.price.raw : "price not found nearby";
    const access = candidate.free ? "free evidence" : "paid/unknown price";
    detailsLine.textContent = `${candidate.label} · ${price} · ${access}`;
  }

  function priceAllowed(candidate) {
    if (candidate.free) return true;
    if (state.freeOnly || !state.paidAcknowledged) return false;
    if (!candidate.price) return false;
    const cap = Number(state.maxPrice);
    return Number.isFinite(cap) && cap > 0 && candidate.price.amount <= cap;
  }

  function setVisibleQuantity() {
    const desired = Math.max(1, Math.min(100, Number.parseInt(state.quantity, 10) || 1));
    const inputs = Array.prototype.slice.call(document.querySelectorAll("input[type='number']"));
    const input = inputs.find((item) => visible(item) && !item.disabled && !(panel && panel.contains(item)));
    if (!input) return false;
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
    setter.call(input, String(desired));
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }

  function scan() {
    if (!panel) return;
    if (state.lastUrl !== location.href) {
      state.lastUrl = location.href;
      state.clicked = false;
      state.armed = state.autoArm;
      if (armButton) armButton.textContent = state.armed ? "Disarm" : "Arm once";
    }

    const candidate = findMintButton();
    updateDetails(candidate);
    if (!state.armed) {
      setMessage(state.message || "Watching only. Arm the helper when ready.");
      return;
    }
    if (!pageLooksLikeOpenSeaMintPage()) {
      setMessage("Armed, but this is not an OpenSea collection/drop page.");
      return;
    }
    const target = targetTimeMs();
    if (target && Date.now() < target) {
      setMessage(`Armed · waiting until ${new Date(target).toLocaleString()}.`);
      return;
    }
    if (!candidate) {
      setMessage("Armed · waiting for a visible Mint/Claim control.");
      return;
    }
    if (!priceAllowed(candidate)) {
      setMessage(candidate.free
        ? "Free evidence found, but the helper is configured for paid-only protection."
        : "Control found, but free-only or price-cap protection blocked the click.");
      return;
    }
    if (!state.autoClick) {
      setMessage("Control found · click it yourself, then review the wallet popup.");
      return;
    }
    if (state.clicked) return;
    if (setVisibleQuantity()) {
      setMessage("Quantity applied. Clicking the page control once; review the wallet popup.");
    } else {
      setMessage("Clicking the page control once; review the wallet popup.");
    }
    state.clicked = true;
    state.armed = false;
    if (armButton) armButton.textContent = "Arm once";
    candidate.element.click();
  }

  function scheduleScan() {
    if (scanQueued) return;
    scanQueued = true;
    window.requestAnimationFrame(() => {
      scanQueued = false;
      scan();
    });
  }

  function style(element, css) {
    element.style.cssText = css;
    return element;
  }

  function addCheckbox(labelText, checked, onChange) {
    const label = document.createElement("label");
    style(label, "display:block;margin:7px 0;cursor:pointer");
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = checked;
    checkbox.addEventListener("change", () => onChange(checkbox.checked));
    label.appendChild(checkbox);
    label.appendChild(document.createTextNode(` ${labelText}`));
    panel.appendChild(label);
    return checkbox;
  }

  function addField(labelText, type, value, onInput) {
    const label = document.createElement("label");
    style(label, "display:block;margin:7px 0;color:#cbd0dc");
    label.appendChild(document.createTextNode(labelText));
    const input = document.createElement("input");
    input.type = type;
    input.value = value;
    style(input, "display:block;box-sizing:border-box;width:100%;margin-top:3px;padding:5px;border:1px solid #566078;border-radius:5px;background:#101217;color:#f4f5f7");
    input.addEventListener("input", () => onInput(input.value));
    label.appendChild(input);
    panel.appendChild(label);
    return input;
  }

  function makePanel() {
    panel = document.createElement("section");
    panel.id = "opensea-mint-bot-browser-helper";
    style(panel, "position:fixed;z-index:2147483647;right:16px;bottom:16px;width:340px;max-height:90vh;overflow:auto;padding:14px;border:1px solid #46516a;border-radius:12px;background:#171a22;color:#f4f5f7;font:12px/1.45 system-ui,sans-serif;box-shadow:0 10px 32px #0009");

    const title = document.createElement("div");
    title.textContent = "OpenSea Mint Assist";
    style(title, "font-weight:700;font-size:15px;margin-bottom:4px");
    panel.appendChild(title);

    const note = document.createElement("div");
    note.textContent = "Browser helper only. Wallet confirmation is always manual.";
    style(note, "color:#aeb7c9;margin-bottom:9px");
    panel.appendChild(note);

    statusLine = document.createElement("div");
    style(statusLine, "min-height:34px;margin-bottom:6px;color:#f4f5f7");
    panel.appendChild(statusLine);
    detailsLine = document.createElement("div");
    style(detailsLine, "min-height:18px;margin-bottom:9px;color:#9ee6ca");
    panel.appendChild(detailsLine);

    const controls = document.createElement("div");
    style(controls, "display:flex;gap:6px;margin-bottom:8px");
    armButton = document.createElement("button");
    armButton.textContent = state.armed ? "Disarm" : "Arm once";
    style(armButton, "padding:7px 10px;border:0;border-radius:6px;background:#2081e2;color:white;cursor:pointer");
    armButton.addEventListener("click", () => {
      state.armed = !state.armed;
      state.clicked = false;
      armButton.textContent = state.armed ? "Disarm" : "Arm once";
      scan();
    });
    controls.appendChild(armButton);

    const hideButton = document.createElement("button");
    hideButton.textContent = "Hide";
    style(hideButton, "padding:7px 10px;border:1px solid #566078;border-radius:6px;background:transparent;color:#f4f5f7;cursor:pointer");
    hideButton.addEventListener("click", () => { panel.remove(); panel = null; });
    controls.appendChild(hideButton);
    panel.appendChild(controls);

    autoArmBox = addCheckbox("Auto-arm on page load", state.autoArm, (value) => {
      state.autoArm = value;
      if (value) {
        state.armed = true;
        state.clicked = false;
        armButton.textContent = "Disarm";
      }
      savePreferences();
      scheduleScan();
    });
    autoClickBox = addCheckbox("Auto-click one page button", state.autoClick, (value) => {
      state.autoClick = value;
      savePreferences();
      scheduleScan();
    });
    freeOnlyBox = addCheckbox("Free/zero-value only", state.freeOnly, (value) => {
      state.freeOnly = value;
      savePreferences();
      scheduleScan();
    });
    paidBox = addCheckbox("Allow paid click (I understand the risk)", state.paidAcknowledged, (value) => {
      state.paidAcknowledged = value;
      savePreferences();
      scheduleScan();
    });

    maxPriceInput = addField("Maximum visible native-coin price", "number", state.maxPrice, (value) => {
      state.maxPrice = value;
      savePreferences();
      scheduleScan();
    });
    maxPriceInput.min = "0";
    maxPriceInput.step = "0.000000000000000001";
    quantityInput = addField("Quantity to apply if a visible number field exists", "number", state.quantity, (value) => {
      state.quantity = value;
      savePreferences();
    });
    quantityInput.min = "1";
    quantityInput.max = "100";
    targetTimeInput = addField("Wait until browser-local time (optional)", "datetime-local", state.targetTime, (value) => {
      state.targetTime = value;
      savePreferences();
      scheduleScan();
    });

    document.body.appendChild(panel);
    setMessage(state.message);
    scheduleScan();
  }

  function start() {
    if (!document.body) {
      window.setTimeout(start, 250);
      return;
    }
    makePanel();
    const observer = new MutationObserver(scheduleScan);
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
    window.setInterval(scheduleScan, 250);
  }

  start();
}());
