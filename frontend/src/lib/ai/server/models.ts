import { z } from "zod";
import { findProvider, isModelSupported } from "../catalog";
import type { ModelList, ModelListRequest } from "../contracts";
import { AiError, publicAiError, upstreamError } from "./errors";

const modelId = z.string().min(1).max(256);
const cursorSchema = z.string().min(1).max(4096);
const flatPage = z.object({ data: z.array(z.object({ id: modelId })).max(10000) });
const claudePage = z.object({
  data: z.array(z.object({ id: modelId, display_name: modelId })).max(10000),
  has_more: z.boolean(), last_id: cursorSchema.nullable().optional(),
});
const geminiPage = z.object({
  models: z.array(z.object({ name: modelId.regex(/^models\//), displayName: modelId.optional() })).max(10000).default([]),
  nextPageToken: cursorSchema.optional(),
});
const endpoints: Record<string, string> = {
  openai: "https://api.openai.com/v1/models",
  deepseek: "https://api.deepseek.com/models",
  claude: "https://api.anthropic.com/v1/models",
  gemini: "https://generativelanguage.googleapis.com/v1beta/models",
};

function parsePage(provider: string, data: unknown) {
  if (provider === "claude") {
    const page = claudePage.parse(data);
    if (page.has_more && !page.last_id) throw new Error("Missing pagination cursor");
    return { models: page.data.map((m) => ({ id: m.id, name: m.display_name })), next: page.has_more ? page.last_id : null };
  }
  if (provider === "gemini") {
    const page = geminiPage.parse(data);
    return { models: page.models.map((m) => ({ id: m.name.slice(7), name: m.displayName ?? m.name.slice(7) })), next: page.nextPageToken };
  }
  return { models: flatPage.parse(data).data.map((m) => ({ id: m.id, name: m.id })), next: null };
}

async function fetchPage(credentials: ModelListRequest, cursor: string | undefined, signal: AbortSignal) {
  const { provider, apiKey } = credentials;
  const url = new URL(endpoints[provider]);
  const headers: Record<string, string> = {};
  if (provider === "claude") {
    headers["x-api-key"] = apiKey; headers["anthropic-version"] = "2023-06-01";
    url.searchParams.set("limit", "1000");
    if (cursor) url.searchParams.set("after_id", cursor);
  } else if (provider === "gemini") {
    headers["x-goog-api-key"] = apiKey; url.searchParams.set("pageSize", "1000");
    if (cursor) url.searchParams.set("pageToken", cursor);
  } else headers.Authorization = `Bearer ${apiKey}`;
  const response = await fetch(url, { headers, signal, cache: "no-store", redirect: "error" });
  const data: unknown = await response.json().catch(() => null);
  if (!response.ok) throw upstreamError(response.status, data);
  try { return parsePage(provider, data); }
  catch { throw new AiError("INVALID_OUTPUT", "厂商模型列表格式无效，请稍后重试。"); }
}

export async function discoverModels(credentials: ModelListRequest, cancellation: AbortSignal): Promise<ModelList> {
  const provider = findProvider(credentials.provider);
  if (!provider) throw new AiError("INVALID_REQUEST", "不支持的模型厂商。", 400);
  const signal = AbortSignal.any([cancellation, AbortSignal.timeout(30_000)]);
  try {
    signal.throwIfAborted();
    if (provider.id === "qwen") return { source: "preset", queriedAt: new Date().toISOString(), models: provider.models.map((m) => ({ ...m, supported: isModelSupported(provider.id, m.id) })) };
    const models = new Map<string, ModelList["models"][number]>();
    const cursors = new Set<string>();
    let cursor: string | undefined;
    do {
      const page = await fetchPage(credentials, cursor, signal);
      signal.throwIfAborted();
      for (const model of page.models) models.set(model.id, { ...model, supported: isModelSupported(provider.id, model.id) });
      cursor = page.next ?? undefined;
      if (models.size > 10000 || (cursor && (cursors.has(cursor) || cursors.size >= 99))) throw new AiError("INVALID_OUTPUT", "厂商模型列表分页异常，请稍后重试。");
      if (cursor) cursors.add(cursor);
    } while (cursor);
    return { models: [...models.values()], source: "vendor", queriedAt: new Date().toISOString() };
  } catch (error) { throw publicAiError(signal.aborted ? signal.reason : error); }
}
