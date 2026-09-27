# Development status

Last updated: September 2026. The project is an **MVP**: the full pipeline works end to
end against the real APIs, but real-world accuracy hasn't been measured yet. For setup and
usage, see the [README](../README.md).

## Current state

| Area | State |
|---|---|
| Backend pipeline: extraction → evidence → verification → citation checks | Working, tested against the real APIs |
| Web search (Tavily), with trusted domains, per-site limit and a news query for time-sensitive claims | Working, tested against the real APIs |
| Google Fact Check integration | Implemented and unit-tested; **not yet tested with a real key** |
| SQLite result cache (24 h) | Working |
| Extension: context menu, side panel, options page | Working (see "Verification" below) |
| Extension: on-page highlighting, tooltip, card ↔ highlight navigation | Working (see "Verification" below) |
| Accuracy evaluation script (`scripts/eval.py`) | Working. The dataset in `backend/eval/claims.csv` has only 4 example rows |

## How it's built

### Backend (`backend/app/`)

| Module | Role |
|---|---|
| `main.py` | FastAPI app: `POST /check`, `GET /health`, CORS limited to `ALLOWED_ORIGINS`. Checks keys and models at startup |
| `pipeline.py` | The checking pipeline: claim extraction, evidence gathering, verification, citation validation, time limits |
| `prompts.py` | Extraction and verification prompts |
| `llm.py` | Every LLM call goes through `complete_json()`. It requests JSON mode, strips `<think>` blocks and code fences, validates with Pydantic, and retries once. Swapping providers means changing only this module |
| `search.py` | Tavily search, `sources.yaml` loading, host → site mapping, date parsing |
| `factcheck_api.py` | Google Fact Check `claims:search` client |
| `cache.py` | SQLite cache. The key covers the text, both model names, `MAX_CLAIMS`, the trusted-domain list and `PIPELINE_VERSION` |
| `config.py` | Settings from `backend/.env`. Keys come from `*_FILE` paths and are never logged |
| `schemas.py`, `usage.py` | API and LLM schemas; per-request cost counters |

**Rules enforced in code**, not just in prompts:
- Citations to evidence items that don't exist are dropped. The remaining numbers are
  renumbered to match the `sources` list shown to the user.
- A verdict other than *unverifiable* that has no valid citation left is downgraded to
  *unverifiable* with low confidence.
- A *contradicted* or *misleading* verdict that cites only reference sites
  (the `reference` group in `sources.yaml`) gets low confidence.
- Only Google fact-checks the model actually cited appear in `existing_fact_checks`.
- Each claim has a 45 s time limit and the whole request 80 s (the extension waits up to
  90 s). A claim that fails or times out comes back *unverifiable* with a note, and the
  rest of the response is still returned. Responses with a failed claim aren't cached.

**Evidence gathering per claim:**
- 1–2 general Tavily queries, 10 results each.
- One news-mode query if the extraction step flags the claim as time-sensitive. News
  results carry publication dates and are ranked first.
- The optional Google Fact Check lookup.
- Results are deduplicated by URL and by content, and capped at 2 per site. Missing
  dates are read from the URL where possible. Today's date and each item's publication
  date go into the verification prompt.

### Extension (`extension/`)

| File | Role |
|---|---|
| `background.js` | Context menu. On click, injects the content script into that tab only (`activeTab` + `scripting`), captures the selection immediately, and opens the side panel |
| `content.js` | Captures the selection as text nodes, highlights claims with the CSS Custom Highlight API, shows the Shadow-DOM tooltip, and detects removed or re-rendered text |
| `matcher.js` | Pure functions for finding a claim's text inside the captured selection: exact match first, then normalized (case, whitespace, quotes, dashes). Unit-tested with `node --test` |
| `sidepanel.*` | Results UI: loading state, errors, summary, claim cards, legend, clearing highlights |
| `options.*` | Backend URL and the "highlight supported claims" toggle |
| `test-page.html` | Page with tricky markup for manual testing |

## Verification

**Automated:**
- 97 backend tests: all external APIs are mocked, and settings are isolated from the
  local `.env`.
