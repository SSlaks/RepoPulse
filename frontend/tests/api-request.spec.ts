import { expect, test } from "@playwright/test";
import { ApiError, requestJson } from "../src/lib/api-request";

const originalFetch = globalThis.fetch;
test.afterEach(() => { globalThis.fetch = originalFetch; });

function response(body: string, status = 200, contentType = "application/json") {
  globalThis.fetch = async () => new Response(body, { status, headers: { "Content-Type": contentType } });
}

function hangingFetch() {
  globalThis.fetch = async (_input, init) => new Promise<Response>((_resolve, reject) => {
    const signal = init?.signal;
    if (signal?.aborted) return reject(signal.reason);
    signal?.addEventListener("abort", () => reject(signal.reason), { once: true });
  });
}

test("普通 GET 保留 JSON 和请求头，并传递缓存选项", async () => {
  let options: RequestInit | undefined;
  globalThis.fetch = async (_url, init) => {
    options = init;
    return new Response('{"ok":true}');
  };
  await expect(requestJson("http://fixture/success", { revalidate: null, headers: { "X-Test": "request" } })).resolves.toEqual({ ok: true });
  expect(options?.cache).toBe("no-store");
  expect(new Headers(options?.headers).get("X-Test")).toBe("request");
  await requestJson("http://fixture/cached", { revalidate: 123 });
  expect(options).toMatchObject({ next: { revalidate: 123 } });
  await requestJson("http://fixture/default-cache");
  expect(options).toMatchObject({ next: { revalidate: 300 } });
});

test("HTTP 错误保留状态、错误码和服务端文案", async () => {
  response('{"error":{"message":"暂时维护","code":"MAINTENANCE"}}', 503);
  const error = await requestJson("http://fixture/unavailable").catch((reason: unknown) => reason);
  expect(error).toBeInstanceOf(ApiError);
  expect(error).toMatchObject({ status: 503, code: "MAINTENANCE", message: "暂时维护" });
});

test("HTML 404 与非标准错误响应仍保留 HTTP 状态", async () => {
  response("<html>not found</html>", 404, "text/html");
  await expect(requestJson("http://fixture/missing")).rejects.toMatchObject({ status: 404, code: "HTTP_404" });
  response('{"detail":"unavailable"}', 502);
  await expect(requestJson("http://fixture/proxy")).rejects.toMatchObject({ status: 502, code: "HTTP_502" });
});

test("网络失败和成功状态坏 JSON 有独立错误码", async () => {
  globalThis.fetch = async () => { throw new TypeError("fetch failed"); };
  await expect(requestJson("http://fixture/network")).rejects.toMatchObject({ status: null, code: "NETWORK_ERROR" });
  response("invalid-json");
  await expect(requestJson("http://fixture/json")).rejects.toMatchObject({ code: "INVALID_RESPONSE" });
});

test("可配置超时中止 fetch 并归类 TIMEOUT", async () => {
  hangingFetch();
  await expect(requestJson("http://fixture/slow", { timeoutMs: 25 })).rejects.toMatchObject({ status: null, code: "TIMEOUT" });
});

test("默认超时为 10 秒", async () => {
  hangingFetch();
  const started = performance.now();
  await expect(requestJson("http://fixture/default-timeout")).rejects.toMatchObject({ code: "TIMEOUT" });
  expect(performance.now() - started).toBeGreaterThanOrEqual(9_900);
  expect(performance.now() - started).toBeLessThan(12_000);
});

test("用户取消与已取消 signal 原样传播 AbortError", async () => {
  hangingFetch();
  const controller = new AbortController();
  const reason = new DOMException("用户切换筛选", "AbortError");
  const request = requestJson("http://fixture/cancel", { signal: controller.signal });
  controller.abort(reason);
  await expect(request).rejects.toBe(reason);
  await expect(requestJson("http://fixture/already-cancelled", { signal: controller.signal })).rejects.toBe(reason);
});

test("响应正文一直挂起时也受同一超时控制", async () => {
  globalThis.fetch = async (_input, init) => {
    const stream = new ReadableStream({
      start(controller) {
        init?.signal?.addEventListener("abort", () => controller.error(init.signal?.reason), { once: true });
      },
    });
    return new Response(stream);
  };
  await expect(requestJson("http://fixture/slow-body", { timeoutMs: 25 })).rejects.toMatchObject({ code: "TIMEOUT" });
});

test("错误响应正文挂起时优先归类超时", async () => {
  globalThis.fetch = async (_input, init) => new Response(new ReadableStream({
    start(controller) {
      init?.signal?.addEventListener("abort", () => controller.error(init.signal?.reason), { once: true });
    },
  }), { status: 503 });
  await expect(requestJson("http://fixture/slow-error-body", { timeoutMs: 25 })).rejects.toMatchObject({ code: "TIMEOUT" });
});
