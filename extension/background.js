// Service worker: adds the "Fact-check this" context menu. On click it injects the content
// script into that page only (activeTab + scripting), captures the selection right away,
// and hands the check to the side panel. Nothing runs on pages the user hasn't asked to check.

const MENU_ID = "fact-check-selection";

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: MENU_ID,
    title: "Fact-check this",
    contexts: ["selection"],
  });
  // Clicking the toolbar icon opens the panel too (shows the last result).
  chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch(console.error);
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== MENU_ID || !tab) return;

  // sidePanel.open() only works inside the user gesture, so call it before any await.
  chrome.sidePanel.open({ windowId: tab.windowId }).catch(console.error);

  const check = {
    id: crypto.randomUUID(),
    text: info.selectionText || "",
    url: info.frameUrl || tab.url || null,
    tabId: tab.id,
    frameId: info.frameId ?? 0,
    highlight: false, // true once the content script captured the selection as a DOM range
    createdAt: Date.now(),
  };

  // Capture now: the user may click elsewhere while the check runs, losing the selection.
  try {
    const target = { tabId: tab.id, frameIds: [check.frameId] };
    await chrome.scripting.executeScript({ target, files: ["matcher.js", "content.js"] });
    const [{ result } = {}] = await chrome.scripting.executeScript({
      target,
      func: (id) => globalThis.__infoChecker.capture(id),
      args: [check.id],
    });
    if (result?.ok) {
      check.text = result.text; // same text the highlights are matched against
      check.highlight = result.canHighlight;
    }
  } catch (err) {
    // Browser pages, the Web Store, PDFs, some iframes: check the text without highlighting.
    console.warn("Info Checker: can't inject into this page; highlighting is off.", err?.message);
  }

  await chrome.storage.session.set({ pendingCheck: check });
});
