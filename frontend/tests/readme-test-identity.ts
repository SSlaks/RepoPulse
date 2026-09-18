import { createHash } from "node:crypto";
import type { Page, TestInfo } from "@playwright/test";

export async function setReadmeTestIdentity(page: Page, testInfo: TestInfo): Promise<void> {
  const token = process.env.TRUSTED_PROXY_TOKEN;
  if (!token) throw new Error("Browser E2E requires a test TRUSTED_PROXY_TOKEN");

  const digest = createHash("sha256").update(testInfo.testId).digest("hex");
  const groups = digest.slice(0, 24).match(/.{4}/g);
  if (!groups) throw new Error("Could not derive a browser test IP address");
  await page.setExtraHTTPHeaders({
    "X-RepoPulse-Client-IP": `2001:db8:${groups.join(":")}`,
    "X-RepoPulse-Proxy-Token": token,
  });
}