- 12 matcher tests.
- Both suites run in CI with no secrets.

**Tested against the real APIs:**
- Model selection.
- Smoke tests: a false claim, a true claim, an opinion, and a time-sensitive claim about
  a change of UK Prime Minister. That last one is answered from dated news evidence, not
  from the model's training data.
- `eval.py` on the example rows.

**Tested in Chrome, but not as an installed extension.** The real `content.js`,
`matcher.js` and side panel code ran in ordinary tabs, with small stand-ins for the
`chrome.*` extension APIs and the real backend behind them. This covered:
- capture and matching across bold and link tags, nested spans, curly quotes and `<br>`
- a repeated sentence outside the selection not being highlighted
- the tooltip and "Details", card click → scroll and flash
- simulated feed re-rendering
- behaviour when the Highlight API is unavailable
- the error states
- confirming the page's `<body>` HTML is unchanged

**Not yet verified:**
1. The installed, unpacked extension end to end: context menu → script injection → side
   panel → highlights.
2. Highlights registered from the extension's isolated world. The Highlight API registry is
   per document, so this should behave like the in-page test, but it hasn't been confirmed.
3. Real Facebook posts: capture, highlights surviving scrolling, tooltip styling.
4. The Google Fact Check path with a real API key.

### Manual test checklist

**`extension/test-page.html`** (serve with `python3 -m http.server` from `extension/`):
1. Check cases 1–5, clicking elsewhere during the wait. The highlights land on the right text.
2. The sidebar copy of the repeated sentence is not highlighted.
3. Hover shows the tooltip, and **Details** focuses the matching card.
4. Clicking a card scrolls to its highlight and flashes it.
5. The "supported" setting takes effect without re-checking.
6. **Simulate feed re-render**: the post's card changes to "No longer visible on the page".
7. A new check replaces the old highlights, and **Clear highlights** toggles them.

**A real news article:**
- Select paragraphs containing links or bold text, and a quote in curly quotes.
- Click a card whose claim is far down the page: the page scrolls back to it.
- Other tabs are unaffected.

**Facebook:**
1. Expand "See more", then select and check the post.
2. The tooltip isn't clipped or restyled by the page.
3. Scroll the post away and back. Either the highlight survives, or the card says "No
   longer visible". No console errors either way.
4. No slowdown while scrolling.
5. Checking a second post clears the first post's highlights.

## Known issues

- **Verdict variance between runs.** A claim about a former office holder has come back
  both *outdated* and *contradicted* with the same prompt and model. The size of this
  hasn't been measured.
- **Few dates on general web results.** Only news-mode results reliably have dates.
  Claims not flagged as time-sensitive don't get a news query.
- **Thin or repetitive evidence for some claims.** Search can return the same article
  under several URLs. These are deduplicated, which sometimes leaves only 2–3 distinct
  sources.
- **No highlighting without script access.** When the extension can't inject its script
  (browser pages, PDFs, some iframes), it falls back to Chrome's plain `selectionText`.
  That text has no line breaks, and nothing is highlighted.
- **One element added to the page.** The tooltip host is attached to `<html>`. It's
  re-added if the page removes it.
- **The default models were chosen from a handful of samples**, not a systematic
  evaluation.

## Next steps

1. Run the "Not yet verified" items above with the installed extension, and fix whatever
   they turn up.
2. Build an evaluation set of 30–50 claims from Snopes, PolitiFact and Full Fact
   (`backend/eval/claims.csv`; rating mapping in the README). Then use `eval.py` to
   compare models and prompts, and to measure run-to-run consistency.
3. Test the Google Fact Check integration with a real key.
4. Possible improvements:
   - Re-locate claims after a feed re-renders the post, instead of dropping the
     highlight.
   - A per-claim "why" view in the side panel, showing the evidence the model saw.
   - Bump `PIPELINE_VERSION` in `backend/app/cache.py` whenever prompts or pipeline
     logic change.

Out of scope for now: image reverse search, automatic feed scanning, user accounts.
