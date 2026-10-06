import { expect, test, type APIRequestContext, type Page, type Route } from "@playwright/test";

import { rankingPageFixture } from "./reliability-fixtures";

const API = "http://127.0.0.1:18081";
const FILTER_OPTIONS = {
  languages: [{ value: "TypeScript", label: "TypeScript", count: 12 }],
  topics: [{ value: "testing", label: "testing", count: 8 }],
};

interface ApiRule {
  status?: number;
  body?: unknown;
  disconnect?: boolean;
  delayMs?: number;
  ranking?: { prefix?: string; count?: number; total?: number };
  browser?: ApiRule;
}

test.beforeEach(async ({ page }, testInfo) => {
  test.skip(!testInfo.project.name.startsWith("reliability-"), "Requires the isolated programmable reliability API");
  await page.context().addCookies([{ name: "repopulse-methodology-seen", value: "1", url: "http://127.0.0.1:13001" }]);
});

test.afterEach(async ({ request }, testInfo) => {
  if (!testInfo.project.name.startsWith("reliability-")) return;
  await configure(request, "/api/v1/rankings", {});
});

async function configure(request: APIRequestContext, path: string, rule: ApiRule) {
  const response = await request.post(`${API}/__control`, { data: { path, ...rule } });
  expect(response.ok()).toBeTruthy();
}

async function counts(request: APIRequestContext) {
  return request.get(`${API}/__counts`).then((response) => response.json()) as Promise<Record<string, number>>;
}

async function proxyToFakeApi(page: Page, pathname: string) {
  await page.route((url) => url.pathname === pathname, async (route) => {
    const source = new URL(route.request().url());
    try {
      const response = await route.fetch({
        url: `${API}${source.pathname}${source.search}`,
        headers: { ...route.request().headers(), "x-reliability-browser": "1" },
      });
      await route.fulfill({ response });
    } catch {
      await route.abort("aborted").catch(() => undefined);
    }
  });
}

async function configureFilterFailure(request: APIRequestContext, browser: ApiRule) {
  await configure(request, "/api/v1/filters", {
    status: 503,
    body: { error: { code: "FILTERS_UNAVAILABLE", message: "筛选服务暂时不可用" } },
    browser: { status: 200, ...browser },
  });
}

async function retryFilterOptions(page: Page) {
  await page.getByRole("button", { name: "重试筛选选项" }).click();
  await expect(page.locator(".filter-options-status")).toHaveCount(0);
}

async function expectQuery(page: Page, expected: Record<string, string | null>) {
  await expect.poll(() => page.evaluate(() => location.search)).toBeTruthy();
  const url = new URL(page.url());
  for (const [name, value] of Object.entries(expected)) expect(url.searchParams.get(name)).toBe(value);
}

async function waitForRanking(page: Page, name: string) {
  await expect(page.locator(".ranking-panel")).toHaveAttribute("aria-busy", "false");
  await expect(page.getByText(name, { exact: true }).first()).toBeVisible();
}

async function visibleRepositoryLink(page: Page, index: number) {
  const link = page.locator(".repo-cell a:visible, .mobile-repo:visible").nth(index);
  await link.evaluate((element) => element.scrollIntoView({ block: "center", behavior: "instant" }));
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));
  return link;
}

async function scrollPosition(page: Page) {
  return page.evaluate(() => window.scrollY);
}

