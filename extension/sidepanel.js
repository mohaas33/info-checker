import { getBackendUrl, getShowSupported, MAX_TEXT_CHARS, REQUEST_TIMEOUT_MS } from "./config.js";

// Most severe first; also the order used in the summary line.
const VERDICTS = {
  contradicted: "Contradicted",
  misleading: "Misleading",
  outdated: "Outdated",
  unverifiable: "Unverifiable",
  supported: "Supported",
  not_checkable: "Not checkable",
};
const TYPE_LABELS = { factual: "factual claim", quote: "quote", opinion: "opinion", prediction: "prediction" };
const LOCATE_NOTES = {
  not_found: "Couldn't locate this text on the page",
  gone: "No longer visible on the page",
};
// Ignore a stale pending request (e.g. the panel was closed mid-check long ago).
const PENDING_MAX_AGE_MS = 10 * 60_000;

const $ = (id) => document.getElementById(id);
let current = null; // the in-flight request: { request, controller }
let lastRequest = null;
// What the results view shows: { request, response, statuses } (statuses from the page).
let shown = null;

// ---------- view switching ----------

function show(state) {
  for (const s of document.querySelectorAll(".state")) s.hidden = s.id !== state;
}

function showError(title, detail = "") {
  shown = null;
  $("error-title").textContent = title;
  $("error-detail").textContent = detail;
  $("retry").hidden = !lastRequest;
  show("error");
}

// ---------- running a check ----------

async function runCheck(request) {
  current?.controller.abort(new DOMException("superseded", "AbortError"));
  lastRequest = request;
  shown = null;

  const text = (request.text || "").trim();
  if (!text) {
    showError("No text selected.", "Select some text on the page, then right-click and choose “Fact-check this”.");
    return;
  }
  if (text.length > MAX_TEXT_CHARS) {
    showError("The selection is too long.",
      `It has ${text.length.toLocaleString()} characters; the limit is ${MAX_TEXT_CHARS.toLocaleString()}. Select a shorter passage.`);
    return;
  }

  const controller = new AbortController();
  const started = Date.now();
  const timer = setInterval(() => {
    $("elapsed").textContent = `${Math.floor((Date.now() - started) / 1000)}s`;
  }, 1000);
  const timeout = setTimeout(
    () => controller.abort(new DOMException("timeout", "TimeoutError")), REQUEST_TIMEOUT_MS);
  current = { request, controller };

  $("elapsed").textContent = "0s";
  $("loading-text").textContent = text.length > 400 ? `${text.slice(0, 400)}…` : text;
  show("loading");

  const backend = await getBackendUrl();
  let outcome;
  try {
    // Send the text exactly as captured (not trimmed), so returned spans match the page.
    const resp = await fetch(`${backend}/check`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: request.text, url: request.url }),
      signal: controller.signal,
    });
    const body = await resp.json().catch(() => null);
    if (resp.ok && !Array.isArray(body?.claims)) {
      outcome = { ok: false, title: "The server sent an unexpected response.", detail: "" };
    } else {
      outcome = resp.ok
        ? { ok: true, response: body }
        : { ok: false, ...httpError(resp.status, body) };
    }
  } catch (err) {
    const reason = controller.signal.reason;
    if (reason?.message === "superseded") return; // a newer request took over
    if (reason?.message === "cancelled") {
      show("idle");
      return;
    }
    outcome = { ok: false, ...networkError(err, reason, backend) };
  } finally {
    clearInterval(timer);
    clearTimeout(timeout);
    if (current?.controller === controller) current = null;
  }

  await chrome.storage.session.set({ lastOutcome: { id: request.id, ...outcome } });
  renderOutcome(request, outcome);
}

function httpError(status, body) {
  const detail = body?.detail;
  if (status === 422) {
    const msg = Array.isArray(detail) ? detail.map((d) => d.msg).join("; ") : String(detail ?? "");
    return { title: "The server rejected the text.", detail: msg.replace(/^Value error, /, "") };
  }
  if (status === 502) {
    return { title: "The server couldn't analyze the text.", detail: String(detail ?? "") };
  }
  return { title: `Server error (HTTP ${status}).`, detail: typeof detail === "string" ? detail : "" };
}

function networkError(err, reason, backend) {
  if (reason?.name === "TimeoutError") {
    return {
      title: "The check took too long.",
      detail: `No answer after ${REQUEST_TIMEOUT_MS / 1000} seconds. Try a shorter selection, or try again.`,
    };
  }
  return {
    title: "Can't reach the fact-check server.",
    detail: `Nothing answered at ${backend}. Is the backend running? You can change the address in Settings. (${err.message})`,
  };
}

