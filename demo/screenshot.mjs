// Captures the screenshots in the README.
//
//   npm i playwright && node demo/screenshot.mjs
//
// Needs the stack running on :8000 with a run already parked at the approval
// gate — see the header of demo/capture.sh, which sets that up first.
//
// Uses the system Chrome (`channel: "chrome"`) rather than downloading a
// browser, since this is a one-off authoring tool and not part of CI.

import { chromium } from "playwright";

const BASE = process.env.DESKHAND_URL ?? "http://127.0.0.1:8000";
const OUT = process.env.OUT_DIR ?? "demo";

const browser = await chromium.launch({ channel: "chrome" });
const page = await browser.newPage({
  viewport: { width: 1440, height: 900 },
  deviceScaleFactor: 2, // retina, so the README image is not mushy
});

await page.goto(BASE);

// Sign in as somebody who may approve.
await page.fill("#email", "owner@northwind.test");
await page.fill("#password", "demo-password-123");
await page.click("button[type=submit]");
await page.waitForSelector(".sidebar", { timeout: 15000 });

// The sidebar surfaces anything waiting on a human first. Clicking it opens
// the run that stopped.
await page.waitForSelector(".chip.awaiting_approval", { timeout: 20000 });
await page.click(".ticket:has(.chip.awaiting_approval)");
await page.waitForSelector(".approval", { timeout: 15000 });
await page.waitForTimeout(1200); // let the trajectory finish streaming in

await page.screenshot({ path: `${OUT}/approval-gate.png` });
console.log(`wrote ${OUT}/approval-gate.png`);

// A second shot: the injected-instruction ticket, showing tool output rendered
// as untrusted.
await page.click(".ticket:has-text('NW-4')");
await page.waitForTimeout(800);
const runButton = page.locator("button.primary:has-text('Run the agent')");
if (await runButton.count()) {
  await runButton.click();
  await page.waitForSelector(".step", { timeout: 20000 });
  await page.waitForTimeout(4000);
}
await page.screenshot({ path: `${OUT}/fenced-content.png` });
console.log(`wrote ${OUT}/fenced-content.png`);

await browser.close();
