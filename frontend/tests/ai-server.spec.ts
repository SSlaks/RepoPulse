import { expect, test } from "@playwright/test";
import { AI_PROVIDERS } from "../src/lib/ai/catalog";
import { credentialsSchema, readmeRequestSchema } from "../src/lib/ai/contracts";
import { callModel, parseCompletion, providerRequest } from "../src/lib/ai/server/provider";
import { publicAiError, upstreamError } from "../src/lib/ai/server/errors";
import { fingerprintMarkdown, saveReadmeTranslation } from "../src/lib/ai/readme-storage";
import { splitMarkdown, translateReadme, validateTranslation } from "../src/lib/ai/server/translate";
import { generateReadme, listModels, testConnection } from "../src/lib/ai/server/http";

const credentials = { provider: "deepseek", model: "deepseek-flash", apiKey: "sk-test-secret" };
const originalFetch = globalThis.fetch;
test.afterEach(() => { globalThis.fetch = originalFetch; });
function completion(content: string) { return Response.json({ choices: [{ finish_reason: "stop", message: { content } }] }); }
function request(body: unknown, signal?: AbortSignal) { return new Request("http://localhost/api/ai/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal }); }

test("credentials whitelist rejects custom endpoints, models and oversized documents", () => {
  expect(credentialsSchema.safeParse(credentials).success).toBe(true);
  expect(readmeRequestSchema.parse({ ...credentials, markdown: "README" }).mode).toBe("summary");
  expect(readmeRequestSchema.safeParse({ ...credentials, markdown: "README", mode: "other" }).success).toBe(false);
  for (const invalid of [{ ...credentials, endpoint: "http://localhost" }, { ...credentials, model: "unknown" }, { ...credentials, apiKey: "bad\nkey" }]) expect(credentialsSchema.safeParse(invalid).success).toBe(false);
  expect(readmeRequestSchema.safeParse({ ...credentials, markdown: "a".repeat(100001) }).success).toBe(false);
});

for (const provider of AI_PROVIDERS) {
  test(`${provider.id} uses fixed endpoint and sends credentials only in headers`, async () => {
    let called = false;
    const selected = { ...credentials, provider: provider.id, model: provider.models[0].id };
    globalThis.fetch = async (url, options) => {
      called = true;
      expect(String(url)).toMatch(/^https:\/\//);
      expect(String(url)).not.toContain(credentials.apiKey);
      expect(String(options?.body)).not.toContain(credentials.apiKey);
      expect(JSON.stringify(options?.headers)).toContain(credentials.apiKey);
      expect(options?.redirect).toBe("error");
      expect(options?.cache).toBe("no-store");
      const body = JSON.parse(String(options?.body));
      if (provider.id === "claude") {
        expect(body.system).toBe("system");
        return Response.json({ stop_reason: "end_turn", content: [{ type: "text", text: "OK" }] });
      }
      if (provider.id === "gemini") {
        expect(body.systemInstruction.parts[0].text).toBe("system");
        return Response.json({ candidates: [{ finishReason: "STOP", content: { parts: [{ text: "private reasoning", thought: true }, { text: "OK" }] } }] });
      }
      expect(body.messages[0].content).toBe("system");
      return completion("OK");
    };
    expect(await callModel(selected, "system", "input", new AbortController().signal, 128)).toBe("OK");
    expect(called).toBe(true);
  });
}

test("vendor errors and malformed output never echo credentials", async () => {
  for (const [status, code] of [[401, "INVALID_KEY"], [403, "FORBIDDEN"], [402, "QUOTA"], [429, "RATE_LIMIT"], [500, "UNAVAILABLE"]] as const) {
    globalThis.fetch = async () => Response.json({ error: { message: credentials.apiKey } }, { status });
    const response = await testConnection(request(credentials));
    expect(response.headers.get("cache-control")).toBe("no-store");
    const text = await response.text(); expect(text).toContain(code); expect(text).not.toContain(credentials.apiKey);
  }
  expect(upstreamError(429, { error: { code: "insufficient_quota" } }).code).toBe("QUOTA");
  expect(() => parseCompletion("openai", { choices: [{ finish_reason: "length", message: { content: "partial" } }] })).toThrow();
  expect(() => parseCompletion("claude", { stop_reason: "max_tokens", content: [{ type: "text", text: "partial" }] })).toThrow();
  expect(() => parseCompletion("gemini", { candidates: [{ finishReason: "SAFETY" }] })).toThrow();
  expect(() => parseCompletion("openai", { choices: [{ finish_reason: "stop", message: { content: "" } }] })).toThrow();
});

test("AI request handling never logs credentials or raw vendor errors", async () => {
  const captured: string[] = [];
  const originalLog = console.log;
  const originalWarn = console.warn;
  const originalError = console.error;
  const capture = (...values: unknown[]) => captured.push(values.map(String).join(" "));
  console.log = capture;
  console.warn = capture;
  console.error = capture;
  globalThis.fetch = async () => Response.json(
    { error: { message: `vendor detail contains ${credentials.apiKey}` } },
    { status: 500 },
  );

  try {
    const response = await testConnection(request(credentials));
    expect(response.status).toBe(502);
    expect(await response.text()).not.toContain(credentials.apiKey);
    expect(captured.join("\n")).not.toContain(credentials.apiKey);
    expect(captured.join("\n")).not.toContain("vendor detail");
  } finally {
    console.log = originalLog;
    console.warn = originalWarn;
    console.error = originalError;
  }
});

test("request validation rejects cross-origin and malformed bodies without vendor calls", async () => {
  globalThis.fetch = async () => { throw new Error("must not call vendor"); };
  expect((await testConnection(request({ ...credentials, endpoint: "https://evil.test" }))).status).toBe(400);
  const cross = request(credentials); cross.headers.set("origin", "https://evil.test");
  expect((await testConnection(cross)).status).toBe(403);
  expect((await generateReadme(request({ ...credentials, markdown: "a".repeat(100001) }))).status).toBe(400);
});

test("configured public site origin is accepted behind a container bind address", async () => {
  const previousSiteUrl = process.env.NEXT_PUBLIC_SITE_URL;
  process.env.NEXT_PUBLIC_SITE_URL = "https://repopulse.example.com";
  globalThis.fetch = async () => completion("OK");
  const sameSite = request(credentials);
  sameSite.headers.set("origin", "https://repopulse.example.com");

  try {
    expect((await testConnection(sameSite)).status).toBe(200);
  } finally {
    if (previousSiteUrl === undefined) delete process.env.NEXT_PUBLIC_SITE_URL;
    else process.env.NEXT_PUBLIC_SITE_URL = previousSiteUrl;
  }
});

test("local loopback aliases remain usable for browser same-origin requests", async () => {
  const previousSiteUrl = process.env.NEXT_PUBLIC_SITE_URL;
  process.env.NEXT_PUBLIC_SITE_URL = "http://localhost:3000";
  globalThis.fetch = async () => completion("OK");
  const loopback = request(credentials);
  loopback.headers.set("origin", "http://127.0.0.1:3000");
  loopback.headers.set("sec-fetch-site", "same-origin");

  try {
    expect((await testConnection(loopback)).status).toBe(200);
  } finally {
    if (previousSiteUrl === undefined) delete process.env.NEXT_PUBLIC_SITE_URL;
    else process.env.NEXT_PUBLIC_SITE_URL = previousSiteUrl;
  }
});

test("Markdown grouping preserves exact code, links and source order", () => {
  const source = "# Hello\n\n" + "A paragraph.\n\n".repeat(600) + "```js\nconst x = 1;\n```\n\n[Docs](https://example.com)\n";
  const chunks = splitMarkdown(source);
  expect(chunks.map((chunk) => chunk.source).join("")).toBe(source);
  expect(chunks.length).toBeGreaterThan(2);
  expect(chunks.some((chunk) => chunk.literal && chunk.source.includes("const x"))).toBe(true);
  expect(() => validateTranslation("[Docs](https://a.test)", "[文档](https://evil.test)")).toThrow();
  expect(() => validateTranslation("`npm install`", "`npm remove`")).toThrow();
  expect(() => validateTranslation("[Docs](https://a.test)", "[文档](https://a.test)")).not.toThrow();
});

test("summary mode only generates a concise project overview", async () => {
  let calls = 0;
  globalThis.fetch = async (_url, options) => {
    calls++;
    const body = JSON.parse(String(options?.body));
    expect(body.messages[0].content).toContain("what project this is and what it is used for");
    const input = JSON.parse(body.messages[1].content);
    expect(input.document).toContain("# Hello");
    return completion(JSON.stringify({ summary: "这是一个用于展示项目增长趋势的开源项目。\n\n- 提供仓库详情\n- 展示趋势图\n- 读取 README" }));
  };
  const response = await generateReadme(request({ ...credentials, mode: "summary", markdown: "# Hello\n\nA project." }));
  const events = (await response.text()).trim().split("\n").map((line) => JSON.parse(line));
  expect(events.map((event) => event.type)).toEqual(["progress", "progress", "result"]);
  expect(events.at(-1).result).toEqual({ mode: "summary", summary: expect.stringContaining("这是一个") });
  expect(calls).toBe(1);
});

test("summary mode rejects more than three key points", async () => {
  globalThis.fetch = async () => completion(JSON.stringify({
    summary: "这是一个项目介绍，用于演示摘要边界。\n\n- 要点一\n- 要点二\n- 要点三\n- 要点四",
  }));
  const response = await generateReadme(request({ ...credentials, mode: "summary", markdown: "# Project" }));
  const events = (await response.text()).trim().split("\n").map((line) => JSON.parse(line));
  expect(events.at(-1)).toMatchObject({ type: "error", error: { code: "INVALID_OUTPUT" } });
  expect(events.at(-1).error.message).toContain("要点过多");
});

test("README fingerprints are stable SHA-256 values", async () => {
  expect(await fingerprintMarkdown("")).toBe("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
  expect(await fingerprintMarkdown("hello")).toBe("2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824");
  expect(await fingerprintMarkdown("hello")).toBe(await fingerprintMarkdown("hello"));
});

test("cancelling an IndexedDB save aborts the pending write", async () => {
  const originalDescriptor = Object.getOwnPropertyDescriptor(globalThis, "indexedDB");
  const controller = new AbortController();
  let abortCalled = false;
  let markStarted: (() => void) | undefined;
  const started = new Promise<void>((resolve) => { markStarted = resolve; });
  const transaction = {
    error: null,
    objectStore: () => ({ put: () => { markStarted?.(); return {}; } }),
    abort: () => {
      abortCalled = true;
      queueMicrotask(() => transaction.onabort?.(new Event("abort")));
    },
    onabort: null as ((event: Event) => void) | null,
    oncomplete: null as ((event: Event) => void) | null,
    onerror: null as ((event: Event) => void) | null,
  };
  const database = {
    objectStoreNames: { contains: () => true },
    transaction: () => transaction,
    close: () => undefined,
  };
  const openRequest = { result: database, error: null, onsuccess: null as ((event: Event) => void) | null };
  const fakeIndexedDb = {
    open: () => {
      queueMicrotask(() => openRequest.onsuccess?.(new Event("success")));
      return openRequest;
    },
  };
  Object.defineProperty(globalThis, "indexedDB", { configurable: true, value: fakeIndexedDb });
  try {
    const saving = saveReadmeTranslation({
      repository: "owner/repository",
      translation: "# 译文",
      sourceFingerprint: "0".repeat(64),
      generatedAt: "2026-09-14T00:00:00.000Z",
      modelName: "测试模型",
      imageBaseUrl: "https://github.com/owner/repository/blob/main/README.md",
    }, controller.signal);
    await started;
    controller.abort();
    await expect(saving).rejects.toMatchObject({ name: "AbortError" });
    expect(abortCalled).toBe(true);
  } finally {
    if (originalDescriptor) Object.defineProperty(globalThis, "indexedDB", originalDescriptor);
    else Reflect.deleteProperty(globalThis, "indexedDB");
  }
});

test("translation mode streams milestones and preserves literal code without generating a summary", async () => {
  const calls: string[] = [];
  globalThis.fetch = async (_url, options) => {
    const body = JSON.parse(String(options?.body));
    expect(body.messages[0].content).toContain("untrusted document data");
    const input = JSON.parse(body.messages[1].content);
    calls.push(input.document);
    return completion(JSON.stringify({ translation: input.document.replace("Hello", "你好").trim() }));
  };
  const response = await generateReadme(request({ ...credentials, mode: "translation", markdown: "# Hello\n\n```js\nconst x = 1;\n```\n" }));
  const events = (await response.text()).trim().split("\n").map((line) => JSON.parse(line));
  expect(events[0].type).toBe("progress");
  const result = events.at(-1).result;
  expect(result.mode).toBe("translation");
  expect(result).not.toHaveProperty("summary");
  expect(result.translation).toContain("# 你好");
  expect(result.translation).toContain("```js\nconst x = 1;\n```");
  expect(events.some((event) => event.type === "progress" && event.message === "正在校验译文")).toBe(true);
  expect(calls).toHaveLength(1);
});

test("translation helper does not call a second summary model", async () => {
  let calls = 0;
  globalThis.fetch = async (_url, options) => {
    calls++;
    const body = JSON.parse(String(options?.body));
    const input = JSON.parse(body.messages[1].content);
    return completion(JSON.stringify({ translation: input.document.replace("Hello", "你好") }));
  };
  const result = await translateReadme(credentials, "# Hello", new AbortController().signal, () => undefined);
  expect(result).toEqual({ mode: "translation", translation: "# 你好" });
  expect(calls).toBe(1);
});

test("invalid JSON and truncated connections produce errors instead of fabricated translations", async () => {
  globalThis.fetch = async () => completion("not JSON");
  const response = await generateReadme(request({ ...credentials, markdown: "# Hello" }));
  const body = await response.text();
  expect(body).toContain('"type":"error"'); expect(body).not.toContain('"type":"result"');
});

test("cancellation aborts upstream and timeout errors are explicit", async () => {
  const cancellation = new AbortController();
  globalThis.fetch = async (_url, options) => new Promise<Response>((_resolve, reject) => {
    options?.signal?.addEventListener("abort", () => reject(options.signal?.reason));
    cancellation.abort();
  });
  await expect(translateReadme(credentials, "Hello", cancellation.signal, () => undefined)).rejects.toMatchObject({ code: "CANCELLED" });
  expect(publicAiError(new DOMException("deadline", "TimeoutError")).code).toBe("TIMEOUT");
  expect(() => providerRequest({ ...credentials, provider: "invalid" }, "", "", 1)).toThrow();
});

test("upstream deadline is 90 seconds and task deadline is ten minutes", async () => {
  const timeout = AbortSignal.timeout;
  const deadlines: number[] = [];
  AbortSignal.timeout = (milliseconds: number) => {
    deadlines.push(milliseconds);
    const controller = new AbortController();
    if (milliseconds === 90_000) queueMicrotask(() => controller.abort(new DOMException("deadline", "TimeoutError")));
    return controller.signal;
  };
  globalThis.fetch = async (_url, options) => new Promise<Response>((_resolve, reject) => {
    if (options?.signal?.aborted) reject(options.signal.reason);
    else options?.signal?.addEventListener("abort", () => reject(options.signal?.reason));
  });
  try {
    const response = await generateReadme(request({ ...credentials, markdown: "Hello" }));
    expect(await response.text()).toContain('"code":"TIMEOUT"');
    expect(deadlines).toEqual([600_000, 90_000]);
  } finally { AbortSignal.timeout = timeout; }
});

test("closing the response stream propagates cancellation to the vendor", async () => {
  let aborted = false;
  globalThis.fetch = async (_url, options) => new Promise<Response>((_resolve, reject) => {
    options?.signal?.addEventListener("abort", () => { aborted = true; reject(options.signal?.reason); });
  });
  const response = await generateReadme(request({ ...credentials, markdown: "Hello" }));
  const reader = response.body?.getReader();
  expect(reader).toBeTruthy();
  await reader?.read();
  await reader?.cancel();
  expect(aborted).toBe(true);
});

test("reference definitions are preserved across chunk boundaries", async () => {
  globalThis.fetch = async (_url, options) => {
    const body = JSON.parse(String(options?.body));
    const input = JSON.parse(body.messages[1].content);
    return completion(JSON.stringify({ translation: input.document.replace("Title", "标题").trim() }));
  };
  const markdown = "# Title\n\n[Docs][docs]\n\n[docs]: https://example.com\n";
  const translated = await translateReadme(credentials, markdown, new AbortController().signal, () => undefined);
  expect(translated.translation).toContain("[docs]: https://example.com");
});

// Discovery tests use the same HTTP boundary as generation, with no real vendor calls.

for (const provider of ["openai", "deepseek", "claude", "gemini"]) {
  test(`${provider} discovery normalizes models, deduplicates and marks compatibility`, async () => {
    const known = AI_PROVIDERS.find((p) => p.id === provider)!.models[0].id;
    let calls = 0;
    globalThis.fetch = async (url, options) => {
      calls++;
      expect(String(url)).not.toContain(credentials.apiKey);
      expect(JSON.stringify(options?.headers)).toContain(credentials.apiKey);
      expect(options?.redirect).toBe("error");
      expect(options?.cache).toBe("no-store");
      if (provider === "claude") {
        if (calls === 2) expect(String(url)).toContain("after_id=cursor");
        return Response.json({ data: [{ id: known, display_name: "Known" }, { id: "future-model", display_name: "Future" }], has_more: calls === 1, last_id: "cursor" });
      }
      if (provider === "gemini") {
        if (calls === 2) expect(String(url)).toContain("pageToken=cursor");
        return Response.json({ models: [{ name: `models/${known}` }, { name: "models/future-model", displayName: "Future" }], ...(calls === 1 ? { nextPageToken: "cursor" } : {}) });
      }
      return Response.json({ data: [{ id: known }, { id: known }, { id: "future-model" }] });
    };
    const response = await listModels(request({ provider, apiKey: credentials.apiKey }));
    expect(response.status).toBe(200);
    const body = await response.json();
    expect(body.source).toBe("vendor");
    expect(body.models).toHaveLength(2);
    expect(body.models[0]).toMatchObject({ id: known, supported: true });
    expect(body.models[1]).toMatchObject({ id: "future-model", supported: false });
    expect(calls).toBe(provider === "claude" || provider === "gemini" ? 2 : 1);
    expect(credentialsSchema.safeParse({ provider, apiKey: credentials.apiKey, model: "future-model" }).success).toBe(false);
  });
}

test("discovery errors redact secrets, reject malformed pages and preserve empty results", async () => {
  for (const status of [401, 403, 429, 500]) {
    globalThis.fetch = async () => Response.json({ error: { message: credentials.apiKey } }, { status });
    const response = await listModels(request({ provider: "openai", apiKey: credentials.apiKey }));
    expect(response.ok).toBe(false);
    expect(await response.text()).not.toContain(credentials.apiKey);
  }
  for (const payload of [null, { data: [{ id: 42 }] }]) {
    globalThis.fetch = async () => Response.json(payload);
    expect(await (await listModels(request({ provider: "openai", apiKey: credentials.apiKey }))).text()).toContain("INVALID_OUTPUT");
  }
  globalThis.fetch = async () => Response.json({ data: [] });
  expect((await (await listModels(request({ provider: "openai", apiKey: credentials.apiKey }))).json()).models).toEqual([]);
});

test("discovery stops repeated cursors, supports cancellation and a 30 second total deadline", async () => {
  const timeout = AbortSignal.timeout;
  let count = 0;
  globalThis.fetch = async () => { count++; return Response.json({ models: [], nextPageToken: "same" }); };
  expect(await (await listModels(request({ provider: "gemini", apiKey: credentials.apiKey }))).text()).toContain("INVALID_OUTPUT");
  expect(count).toBe(2);
  const cancelled = new AbortController(); cancelled.abort();
  expect(await (await listModels(request({ provider: "openai", apiKey: credentials.apiKey }, cancelled.signal))).text()).toContain("CANCELLED");
  AbortSignal.timeout = (ms) => {
    expect(ms).toBe(30000);
    const controller = new AbortController();
    queueMicrotask(() => controller.abort(new DOMException("timeout", "TimeoutError")));
    return controller.signal;
  };
  globalThis.fetch = async (_url, options) => new Promise<Response>((_resolve, reject) => {
    if (options?.signal?.aborted) reject(options.signal.reason);
    else options?.signal?.addEventListener("abort", () => reject(options.signal?.reason));
  });
  try { expect(await (await listModels(request({ provider: "openai", apiKey: credentials.apiKey }))).text()).toContain("TIMEOUT"); }
  finally { AbortSignal.timeout = timeout; }
});

test("Qwen presets and invalid requests make no discovery calls", async () => {
  globalThis.fetch = async () => { throw new Error("unexpected call"); };
  expect((await (await listModels(request({ provider: "qwen", apiKey: credentials.apiKey }))).json()).source).toBe("preset");
  for (const body of [{ provider: "unknown", apiKey: credentials.apiKey }, { provider: "openai", apiKey: "" }, { provider: "openai", apiKey: credentials.apiKey, endpoint: "https://example.com" }]) {
    expect((await listModels(request(body))).status).toBe(400);
  }
  const crossOrigin = new Request("http://localhost/api/ai/models", { method: "POST", headers: { origin: "https://example.com", "Content-Type": "application/json" }, body: JSON.stringify(credentials) });
  expect((await listModels(crossOrigin)).status).toBe(403);
  expect(() => providerRequest({ ...credentials, model: "__proto__" }, "", "", 128)).toThrow();
});

test("explicit model capabilities determine token limits and parameters", () => {
  for (const provider of AI_PROVIDERS) for (const model of provider.models) {
    const req = providerRequest({ ...credentials, provider: provider.id, model: model.id }, "system", "input", 12000);
    const body = JSON.parse(JSON.stringify(req.body));
    if (provider.id === "gemini") expect(body.generationConfig).toEqual({ maxOutputTokens: 12000, thinkingConfig: { thinkingLevel: "minimal" } });
    else expect(body.max_tokens).toBe(provider.id === "qwen" ? 8192 : 12000);
    if (provider.id === "openai") expect(body.store).toBe(false);
    if (provider.id === "deepseek") expect(body.thinking).toEqual({ type: "disabled" });
    if (model.id === "qwen-plus") expect(body.enable_thinking).toBe(false);
    if (model.id === "qwen-max") expect(body).not.toHaveProperty("enable_thinking");
  }
});

test("cancelling discovery aborts an in-flight vendor request", async () => {
  const controller = new AbortController();
  let aborted = false;
  globalThis.fetch = async (_url, options) => new Promise<Response>((_resolve, reject) => {
    options?.signal?.addEventListener("abort", () => { aborted = true; reject(options.signal?.reason); });
    controller.abort();
  });
  const response = await listModels(request({ provider: "openai", apiKey: credentials.apiKey }, controller.signal));
  expect(aborted).toBe(true);
  expect(await response.text()).toContain("CANCELLED");
});
