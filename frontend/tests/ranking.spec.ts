import { expect, type Page, test } from "@playwright/test";

const ONE_PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=",
  "base64",
);

function rankingResponse(
  repositories: Array<{ owner: string; name: string; ownerGithubId: number | null }>,
  options: { page?: number; limit?: number; total?: number } = {},
) {
  const page = options.page ?? 1;
  const limit = options.limit ?? 15;
  return {
    data: repositories.map((repository, index) => ({
      rank: index + 1,
      previous_rank: index + 1,
      full_name: `${repository.owner}/${repository.name}`,
      owner: repository.owner,
      owner_github_id: repository.ownerGithubId,
      name: repository.name,
      description: "用于头像加载测试的项目",
      language: "TypeScript",
      topics: ["testing"],
      total_stars: 1_000 - index,
      star_delta: 10 - index,
      growth_rate: 0.01,
      baseline_available: true,
      last_updated_at: "2026-09-12T00:00:00Z",
      github_url: `https://github.com/${repository.owner}/${repository.name}`,
    })),
    meta: {
      period_days: 14,
      as_of: "2026-09-12T00:00:00Z",
      baseline_at: "2026-08-29T00:00:00Z",
      generated_at: "2026-09-12T00:00:00Z",
      coverage: repositories.length,
      total: options.total ?? repositories.length,
      page,
      limit,
      data_mode: "live",
    },
  };
}

async function loadMockRanking(
  page: Page,
  repositories: Array<{ owner: string; name: string; ownerGithubId: number | null }>,
) {
  await page.route("**/api/v1/rankings?**", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "Access-Control-Allow-Origin": "*" },
      body: JSON.stringify(rankingResponse(repositories)),
    });
  });
  await page.goto("/?period=7");
  await enterRanking(page);
  await page.getByRole("button", { name: "14 天" }).click();
  await expect(page.getByRole("heading", { name: "14 天增长排行" })).toBeVisible();
}

async function enterRanking(page: Page) {
  const startButton = page.getByRole("button", { name: "现在开始" });
  await startButton.waitFor({ state: "visible", timeout: 2_000 }).catch(() => undefined);
  if (await startButton.isVisible()) await startButton.click();
}

test("首次进入先介绍数据口径，确认后记住选择", async ({ page }, testInfo) => {
  await page.goto("/?period=7");

  const dialog = page.getByRole("dialog", { name: "先了解数据，再发现增长。" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText("净增长 = 截止 Star − 起点 Star")).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("methodology-onboarding.png"),
    fullPage: false,
    animations: "disabled",
    caret: "initial",
  });
  await dialog.getByRole("button", { name: "现在开始" }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByRole("heading", { name: "开源项目增长榜" })).toBeVisible();

  await page.reload();
  await expect(dialog).toBeHidden();
});

test("小灯开关可切换并记住深浅主题", async ({ page }, testInfo) => {
  await page.addInitScript(() => {
    if (sessionStorage.getItem("theme-test-initialized")) return;
    localStorage.setItem("repopulse-theme", "light");
    sessionStorage.setItem("theme-test-initialized", "1");
  });
  await page.goto("/?period=7");
  await enterRanking(page);

  const themeToggle = page.getByRole("button", { name: "切换深色/浅色模式" });
  const initialTheme = await page.locator("html").getAttribute("data-theme");
  await themeToggle.click();
  await expect(page.locator("html")).toHaveAttribute(
    "data-theme",
    initialTheme === "dark" ? "light" : "dark",
  );
  await page.screenshot({
    path: testInfo.outputPath("ranking-dark.png"),
    fullPage: true,
    animations: "disabled",
    caret: "initial",
  });

  const selectedTheme = await page.locator("html").getAttribute("data-theme");
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", selectedTheme ?? "light");
});

