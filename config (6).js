/*
 * Runtime configuration for the standalone web client.
 *
 * When the page is served by this project's backend, leave apiBase empty:
 * the browser will call the same origin automatically.
 *
 * When the page is hosted separately, set apiBase to the backend origin,
 * for example "https://your-api.example.com". Brand fields intentionally
 * remain empty so every deployment can replace them.
 */
window.APP_CONFIG = Object.freeze({
  apiBase: "",
  appName: "",
  ownerName: "",
  contactText: "",
  contactUrl: "",
});
