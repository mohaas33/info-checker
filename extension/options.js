import { DEFAULT_BACKEND_URL, getBackendUrl, getShowSupported } from "./config.js";

const input = document.getElementById("backend-url");
const status = document.getElementById("status");

function setStatus(text, cls = "") {
  status.textContent = text;
  status.className = cls;
}

function parseUrl(value) {
  try {
    const u = new URL(value.trim());
    if (u.protocol !== "http:" && u.protocol !== "https:") return null;
    return u.origin; // the backend lives at the root of its origin
  } catch {
    return null;
  }
}

async function save() {
  const origin = parseUrl(input.value || DEFAULT_BACKEND_URL);
  if (!origin) return setStatus("Enter a full http:// or https:// URL.", "err");

  // Must be called directly from the click handler (a user gesture).
  const granted = await chrome.permissions.request({ origins: [`${origin}/*`] });
  if (!granted) return setStatus(`Permission to contact ${origin} was not granted.`, "err");

  await chrome.storage.sync.set({ backendUrl: origin });
  input.value = origin;
  setStatus("Saved.", "ok");
}

async function test() {
  const backend = parseUrl(input.value) || (await getBackendUrl());
  setStatus(`Contacting ${backend}…`);
  try {
    const resp = await fetch(`${backend}/health`, { signal: AbortSignal.timeout(5000) });
    const body = await resp.json();
    if (!resp.ok || body.status !== "ok") throw new Error(`HTTP ${resp.status}`);
    setStatus(`Connected. Models: ${body.extraction_model} / ${body.verification_model}.`, "ok");
  } catch (err) {
    setStatus(`Can't reach ${backend}: ${err.message}. Is the backend running, and was the address saved?`, "err");
  }
}

async function reset() {
  await chrome.storage.sync.remove("backendUrl");
  input.value = DEFAULT_BACKEND_URL;
  setStatus("Reset to the default.", "ok");
}

document.getElementById("save").addEventListener("click", save);
document.getElementById("test").addEventListener("click", test);
document.getElementById("reset").addEventListener("click", reset);
getBackendUrl().then((url) => { input.value = url; });

const showSupported = document.getElementById("show-supported");
getShowSupported().then((v) => { showSupported.checked = v; });
showSupported.addEventListener("change", () => {
  chrome.storage.sync.set({ showSupported: showSupported.checked });
});
