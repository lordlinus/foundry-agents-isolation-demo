// Runtime config for the UI.
//
// When the UI is served by the FastAPI backend (local dev or single-container deploy),
// leave API_BASE empty so fetch("/api/...") hits the same origin.
//
// When the UI is hosted standalone on GitHub Pages, set API_BASE to the deployed
// Container App URL (e.g. "https://ca-foundry-demo.<hash>.<region>.azurecontainerapps.io").
// The GitHub Actions workflow rewrites this value at publish time from the
// `API_BASE` repository variable.
window.API_BASE = "";

// Helper: build a backend URL. Usage: api("/api/chat") or api(`/api/history?${qs}`).
window.api = function (path) {
  const base = (window.API_BASE || "").replace(/\/+$/, "");
  return base + path;
};
