#!/usr/bin/env node
// Login-state capture for the verify-loop QA agent (spec §2.17).
// Opens a headed Chrome window on --url in a fresh context; the user logs in by hand. The context's storageState
// (cookies + localStorage, never passwords) is saved to --out every 2 s and once more at the end. Finishes when the
// page URL matches --until-url, when the user closes the window, or at --timeout-ms. Needs no stdin.
// Usage: node capture-auth.mjs --url URL --out FILE [--until-url REGEX] [--timeout-ms N]
//        [--browser chrome|msedge|chromium] [--executable-path FILE]
// Exit: 0 state saved · 1 runtime error · 2 usage error.

import { createRequire } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const TOOLBOX = dirname(fileURLToPath(import.meta.url));
const SAVE_EVERY_MS = 2000;

function usage(msg) {
  process.stderr.write(`capture-auth: ${msg}\n`);
  process.stderr.write("usage: node capture-auth.mjs --url URL --out FILE [--until-url REGEX] [--timeout-ms N] " +
    "[--browser chrome|msedge|chromium] [--executable-path FILE]\n");
  process.exit(2);
}

function parseArgs(argv) {
  const opts = { url: null, out: null, untilUrl: null, timeoutMs: 600000, browser: "chrome", executablePath: null };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    const next = () => {
      if (i + 1 >= argv.length) usage(`${a} needs a value`);
      return argv[++i];
    };
    if (a === "--url") opts.url = next();
    else if (a === "--out") opts.out = next();
    else if (a === "--until-url") opts.untilUrl = next();
    else if (a === "--timeout-ms") opts.timeoutMs = Number(next());
    else if (a === "--browser") opts.browser = next();
    else if (a === "--executable-path") opts.executablePath = next();
    else usage(`unknown argument ${a}`);
  }
  if (!opts.url) usage("--url is required");
  if (!opts.out) usage("--out is required");
  if (!Number.isFinite(opts.timeoutMs) || opts.timeoutMs <= 0) usage("--timeout-ms must be a positive number");
  if (!["chrome", "msedge", "chromium"].includes(opts.browser)) usage("--browser must be chrome, msedge or chromium");
  return opts;
}

function loadPlaywright() {
  const require = createRequire(join(TOOLBOX, "node_modules", "@playwright", "mcp", "package.json"));
  try {
    return require("playwright-core");
  } catch (e) {
    process.stderr.write(
      `capture-auth: playwright-core not found; run vloop setup --install-browser\n`);
    process.exit(1);
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  let until = null;
  if (opts.untilUrl) {
    try {
      until = new RegExp(opts.untilUrl);
    } catch (e) {
      usage(`invalid --until-url regex: ${e.message}`);
    }
  }
  const { chromium } = loadPlaywright();
  const launch = { headless: false };
  if (opts.executablePath) launch.executablePath = opts.executablePath;
  else if (opts.browser !== "chromium") launch.channel = opts.browser;
  const browser = await chromium.launch(launch);
  const context = await browser.newContext({ ignoreHTTPSErrors: true, viewport: null });
  const page = await context.newPage();

  let closed = false;
  browser.on("disconnected", () => { closed = true; });
  context.on("close", () => { closed = true; });
  page.on("close", () => { if (context.pages().length === 0) closed = true; });

  let saved = false;
  const save = async () => {
    try {
      await context.storageState({ path: opts.out });
      saved = true;
    } catch (e) {
      // context already gone; keep the last good save
    }
  };

  process.stderr.write(`capture-auth: log in in the Chrome window (${opts.url}); ` +
    (until ? `finishes when the URL matches /${opts.untilUrl}/, ` : "") +
    "or close the window when done.\n");
  try {
    await page.goto(opts.url, { waitUntil: "domcontentloaded" });
  } catch (e) {
    process.stderr.write(`capture-auth: navigation warning: ${e.message.split("\n")[0]}\n`);
  }

  const deadline = Date.now() + opts.timeoutMs;
  let reason = "timeout";
  while (true) {
    if (closed) { reason = "window closed"; break; }
    await save();
    if (until && context.pages().some((p) => until.test(p.url()))) {
      await sleep(1000); // let post-login redirects settle their cookies/localStorage
      await save();
      reason = "logged-in URL reached";
      break;
    }
    if (Date.now() >= deadline) break;
    await sleep(SAVE_EVERY_MS);
  }
  if (!closed) {
    await save();
    await browser.close().catch(() => {});
  }
  if (!saved) {
    process.stderr.write("capture-auth: no login state could be saved\n");
    process.exit(1);
  }
  process.stderr.write(`capture-auth: done (${reason})\n`);
  process.exit(0);
}

main().catch((e) => {
  process.stderr.write(`capture-auth: error: ${e && e.message ? e.message.split("\n")[0] : e}\n`);
  process.exit(1);
});
