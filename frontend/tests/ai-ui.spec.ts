import { expect, type Page, test } from "@playwright/test";
import { AI_STORAGE_KEY } from "../src/lib/ai/storage";

const saved = { selected: "deepseek", providers: { deepseek: { provider: "deepseek", model: "deepseek-flash", apiKey: "sk-ui-fixture" } } };
async function configure(page: Page) {
  await page.addInitScript(({ key, value }) => { localStorage.setItem(key, JSON.stringify(value)); }, { key: AI_STORAGE_KEY, value: saved });
}

test("settings save, switch, reload and delete with bundled provider avatars", async ({ page }, testInfo) => {
  await page.goto("/settings/ai");
  await expect(page.getByRole("heading", { name: "大模型设置" })).toBeVisible();
  await page.getByLabel("API Key", { exact: true }).fill("sk-ui-fixture");
  await page.getByRole("button", { name: "显示密钥" }).click();
  await expect(page.getByLabel("API Key", { exact: true })).toHaveAttribute("type", "text");
  await page.getByLabel("选择模型").selectOption("deepseek-v4-pro");
  await page.getByRole("button", { name: "保存配置" }).click();
  await expect(page.getByRole("status")).toContainText("已保存");
  await page.reload();
  await expect(page.getByLabel("选择模型")).toHaveValue("deepseek-v4-pro");
  await expect(page.getByLabel("API Key", { exact: true })).toHaveAttribute("type", "password");
  await page.getByRole("button", { name: /OpenAI/ }).click();
  await expect(page.getByLabel("选择模型")).toHaveValue("gpt-4.1-mini");
  await expect(page.getByLabel("API Key", { exact: true })).toHaveValue("");
  await page.getByRole("button", { name: /DeepSeek/ }).click();
  await expect(page.getByLabel("API Key", { exact: true })).toHaveValue("sk-ui-fixture");
  const images = await page.locator('.ai-provider-logo img').evaluateAll((nodes) => nodes.every((node) => node instanceof HTMLImageElement && node.complete && node.naturalWidth > 0));
  expect(images).toBe(true);
  await expect(page.locator(".ai-provider-config input")).toHaveCount(1);
  await page.screenshot({ path: testInfo.outputPath("settings-light.png"), fullPage: true, animations: "disabled" });
  await page.getByRole("button", { name: "切换深色/浅色模式" }).click();
  await expect(page.locator("body")).toHaveCSS("background-color", "rgb(12, 17, 28)");
  await page.screenshot({ path: testInfo.outputPath("settings-dark.png"), fullPage: true, animations: "disabled" });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.getByRole("button", { name: "删除当前厂商配置" }).click();
  await page.reload();
  await expect(page.getByLabel("API Key", { exact: true })).toHaveValue("");
});

test("connection testing makes a request, shows vendor failure and success", async ({ page }) => {
  await page.goto("/settings/ai");
  await page.getByLabel("API Key", { exact: true }).fill("sk-ui-fixture");
  await page.route("**/api/ai/test", (route) => route.fulfill({ status: 401, json: { error: { code: "INVALID_KEY", message: "API Key 无效" } } }));
  await page.getByRole("button", { name: "测试连接" }).click();
  await expect(page.getByRole("main").getByRole("alert")).toContainText("API Key 无效");
  await page.unroute("**/api/ai/test");
  await page.route("**/api/ai/test", (route) => route.fulfill({ json: { ok: true } }));
  await page.getByRole("button", { name: "测试连接" }).click();
  await expect(page.getByRole("status")).toContainText("连接成功");
});

test("storage failure is visible and never claims configuration was saved", async ({ page }) => {
  await page.addInitScript(() => { Storage.prototype.setItem = () => { throw new DOMException("blocked", "SecurityError"); }; });
  await page.goto("/settings/ai");
  await page.getByLabel("API Key", { exact: true }).fill("sk-ui-fixture");
  await page.getByRole("button", { name: "保存配置" }).click();
  await expect(page.getByRole("main").getByRole("alert")).toContainText("无法保存配置");
  await expect(page.getByRole("status")).toHaveCount(0);
});

