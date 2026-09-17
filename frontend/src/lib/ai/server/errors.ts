export class AiError extends Error {
  constructor(public code: string, message: string, public status = 502, public retryAfter?: number) { super(message); }
}

export function publicAiError(error: unknown): AiError {
  if (error instanceof AiError) return error;
  if (error instanceof Error && error.name === "TimeoutError") return new AiError("TIMEOUT", "模型处理超时，请稍后重试。", 504);
  if (error instanceof Error && error.name === "AbortError") return new AiError("CANCELLED", "已取消生成。", 499);
  return new AiError("UNAVAILABLE", "模型服务暂时不可用，请稍后重试。");
}

export function upstreamError(status: number, body: unknown): AiError {
  // Only inspect machine codes; never echo vendor text, which may contain credentials.
  const error = body && typeof body === "object" && "error" in body ? body.error : null;
  const code = error && typeof error === "object" && "code" in error ? String(error.code) : "";
  if (status === 402 || /quota|balance|arrear|credit/i.test(code)) return new AiError("QUOTA", "模型账户额度不足，请到厂商平台检查余额。", 402);
  if (status === 401) return new AiError("INVALID_KEY", "API Key 无效，请检查后重新保存。", 401);
  if (status === 403) return new AiError("FORBIDDEN", "当前密钥没有模型访问权限，或服务区域不匹配。", 403);
  if (status === 429) return new AiError("RATE_LIMIT", "调用频率或配额受限，请稍后重试。", 429);
  if (status === 404 || status === 400) return new AiError("MODEL_UNAVAILABLE", "所选模型不可用，请检查账户权限或更换模型。", 400);
  return new AiError("UNAVAILABLE", "模型服务暂时不可用，请稍后重试。");
}
