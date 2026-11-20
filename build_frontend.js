const fs = require("fs");

const template = fs.readFileSync("frontend/index.template.html", "utf8");
const worldMap = fs.readFileSync("map-countries.svgfrag", "utf8");
const html = template
  .replaceAll("__WORLD_MAP_COUNTRIES__", worldMap);

fs.writeFileSync("frontend/index.html", html, "utf8");
fs.copyFileSync("frontend/index.html", "index.html");
fs.copyFileSync("frontend/config.js", "config.js");
fs.cpSync("frontend/vendor", "vendor", { recursive: true });

const worker = `const HTML = ${JSON.stringify(html)};
const RUNTIME_CONFIG = ${JSON.stringify(
  {
    apiBase: "",
    appName: "",
    ownerName: "",
    contactText: "",
    contactUrl: "",
  },
  null,
  2,
)};
const VENDOR = ${JSON.stringify(
  Object.fromEntries(
    ["xterm.css", "xterm.js", "xterm-addon-fit.js"].map((name) => [
      `/vendor/${name}`,
      {
        type: name.endsWith(".css")
          ? "text/css; charset=utf-8"
          : "application/javascript; charset=utf-8",
        body: fs.readFileSync(`frontend/vendor/${name}`, "utf8"),
      },
    ]),
  ),
  null,
  2,
)};

export default {
  async fetch(request) {
    const url = new URL(request.url);
    if (request.method !== "GET" && request.method !== "HEAD") {
      return new Response("Method Not Allowed", {
        status: 405,
        headers: { Allow: "GET, HEAD" }
      });
    }
    if (url.pathname === "/config.js") {
      return new Response(
        \`window.APP_CONFIG = Object.freeze(\${JSON.stringify(RUNTIME_CONFIG)});\\n\`,
        {
          status: 200,
          headers: {
            "Content-Type": "application/javascript; charset=utf-8",
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff"
          }
        }
      );
    }
    if (VENDOR[url.pathname]) {
      return new Response(VENDOR[url.pathname].body, {
        status: 200,
        headers: {
          "Content-Type": VENDOR[url.pathname].type,
          "Cache-Control": "public, max-age=31536000, immutable",
          "X-Content-Type-Options": "nosniff"
        }
      });
    }
    if (url.pathname !== "/" && url.pathname !== "/index.html") {
      return new Response("Not Found", {
        status: 404,
        headers: { "Content-Type": "text/plain; charset=utf-8" }
      });
    }
    const headers = {
      "Content-Type": "text/html; charset=utf-8",
      "Cache-Control": "public, max-age=0, must-revalidate",
      "X-Content-Type-Options": "nosniff",
      "Referrer-Policy": "no-referrer",
      "Permissions-Policy": "camera=(), microphone=(), geolocation=()"
    };
    return new Response(request.method === "HEAD" ? null : HTML, {
      status: 200,
      headers
    });
  }
};
`;

fs.writeFileSync("worker.js", worker, "utf8");
console.log(
  `built index.html ${Buffer.byteLength(html)} bytes; worker.js ${Buffer.byteLength(worker)} bytes`,
);