// ---------- page highlights (talks to content.js in the checked tab) ----------

async function sendToPage(request, msg) {
  return chrome.tabs.sendMessage(request.tabId, { checkId: request.id, ...msg }, { frameId: request.frameId });
}

async function applyHighlights() {
  if (!shown) return;
  const { request, response } = shown;
  shown.statuses = null;
  let note = "";

  if (!request.highlight) {
    note = "Highlighting isn't available on this page (for example browser pages, PDFs, or text boxes).";
  } else {
    const showSupported = await getShowSupported();
    $("legend-supported").hidden = !showSupported;
    try {
      const res = await sendToPage(request, {
        type: "ic:apply",
        showSupported,
        claims: response.claims.map((c) => ({
          original_text: c.original_text, verdict: c.verdict, explanation: c.explanation,
        })),
      });
      if (res?.ok) shown.statuses = res.statuses;
      else note = "The page changed since the check started, so claims can't be highlighted.";
    } catch {
      note = "The page was closed or reloaded, so claims can't be highlighted.";
    }
  }
  if (!shown) return; // a new check started meanwhile
  $("highlight-bar").hidden = !shown.statuses;
  $("clear-highlights").textContent = "Clear highlights";
  $("highlight-note").textContent = note;
  $("highlight-note").hidden = !note;
  updateCards();
}

function updateCards() {
  const statuses = shown?.statuses;
  [...$("claims").children].forEach((card, i) => {
    const status = statuses?.[i];
    const note = card.querySelector(".locate-note");
    note.textContent = LOCATE_NOTES[status] || "";
    note.hidden = !LOCATE_NOTES[status];
    const revealable = status === "highlighted" || status === "located";
    card.dataset.revealable = String(revealable);
    card.tabIndex = revealable ? 0 : -1;
    card.title = revealable ? "Show on page" : "";
  });
}

async function revealClaim(index) {
  if (!shown?.statuses) return;
  try {
    const res = await sendToPage(shown.request, { type: "ic:reveal", index });
    if (res?.status === "gone" && shown?.statuses) {
      shown.statuses[index] = "gone";
      updateCards();
    }
  } catch {
    shown.statuses = null;
    $("highlight-bar").hidden = true;
    $("highlight-note").textContent = "The page was closed or reloaded, so claims can't be highlighted.";
    $("highlight-note").hidden = false;
    updateCards();
  }
}

// ---------- rendering ----------

function renderOutcome(request, outcome) {
  if (!outcome.ok) return showError(outcome.title, outcome.detail);
  shown = { request, response: outcome.response, statuses: null };
  renderResults(outcome.response);
  applyHighlights();
}

function renderResults(data) {
  const claims = data?.claims ?? [];
  $("summary").textContent = summaryLine(claims);
  $("checked-at").textContent = data?.checked_at
    ? `Checked ${new Date(data.checked_at).toLocaleString()}`
    : "";
  $("claims").replaceChildren(...claims.map(renderCard));
  $("highlight-bar").hidden = true;
  $("highlight-note").hidden = true;
  show("results");
}

function summaryLine(claims) {
  if (!claims.length) return "No claims found in the selection.";
  const counts = {};
  for (const c of claims) counts[c.verdict] = (counts[c.verdict] || 0) + 1;
  const parts = Object.keys(VERDICTS)
    .filter((v) => counts[v])
    .map((v) => `${counts[v]} ${VERDICTS[v].toLowerCase()}`);
  const noun = claims.length === 1 ? "claim" : "claims";
  return `${claims.length} ${noun} checked: ${parts.join(", ")}`;
}

