import type {
  ChartRange,
  FilterResponse,
  RankingFilters,
  RankingResponse,
  ReadmeResponse,
  Repository,
  SnapshotSeriesResponse,
} from "@/lib/types";

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

async function getJson<T>(
  url: string,
  revalidate: number | null = 300,
  headers?: HeadersInit,
): Promise<T> {
  const response = await fetch(
    url,
    revalidate === null ? { cache: "no-store", headers } : { next: { revalidate }, headers },
  );
  if (!response.ok) {
    let message = "数据服务暂时不可用";
    try {
      const payload = (await response.json()) as { error?: { message?: string } };
      message = payload.error?.message ?? message;
    } catch {
      // Keep the user-facing fallback when an upstream proxy returns HTML.
    }
    throw new Error(message);
  }
  return (await response.json()) as T;
}

export function rankingApiUrl(filters: RankingFilters, client = false): string {
  const base = client ? PUBLIC_API_BASE : SERVER_API_BASE;
  return `${base}/api/v1/rankings?${rankingParams(filters)}`;
}

export async function fetchRankings(filters: RankingFilters): Promise<RankingResponse> {
  return getJson<RankingResponse>(rankingApiUrl(filters), null);
}

export async function fetchFilters(): Promise<FilterResponse> {
  return getJson<FilterResponse>(`${SERVER_API_BASE}/api/v1/filters`, 3600);
}

export async function fetchRepository(owner: string, name: string): Promise<Repository> {
  return getJson<Repository>(`${SERVER_API_BASE}/api/v1/repos/${owner}/${name}`);
}

export async function fetchReadme(
  owner: string,
  name: string,
  headers?: HeadersInit,
): Promise<ReadmeResponse> {
  return getJson<ReadmeResponse>(`${SERVER_API_BASE}/api/v1/repos/${owner}/${name}/readme`, null, headers);
}

export async function fetchSnapshots(
  owner: string,
  name: string,
  range: ChartRange,
  client = false,
): Promise<SnapshotSeriesResponse> {
  const base = client ? PUBLIC_API_BASE : SERVER_API_BASE;
  return getJson<SnapshotSeriesResponse>(
    `${base}/api/v1/repos/${owner}/${name}/snapshots?range=${range}`,
  );
}
