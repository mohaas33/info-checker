// Shared settings. Both are configurable on the options page.

export const DEFAULT_BACKEND_URL = "http://localhost:8000";
export const REQUEST_TIMEOUT_MS = 90_000;
export const MAX_TEXT_CHARS = 10_000; // must match the backend's limit

export async function getBackendUrl() {
  const { backendUrl } = await chrome.storage.sync.get("backendUrl");
  return (backendUrl || DEFAULT_BACKEND_URL).replace(/\/+$/, "");
}

// "supported" highlights are off by default: they add noise to the page.
export async function getShowSupported() {
  const { showSupported } = await chrome.storage.sync.get("showSupported");
  return showSupported === true;
}