test("榜单支持周期切换、筛选和详情跳转", async ({ page }, testInfo) => {
  await page.goto("/?period=7");
  await enterRanking(page);

  await expect(page.getByRole("heading", { name: "开源项目增长榜" })).toBeVisible();
  await expect(page.getByRole("button", { name: "7 天" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect.poll(() => page.locator(".ranking-table tbody tr").count()).toBeGreaterThan(0);
  await expect(page.getByText(/\d{4}\/\d{2}\/\d{2} \d{2}:\d{2}/)).toBeVisible();
  await expect(page.locator(".repo-cell a").first()).not.toContainText("/");

  await page.screenshot({
    path: testInfo.outputPath("ranking-desktop.png"),
    fullPage: true,
    caret: "initial",
  });

  await page.getByRole("button", { name: "14 天" }).click();
  await expect(page).toHaveURL(/period=14/);
  await expect(page.getByRole("heading", { name: "14 天增长排行" })).toBeVisible();

  await page.getByLabel("编程语言").selectOption("Python");
  await expect(page).toHaveURL(/language=Python/);
  await expect.poll(() => page.locator(".ranking-table tbody tr").count()).toBeGreaterThan(0);

  await page.getByRole("button", { name: "1 天" }).click();
  await expect(page).toHaveURL(/period=1/);
  await expect(page.getByRole("heading", { name: "1 天增长排行" })).toBeVisible();

  await page.getByLabel("每页数量").selectOption("25");
  await expect(page).toHaveURL(/limit=25/);
  await expect.poll(() => page.locator(".ranking-table tbody tr").count()).toBeLessThanOrEqual(25);

  await page.locator(".repo-cell a:visible, .mobile-repo:visible").first().click();
  await expect(page).toHaveURL(/\/repo\//);
  await expect(page.getByText("Star 趋势")).toBeVisible();
  await expect(page.locator(".recharts-responsive-container svg")).toBeVisible();
});

test("分页支持指定页跳转并拦截非法页码", async ({ page }) => {
  const requests: string[] = [];
  await page.route("**/api/v1/rankings?**", async (route) => {
    const url = new URL(route.request().url());
    const requestedPage = Number(url.searchParams.get("page") ?? "1");
    requests.push(url.search);
    const repositories = Array.from({ length: 15 }, (_, index) => ({
      owner: "jump-owner",
      name: `page-${requestedPage}-repo-${index + 1}`,
      ownerGithubId: null,
    }));
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "Access-Control-Allow-Origin": "*" },
      body: JSON.stringify(rankingResponse(repositories, { page: requestedPage, total: 45 })),
    });
  });

  await page.goto("/?period=7");
  await enterRanking(page);
  await page.getByRole("button", { name: "14 天" }).click();
  await expect(page.getByText("第 1 / 3 页")).toBeVisible();

  const jumpInput = page.getByRole("spinbutton", { name: "页码" });
  await jumpInput.fill("2");
  await jumpInput.press("Enter");
  await expect(page).toHaveURL(/page=2/);
  await expect(page.getByText("第 2 / 3 页")).toBeVisible();
  await expect(page.getByText("page-2-repo-1", { exact: true }).first()).toBeVisible();
  expect(requests.some((query) => query.includes("page=2"))).toBe(true);

  const requestCount = requests.length;
  for (const invalidPage of ["0", "4", "1.5", ""]) {
    await jumpInput.fill(invalidPage);
    await page.getByRole("button", { name: "跳转", exact: true }).click();
    await expect(page.locator(".pagination-error")).toHaveText("请输入 1 到 3 之间的页码");
    expect(requests.length).toBe(requestCount);
    await expect(page).toHaveURL(/page=2/);
  }
});

test("移动端榜单无横向溢出", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/?period=30");
  await enterRanking(page);

  await expect(page.locator(".mobile-ranking-list")).toBeVisible();
  await expect(page.locator(".desktop-table-wrap")).toBeHidden();
  await expect(page.locator(".mobile-main > strong").first()).not.toContainText("/");
  const dimensions = await page.evaluate(() => ({
    viewport: document.documentElement.clientWidth,
    content: document.documentElement.scrollWidth,
  }));
  expect(dimensions.content).toBeLessThanOrEqual(dimensions.viewport);

  await page.screenshot({
    path: testInfo.outputPath("ranking-mobile.png"),
    fullPage: true,
    caret: "initial",
  });
});

