// Reports uncaught errors to the hosting environment so canary analysis sees real failures.
// The demo's environment server answers ./__beacon; elsewhere the request fails quietly.
function report(kind, message) {
  try {
    navigator.sendBeacon("./__beacon", JSON.stringify({ kind, message: String(message).slice(0, 300) }));
  } catch {
    // Telemetry must never break the page.
  }
}

window.addEventListener("error", (event) => report("error", event.message));
window.addEventListener("unhandledrejection", (event) => report("unhandledrejection", event.reason));
