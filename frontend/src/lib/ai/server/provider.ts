import { z } from "zod";
import { findModelCapability } from "../catalog";
import type { AiCredentials } from "../contracts";
import { AiError, publicAiError, upstreamError } from "./errors";

const completionSchema = z.object({ choices: z.array(z.object({ finish_reason: z.string().nullable(), message: z.object({ content: z.string().nullable() }) })).min(1) });
const claudeSchema = z.object({ stop_reason: z.string().nullable(), content: z.array(z.object({ type: z.string(), text: z.string().optional() })) });
const geminiSchema = z.object({ candidates: z.array(z.object({ finishReason: z.string().optional(), content: z.object({ parts: z.array(z.object({ text: z.string().optional(), thought: z.boolean().optional() })) }).optional() })).min(1) });

export function providerRequest(credentials: AiCredentials, system: string, input: string, maxTokens: number) {
  const { provider, apiKey, model } = credentials;
  const capability = findModelCapability(provider, model);
  if (!capability) throw new AiError("INVALID_REQUEST", "所选模型尚未适配，请重新选择模型。", 400);
  const tokenLimit = Math.min(maxTokens, capability.maxOutputTokens);
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (capability.api === "messages") {
    headers["x-api-key"] = apiKey;
    headers["anthropic-version"] = "2023-06-01";
    return { url: "https://api.anthropic.com/v1/messages", headers, body: { model, system, max_tokens: tokenLimit, ...capability.parameters, messages: [{ role: "user", content: input }] } };
  }
  if (capability.api === "generateContent") {
    headers["x-goog-api-key"] = apiKey;
    return { url: `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`, headers,
      body: { systemInstruction: { parts: [{ text: system }] }, contents: [{ role: "user", parts: [{ text: input }] }], generationConfig: { maxOutputTokens: tokenLimit, ...capability.parameters } } };
  }
  const endpoints: Record<string, string> = {
    openai: "https://api.openai.com/v1/chat/completions",
    deepseek: "https://api.deepseek.com/chat/completions",
    qwen: "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
  };
  if (!endpoints[provider]) throw new AiError("INVALID_REQUEST", "不支持的模型厂商。", 400);
  headers.Authorization = `Bearer ${apiKey}`;
  return { url: endpoints[provider], headers, body: {
    model, max_tokens: tokenLimit, stream: false, ...capability.parameters,
    messages: [{ role: "system", content: system }, { role: "user", content: input }],
  } };
}

export function parseCompletion(provider: string, data: unknown): string {
  let text: string | null | undefined;
  let finished = false;
  if (provider === "claude") {
    const value = claudeSchema.parse(data);
    finished = value.stop_reason === "end_turn";
    text = value.content.filter((part) => part.type === "text").map((part) => part.text ?? "").join("");
  } else if (provider === "gemini") {
    const value = geminiSchema.parse(data).candidates[0];
    finished = value.finishReason === "STOP";
    text = value.content?.parts.filter((part) => !part.thought).map((part) => part.text ?? "").join("");
  } else {
    const value = completionSchema.parse(data).choices[0];
    finished = value.finish_reason === "stop";
    text = value.message.content;
  }
  if (!finished || !text?.trim()) throw new AiError("INVALID_OUTPUT", "模型输出为空、被截断或被拦截，请更换模型后重试。");
  return text.trim();
}

export async function callModel(credentials: AiCredentials, system: string, input: string, signal: AbortSignal, maxTokens = 12000): Promise<string> {
  const request = providerRequest(credentials, system, input, maxTokens);
  const timeout = AbortSignal.timeout(90_000);
  const combined = AbortSignal.any([signal, timeout]);
  try {
    const response = await fetch(request.url, { method: "POST", headers: request.headers, body: JSON.stringify(request.body), signal: combined, cache: "no-store", redirect: "error" });
    const data: unknown = await response.json().catch(() => null);
    if (!response.ok) throw upstreamError(response.status, data);
    try { return parseCompletion(credentials.provider, data); }
    catch (error) {
      if (error instanceof AiError) throw error;
      throw new AiError("INVALID_OUTPUT", "模型返回了无法识别的结果，请更换模型后重试。");
    }
  } catch (error) {
    if (combined.aborted) throw publicAiError(combined.reason);
    throw publicAiError(error);
  }
}
