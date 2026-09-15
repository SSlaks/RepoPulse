import { expect, test } from "@playwright/test";

test("完整度随发布版本更新，筛选保持口径，历史版本不推算", async ({ page }, testInfo) => {
  let state: "partial" | "complete" | "legacy" = "partial";
  await page.route("**/api/v1/rankings?**", async (route) => {
    const period = Number(new URL(route.request().url()).searchParams.get("period"));
    await route.fulfill({
      json: {
        data: [],
        meta: {
          period_days: period, as_of: "2026-09-15T02:00:00Z", baseline_at: null,
          generated_at: "2026-09-15T08:00:00Z", coverage: 3536,
          total: 0, page: 1, limit: 15, data_mode: "live",
          collection: state === "legacy" ? null : {
            expected: 3533, succeeded: state === "partial" ? 3532 : 3533,
            missing: state === "partial" ? 1 : 0,
            completeness_percent: state === "partial" ? 99.97 : 100,
            is_partial: state === "partial",
          },
        },
      },
      headers: { "Access-Control-Allow-Origin": "*" },
    });
  });
  await page.goto("/?period=7");
  const start = page.getByRole("button", { name: "现在开始" });
  await start.waitFor({ state: "visible", timeout: 2000 }).catch(() => undefined);
  if (await start.isVisible()) await start.click();
  await page.getByRole("button", { name: "14 天", exact: true }).click();
  const completeness = page.locator(".collection-completeness");
  await expect(completeness).toHaveText("已更新 3,532 / 3,533 个仓库，1 个暂不可用");
  await page.getByRole("button", { name: "30 天", exact: true }).click();
  await expect(completeness).toContainText("3,532 / 3,533");
  const box = await completeness.boundingBox();
  expect(box).not.toBeNull();
  expect(box!.x + box!.width).toBeLessThanOrEqual(page.viewportSize()!.width);
  await page.screenshot({ path: testInfo.outputPath("partial-completeness.png"), fullPage: true });
  state = "complete";
  await page.getByRole("button", { name: "1 天", exact: true }).click();
  await expect(completeness).toHaveText("已更新 3,533 / 3,533 个仓库");
  state = "legacy";
  await page.getByRole("button", { name: "7 天", exact: true }).click();
  await expect(completeness).toHaveCount(0);
});
