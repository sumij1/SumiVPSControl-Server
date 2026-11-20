const fs = require("fs");

const map = fs.readFileSync("map-countries.svgfrag", "utf8");
const svg = `<svg viewBox="0 0 1000 500" xmlns="http://www.w3.org/2000/svg">
<defs>
  <radialGradient id="oceanGradient" cx="48%" cy="38%" r="72%">
    <stop offset="0%" stop-color="#153b32"/>
    <stop offset="62%" stop-color="#0b211a"/>
    <stop offset="100%" stop-color="#050d0a"/>
  </radialGradient>
  <filter id="landShadow" x="-10%" y="-10%" width="120%" height="120%">
    <feDropShadow dx="0" dy="3" stdDeviation="3" flood-color="#000" flood-opacity=".55"/>
  </filter>
  <pattern id="mapGrid" width="50" height="50" patternUnits="userSpaceOnUse">
    <path d="M50 0H0V50" fill="none" stroke="#84c8a82b"/>
    <circle r="1" fill="#91d8b93d"/>
  </pattern>
</defs>
<rect width="1000" height="500" fill="url(#oceanGradient)"/>
<rect width="1000" height="500" fill="url(#mapGrid)" opacity=".7"/>
${map}
<g fill="#a8cfb9" opacity=".45" font-size="12" font-weight="800" letter-spacing="3" font-family="Consolas">
  <text x="170" y="130">NORTH AMERICA</text><text x="300" y="315">SOUTH AMERICA</text>
  <text x="505" y="112">EUROPE</text><text x="520" y="255">AFRICA</text>
  <text x="735" y="138">ASIA</text><text x="867" y="332">AUSTRALIA</text>
</g>
<g>
  <g transform="translate(830 279)"><circle r="24" fill="none" stroke="#70f0b2" opacity=".3"/><circle r="13" fill="#70f0b2" stroke="#fff" stroke-width="2"/></g>
  <g transform="translate(758 167)"><circle r="13" fill="#54d9ff" stroke="#fff" stroke-width="2"/><text y="1" text-anchor="middle" dominant-baseline="middle" font-weight="900" fill="#063442">?</text></g>
  <g transform="translate(465 192)"><circle r="13" fill="#89958f" stroke="#fff" stroke-width="2"/></g>
  <g transform="translate(558 189)"><circle r="13" fill="#46d99a" stroke="#fff" stroke-width="2"/><text y="1" text-anchor="middle" dominant-baseline="middle" font-weight="900" fill="#04301d">!</text></g>
</g>
</svg>`;

const css = `
.map-country{stroke:#0a1d16;stroke-width:.65;stroke-linejoin:round;filter:url(#landShadow)}
.shade-0{fill:#39694f}.shade-1{fill:#315d49}.shade-2{fill:#3e7258}.shade-3{fill:#2d5544}
.shade-4{fill:#447a5e}.shade-5{fill:#355f4d}.shade-6{fill:#3c7056}.shade-7{fill:#305a46}
`;
fs.writeFileSync(
  "map-preview.html",
  `<html><body style="margin:0;background:#07120f;padding:30px"><div style="max-width:1200px;border-radius:22px;overflow:hidden;border:1px solid #34594b">${svg}</div><style>${css}</style></body></html>`,
);
console.log("map preview ready");
