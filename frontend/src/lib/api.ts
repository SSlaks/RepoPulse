import { cache } from "react";

import type {
  ChartRange,
  FilterResponse,
  RankingFilters,
  RankingResponse,
  ReadmeResponse,
  Repository,
  SnapshotSeriesResponse,
} from "@/lib/types";
import { requestJson, type ApiRequestOptions } from "@/lib/api-request";

const SERVER_API_BASE = process.env.API_BASE_URL ?? "http://localhost:8000";
export const PUBLIC_API_BASE = "";

function rankingParams(filters: RankingFilters): URLSearchParams {
  const params = new URLSearchParams({
    period: String(filters.period),
    page: String(filters.page ?? 1),
    limit: String(filters.limit ?? 15),
  });
  if (filters.language) params.set("language", filters.language);
  if (filters.topic) params.set("topic", filters.topic);
  if (filters.minStars) params.set("minStars", String(filters.minStars));
  if (filters.q) params.set("q", filters.q);
  return params;
}

export function rankingApiUrl(filters: RankingFilters, client = false): string {
  const base = client ? PUBLIC_API_BASE : SERVER_API_BASE;
  return `${base}/api/v1/rankings?${rankingParams(filters)}`;
}

export async function fetchRankings(
  filters: RankingFilters,
  client = false,
  options: ApiRequestOptions = {},
): Promise<RankingResponse> {
  return requestJson<RankingResponse>(rankingApiUrl(filters, client), { ...options, revalidate: null });
}

export async function fetchFilters(client = false, options: ApiRequestOptions = {}): Promise<FilterResponse> {
  const base = client ? PUBLIC_API_BASE : SERVER_API_BASE;
  return requestJson<FilterResponse>(`${base}/api/v1/filters`, { ...options, revalidate: client ? null : 3600 });
}

// The timeout signal opts out of Next's fetch memoization; share metadata/page reads per render.
export const fetchRepository = cache(async (owner: string, name: string): Promise<Repository> => {
  return requestJson<Repository>(`${SERVER_API_BASE}/api/v1/repos/${owner}/${name}`);
});

export async function fetchReadme(
  owner: string,
  name: string,
  headers?: HeadersInit,
): Promise<ReadmeResponse> {
  return requestJson<ReadmeResponse>(`${SERVER_API_BASE}/api/v1/repos/${owner}/${name}/readme`, { revalidate: null, headers });
}

export async function fetchSnapshots(
  owner: string,
  name: string,
  range: ChartRange,
  client = false,
  options: ApiRequestOptions = {},
): Promise<SnapshotSeriesResponse> {
  const base = client ? PUBLIC_API_BASE : SERVER_API_BASE;
  return requestJson<SnapshotSeriesResponse>(
    `${base}/api/v1/repos/${owner}/${name}/snapshots?range=${range}`,
    { ...options, ...(client ? { revalidate: null } : {}) },
  );
}
