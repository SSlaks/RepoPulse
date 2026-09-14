import { expect, type Page, test } from "@playwright/test";
import { AI_STORAGE_KEY } from "../src/lib/ai/storage";
import { README_TRANSLATION_DB_NAME, README_TRANSLATION_STORE } from "../src/lib/ai/readme-storage";

const saved = { selected: "deepseek", providers: { deepseek: { provider: "deepseek", model: "deepseek-flash", apiKey: "sk-ui-fixture" } } };
async function configure(page: Page) {
  await page.addInitScript(({ key, value }) => { localStorage.setItem(key, JSON.stringify(value)); }, { key: AI_STORAGE_KEY, value: saved });
}

async function seedTranslationRecord(page: Page, record: {
  repository: string;
  translation: string;
  sourceFingerprint: string;
  generatedAt: string;
  modelName: string;
  imageBaseUrl: string;
}) {
  await page.evaluate(async ({ databaseName, storeName, value }) => {
    const database = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open(databaseName, 1);
      request.onupgradeneeded = () => { if (!request.result.objectStoreNames.contains(storeName)) request.result.createObjectStore(storeName, { keyPath: "repository" }); };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    await new Promise<void>((resolve, reject) => {
      const transaction = database.transaction(storeName, "readwrite");
      transaction.objectStore(storeName).put(value);
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(transaction.error);
    });
    database.close();
  }, { databaseName: README_TRANSLATION_DB_NAME, storeName: README_TRANSLATION_STORE, value: record });
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

test("README keeps summary independent and translates only after the Chinese tab is clicked", async ({ page }, testInfo) => {
  await configure(page);
  let calls = 0;
  await page.route("**/api/ai/readme", async (route) => {
    calls++;
    const body = route.request().postDataJSON();
    expect(body.markdown).toBeTruthy();
    const result = body.mode === "summary"
      ? { mode: "summary", summary: "FastAPI 是一个 Python Web 框架，用于构建高性能 API。\n\n- 支持类型注解\n- 自动生成文档" }
      : { mode: "translation", translation: "# FastAPI 中文说明\n\n支持类型注解与自动文档。\n\n```python\nprint('hello')\n```" };
    const events = [{ type: "progress", completed: 0, total: body.mode === "summary" ? 1 : 2, message: body.mode === "summary" ? "正在连接模型" : "正在翻译第 1 / 1 段", indeterminate: body.mode === "summary" }, { type: "progress", completed: body.mode === "summary" ? 1 : 2, total: body.mode === "summary" ? 1 : 2, message: body.mode === "summary" ? "项目摘要已生成" : "翻译完成" }, { type: "result", result }];
    await route.fulfill({ contentType: "application/x-ndjson", body: events.map((event) => JSON.stringify(event)).join("\n") + "\n" });
  });
  await page.goto("/repo/fastapi/fastapi");
  const original = await page.locator(".readme-content").textContent();
  await page.getByRole("button", { name: "总结翻译" }).click();
  await expect(page.getByRole("region", { name: "AI 摘要" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "FastAPI 中文说明" })).toHaveCount(0);
  expect(calls).toBe(1);
  await expect(page.getByRole("button", { name: "重新总结" })).toBeVisible();
  await expect(page.locator(".readme-content")).toContainText(original ?? "");
  await page.getByRole("button", { name: "中文译文" }).click();
  await expect(page.getByRole("heading", { name: "FastAPI 中文说明" })).toBeVisible();
  expect(calls).toBe(2);
  const summary = await page.locator(".ai-summary").boundingBox();
  const translated = await page.getByRole("heading", { name: "FastAPI 中文说明" }).boundingBox();
  expect(summary && translated && summary.y < translated.y).toBeTruthy();
  await page.locator(".readme-section").screenshot({ path: testInfo.outputPath("readme-translated.png") });
  await page.getByRole("button", { name: "原文", exact: true }).click();
  await expect(page.getByRole("region", { name: "AI 摘要" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "FastAPI 中文说明" })).toHaveCount(0);
  await page.getByRole("button", { name: "中文译文" }).click();
  expect(calls).toBe(2);
  await page.unroute("**/api/ai/readme");
  await page.route("**/api/ai/readme", (route) => route.fulfill({ status: 429, json: { error: { message: "调用频率受限" } } }));
  await page.getByRole("button", { name: "重新翻译" }).click();
  await expect(page.getByRole("main").getByRole("alert")).toContainText("调用频率受限");
  await expect(page.getByRole("heading", { name: "FastAPI 中文说明" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test("README translation records survive leaving and re-entering a repository", async ({ page }) => {
  await configure(page);
  let calls = 0;
  await page.route("**/api/ai/readme", async (route) => {
    calls++;
    const body = route.request().postDataJSON();
    expect(body.mode).toBe("translation");
    const events = [{ type: "progress", completed: 0, total: 2, message: "正在翻译第 1 / 1 段" }, { type: "progress", completed: 2, total: 2, message: "翻译完成" }, { type: "result", result: { mode: "translation", translation: "# FastAPI 中文说明\n\n这是持久化译文。" } }];
    await route.fulfill({ contentType: "application/x-ndjson", body: events.map((event) => JSON.stringify(event)).join("\n") + "\n" });
  });
  await page.goto("/repo/fastapi/fastapi");
  await page.getByRole("button", { name: "中文译文" }).click();
  await expect(page.getByRole("heading", { name: "FastAPI 中文说明" })).toBeVisible();
  await page.goto("/ranking?period=7");
  await page.goto("/repo/fastapi/fastapi");
  await expect(page.getByRole("button", { name: "中文译文" })).toBeEnabled();
  await expect(page.getByRole("heading", { name: "FastAPI 中文说明" })).toHaveCount(0);
  await page.getByRole("button", { name: "中文译文" }).click();
  await expect(page.getByRole("heading", { name: "FastAPI 中文说明" })).toBeVisible();
  expect(calls).toBe(1);
});

test("README keeps a generated translation in the page when IndexedDB save fails", async ({ page }) => {
  await configure(page);
  await page.addInitScript(() => {
    IDBObjectStore.prototype.put = () => { throw new DOMException("write blocked", "QuotaExceededError"); };
  });
  let calls = 0;
  await page.route("**/api/ai/readme", async (route) => {
    calls++;
    const events = [{ type: "progress", completed: 0, total: 2, message: "正在翻译第 1 / 1 段" }, { type: "progress", completed: 2, total: 2, message: "翻译完成" }, { type: "result", result: { mode: "translation", translation: "# FastAPI 中文说明\n\n仅当前页面译文。" } }];
    await route.fulfill({ contentType: "application/x-ndjson", body: events.map((event) => JSON.stringify(event)).join("\n") + "\n" });
  });
  await page.goto("/repo/fastapi/fastapi");
  await page.getByRole("button", { name: "中文译文" }).click();
  await expect(page.getByRole("heading", { name: "FastAPI 中文说明" })).toBeVisible();
  await expect(page.locator(".ai-readme-storage-warning")).toContainText("保存失败");
  await expect(page.getByText("已保存译文", { exact: false })).toHaveCount(0);
  await page.getByRole("button", { name: "原文", exact: true }).click();
  await page.getByRole("button", { name: "中文译文" }).click();
  await expect(page.locator(".ai-readme-storage-warning")).toContainText("保存失败");
  expect(calls).toBe(1);
});

test("README preserves an old record and warns when the source fingerprint changes", async ({ page }) => {
  await page.goto("/repo/fastapi/fastapi");
  await page.evaluate(async ({ databaseName, storeName }) => {
    const database = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open(databaseName, 1);
      request.onupgradeneeded = () => { if (!request.result.objectStoreNames.contains(storeName)) request.result.createObjectStore(storeName, { keyPath: "repository" }); };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    await new Promise<void>((resolve, reject) => {
      const transaction = database.transaction(storeName, "readwrite");
      transaction.objectStore(storeName).put({ repository: "fastapi/fastapi", translation: "# 旧版译文", sourceFingerprint: "0".repeat(64), generatedAt: "2026-09-14T00:00:00.000Z", modelName: "历史模型", imageBaseUrl: "https://github.com/fastapi/fastapi/blob/master/README.md" });
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(transaction.error);
    });
    database.close();
  }, { databaseName: README_TRANSLATION_DB_NAME, storeName: README_TRANSLATION_STORE });
  await page.reload();
  await expect(page.getByRole("status")).toContainText("原文已更新，可重新翻译");
  await page.getByRole("button", { name: "中文译文" }).click();
  await expect(page.getByRole("heading", { name: "旧版译文" })).toBeVisible();
});

test("README keeps the repository record across model changes and a cancelled retranslation", async ({ page }) => {
  await configure(page);
  await page.goto("/repo/fastapi/fastapi");
  await seedTranslationRecord(page, {
    repository: "another/project",
    translation: "# 其他仓库译文",
    sourceFingerprint: "1".repeat(64),
    generatedAt: "2026-09-14T00:00:00.000Z",
    modelName: "其他模型",
    imageBaseUrl: "https://github.com/another/project/blob/main/README.md",
  });
  await seedTranslationRecord(page, {
    repository: "fastapi/fastapi",
    translation: "# 应保留的旧译文",
    sourceFingerprint: "0".repeat(64),
    generatedAt: "2026-09-14T00:00:00.000Z",
    modelName: "历史模型",
    imageBaseUrl: "https://github.com/fastapi/fastapi/blob/master/README.md",
  });
  await page.reload();
  await page.getByRole("button", { name: "中文译文" }).click();
  await expect(page.getByRole("heading", { name: "应保留的旧译文" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "其他仓库译文" })).toHaveCount(0);

  await page.evaluate(({ key }) => {
    localStorage.setItem(key, JSON.stringify({ selected: "openai", providers: { openai: { provider: "openai", model: "gpt-4.1-mini", apiKey: "sk-other-model" } } }));
    window.dispatchEvent(new Event("repopulse-ai-change"));
  }, { key: AI_STORAGE_KEY });
  await expect(page.getByRole("heading", { name: "应保留的旧译文" })).toBeVisible();

  let release: (() => void) | undefined;
  await page.route("**/api/ai/readme", async (route) => {
    await new Promise<void>((resolve) => { release = resolve; });
    await route.fulfill({ contentType: "application/x-ndjson", body: `${JSON.stringify({ type: "result", result: { mode: "translation", translation: "# 不应采用的新译文" } })}\n` }).catch(() => undefined);
  });
  await page.getByRole("button", { name: "重新翻译" }).click();
  await expect.poll(() => Boolean(release)).toBe(true);
  await page.getByRole("button", { name: "原文", exact: true }).click();
  await page.getByRole("button", { name: "中文译文" }).click();
  await expect(page.getByRole("heading", { name: "应保留的旧译文" })).toBeVisible();
  await page.getByRole("button", { name: "取消", exact: true }).click();
  release?.();
  await expect(page.getByRole("main").getByRole("alert")).toContainText("已取消生成");
  await expect(page.getByRole("heading", { name: "不应采用的新译文" })).toHaveCount(0);
  await page.reload();
  await page.getByRole("button", { name: "中文译文" }).click();
  await expect(page.getByRole("heading", { name: "应保留的旧译文" })).toBeVisible();
});

test("README requires an explicit action and can retranslate in-page when IndexedDB stays unavailable", async ({ page }) => {
  await configure(page);
  await page.addInitScript(() => {
    indexedDB.open = () => { throw new DOMException("blocked", "UnknownError"); };
  });
  let calls = 0;
  await page.route("**/api/ai/readme", (route) => {
    calls++;
    return route.fulfill({
      contentType: "application/x-ndjson",
      body: `${JSON.stringify({ type: "result", result: { mode: "translation", translation: `# 仅本页译文 ${calls}` } })}\n`,
    });
  });
  await page.goto("/repo/fastapi/fastapi");
  await expect(page.locator(".ai-readme-storage-error")).toContainText("翻译记录读取失败");
  await page.getByRole("button", { name: "中文译文" }).click();
  expect(calls).toBe(0);
  await expect(page.locator(".ai-error[role=alert]")).toContainText("blocked");
  await page.getByRole("button", { name: "继续翻译（仅本页）" }).click();
  await expect(page.getByRole("heading", { name: "仅本页译文 1" })).toBeVisible();
  await expect(page.locator(".ai-readme-storage-warning")).toContainText("保存失败");
  await page.getByRole("button", { name: "重新翻译" }).click();
  await expect(page.getByRole("heading", { name: "仅本页译文 2" })).toBeVisible();
  await expect(page.locator(".ai-readme-storage-warning")).toContainText("保存失败");
  expect(calls).toBe(2);
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

test("README progress card shows real determinate progress in light and dark themes", async ({ page }, testInfo) => {
  await configure(page);
  await page.addInitScript(() => {
    const originalFetch = window.fetch.bind(window);
    window.fetch = async (input, init) => {
      if (!String(input).endsWith("/api/ai/readme")) return originalFetch(input, init);
      const encoder = new TextEncoder();
      const stream = new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(encoder.encode(`${JSON.stringify({ type: "progress", completed: 2, total: 5, message: "正在翻译第 3 / 4 段" })}\n`));
          (window as typeof window & { finishReadme?: () => void }).finishReadme = () => {
            controller.enqueue(encoder.encode(`${JSON.stringify({ type: "result", result: { mode: "translation", translation: "# 进度测试译文" } })}\n`));
            controller.close();
          };
        },
      });
      return new Response(stream, { headers: { "Content-Type": "application/x-ndjson" } });
    };
  });
  await page.goto("/repo/fastapi/fastapi");
  await page.getByRole("button", { name: "中文译文" }).click();
  await expect(page.locator(".ai-readme-progress-card")).toBeVisible();
  const progressbar = page.getByRole("progressbar", { name: "翻译进度" });
  await expect(progressbar).toHaveAttribute("aria-valuenow", "2");
  await expect(progressbar).toHaveAttribute("aria-valuemax", "5");
  await expect(page.locator(".ai-readme-progress-copy")).toContainText("正在翻译第 3 / 4 段");
  await page.screenshot({ path: testInfo.outputPath(`readme-progress-light-${testInfo.project.name}.png`), fullPage: true, animations: "disabled" });
  await page.getByRole("button", { name: "切换深色/浅色模式" }).click();
  await page.screenshot({ path: testInfo.outputPath(`readme-progress-dark-${testInfo.project.name}.png`), fullPage: true, animations: "disabled" });
  await page.evaluate(() => (window as typeof window & { finishReadme?: () => void }).finishReadme?.());
  await expect(page.getByRole("heading", { name: "进度测试译文" })).toBeVisible();
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