async function expectRestoredRanking(page: Page, expectedScrollY: number) {
  await expect(page).toHaveURL(/\/ranking\?/);
  await expectQuery(page, {
    period: "14",
    language: "TypeScript",
    topic: "testing",
    minStars: "1000",
    q: "fixture",
    limit: "25",
    page: "2",
  });
  await expect(page.getByRole("button", { name: "14 天", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByLabel("编程语言")).toHaveValue("TypeScript");
  await expect(page.getByLabel("项目主题")).toHaveValue("testing");
  await expect(page.getByLabel("最低 Star", { exact: true })).toHaveValue("1000");
  await expect(page.getByLabel("每页数量")).toHaveValue("25");
  await expect(page.getByLabel("搜索仓库或简介")).toHaveValue("fixture");
  await expect(page.getByText("第 2 / 2 页")).toBeVisible();
  await expect.poll(async () => Math.abs((await scrollPosition(page)) - expectedScrollY)).toBeLessThanOrEqual(3);
}

test("SSR 筛选失败独立提示，重试超时后可恢复且不刷新榜单", async ({ page, request }, testInfo) => {
  test.setTimeout(32_000);
  await configureFilterFailure(request, { delayMs: 15_000, body: { languages: [], topics: [] } });
  await proxyToFakeApi(page, "/api/v1/filters");
  const browserFilterRequests: string[] = [];
  page.on("request", (browserRequest) => {
    const url = new URL(browserRequest.url());
    if (url.pathname === "/api/v1/filters") browserFilterRequests.push(browserRequest.url());
  });

  await page.goto("/ranking?period=14&language=TypeScript&topic=testing&minStars=1000&q=fixture&limit=25&page=2");
  await expect(page.getByText("筛选选项暂时不可用", { exact: true })).toBeVisible();
  await expect(page.getByText("initial-ranking", { exact: true }).first()).toBeVisible();
  await expect(page.getByLabel("编程语言")).toBeDisabled();
  await expect(page.getByLabel("项目主题")).toBeDisabled();
  await expect(page.getByLabel("编程语言")).toHaveValue("TypeScript");
  await expect(page.getByLabel("项目主题")).toHaveValue("testing");
  await expect(page.getByLabel("最低 Star", { exact: true })).toBeEnabled();
  await expect(page.getByLabel("每页数量")).toBeEnabled();
  await expect(page.getByLabel("搜索仓库或简介")).toBeEnabled();
  await page.screenshot({ path: testInfo.outputPath("filter-error.png"), fullPage: false, animations: "disabled" });

  const before = await counts(request);
  await page.getByRole("button", { name: "重试筛选选项" }).click();
  await expect(page.getByText("正在重试筛选选项…", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "正在重试" })).toBeDisabled();
  await expect(page.locator(".filter-options-status")).toContainText("请求超时", { timeout: 12_000 });
  await expect(page.getByText("initial-ranking", { exact: true }).first()).toBeVisible();
  await expect(page.getByLabel("编程语言")).toHaveValue("TypeScript");

  await configureFilterFailure(request, { delayMs: 350, body: { languages: [], topics: [] } });
  await page.getByRole("button", { name: "重试筛选选项" }).click();
  await expect(page.getByRole("button", { name: "正在重试" })).toBeDisabled();
  await expect(page.locator(".filter-options-status")).toHaveCount(0);
  await expect(page.getByLabel("编程语言")).toBeEnabled();
  await expect(page.getByLabel("项目主题")).toBeEnabled();
  await expect(page.getByLabel("编程语言")).toHaveValue("TypeScript");
  await expect(page.getByText("initial-ranking", { exact: true }).first()).toBeVisible();

  const after = await counts(request);
  expect(after["/api/v1/filters"]).toBeGreaterThan(before["/api/v1/filters"] ?? 0);
  expect(after["/api/v1/rankings"] ?? 0).toBe(before["/api/v1/rankings"] ?? 0);
  expect(browserFilterRequests).toHaveLength(2);
  expect(browserFilterRequests.every((url) => new URL(url).origin === new URL(page.url()).origin)).toBe(true);
});

test("筛选重试在离开榜单时取消", async ({ page, request }) => {
  await configureFilterFailure(request, { delayMs: 15_000, body: FILTER_OPTIONS });
  await proxyToFakeApi(page, "/api/v1/filters");
  await page.addInitScript(() => {
    const nativeFetch = window.fetch;
    window.fetch = (input, init) => {
      const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
      if (new URL(url, location.href).pathname === "/api/v1/filters") {
        init?.signal?.addEventListener("abort", () => sessionStorage.setItem("filters-request-aborted", "1"), { once: true });
      }
      return nativeFetch(input, init);
    };
  });

  await page.goto("/ranking?period=7");
  await page.getByRole("button", { name: "重试筛选选项" }).click();
  await expect(page.getByRole("button", { name: "正在重试" })).toBeDisabled();
  await page.locator(".repo-cell a:visible, .mobile-repo:visible").first().click();
  await expect(page).toHaveURL(/\/repo\/fixture\/initial-ranking/);
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem("filters-request-aborted"))).toBe("1");
  await page.goBack();
  await expect(page).toHaveURL(/\/ranking\?period=7/);
  await expect(page.getByRole("button", { name: "重试筛选选项" })).toBeEnabled();
  await expect(page.getByText("正在重试筛选选项…", { exact: true })).toHaveCount(0);
});

test("活跃筛选可逐项移除并一键清空，周期和每页数量保持不变", async ({ page, request }, testInfo) => {
  await configureFilterFailure(request, { body: FILTER_OPTIONS });
  await configure(request, "/api/v1/rankings", { ranking: { prefix: "chips", count: 8, total: 50 } });
  await proxyToFakeApi(page, "/api/v1/filters");
  await proxyToFakeApi(page, "/api/v1/rankings");
  await page.goto("/ranking?period=14&language=TypeScript&topic=testing&minStars=1000&q=fixture&limit=25&page=2");
  await retryFilterOptions(page);

  const active = page.locator(".active-filters");
  await expect(active).toBeVisible();
  await expect(page.getByRole("button", { name: "移除语言：TypeScript" })).toBeVisible();
  await expect(page.getByRole("button", { name: "移除主题：testing" })).toBeVisible();
  await expect(page.getByRole("button", { name: "移除最低 Star：1,000+ Star" })).toBeVisible();
  await expect(page.getByRole("button", { name: "移除搜索：fixture" })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("active-filters.png"), fullPage: false, animations: "disabled" });

  await page.getByRole("button", { name: "移除语言：TypeScript" }).click();
  await waitForRanking(page, "chips-p14-1-1");
  await expectQuery(page, { period: "14", language: null, topic: "testing", minStars: "1000", q: "fixture", limit: "25", page: null });
  await expect(page.getByLabel("编程语言")).toHaveValue("");

  await page.getByRole("button", { name: "移除搜索：fixture" }).click();
  await waitForRanking(page, "chips-p14-1-1");
  await expect(page.getByLabel("搜索仓库或简介")).toHaveValue("");
  await expectQuery(page, { q: null, page: null });

  await page.getByRole("button", { name: "清空筛选" }).click();
  await waitForRanking(page, "chips-p14-1-1");
  await expect(active).toHaveCount(0);
  await expect(page.getByLabel("项目主题")).toHaveValue("");
  await expect(page.getByLabel("最低 Star", { exact: true })).toHaveValue("0");
  await expect(page.getByLabel("搜索仓库或简介")).toHaveValue("");
  await expect(page.getByRole("button", { name: "14 天", exact: true })).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByLabel("每页数量")).toHaveValue("25");
  await expectQuery(page, { period: "14", language: null, topic: null, minStars: null, q: null, limit: "25", page: null });
});

