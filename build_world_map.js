const fs = require("fs");

const geo = JSON.parse(fs.readFileSync("world-countries.geojson", "utf8"));

function project([longitude, latitude]) {
  return [
    Math.max(0, Math.min(1000, ((longitude + 180) / 360) * 1000)),
    Math.max(0, Math.min(500, ((90 - latitude) / 180) * 500)),
  ];
}

function ringPath(ring) {
  let path = "";
  let previousX = null;
  let started = false;
  for (const coordinate of ring) {
    const [x, y] = project(coordinate);
    if (!Number.isFinite(x) || !Number.isFinite(y)) continue;
    const command = !started || Math.abs(x - previousX) > 500 ? "M" : "L";
    path += `${command}${x.toFixed(1)} ${y.toFixed(1)} `;
    previousX = x;
    started = true;
  }
  return started ? `${path}Z ` : "";
}

let output = '<g class="map-countries" fill-rule="evenodd">';
for (const feature of geo.features) {
  const geometry = feature.geometry;
  if (!geometry) continue;
  const polygons =
    geometry.type === "Polygon"
      ? [geometry.coordinates]
      : geometry.type === "MultiPolygon"
        ? geometry.coordinates
        : [];
  let path = "";
  for (const polygon of polygons) {
    for (const ring of polygon) path += ringPath(ring);
  }
  if (!path) continue;
  const shade = (Number(feature.properties.MAPCOLOR7) || 1) % 8;
  const name = String(feature.properties.NAME || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;");
  output += `<path class="map-country shade-${shade}" d="${path.trim()}"><title>${name}</title></path>`;
}
output += "</g>";
fs.writeFileSync("map-countries.svgfrag", output);
console.log(`world map fragment: ${Buffer.byteLength(output)} bytes`);
