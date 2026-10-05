export const API_REQUEST_TIMEOUT_MS = 10_000;

export interface ApiRequestOptions {
  revalidate?: number | null;
  headers?: HeadersInit;
  signal?: AbortSignal;
  timeoutMs?: number;
}

export class ApiError extends Error {
  constructor(message: string, public readonly status: number | null, public readonly code: string) {
    super(message);
    this.name = "ApiError";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

async function httpError(response: Response, signal: AbortSignal): Promise<ApiError> {
  // A proxy can return HTML; keep a useful fallback instead of exposing its response body.
  const payload: unknown = await response.json().catch(() => null);
  signal.throwIfAborted();
  const error = isRecord(payload) && isRecord(payload.error) ? payload.error : null;
  const message = typeof error?.message === "string" && error.message.trim()
    ? error.message : "数据服务暂时不可用，请稍后重试。";
  const code = typeof error?.code === "string" && error.code.trim()
    ? error.code : `HTTP_${response.status}`;
  return new ApiError(message, response.status, code);
}

export async function requestJson<T>(url: string, options: ApiRequestOptions = {}): Promise<T> {
  const deadline = AbortSignal.timeout(options.timeoutMs ?? API_REQUEST_TIMEOUT_MS);
  const signal = options.signal ? AbortSignal.any([options.signal, deadline]) : deadline;
  const revalidate = options.revalidate === undefined ? 300 : options.revalidate;
  try {
    signal.throwIfAborted();
    const response = await fetch(url, revalidate === null
      ? { cache: "no-store", headers: options.headers, signal }
      : { next: { revalidate }, headers: options.headers, signal });
    if (!response.ok) throw await httpError(response, signal);
    try {
      const payload: T = await response.json();
      signal.throwIfAborted();
      return payload;
    } catch {
      signal.throwIfAborted();
      throw new ApiError("数据服务返回了无法识别的结果，请重试。", response.status, "INVALID_RESPONSE");
    }
  } catch (error) {
    // Superseding a request is an intentional cancellation, not a visible network failure.
    if (options.signal?.aborted) throw options.signal.reason;
    if (deadline.aborted) throw new ApiError("请求超时，请稍后重试。", null, "TIMEOUT");
    if (error instanceof ApiError) throw error;
    throw new ApiError("网络连接失败，请检查网络后重试。", null, "NETWORK_ERROR");
  }
}
