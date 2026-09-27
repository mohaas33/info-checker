// Pure text-matching functions for locating claims inside a captured selection.
// No DOM access here: nodes are opaque references carried through `pieces`/`segments`.
// Loaded as a classic script in pages (window.InfoCheckerMatcher) and via require() in
// node tests.

(function (root) {
  "use strict";

  // Same normalization idea as the backend's locate_span, plus dashes, ellipsis and
  // zero-width characters (common on Facebook).
  const CHAR_MAP = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
    "«": '"', "»": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-",
    "―": "-", "−": "-",
    "…": "...",
  };
  const DROP = /[​-‍⁠﻿­]/;
  const SPACE = /\s/;

  /**
   * Flatten pieces into one string with a map back to each piece.
   * pieces: [{ text, node?, nodeOffset? }]. Pieces without a node are separators
   * (e.g. "\n" between blocks): they are part of the text but map to no DOM node.
   * Returns { text, segments: [{ start, end, node, nodeOffset }] }.
   */
  function buildFlatText(pieces) {
    let text = "";
    const segments = [];
    for (const p of pieces) {
      if (!p.text) continue;
      segments.push({
        start: text.length,
        end: text.length + p.text.length,
        node: p.node ?? null,
        nodeOffset: p.nodeOffset ?? 0,
      });
      text += p.text;
    }
    return { text, segments };
  }

  /**
   * Normalize for fuzzy matching: lowercase, collapse whitespace, straighten quotes,
   * unify dashes, drop zero-width characters. Keeps a map from each output character to
   * its [start, end) in the input, so matches can be mapped back exactly.
   */
  function normalizeWithMap(s) {
    let out = "";
    const starts = [];
    const ends = [];
    let i = 0;
    while (i < s.length) {
      const cp = s.codePointAt(i);
      const len = cp > 0xffff ? 2 : 1;
      const ch = s.slice(i, i + len);

      if (DROP.test(ch)) {
        i += len;
        continue;
      }
      if (SPACE.test(ch)) {
        let j = i + len;
        while (j < s.length && (SPACE.test(s[j]) || DROP.test(s[j]))) j++;
        if (out.length && out[out.length - 1] !== " ") {
          out += " ";
          starts.push(i);
          ends.push(j);
        }
        i = j;
        continue;
      }
      const mapped = (CHAR_MAP[ch] ?? ch).toLowerCase();
      for (let k = 0; k < mapped.length; k++) {
        out += mapped[k];
        starts.push(i);
        ends.push(i + len);
      }
      i += len;
    }
    return { text: out, starts, ends };
  }

  function overlaps(a, b) {
    return a.start < b.end && b.start < a.end;
  }

  function* occurrences(haystack, needle) {
    let from = 0;
    while (true) {
      const i = haystack.indexOf(needle, from);
      if (i === -1) return;
      yield i;
      from = i + 1;
    }
  }

  /**
   * Find needle in haystack. Exact match first, then normalized. Skips occurrences that
   * overlap spans in `used` (so two claims with the same wording get different spans).
   * Returns { start, end, method } in haystack coordinates, or null. Never guesses.
   */
  function findSpan(haystack, needle, used = []) {
    const target = (needle || "").trim();
    if (!target || !haystack) return null;

    for (const i of occurrences(haystack, target)) {
      const span = { start: i, end: i + target.length, method: "exact" };
      if (!used.some((u) => overlaps(u, span))) return span;
    }

    const h = normalizeWithMap(haystack);
    const n = normalizeWithMap(target).text.trim();
    if (!n) return null;
    for (const i of occurrences(h.text, n)) {
      const span = { start: h.starts[i], end: h.ends[i + n.length - 1], method: "normalized" };
      if (!used.some((u) => overlaps(u, span))) return span;
    }
    return null;
  }

  /**
   * Convert a [start, end) span of the flat text into node positions.
   * Separator segments are skipped: a span starting in a separator snaps forward to the
   * next text node, one ending in a separator snaps back. Returns
   * { startIndex, startOffset, endIndex, endOffset } (indices into segments, offsets
   * within each node's data), or null if the span covers no text node.
   */
  function spanToSegments(segments, start, end) {
    if (!(end > start)) return null;
    let startIndex = -1;
    let endIndex = -1;
    for (let k = 0; k < segments.length; k++) {
      const seg = segments[k];
      if (!seg.node || seg.end <= start || seg.start >= end) continue;
      if (startIndex === -1) startIndex = k;
      endIndex = k;
    }
    if (startIndex === -1) return null;
    const s = segments[startIndex];
    const e = segments[endIndex];
    return {
      startIndex,
      startOffset: s.nodeOffset + Math.max(start, s.start) - s.start,
      endIndex,
      endOffset: e.nodeOffset + Math.min(end, e.end) - e.start,
    };
  }

  /** True if two strings read the same, ignoring normalization and all whitespace. */
  function sameText(a, b) {
    const squash = (s) => normalizeWithMap(s).text.replace(/ /g, "");
    return squash(a) === squash(b);
  }

  const api = { buildFlatText, normalizeWithMap, findSpan, spanToSegments, sameText };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.InfoCheckerMatcher = api;
})(globalThis);
