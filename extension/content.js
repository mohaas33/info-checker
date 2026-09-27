// Content script, injected on demand (chrome.scripting) only into pages the user asked to
// check. It captures the selection, highlights located claims with the CSS Custom
// Highlight API, and shows a hover tooltip.
//
// It never modifies the page's HTML. The only additions are a constructed stylesheet
// (document.adoptedStyleSheets) for ::highlight() rules and one tooltip host element whose
// content lives in a closed Shadow DOM.

(() => {
  "use strict";
  if (globalThis.__infoChecker) return; // already injected into this frame

  const M = globalThis.InfoCheckerMatcher;
  const PREFIX = "info-checker";
  const HIGHLIGHT_VERDICTS = ["contradicted", "misleading", "outdated", "unverifiable", "supported"];
  const LABELS = {
    contradicted: "Contradicted", misleading: "Misleading", outdated: "Outdated",
    unverifiable: "Unverifiable", supported: "Supported", not_checkable: "Not checkable",
  };
  const COLORS = {
    contradicted: "#cf222e", misleading: "#d1590b", outdated: "#8250df",
    unverifiable: "#6e7781", supported: "#1a7f37",
  };
  const SKIP_TAGS = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "TEXTAREA", "INPUT", "SELECT"]);
  const GONE_CHECK_MS = 1500;

  const canHighlight = typeof CSS !== "undefined" && !!CSS.highlights && typeof Highlight === "function";

  const captures = new Map(); // checkId -> { text, segments }
  let active = null; // { checkId, entries: [...] }
  let goneTimer = null;

  // ---------- capture ----------

  function isVisible(el, cache) {
    if (!el) return true;
    if (!cache.has(el)) cache.set(el, el.checkVisibility ? el.checkVisibility() : true);
    return cache.get(el);
  }

  function blockOf(el, root, cache) {
    let cur = el;
    while (cur && cur !== root.parentElement) {
      if (cache.has(cur)) return cache.get(cur);
      const display = getComputedStyle(cur).display;
      if (!display.startsWith("inline") && display !== "contents") {
        cache.set(el, cur);
        return cur;
      }
      cur = cur.parentElement;
    }
    return root;
  }

  /** Text pieces inside the range, with "\n" separators at block boundaries and <br>. */
  function collectPieces(range) {
    const root = range.commonAncestorContainer;
    const pieces = [];
    const visCache = new Map();
    const blockCache = new Map();
    let lastBlock = null;

    // Separators map to no DOM node. Consecutive ones merge, and "\n" beats " ".
    const separator = (text) => {
      if (!pieces.length) return;
      const last = pieces[pieces.length - 1];
      if (last.node) pieces.push({ text });
      else if (text === "\n") last.text = "\n";
    };

    const addText = (node) => {
      const start = node === range.startContainer ? range.startOffset : 0;
      const end = node === range.endContainer ? range.endOffset : node.data.length;
      if (end <= start || !isVisible(node.parentElement, visCache)) return;
      const block = node.parentElement ? blockOf(node.parentElement, root, blockCache) : null;
      if (lastBlock && block !== lastBlock) separator("\n");
      lastBlock = block;
      const text = node.data.slice(start, end);
      // Whitespace-only nodes (source indentation between tags) render as at most a space.
      if (!text.trim()) separator(" ");
      else pieces.push({ text, node, nodeOffset: start });
    };

    if (root.nodeType === Node.TEXT_NODE) {
      addText(root);
      return pieces;
    }
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT, {
      acceptNode(n) {
        if (n.nodeType === Node.ELEMENT_NODE) {
          if (SKIP_TAGS.has(n.tagName) || !range.intersectsNode(n)) return NodeFilter.FILTER_REJECT;
          return n.tagName === "BR" ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
        }
        return range.intersectsNode(n) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
      },
    });
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      if (n.nodeType === Node.TEXT_NODE) addText(n);
      else separator("\n"); // <br>
    }
    return pieces;
  }

  function capture(checkId) {
    const sel = getSelection();
    if (!sel || !sel.rangeCount || sel.isCollapsed) return { ok: false, reason: "no-selection" };
    const flat = M.buildFlatText(collectPieces(sel.getRangeAt(0)));
    if (!flat.text.trim()) return { ok: false, reason: "no-text" }; // e.g. selection in a text box
    clearAll(); // a new check on this page replaces the previous highlights
    captures.clear();
    captures.set(checkId, flat);
    return { ok: true, text: flat.text, canHighlight };
  }

  // ---------- styles & highlights ----------

  let sheet = null;

  function ensureStyles() {
    if (!canHighlight) return;
    const deco = {
      contradicted: "underline wavy", misleading: "underline dashed", outdated: "underline solid",
      unverifiable: "underline dotted", supported: "underline double",
    };
    const css = HIGHLIGHT_VERDICTS.map((v) => `
      ::highlight(${PREFIX}-${v}) {
        background-color: ${COLORS[v]}33;
        text-decoration: ${deco[v]} ${COLORS[v]};
        text-decoration-thickness: 2px;
      }`).join("\n") + `
      ::highlight(${PREFIX}-flash) { background-color: #ffd33d; color: #1f2328; }`;
    try {
      if (!sheet) {
        sheet = new CSSStyleSheet();
        sheet.replaceSync(css);
      }
      // Pages sometimes reassign adoptedStyleSheets; re-add ours if it was dropped.
      if (!document.adoptedStyleSheets.includes(sheet)) {
        document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
      }
    } catch (err) {
      console.warn("[info-checker] could not add highlight styles", err);
    }
  }

  function highlightFor(name) {
    const key = `${PREFIX}-${name}`;
    let h = CSS.highlights.get(key);
    if (!h) {
      h = new Highlight();
      if (name === "flash") h.priority = 1;
      CSS.highlights.set(key, h);
    }
    return h;
  }

  function removeFromHighlights(entry) {
    if (canHighlight && entry.range && entry.highlighted) {
      CSS.highlights.get(`${PREFIX}-${entry.verdict}`)?.delete(entry.range);
    }
    entry.highlighted = false;
  }

  function clearAll() {
    if (canHighlight) {
      for (const v of [...HIGHLIGHT_VERDICTS, "flash"]) CSS.highlights.get(`${PREFIX}-${v}`)?.clear();
    }
    active = null;
    hideTooltip(true);
    stopWatching();
  }

  // ---------- apply results ----------

  function makeRange(segments, pos) {
    const s = segments[pos.startIndex].node;
    const e = segments[pos.endIndex].node;
    if (!s.isConnected || !e.isConnected) return null;
    const r = document.createRange();
    r.setStart(s, pos.startOffset);
    r.setEnd(e, pos.endOffset);
    return r;
  }

  /** claims: [{ original_text, verdict, explanation }]. Returns a status per claim. */
  function apply(checkId, claims, showSupported) {
    const cap = captures.get(checkId);
    if (!cap) return { ok: false, reason: "unknown-check" };
    clearAll();
    ensureStyles();

    const used = [];
    const entries = claims.map((c, index) => {
      const entry = { index, verdict: c.verdict, explanation: c.explanation || "", status: "not_highlighted" };
      if (c.verdict === "not_checkable") return entry;

      const span = M.findSpan(cap.text, c.original_text, used);
      const pos = span && M.spanToSegments(cap.segments, span.start, span.end);
      if (!pos) {
        entry.status = "not_found";
        return entry;
      }
      used.push(span);
      let range = null;
      try {
        range = makeRange(cap.segments, pos);
      } catch { /* node text changed since capture: offsets no longer valid */ }
      if (!range || !M.sameText(range.toString(), cap.text.slice(span.start, span.end))) {
        entry.status = "gone";
        return entry;
      }
      entry.range = range;
      entry.expected = range.toString();
      entry.status = "located";
      if (canHighlight && (c.verdict !== "supported" || showSupported)) {
        highlightFor(c.verdict).add(range);
        entry.highlighted = true;
        entry.status = "highlighted";
      }
      return entry;
    });

    active = { checkId, entries };
    if (entries.some((e) => e.range)) startWatching();
    return { ok: true, canHighlight, statuses: entries.map((e) => e.status) };
  }

  // ---------- detached / collapsed ranges ----------

  function isGone(entry) {
    try {
      const r = entry.range;
      if (!r || r.collapsed) return true;
      if (!r.startContainer.isConnected || !r.endContainer.isConnected) return true;
      // Removed nodes move range boundaries; if the text changed, it no longer marks the claim.
      return !M.sameText(r.toString(), entry.expected);
    } catch {
      return true;
    }
  }

  function checkGone() {
    if (!active) return stopWatching();
    const gone = [];
    for (const e of active.entries) {
      if (e.range && isGone(e)) {
        removeFromHighlights(e);
        e.range = null;
        e.status = "gone";
        gone.push(e.index);
        if (tooltipEntry === e) hideTooltip(true);
      }
    }
    if (gone.length) notify({ type: "ic:claims-gone", checkId: active.checkId, indices: gone });
    if (!active.entries.some((e) => e.range)) stopWatching();
  }

  function startWatching() {
    stopWatching();
    goneTimer = setInterval(() => { try { checkGone(); } catch { /* never throw */ } }, GONE_CHECK_MS);
    document.addEventListener("mousemove", onMouseMove, { capture: true, passive: true });
  }

  function stopWatching() {
    clearInterval(goneTimer);
    goneTimer = null;
    document.removeEventListener("mousemove", onMouseMove, { capture: true });
  }

  // ---------- reveal (card clicked in the side panel) ----------

  function reveal(checkId, index) {
    const entry = active?.checkId === checkId ? active.entries[index] : null;
    if (!entry) return { status: "unknown" };
    if (!entry.range) return { status: entry.status };
    if (isGone(entry)) {
      checkGone();
      return { status: "gone" };
    }
    const node = entry.range.startContainer;
    const el = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
    el?.scrollIntoView({ block: "center", behavior: "smooth" });
    if (canHighlight) flash(entry.range);
    return { status: entry.status };
  }

  let flashTimers = [];
  function flash(range) {
    ensureStyles();
    const h = highlightFor("flash");
    flashTimers.forEach(clearTimeout);
    h.clear();
    // Blink three times after the smooth scroll settles.
    flashTimers = [350, 650, 950, 1250, 1550, 1850].map((t, i) =>
      setTimeout(() => (i % 2 === 0 ? h.add(range) : h.delete(range)), t));
  }

  // ---------- tooltip ----------

  let host = null;
  let tip = null;
  let tooltipEntry = null;
  let hideTimer = null;

  function ensureTooltip() {
    if (host) {
      if (!host.isConnected) document.documentElement.append(host); // page removed it
      return;
    }
    host = document.createElement("info-checker-tooltip");
    host.style.cssText = "all: initial; position: fixed; top: 0; left: 0; z-index: 2147483647; display: none;";
    const shadow = host.attachShadow({ mode: "closed" });
    shadow.innerHTML = `
      <style>
        :host { all: initial; }
        .tip {
          box-sizing: border-box; max-width: 320px; padding: 8px 10px;
          font: 13px/1.4 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
          color: #1f2328; background: #fff; border: 1px solid #d0d7de; border-radius: 8px;
          box-shadow: 0 4px 16px rgba(0,0,0,.18);
        }
        @media (prefers-color-scheme: dark) {
          .tip { color: #e6edf3; background: #161b22; border-color: #30363d; }
        }
        .badge {
          display: inline-block; padding: 1px 8px; border-radius: 999px; color: #fff;
          font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: .02em;
        }
        .text {
          margin: 6px 0; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
          overflow: hidden;
        }
        button {
          font: inherit; font-size: 12px; padding: 2px 10px; cursor: pointer;
          border: 1px solid #d0d7de; border-radius: 6px; background: transparent; color: inherit;
        }
        .head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
        .brand { font-size: 11px; opacity: .6; white-space: nowrap; }
      </style>
      <div class="tip" role="tooltip">
        <div class="head"><span class="badge"></span><span class="brand">Info Checker</span></div>
        <div class="text"></div>
        <button type="button">Details</button>
      </div>`;
    tip = {
      root: shadow.querySelector(".tip"),
      badge: shadow.querySelector(".badge"),
      text: shadow.querySelector(".text"),
      button: shadow.querySelector("button"),
    };
    tip.button.addEventListener("click", () => {
      if (active && tooltipEntry) {
        notify({ type: "ic:focus-claim", checkId: active.checkId, index: tooltipEntry.index });
      }
    });
    host.addEventListener("mouseenter", () => clearTimeout(hideTimer));
    host.addEventListener("mouseleave", () => hideTooltip());
    document.documentElement.append(host);
  }

  function showTooltip(entry, rect) {
    ensureTooltip();
    clearTimeout(hideTimer);
    if (tooltipEntry !== entry) {
      tooltipEntry = entry;
      tip.badge.textContent = LABELS[entry.verdict] || entry.verdict;
      tip.badge.style.background = COLORS[entry.verdict] || "#6e7781";
      // Full explanation, clamped to 3 lines by CSS. Citation numbers refer to the panel.
      tip.text.textContent = entry.explanation.replace(/\s*\[\d+(?:,\s*\d+)*\]/g, "");
    }
    host.style.display = "block";
    const w = host.offsetWidth;
    const h = host.offsetHeight;
    let top = rect.bottom + 6;
    if (top + h > innerHeight - 4) top = Math.max(4, rect.top - h - 6);
    const left = Math.min(Math.max(4, rect.left), innerWidth - w - 4);
    host.style.top = `${top}px`;
    host.style.left = `${left}px`;
  }

  function hideTooltip(now = false) {
    clearTimeout(hideTimer);
    const hide = () => {
      if (host) host.style.display = "none";
      tooltipEntry = null;
    };
    if (now) hide();
    else hideTimer = setTimeout(hide, 250); // time to move the pointer into the tooltip
  }

  // ---------- hover ----------

  let lastMove = 0;
  let pendingMove = null;

  function onMouseMove(e) {
    pendingMove = e;
    const wait = 60 - (Date.now() - lastMove); // throttle
    if (wait > 0) {
      if (!onMouseMove.timer) {
        onMouseMove.timer = setTimeout(() => {
          onMouseMove.timer = null;
          onMouseMove(pendingMove);
        }, wait);
      }
      return;
    }
    lastMove = Date.now();
    try {
      handleHover(e.clientX, e.clientY, e.composedPath?.() || []);
    } catch { /* never throw into the page */ }
  }

  function handleHover(x, y, path) {
    if (!active) return;
    if (host && path.includes(host)) return; // pointer is on the tooltip
    const caret = document.caretRangeFromPoint?.(x, y);
    let hit = null;
    let hitRect = null;
    if (caret) {
      for (const e of active.entries) {
        if (!e.highlighted || !e.range) continue;
        let inRange = false;
        try { inRange = e.range.isPointInRange(caret.startContainer, caret.startOffset); } catch { /* other doc */ }
        if (!inRange) continue;
        // The caret snaps to the nearest text, so confirm the pointer is over the text itself.
        for (const r of e.range.getClientRects()) {
          if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) {
            hit = e;
            hitRect = r;
            break;
          }
        }
        if (hit) break;
      }
    }
    if (hit) {
      if (isGone(hit)) return checkGone();
      showTooltip(hit, hitRect);
    } else if (tooltipEntry) {
      hideTooltip();
    }
  }

  // ---------- messaging ----------

  function notify(msg) {
    try {
      chrome.runtime.sendMessage(msg).catch(() => { /* side panel closed */ });
    } catch { /* extension reloaded; nothing to tell */ }
  }

  chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    try {
      switch (msg?.type) {
        case "ic:apply":
          sendResponse(apply(msg.checkId, msg.claims || [], !!msg.showSupported));
          break;
        case "ic:reveal":
          sendResponse(reveal(msg.checkId, msg.index));
          break;
        case "ic:clear":
          clearAll();
          sendResponse({ ok: true });
          break;
        default:
          return false;
      }
    } catch (err) {
      sendResponse({ ok: false, reason: String(err?.message || err) });
    }
    return false;
  });

  globalThis.__infoChecker = { capture, apply, reveal, clearAll };
})();
