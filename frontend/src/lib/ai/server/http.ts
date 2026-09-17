import { credentialsSchema, modelListRequestSchema, readmeRequestSchema, type AiEvent } from "../contracts";
import { AiError, publicAiError } from "./errors";
import { acquireLease } from "./limiter";
import { discoverModels } from "./models";
import { callModel } from "./provider";
import { summarizeReadme, translateReadme } from "./translate";

const HEADERS = { "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" };

function isAllowedOrigin(request: Request, origin: string): boolean {
  const configuredSiteUrl = process.env.NEXT_PUBLIC_SITE_URL;
  if (configuredSiteUrl) {
    try {
      if (new URL(configuredSiteUrl).origin === origin) return true;
    } catch {
      return false;
    }
  }
  if (new URL(request.url).origin === origin) return true;
  // Next may expose its 0.0.0.0 bind address while a local browser uses localhost or 127.0.0.1.
  return request.headers.get("sec-fetch-site") === "same-origin" && isLoopbackOrigin(origin);
}

function isLoopbackOrigin(origin: string): boolean {
  try {
    const url = new URL(origin);
    return url.protocol === "http:" && ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
  } catch {
    return false;
  }
}

async function readBody(request: Request): Promise<unknown> {
  const origin = request.headers.get("origin");
  if (origin && !isAllowedOrigin(request, origin)) throw new AiError("ORIGIN", "不允许跨站调用。", 403);
  if (!request.headers.get("content-type")?.startsWith("application/json")) throw new AiError("INVALID_REQUEST", "请求格式无效。", 400);
  const reader = request.body?.getReader();
  if (!reader) throw new AiError("INVALID_REQUEST", "请求内容为空。", 400);
  const decoder = new TextDecoder();
  let text = "";
  let size = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > 1_000_000) { await reader.cancel(); throw new AiError("TOO_LARGE", "请求内容过大。", 413); }
      text += decoder.decode(value, { stream: true });
    }
    text += decoder.decode();
    try { return JSON.parse(text); }
    catch { throw new AiError("INVALID_REQUEST", "请求格式无效。", 400); }
  } finally { reader.releaseLock(); }
}

function errorResponse(error: unknown) {
  const safe = publicAiError(error);
  const headers = new Headers(HEADERS);
  if (safe.retryAfter) headers.set("Retry-After", String(safe.retryAfter));
  return Response.json({ error: { code: safe.code, message: safe.message } }, { status: safe.status, headers });
}

export async function testConnection(request: Request): Promise<Response> {
  try {
    const result = credentialsSchema.safeParse(await readBody(request));
    if (!result.success) throw new AiError("INVALID_REQUEST", "请填写有效密钥并选择已适配模型。", 400);
    const lease = await acquireLease(request, "ai_probe");
    try {
      await callModel(result.data, "Reply with only OK.", "Connection test", request.signal, 128);
    } finally {
      await lease.release();
    }
    return Response.json({ ok: true }, { headers: HEADERS });
  } catch (error) { return errorResponse(error); }
}

export async function generateReadme(request: Request): Promise<Response> {
  try {
    const parsed = readmeRequestSchema.safeParse(await readBody(request));
    if (!parsed.success) throw new AiError("INVALID_REQUEST", "请选择已适配模型、填写密钥，并提供不超过 100,000 字符的 README。", 400);
    const lease = await acquireLease(request, "ai_generate");
    const cancellation = new AbortController();
    const signal = AbortSignal.any([request.signal, cancellation.signal, AbortSignal.timeout(600_000)]);
    lease.start((error) => cancellation.abort(error));
    let closed = false;
    const stream = new ReadableStream<Uint8Array>({
      async start(controller) {
        const encoder = new TextEncoder();
        const send = (event: AiEvent) => { if (!closed) controller.enqueue(encoder.encode(JSON.stringify(event) + "\n")); };
        try {
          const result = parsed.data.mode === "summary"
            ? await summarizeReadme(parsed.data, parsed.data.markdown, signal, send)
            : await translateReadme(parsed.data, parsed.data.markdown, signal, send);
          signal.throwIfAborted();
          send({ type: "result", result });
        } catch (error) {
          const safe = publicAiError(error);
          send({ type: "error", error: { code: safe.code, message: safe.message } });
        } finally {
          await lease.release();
          if (!closed) { closed = true; controller.close(); }
        }
      },
      async cancel() {
        closed = true;
        cancellation.abort(new DOMException("The request was cancelled", "AbortError"));
        await lease.release();
      },
    });
    return new Response(stream, { headers: { ...HEADERS, "Content-Type": "application/x-ndjson; charset=utf-8", "X-Accel-Buffering": "no" } });
  } catch (error) { return errorResponse(error); }
}

export async function listModels(request: Request): Promise<Response> {
  try {
    const parsed = modelListRequestSchema.safeParse(await readBody(request));
    if (!parsed.success) throw new AiError("INVALID_REQUEST", "请选择厂商并填写有效 API Key。", 400);
    const lease = await acquireLease(request, "ai_probe");
    try {
      return Response.json(await discoverModels(parsed.data, request.signal), { headers: HEADERS });
    } finally {
      await lease.release();
    }
  } catch (error) { return errorResponse(error); }
}