test("详情页显示本地化简介和仓库名", async ({ page }) => {
  await page.goto("/repo/fastapi/fastapi");

  await expect(page.getByRole("heading", { name: "fastapi", exact: true })).toBeVisible();
  await expect(page.locator(".repo-title span")).toHaveText("fastapi");
  await expect(page.locator(".repo-description")).toContainText("现代、高性能且易于学习");
});

test("详情页返回时恢复榜单页码和筛选条件", async ({ page }) => {
  const returnTo = "/?period=14&language=Python&limit=25&page=2";
  await page.goto(`/repo/fastapi/fastapi?returnTo=${encodeURIComponent(returnTo)}`);

  await page.getByRole("link", { name: "返回增长榜" }).click();
  await expect(page).toHaveURL(/period=14.*language=Python.*limit=25.*page=2/);
  await expect(page.getByRole("heading", { name: "14 天增长排行" })).toBeVisible();
});

test("详情页显示 README 内容和来源", async ({ page }) => {
  await page.goto("/repo/fastapi/fastapi");

  await expect(page.getByRole("heading", { name: "项目文档", exact: true })).toBeVisible();
  await expect(page.locator(".readme-content")).not.toBeEmpty();
  const sourceLink = page.getByRole("link", { name: "在 GitHub 查看 README.md" });
  await expect(sourceLink).toBeVisible();
  await expect(sourceLink).toHaveAttribute(
    "href",
    "https://github.com/fastapi/fastapi/blob/master/README.md",
  );
  await expect(sourceLink).toHaveAttribute("target", "_blank");
  await expect(sourceLink).toHaveAttribute("rel", "noreferrer");
});

test("头像首次未缓存时会退避重试并自动恢复", async ({ page }) => {
  await page.clock.install();
  let avatarRequests = 0;
  await page.route("**/api/v1/avatars/987654321*", async (route) => {
    avatarRequests += 1;
    if (avatarRequests === 1) {
      await route.fulfill({ status: 404, headers: { "Access-Control-Allow-Origin": "*" } });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "image/png",
      headers: { "Access-Control-Allow-Origin": "*" },
      body: ONE_PIXEL_PNG,
    });
  });
  await loadMockRanking(page, [
    { owner: "avatar-owner", name: "retry-success", ownerGithubId: 987654321 },
  ]);

  const avatar = page.locator('img[alt="avatar-owner 头像"]:visible').first();
  await avatar.scrollIntoViewIfNeeded();
  await expect(avatar).toHaveAttribute("src", "/avatar-fallback.svg");
  await page.clock.fastForward(1_000);

  await expect.poll(() => avatarRequests).toBe(2);
  await expect(avatar).toHaveAttribute("src", /\/avatars\/987654321\?retry=1$/);
});

test("头像重试耗尽或缺少 owner ID 时保持默认图", async ({ page }) => {
  await page.clock.install();
  let avatarRequests = 0;
  await page.route("**/api/v1/avatars/123456789*", async (route) => {
    avatarRequests += 1;
    await route.fulfill({ status: 404, headers: { "Access-Control-Allow-Origin": "*" } });
  });
  await loadMockRanking(page, [
    { owner: "missing-owner", name: "retry-failure", ownerGithubId: 123456789 },
    { owner: "unknown-owner", name: "no-owner-id", ownerGithubId: null },
  ]);

  const missingAvatar = page.locator('img[alt="missing-owner 头像"]:visible').first();
  const unknownAvatar = page.locator('img[alt="unknown-owner 头像"]:visible').first();
  await missingAvatar.scrollIntoViewIfNeeded();
  await expect(missingAvatar).toHaveAttribute("src", "/avatar-fallback.svg");
  await expect(unknownAvatar).toHaveAttribute("src", "/avatar-fallback.svg");

  for (const [index, delay] of [1_000, 2_000, 4_000, 8_000].entries()) {
    await page.clock.fastForward(delay);
    await expect.poll(() => avatarRequests).toBe(index + 2);
  }

  await expect.poll(() => avatarRequests).toBe(5);
  await expect(missingAvatar).toHaveAttribute("src", "/avatar-fallback.svg");
  await expect(unknownAvatar).toHaveAttribute("src", "/avatar-fallback.svg");
});