test("README guides unconfigured user to settings and returns without an automatic call", async ({ page }) => {
  let calls = 0;
  await page.route("**/api/ai/readme", (route) => { calls++; return route.abort(); });
  await page.goto("/repo/fastapi/fastapi");
  await page.getByRole("button", { name: "总结翻译" }).click();
  await page.getByRole("link", { name: "前往设置" }).click();
  await page.getByLabel("API Key", { exact: true }).fill("sk-ui-fixture");
  await page.getByRole("button", { name: "保存配置" }).click();
  await page.getByRole("link", { name: "返回项目 README" }).click();
  await expect(page.getByRole("button", { name: "总结翻译" })).toBeVisible();
  expect(calls).toBe(0);
});

test("README displays summary above Chinese translation, original toggle and retry", async ({ page }, testInfo) => {
  await configure(page);
  let calls = 0;
  await page.route("**/api/ai/readme", async (route) => {
    calls++;
    expect(route.request().postDataJSON().markdown).toBeTruthy();
    const events = [{ type: "progress", completed: 0, total: 2, message: "正在翻译" }, { type: "result", result: { summary: "FastAPI 是一个 Python Web 框架。", translation: "# FastAPI 中文说明\n\n支持类型注解与自动文档。\n\n```python\nprint('hello')\n```" } }];
    await route.fulfill({ contentType: "application/x-ndjson", body: events.map((event) => JSON.stringify(event)).join("\n") + "\n" });
  });
  await page.goto("/repo/fastapi/fastapi");
  const original = await page.locator(".readme-content").textContent();
  await page.getByRole("button", { name: "总结翻译" }).click();
  await expect(page.getByRole("region", { name: "AI 摘要" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "FastAPI 中文说明" })).toBeVisible();
  const summary = await page.locator(".ai-summary").boundingBox();
  const translated = await page.getByRole("heading", { name: "FastAPI 中文说明" }).boundingBox();
  expect(summary && translated && summary.y < translated.y).toBeTruthy();
  await page.locator(".readme-section").screenshot({ path: testInfo.outputPath("readme-translated.png") });
  await page.getByRole("button", { name: "原文", exact: true }).click();
  await expect(page.locator(".readme-content")).toHaveText(original ?? "");
  await page.getByRole("button", { name: "中文译文" }).click();
  expect(calls).toBe(1);
  await page.unroute("**/api/ai/readme");
  await page.route("**/api/ai/readme", (route) => route.fulfill({ status: 429, json: { error: { message: "调用频率受限" } } }));
  await page.getByRole("button", { name: "重新生成" }).click();
  await expect(page.getByRole("main").getByRole("alert")).toContainText("调用频率受限");
  await expect(page.getByRole("heading", { name: "FastAPI 中文说明" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("README generation can be cancelled without duplicate requests", async ({ page }) => {
  await configure(page);
  let release: (() => void) | undefined;
  await page.route("**/api/ai/readme", async (route) => {
    await new Promise<void>((resolve) => { release = resolve; });
    await route.abort().catch(() => undefined);
  });
  await page.goto("/repo/fastapi/fastapi");
  await page.getByRole("button", { name: "总结翻译" }).click();
  await expect(page.getByRole("button", { name: "生成中" })).toBeDisabled();
  await expect.poll(() => Boolean(release)).toBe(true);
  await page.getByRole("button", { name: "取消", exact: true }).click();
  release?.();
  await expect(page.getByRole("main").getByRole("alert")).toContainText("已取消生成");
  await expect(page.getByRole("button", { name: "总结翻译" })).toBeEnabled();
});

test("discovery groups unknown models, keeps selection, and invalidates connection status", async ({ page }) => {
  await page.goto("/settings/ai");
  await page.getByRole("button", { name: /OpenAI/ }).click();
  await page.getByLabel("API Key", { exact: true }).fill("sk-fixture");
  await page.route("**/api/ai/models", (route) => route.fulfill({ json: { source: "vendor", queriedAt: new Date().toISOString(), models: [{ id: "gpt-4.1-mini", name: "GPT-4.1 mini", supported: true }, { id: "future-model", name: "Future", supported: false }] } }));
  await page.getByRole("button", { name: "获取模型列表", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("返回 2 个模型");
  await expect(page.getByLabel("选择模型")).toHaveValue("gpt-4.1-mini");
  await expect(page.locator('option[value="future-model"]')).toBeDisabled();
  await expect(page.locator('option[value="gpt-4.1"]')).toContainText("本次未返回");
  await page.route("**/api/ai/test", (route) => route.fulfill({ json: { ok: true } }));
  await page.getByRole("button", { name: "测试连接", exact: true }).click();
  await expect(page.getByText(/连接成功，测试通过/)).toBeVisible();
  await page.getByLabel("选择模型").selectOption("gpt-4.1");
  await expect(page.getByText(/连接成功，测试通过/)).toHaveCount(0);
  await page.getByLabel("API Key", { exact: true }).fill("sk-other");
  await expect(page.locator('option[value="future-model"]')).toHaveCount(0);
  await page.unroute("**/api/ai/models");
  await page.route("**/api/ai/models", (route) => route.fulfill({ status: 401, json: { error: { message: "API Key 无效" } } }));
  await page.getByRole("button", { name: "获取模型列表", exact: true }).click();
  await expect(page.getByRole("main").getByRole("alert")).toContainText("已保留推荐预设");
  await expect(page.getByLabel("选择模型")).toHaveValue("gpt-4.1");
});

test("legacy models survive reload and Qwen clearly uses mainland presets", async ({ page }) => {
  await page.addInitScript(({ key }) => localStorage.setItem(key, JSON.stringify({ selected: "openai", providers: { openai: { provider: "openai", model: "retired-model", apiKey: "sk-legacy" }, deepseek: { provider: "deepseek", model: "deepseek-flash", apiKey: "sk-other" } } })), { key: AI_STORAGE_KEY });
  await page.goto("/settings/ai");
  await expect(page.getByLabel("选择模型")).toHaveValue("retired-model");
  await expect(page.getByText("已保存的模型需重新选择；密钥和其他厂商配置仍保留。")).toBeVisible();
  await expect(page.getByLabel("API Key", { exact: true })).toHaveValue("sk-legacy");
  await page.getByRole("button", { name: /DeepSeek/ }).click();
  await expect(page.getByLabel("API Key", { exact: true })).toHaveValue("sk-other");
  await page.getByRole("button", { name: /通义千问/ }).click();
  await expect(page.getByText(/来源：官方文档维护的中国内地预设/)).toBeVisible();
  await expect(page.getByRole("button", { name: "获取模型列表", exact: true })).toHaveCount(0);
});

test("switching providers discards a pending discovery response", async ({ page }) => {
  let release: (() => void) | undefined;
  await page.route("**/api/ai/models", async (route) => {
    await new Promise<void>((resolve) => { release = resolve; });
    await route.fulfill({ json: { source: "vendor", queriedAt: new Date().toISOString(), models: [{ id: "stale-model", name: "Stale", supported: false }] } }).catch(() => undefined);
  });
  await page.goto("/settings/ai");
  await page.getByLabel("API Key", { exact: true }).fill("sk-fixture");
  await page.getByRole("button", { name: "获取模型列表", exact: true }).click();
  await expect.poll(() => Boolean(release)).toBe(true);
  await page.getByRole("button", { name: /OpenAI/ }).click();
  release?.();
  await expect(page.getByLabel("选择模型")).toHaveValue("gpt-4.1-mini");
  await expect(page.locator('option[value="stale-model"]')).toHaveCount(0);
  await expect(page.getByRole("button", { name: "获取模型列表", exact: true })).toBeEnabled();
});

test("empty discovery and explicit cancellation retain presets", async ({ page }) => {
  await page.goto("/settings/ai");
  await page.getByLabel("API Key", { exact: true }).fill("sk-fixture");
  await page.route("**/api/ai/models", (route) => route.fulfill({ json: { source: "vendor", queriedAt: new Date().toISOString(), models: [] } }));
  await page.getByRole("button", { name: "获取模型列表", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("未返回模型");
  await expect(page.getByLabel("选择模型")).toHaveValue("deepseek-flash");
  await page.unroute("**/api/ai/models");
  let release: (() => void) | undefined;
  await page.route("**/api/ai/models", async (route) => {
    await new Promise<void>((resolve) => { release = resolve; });
    await route.abort().catch(() => undefined);
  });
  await page.getByRole("button", { name: "获取模型列表", exact: true }).click();
  await expect.poll(() => Boolean(release)).toBe(true);
  await page.getByRole("button", { name: "取消查询" }).click();
  release?.();
  await expect(page.getByRole("main").getByRole("alert")).toContainText("已取消模型查询");
  await expect(page.getByLabel("选择模型")).toHaveValue("deepseek-flash");
});