test("查询期间隐藏旧榜单并以原结果高度显示骨架", async ({ page, request }, testInfo) => {
  const initial = rankingPageFixture("before-loading", { count: 8, total: 24 });
  await configureFilterFailure(request, { body: FILTER_OPTIONS });
  await configure(request, "/api/v1/rankings", { body: initial });
  await proxyToFakeApi(page, "/api/v1/filters");

  let release: () => void = () => undefined;
  let started = false;
  const blocked = new Promise<void>((resolve) => { release = resolve; });
  await page.route((url) => url.pathname === "/api/v1/rankings", async (route: Route) => {
    started = true;
    await blocked;
    await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(rankingPageFixture("after-loading", { count: 3 })) });
  });

  try {
    await page.goto("/ranking?period=7");
    await expect(page.getByText("before-loading-1", { exact: true }).first()).toBeVisible();
    const results = page.locator(".ranking-results");
    const panel = page.locator(".ranking-panel");
    const beforeHeight = await results.evaluate((element) => element.getBoundingClientRect().height);
    const beforePanelHeight = await panel.evaluate((element) => element.getBoundingClientRect().height);

    await page.getByLabel("最低 Star", { exact: true }).selectOption("1000");
    await expect.poll(() => started).toBe(true);
    await expect(panel).toHaveAttribute("aria-busy", "true");
    await expect(page.getByText("before-loading-1", { exact: true })).toHaveCount(0);
    await expect(page.locator(".ranking-loading")).toBeVisible();
    await expect(page.locator(".ranking-loading").getByRole("status")).toHaveText("正在加载榜单…");
    const loadingHeight = await results.evaluate((element) => element.getBoundingClientRect().height);
    const loadingPanelHeight = await panel.evaluate((element) => element.getBoundingClientRect().height);
    expect(Math.abs(loadingHeight - beforeHeight)).toBeLessThanOrEqual(2);
    expect(Math.abs(loadingPanelHeight - beforePanelHeight)).toBeLessThanOrEqual(2);
    await panel.scrollIntoViewIfNeeded();
    await page.screenshot({ path: testInfo.outputPath("ranking-loading.png"), fullPage: false, animations: "disabled" });

    release();
    await waitForRanking(page, "after-loading-1");
  } finally {
    release();
  }
});

test("详情页返回控件和浏览器后退恢复交互后的筛选、分页与滚动位置", async ({ page, request }) => {
  await configureFilterFailure(request, { body: FILTER_OPTIONS });
  await configure(request, "/api/v1/rankings", { ranking: { prefix: "navigation", count: 15, total: 50 } });
  await proxyToFakeApi(page, "/api/v1/filters");
  await proxyToFakeApi(page, "/api/v1/rankings");
  await page.goto("/ranking?period=7&language=TypeScript&topic=testing&minStars=1000&q=fixture&limit=25");
  await retryFilterOptions(page);

  await page.getByRole("button", { name: "14 天", exact: true }).click();
  await waitForRanking(page, "navigation-p14-1-1");
  await page.getByRole("button", { name: "下一页" }).click();
  await waitForRanking(page, "navigation-p14-2-1");
  await expect(page.getByText("第 2 / 2 页")).toBeVisible();

  const returnLink = await visibleRepositoryLink(page, 7);
  const returnScrollY = await scrollPosition(page);
  expect(returnScrollY).toBeGreaterThan(0);
  await returnLink.click();
  await expect(page).toHaveURL(/\/repo\/fixture\/navigation-p14-2-8/);
  await page.getByRole("link", { name: "返回增长榜" }).click();
  await expectRestoredRanking(page, returnScrollY);

  const historyLink = await visibleRepositoryLink(page, 10);
  const historyScrollY = await scrollPosition(page);
  expect(historyScrollY).toBeGreaterThan(0);
  await historyLink.click();
  await expect(page).toHaveURL(/\/repo\/fixture\/navigation-p14-2-11/);
  await page.goBack();
  await expectRestoredRanking(page, historyScrollY);
});
