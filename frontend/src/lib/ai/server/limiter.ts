import { z } from "zod";

import {
  INTERNAL_SERVICE_TOKEN_HEADER,
  TRUSTED_CLIENT_IP_HEADER,
  TRUSTED_PROXY_TOKEN_HEADER,
  copyTrustedProxyHeaders,
} from "@/lib/trusted-proxy";
import { AiError, publicAiError } from "./errors";

export type LeasePolicy = "readme" | "ai_generate" | "ai_probe";

const LEASE_RENEWAL_MS = 15_000;
const leaseResponseSchema = z.object({
  lease_id: z.string().min(20).max(256),
  expires_at: z.number().int().positive(),
  lease_seconds: z.number().int().positive().optional(),
});

type LeaseRequest = {
  requestSignal: AbortSignal;
  headers: Headers;
  policy: LeasePolicy;
  leaseId?: string;
};

export type LeaseHandle = {
  start(onLost: (error: unknown) => void): void;
  release(): Promise<void>;
};

function isProduction(): boolean {
  return process.env.NODE_ENV === "production";
}

function identityHeaders(request: Request): Headers | null {
  const headers = copyTrustedProxyHeaders(request.headers);
  if (isProduction() && (!headers.has(TRUSTED_CLIENT_IP_HEADER) || !headers.has(TRUSTED_PROXY_TOKEN_HEADER))) {
    throw new AiError("IDENTITY_UNAVAILABLE", "可信客户端身份暂时不可用，请稍后重试。", 503, 5);
  }

  const serviceToken = process.env.INTERNAL_SERVICE_TOKEN;
  if (!serviceToken) {
    if (isProduction()) throw new AiError("LIMITER_UNAVAILABLE", "限流服务暂时不可用，请稍后重试。", 503, 5);
    return null;
  }
  headers.set(INTERNAL_SERVICE_TOKEN_HEADER, serviceToken);
  return headers;
}

function coordinatorUrl(path: string): string {
  const base = process.env.API_BASE_URL ?? "http://localhost:8000";
  return `${base.replace(/\/$/, "")}/internal/limits/${path}`;
}

function retryAfterHeader(response: Response): number | undefined {
  const value = response.headers.get("retry-after");
  if (!value) return undefined;
  const seconds = Number(value);
  return Number.isFinite(seconds) ? Math.max(1, Math.ceil(seconds)) : undefined;
}

async function coordinatorRequest(
  path: "acquire" | "renew" | "release",
  lease: LeaseRequest,
): Promise<unknown> {
  const timeout = AbortSignal.timeout(1_500);
  const signal = AbortSignal.any([lease.requestSignal, timeout]);
  try {
    const response = await fetch(coordinatorUrl(path), {
      method: "POST",
      headers: { ...Object.fromEntries(lease.headers), "Content-Type": "application/json" },
      body: JSON.stringify({ policy: lease.policy, kind: "internal", ...(lease.leaseId ? { lease_id: lease.leaseId } : {}) }),
      signal,
      cache: "no-store",
      redirect: "error",
    });
    if (response.status === 429) {
      throw new AiError("RATE_LIMIT", "调用频率或并发额度已用尽，请稍后重试。", 429, retryAfterHeader(response));
    }
    if (!response.ok) {
      if (response.status === 409 && path === "renew") throw new AiError("LEASE_LOST", "限流租约已失效，请重试。", 503, 5);
      throw new AiError("LIMITER_UNAVAILABLE", "限流服务暂时不可用，请稍后重试。", 503, 5);
    }
    if (path === "release") return null;
    const parsed = leaseResponseSchema.safeParse(await response.json().catch(() => null));
    if (!parsed.success) throw new AiError("LIMITER_UNAVAILABLE", "限流服务返回无效结果，请稍后重试。", 503, 5);
    return parsed.data;
  } catch (error) {
    if (error instanceof AiError) throw error;
    if (lease.requestSignal.aborted) throw publicAiError(lease.requestSignal.reason);
    throw new AiError("LIMITER_UNAVAILABLE", "限流服务暂时不可用，请稍后重试。", 503, 5);
  }
}

class ManagedLease implements LeaseHandle {
  private timer: ReturnType<typeof setInterval> | undefined;
  private renewing = false;
  private released = false;

  constructor(private readonly request: LeaseRequest & { leaseId: string }) {}

  start(onLost: (error: unknown) => void): void {
    if (this.timer || this.released) return;
    this.timer = setInterval(() => {
      if (this.renewing || this.released) return;
      this.renewing = true;
      void coordinatorRequest("renew", this.request)
        .catch((error: unknown) => {
          if (!this.released) {
            this.stopTimer();
            onLost(error);
          }
        })
        .finally(() => { this.renewing = false; });
    }, LEASE_RENEWAL_MS);
  }

  async release(): Promise<void> {
    if (this.released) return;
    this.released = true;
    this.stopTimer();
    try {
      await coordinatorRequest("release", { ...this.request, requestSignal: new AbortController().signal });
    } catch {
      // The coordinator lease expires on its own when a release cannot reach it.
    }
  }

  private stopTimer(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = undefined;
  }
}

const localLease: LeaseHandle = { start: () => undefined, release: async () => undefined };

export async function acquireLease(request: Request, policy: LeasePolicy): Promise<LeaseHandle> {
  const headers = identityHeaders(request);
  if (!headers) return localLease;
  const result = await coordinatorRequest("acquire", { requestSignal: request.signal, headers, policy });
  const parsed = leaseResponseSchema.parse(result);
  return new ManagedLease({ requestSignal: request.signal, headers, policy, leaseId: parsed.lease_id });
}
