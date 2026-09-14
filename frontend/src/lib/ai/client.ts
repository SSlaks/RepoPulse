import { z } from "zod";
import { eventSchema, modelListResponseSchema, type ModelListRequest, type ModelList, type AiCredentials, type AiEvent, type AiResult } from "./contracts";

const errorSchema = z.object({ error: z.object({ message: z.string() }) });
async function post(path: string, body: unknown, signal: AbortSignal): Promise<Response> {
  const response = await fetch(`/api/ai/${path}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal, cache: "no-store" });
  if (!response.ok) {
    const parsed = errorSchema.safeParse(await response.json().catch(() => null));
    throw new Error(parsed.success ? parsed.data.error.message : "AI 服务暂时不可用，请稍后重试。");
  }
  return response;
}

export async function testAiConnection(credentials: AiCredentials, signal: AbortSignal): Promise<void> {
  const response = await post("test", credentials, signal);
  const result: unknown = await response.json();
  if (!z.object({ ok: z.literal(true) }).safeParse(result).success) throw new Error("连接测试返回无效结果。");
}

export async function requestReadme(credentials: AiCredentials, markdown: string, signal: AbortSignal, progress: (event: Extract<AiEvent, { type: "progress" }>) => void): Promise<AiResult> {
  const response = await post("readme", { ...credentials, markdown }, signal);
  const reader = response.body?.getReader();
  if (!reader) throw new Error("未收到生成结果。");
  const decoder = new TextDecoder();
  let pending = "";
  let result: AiResult | null = null;
  function accept(line: string) {
    if (!line.trim()) return;
    const parsed = eventSchema.safeParse(JSON.parse(line));
    if (!parsed.success) throw new Error("生成结果格式无效。");
    const event = parsed.data;
    if (event.type === "error") throw new Error(event.error.message);
    if (event.type === "progress") progress(event);
    if (event.type === "result") result = event.result;
  }
  try {
    while (true) {
      const { done, value } = await reader.read();
      pending += done ? decoder.decode() : decoder.decode(value, { stream: true });
      if (pending.length > 4_000_000) throw new Error("生成内容过大。");
      const lines = pending.split("\n");
      pending = lines.pop() ?? "";
      lines.forEach(accept);
      if (done) break;
    }
    accept(pending);
    if (!result) throw new Error("连接已中断，未收到完整生成结果，请重试。");
    return result;
  } finally { await reader.cancel().catch(() => undefined); reader.releaseLock(); }
}

export async function fetchAiModels(credentials: ModelListRequest, signal: AbortSignal): Promise<ModelList> {
  const response = await post("models", credentials, signal);
  const parsed = modelListResponseSchema.safeParse(await response.json());
  if (!parsed.success) throw new Error("模型列表返回无效结果。");
  return parsed.data;
}
