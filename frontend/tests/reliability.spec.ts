import { expect, test, type APIRequestContext, type Page, type Response, type Route, type TestInfo } from "@playwright/test";
import { rankingFixture, snapshotsFixture } from "./reliability-fixtures";

const API = "http://127.0.0.1:18081";

test.beforeEach(async ({ page }, testInfo) => {
  test.skip(!testInfo.project.name.startsWith("reliability-"), "Requires playwright.reliability.config.ts and the isolated programmable API");
  await page.context().addCookies([{ name: "repopulse-methodology-seen", value: "1", url: "http://127.0.0.1:13001" }]);
  // A late response must still be rejected when cancellation is unsupported by the transport.
  await page.addInitScript(() => {
    const original = window.fetch;
    window.fetch = (input, init) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      return /\/api\/v1\/(rankings|repos\/.*\/snapshots)/.test(url)
        ? original(input, { ...init, signal: undefined })
        : original(input, init);
    };
  });
});

function gate() {
  let release: () => void = () => undefined;
  const wait = new Promise<void>((resolve) => { release = resolve; });
  return { wait, release };
}

async function fulfill(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

async function settleResponse(page: Page, response: Promise<Response>) {
  await (await response).finished();
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
}

async function configure(request: APIRequestContext, path: string, options: { status?: number; body?: unknown; disconnect?: boolean; delayMs?: number }) {
  const result = await request.post(`${API}/__control`, { data: { path, ...options } });
  expect(result.ok()).toBeTruthy();
}

function repoName(testInfo: TestInfo, label: string) {
  return `${label}-${testInfo.project.name}`;
}

async function chartPage(page: Page, testInfo: TestInfo, label = "chart") {
  await page.goto(`/repo/fixture/${repoName(testInfo, label)}`);
  await expect(page.locator(".chart-heading strong")).toHaveText("+40");
}

test("榜单新周期加载隐藏旧行，旧 finally 不解除新 loading", async ({ page }) => {
  const older = gate();
  const newer = gate();
  const requests: string[] = [];
  await page.route("**/api/v1/rankings?**", async (route) => {
    const period = new URL(route.request().url()).searchParams.get("period") ?? "7";
    requests.push(period);
    await (period === "14" ? older.wait : newer.wait);
    await fulfill(route, rankingFixture(`period-${period}`, Number(period)));
  });
  try {
    await page.goto("/ranking?period=7");
    await expect(page.getByText("initial-ranking", { exact: true }).first()).toBeVisible();
    await page.getByRole("button", { name: "14 天", exact: true }).click();
    await expect.poll(() => requests).toContain("14");
    await expect(page.getByText("initial-ranking", { exact: true })).toHaveCount(0);
    await page.getByRole("button", { name: "30 天", exact: true }).click();
    await expect.poll(() => requests).toContain("30");
    const oldFinished = page.waitForResponse((response) => response.url().includes("period=14"));
    older.release();
    await settleResponse(page, oldFinished);
    await expect(page.locator(".ranking-panel")).toHaveAttribute("aria-busy", "true");
    await expect(page.getByText("period-14", { exact: true })).toHaveCount(0);
    newer.release();
    await expect(page.getByText("period-30", { exact: true }).first()).toBeVisible();
    await expect(page.locator(".ranking-panel")).toHaveAttribute("aria-busy", "false");
  } finally { older.release(); newer.release(); }
});

test("榜单最后请求成功后，晚到旧失败不覆盖数据或弹提示", async ({ page }) => {
  const older = gate();
  let firstStarted = false;
  await page.route("**/api/v1/rankings?**", async (route) => {
    const period = new URL(route.request().url()).searchParams.get("period");
    if (period === "14") {
      firstStarted = true;
      await older.wait;
      await fulfill(route, { error: { code: "LATE_ERROR", message: "旧请求失败" } }, 503);
    } else await fulfill(route, rankingFixture("latest-ranking", 30));
  });
  try {
    await page.goto("/ranking?period=7");
    await page.getByRole("button", { name: "14 天", exact: true }).click();
    await expect.poll(() => firstStarted).toBeTruthy();
    await page.getByRole("button", { name: "30 天", exact: true }).click();
    await expect(page.getByText("latest-ranking", { exact: true }).first()).toBeVisible();
    const oldFinished = page.waitForResponse((response) => response.url().includes("period=14"));
    older.release();
    await settleResponse(page, oldFinished);
    await expect(page.getByText("latest-ranking", { exact: true }).first()).toBeVisible();
    await expect(page.locator(".error-state")).toHaveCount(0);
  } finally { older.release(); }
});

test("榜单网络失败可重试恢复", async ({ page }) => {
  let failed = true;
  await page.route("**/api/v1/rankings?**", async (route) => {
    if (failed) await route.abort("failed");
    else await fulfill(route, rankingFixture("ranking-recovered", 14));
  });
  await page.goto("/ranking?period=7");
  await page.getByRole("button", { name: "14 天", exact: true }).click();
  await expect(page.getByText("榜单暂时没有响应")).toBeVisible();
  failed = false;
  await page.getByRole("button", { name: "重新加载", exact: true }).click();
  await expect(page.getByText("ranking-recovered", { exact: true }).first()).toBeVisible();
});

test("图表如实显示正、负、零净增长，零点与单点为历史不足", async ({ page }, testInfo) => {
  let stars = [100, 125];
  const colors: string[] = [];
  await page.route("**/api/v1/repos/*/*/snapshots?**", (route) => fulfill(route, snapshotsFixture(stars, new URL(route.request().url()).searchParams.get("range") ?? "90d")));
  await chartPage(page, testInfo, "signed");
  for (const [values, expected, range] of [
    [[100, 125], "+25", "30 天"], [[100, 93], "-7", "1 年"], [[100, 100], "0", "90 天"],
    [[], "历史数据不足", "30 天"], [[100], "历史数据不足", "1 年"],
  ] as const) {
    stars = [...values];
    await page.getByRole("button", { name: range, exact: true }).click();
    await expect(page.locator(".chart-heading strong")).toHaveText(expected);
    await expect(page.locator(".chart-heading strong")).toHaveClass(expected === "+25" ? "positive" : expected === "-7" ? "negative" : "neutral");
    colors.push(await page.locator(".chart-heading strong").evaluate((element) => getComputedStyle(element).color));
    if (expected === "-7") await page.screenshot({ path: testInfo.outputPath("chart-negative.png"), fullPage: true, animations: "disabled" });
  }
  expect(colors[1]).not.toBe(colors[0]);
  expect(colors[2]).not.toBe(colors[0]);
  await page.screenshot({ path: testInfo.outputPath("chart-insufficient.png"), fullPage: true });
  const dimensions = await page.evaluate(() => [document.documentElement.clientWidth, document.documentElement.scrollWidth]);
  expect(dimensions[1]).toBeLessThanOrEqual(dimensions[0]);
});

test("图表旧响应及 finally 不能解除新 loading 或覆盖新范围", async ({ page }, testInfo) => {
  const older = gate();
  const newer = gate();
  const requested: string[] = [];
  await page.route("**/api/v1/repos/*/*/snapshots?**", async (route) => {
    const range = new URL(route.request().url()).searchParams.get("range") ?? "90d";
    requested.push(range);
    await (range === "30d" ? older.wait : newer.wait);
    await fulfill(route, snapshotsFixture(range === "30d" ? [100, 999] : [100, 93], range));
  });
  try {
    await chartPage(page, testInfo, "race");
    await page.getByRole("button", { name: "30 天", exact: true }).click();
    await expect.poll(() => requested).toContain("30d");
    await expect(page.locator(".chart-heading strong")).not.toHaveText("+40");
    await page.getByRole("button", { name: "1 年", exact: true }).click();
    await expect.poll(() => requested).toContain("365d");
    const oldFinished = page.waitForResponse((response) => response.url().includes("range=30d"));
    older.release();
    await settleResponse(page, oldFinished);
    await expect(page.locator(".chart-tool")).toHaveAttribute("aria-busy", "true");
    await expect(page.locator(".chart-heading strong")).not.toHaveText("+899");
    newer.release();
    await expect(page.locator(".chart-heading strong")).toHaveText("-7");
    await expect(page.locator(".chart-tool")).toHaveAttribute("aria-busy", "false");
  } finally { older.release(); newer.release(); }
});

test("图表最后请求成功后忽略晚到旧响应", async ({ page }, testInfo) => {
  const older = gate();
  let firstStarted = false;
  await page.route("**/api/v1/repos/*/*/snapshots?**", async (route) => {
    const range = new URL(route.request().url()).searchParams.get("range") ?? "90d";
    if (range === "30d") { firstStarted = true; await older.wait; }
    await fulfill(route, snapshotsFixture(range === "30d" ? [100, 999] : [100, 93], range));
  });
  try {
    await chartPage(page, testInfo, "latest");
    await page.getByRole("button", { name: "30 天", exact: true }).click();
    await expect.poll(() => firstStarted).toBeTruthy();
    await page.getByRole("button", { name: "1 年", exact: true }).click();
    await expect(page.locator(".chart-heading strong")).toHaveText("-7");
    const oldFinished = page.waitForResponse((response) => response.url().includes("range=30d"));
    older.release();
    await settleResponse(page, oldFinished);
    await expect(page.locator(".chart-heading strong")).toHaveText("-7");
  } finally { older.release(); }
});

test("SSR 快照失败显示错误而非空数据，并能重试", async ({ page, request }, testInfo) => {
  const name = repoName(testInfo, "snapshot-error");
  await configure(request, `/api/v1/repos/fixture/${name}/snapshots`, { status: 503, body: { error: { code: "UNAVAILABLE", message: "快照服务暂不可用" } } });
  await page.route("**/api/v1/repos/*/*/snapshots?**", (route) => fulfill(route, snapshotsFixture([100, 125])));
  await page.goto(`/repo/fixture/${name}`);
  await expect(page.locator(".chart-tool")).toContainText("快照服务暂不可用");
  await expect(page.locator(".chart-heading strong")).not.toHaveText("历史数据不足");
  await page.locator(".chart-tool").getByRole("button", { name: /重/ }).click();
  await expect(page.locator(".chart-heading strong")).toHaveText("+25");
});

test("仓库 HTTP 404 才显示项目不存在", async ({ page, request }, testInfo) => {
  const name = repoName(testInfo, "missing");
  await configure(request, `/api/v1/repos/fixture/${name}`, { status: 404, body: { error: { code: "NOT_FOUND", message: "不存在" } } });
  await page.goto(`/repo/fixture/${name}`);
  await expect(page.getByRole("heading", { name: "没有找到这个项目" })).toBeVisible();
});

for (const failure of ["503", "network", "timeout"] as const) {
  test(`SSR 仓库 ${failure} 进入错误页，重试确实重新请求并恢复`, async ({ page, request }, testInfo) => {
    const name = repoName(testInfo, `repo-${failure}`);
    const path = `/api/v1/repos/fixture/${name}`;
    await configure(request, path, failure === "503"
      ? { status: 503, body: { error: { code: "UNAVAILABLE", message: "暂不可用" } } }
      : failure === "network" ? { disconnect: true } : { delayMs: 15_000 });
    await page.goto(`/repo/fixture/${name}`);
    await expect(page.getByRole("heading", { name: "页面暂时没有响应" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "没有找到这个项目" })).toHaveCount(0);
    const before = await request.get(`${API}/__counts`).then((response) => response.json()) as Record<string, number>;
    await configure(request, path, {});
    await page.getByRole("button", { name: "重新加载", exact: true }).click();
    await expect(page.getByRole("heading", { name, exact: true })).toBeVisible();
    const after = await request.get(`${API}/__counts`).then((response) => response.json()) as Record<string, number>;
    expect(after[path]).toBeGreaterThan(before[path]);
  });
}
