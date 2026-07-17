const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "static", "index.html"), "utf8");
const match = html.match(/<script>\s*([\s\S]*?)\s*<\/script>\s*<\/body>/);

if (!match) {
  throw new Error("Embedded application script was not found");
}

new Function(match[1]);

const requiredMarkers = [
  "processNotificationState",
  "renderNotificationPanel",
  "data-notify-toggle",
  "enableBrowserNotifications",
  "hotspots",
  "group_messages",
];

for (const marker of requiredMarkers) {
  if (!html.includes(marker)) {
    throw new Error(`Notification marker is missing: ${marker}`);
  }
}

console.log("Embedded JavaScript and notification center checks passed");
