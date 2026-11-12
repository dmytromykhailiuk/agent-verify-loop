// E2E-B6 helper: log in to the verify-toy app headlessly and save Playwright storageState.
// Usage: node toy_login.mjs --url http://localhost:4317 --out <state>/auth/verify-toy.json
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const toolbox = path.join(here, "..", "..", "qa-toolbox");
const require = createRequire(path.join(toolbox, "node_modules", "@playwright", "mcp", "package.json"));
const { chromium } = require("playwright-core");

const args = Object.fromEntries(
  process.argv.slice(2).reduce((acc, a, i, all) => (a.startsWith("--") ? [...acc, [a.slice(2), all[i + 1]]] : acc), []),
);
const base = args.url || "http://localhost:4317";
const out = args.out;
if (!out) {
  console.error("usage: node toy_login.mjs --url URL --out FILE");
  process.exit(2);
}

const browser = await chromium.launch({ channel: "chrome", headless: true });
const context = await browser.newContext();
const page = await context.newPage();
await page.goto(`${base}/login.html`);
await page.fill("#user", "e2e-user");
await page.fill("#password", "toy-password");
await Promise.all([page.waitForURL(/\/account$/), page.click("#submit")]);
const who = await page.textContent("#who");
fs.mkdirSync(path.dirname(out), { recursive: true, mode: 0o700 });
await context.storageState({ path: out });
fs.chmodSync(out, 0o600);
await browser.close();
console.log(`saved ${out} (${who})`);