function renderCard(c, index) {
  const card = $("claim-card").content.firstElementChild.cloneNode(true);
  const q = (sel) => card.querySelector(sel);
  card.dataset.index = String(index);

  const badge = q(".badge");
  badge.dataset.verdict = c.verdict;
  badge.textContent = VERDICTS[c.verdict] ?? c.verdict;
  q(".type").textContent = TYPE_LABELS[c.type] ?? c.type;
  q(".confidence").textContent = c.verdict === "not_checkable" ? "" : `Confidence: ${c.confidence}`;
  q(".claim").textContent = c.claim;
  q(".explanation").textContent = c.explanation;

  // Show the original wording when the rewrite differs noticeably from it.
  const norm = (s) => (s || "").toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
  if (c.original_text && norm(c.original_text) !== norm(c.claim)) {
    q(".original").textContent = `“${c.original_text}”`;
    q(".original").hidden = false;
  }

  if (c.existing_fact_checks?.length) {
    q(".fact-checks ul").replaceChildren(...c.existing_fact_checks.map((f) => {
      const li = document.createElement("li");
      li.append(link(f.url, f.publisher), `: ${f.rating}`);
      return li;
    }));
    q(".fact-checks").hidden = false;
  }

  if (c.sources?.length) {
    // Numbered to match the [1], [2] citations in the explanation.
    q(".sources ol").replaceChildren(...c.sources.map((s) => {
      const li = document.createElement("li");
      const pub = document.createElement("span");
      pub.className = "publisher";
      pub.textContent = ` — ${s.publisher}`;
      li.append(link(s.url, s.title), pub);
      return li;
    }));
    q(".sources").hidden = false;
  }
  return card;
}

// All server data goes through textContent; links only for http(s) URLs.
function link(url, text) {
  let safe = null;
  try {
    const u = new URL(url);
    if (u.protocol === "http:" || u.protocol === "https:") safe = u.href;
  } catch { /* not a URL */ }
  if (!safe) {
    const span = document.createElement("span");
    span.textContent = text;
    return span;
  }
  const a = document.createElement("a");
  a.href = safe;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  a.textContent = text || safe;
  return a;
}

// ---------- wiring ----------

$("cancel").addEventListener("click", () => {
  current?.controller.abort(new DOMException("cancelled", "AbortError"));
});
$("retry").addEventListener("click", () => lastRequest && runCheck(lastRequest));
$("options-link").addEventListener("click", (e) => {
  e.preventDefault();
  chrome.runtime.openOptionsPage();
});

// Clicking a card scrolls the page to its highlight and flashes it.
$("claims").addEventListener("click", (e) => {
  if (e.target.closest("a")) return; // source links open normally
  const card = e.target.closest(".card");
  if (card?.dataset.revealable === "true") revealClaim(Number(card.dataset.index));
});
$("claims").addEventListener("keydown", (e) => {
  const card = e.target.closest?.(".card");
  if ((e.key === "Enter" || e.key === " ") && card === e.target && card.dataset.revealable === "true") {
    e.preventDefault();
    revealClaim(Number(card.dataset.index));
  }
});

// Toggles between clearing and re-showing the page highlights.
$("clear-highlights").addEventListener("click", async () => {
  if (!shown) return;
  if (shown.statuses) {
    await sendToPage(shown.request, { type: "ic:clear" }).catch(() => {});
    shown.statuses = null;
    updateCards();
    $("clear-highlights").textContent = "Show highlights";
  } else {
    applyHighlights();
  }
});

// Messages from the content script in the checked tab.
chrome.runtime.onMessage.addListener((msg, sender) => {
  if (!shown || msg?.checkId !== shown.request.id || sender.tab?.id !== shown.request.tabId) return;
  if (msg.type === "ic:focus-claim") {
    const card = $("claims").children[msg.index];
    if (!card) return;
    card.scrollIntoView({ block: "center", behavior: "smooth" });
    card.focus({ preventScroll: true });
    card.classList.remove("pulse");
    void card.offsetWidth; // restart the animation
    card.classList.add("pulse");
  } else if (msg.type === "ic:claims-gone" && shown.statuses) {
    for (const i of msg.indices) shown.statuses[i] = "gone";
    updateCards();
  }
});

// The "highlight supported claims" option changed: re-apply to the current page.
chrome.storage.sync.onChanged.addListener((changes) => {
  if (changes.showSupported && shown?.statuses) applyHighlights();
});

// A new "Fact-check this" click while the panel is already open.
chrome.storage.session.onChanged.addListener((changes) => {
  const req = changes.pendingCheck?.newValue;
  if (req) runCheck(req);
});

// Panel just opened: run the pending request, or show its saved result.
(async () => {
  const { pendingCheck, lastOutcome } = await chrome.storage.session.get(["pendingCheck", "lastOutcome"]);
  if (!pendingCheck) return show("idle");
  if (lastOutcome?.id === pendingCheck.id) {
    lastRequest = pendingCheck;
    return renderOutcome(pendingCheck, lastOutcome);
  }
  if (Date.now() - pendingCheck.createdAt < PENDING_MAX_AGE_MS) return runCheck(pendingCheck);
  show("idle");
})();
