# Web client

The browser client is self-contained except for optional media playback:

- `index.html`: generated application
- `config.js`: API address and empty brand fields
- `vendor/`: local xterm CSS and JavaScript

The backend serves this directory automatically at `/`.

For standalone hosting, upload `index.html`, `config.js`, and `vendor/`, then set `apiBase` in `config.js`. Configure `CORS_ORIGINS` on the backend if the page and API use different origins.

The default client calls the same origin when `apiBase` is empty. All deployment names and contact fields are intentionally blank.
