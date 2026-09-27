// Run: node --test extension/tests/
const test = require("node:test");
const assert = require("node:assert/strict");
const M = require("../matcher.js");

// Fake "nodes" are plain objects; the matcher only carries them through.
function flat(...parts) {
  return M.buildFlatText(parts.map((p, i) =>
    typeof p === "string" ? { text: p, node: { id: i } } : p));
}
const SEP = { text: "\n" }; // block separator, no node

function locate(f, needle, used) {
  const span = M.findSpan(f.text, needle, used);
  if (!span) return null;
  const pos = M.spanToSegments(f.segments, span.start, span.end);
  return { span, pos, text: f.text.slice(span.start, span.end) };
}

test("exact match in a single node", () => {
  const f = flat("The Eiffel Tower is 330 metres tall. It opened in 1889.");
  const r = locate(f, "It opened in 1889.");
  assert.equal(r.span.method, "exact");
  assert.equal(r.text, "It opened in 1889.");
  assert.deepEqual(r.pos, { startIndex: 0, startOffset: 37, endIndex: 0, endOffset: 55 });
});

test("text split across several nodes (bold, link, nested spans)", () => {
  // <p>The <b>Eiffel Tower</b> is <a>330 metres</a> tall.</p>
  const f = flat("The ", "Eiffel Tower", " is ", "330 metres", " tall.");
  const r = locate(f, "Eiffel Tower is 330 metres tall");
  assert.equal(r.text, "Eiffel Tower is 330 metres tall");
  assert.deepEqual(r.pos, { startIndex: 1, startOffset: 0, endIndex: 4, endOffset: 5 });
});

test("partial first and last node use node offsets", () => {
  // Captured selection started mid-node: the first piece begins at offset 10 of its node.
  const f = M.buildFlatText([
    { text: "Great Wall ", node: "n0", nodeOffset: 10 },
    { text: "of China", node: "n1", nodeOffset: 0 },
  ]);
  const r = locate(f, "Wall of");
  assert.deepEqual(r.pos, { startIndex: 0, startOffset: 16, endIndex: 1, endOffset: 2 });
});

test("case and whitespace differences", () => {
  const f = flat("The  Great\n  Wall ", "of   China is visible.");
  const r = locate(f, "the great wall of china IS visible");
  assert.equal(r.span.method, "normalized");
  assert.equal(r.text, "The  Great\n  Wall of   China is visible");
});

test("block separators count as whitespace, and spans snap to text nodes", () => {
  const f = flat("First paragraph ends here.", SEP, "Second one starts.");
  const r = locate(f, "ends here. Second one");
  assert.equal(r.text, "ends here.\nSecond one");
  assert.equal(r.pos.startIndex, 0);
  assert.equal(r.pos.endIndex, 2);
  // A span that is only a separator maps to no node.
  assert.equal(M.spanToSegments(f.segments, 26, 27), null);
});

test("curly quotes and apostrophes match straight ones", () => {
  const f = flat("“The only thing we have to fear is fear itself,” said Roosevelt. It’s famous.");
  const r = locate(f, '"The only thing we have to fear is fear itself," said Roosevelt');
  assert.equal(r.span.method, "normalized");
  assert.ok(r.text.startsWith("“The only"));
  assert.ok(r.text.endsWith("said Roosevelt"));
  assert.equal(locate(f, "It's famous.").text, "It’s famous.");
});

test("dashes, ellipsis and zero-width characters", () => {
  const f = flat("COVID‑19 — declared a pandemic… in March​ 2020.");
  const r = locate(f, "COVID-19 - declared a pandemic... in March 2020");
  assert.ok(r);
  assert.equal(r.text, "COVID‑19 — declared a pandemic… in March​ 2020");
});

test("repeated sentence: each claim gets its own occurrence", () => {
  const f = flat("The Moon is made of cheese. Some say so. ", "The Moon is made of cheese.");
  const first = M.findSpan(f.text, "The Moon is made of cheese.");
  const second = M.findSpan(f.text, "The Moon is made of cheese.", [first]);
  assert.equal(first.start, 0);
  assert.equal(second.start, 41);
  assert.equal(M.findSpan(f.text, "The Moon is made of cheese.", [first, second]), null);
});

test("normalized repeated match also skips used spans", () => {
  const f = flat("It’s true. It’s true.");
  const a = M.findSpan(f.text, "it's true");
  const b = M.findSpan(f.text, "it's true", [a]);
  assert.deepEqual([a.start, b.start], [0, 11]);
});

test("text that isn't there returns null (no guessing)", () => {
  const f = flat("The Eiffel Tower is 330 metres tall.");
  assert.equal(M.findSpan(f.text, "The Eiffel Tower is 300 metres tall."), null);
  assert.equal(M.findSpan(f.text, "Paris"), null);
  assert.equal(M.findSpan(f.text, ""), null);
  assert.equal(M.findSpan(f.text, "   "), null);
  assert.equal(M.findSpan("", "anything"), null);
});

test("surrogate pairs (emoji) keep offsets correct", () => {
  const f = flat("Great news \u{1F389} the bridge opened in 2024.");
  const r = locate(f, "THE BRIDGE opened in 2024");
  assert.equal(r.text, "the bridge opened in 2024");
});

test("sameText ignores whitespace and normalization", () => {
  assert.ok(M.sameText("It’s  here.\nNow", "it's here. now"));
  assert.ok(!M.sameText("It's here", "It's there"));
});
