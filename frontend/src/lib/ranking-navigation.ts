import type { RankingFilters } from "@/lib/types";

const POSITION_KEY = "repopulse-ranking-position";
const POSITION_MAX_AGE_MS = 60 * 60 * 1000;

export function buildRankingPath(pathname: string, filters: RankingFilters): string {
  const params = new URLSearchParams();
  params.set("period", String(filters.period));
  if (filters.language) params.set("language", filters.language);
  if (filters.topic) params.set("topic", filters.topic);
  if (filters.minStars) params.set("minStars", String(filters.minStars));
  if (filters.q) params.set("q", filters.q);
  if (filters.limit && filters.limit !== 15) params.set("limit", String(filters.limit));
  if (filters.page && filters.page > 1) params.set("page", String(filters.page));
  return `${pathname}?${params}`;
}

export function saveRankingPosition(path: string): void {
  try {
    sessionStorage.setItem(POSITION_KEY, JSON.stringify({ path, scrollY: window.scrollY, savedAt: Date.now() }));
  } catch {
    // 浏览器禁用存储时，继续使用框架的默认返回行为。
  }
}

export function restoreRankingPosition(path: string): void {
  try {
    const raw = sessionStorage.getItem(POSITION_KEY);
    if (!raw) return;
    const position: unknown = JSON.parse(raw);
    if (typeof position !== "object" || position === null || !("path" in position)
      || !("scrollY" in position) || !("savedAt" in position)) return;
    if (position.path !== path || typeof position.scrollY !== "number" || !Number.isFinite(position.scrollY)
      || position.scrollY < 0 || typeof position.savedAt !== "number"
      || Date.now() - position.savedAt > POSITION_MAX_AGE_MS) return;
    sessionStorage.removeItem(POSITION_KEY);
    window.scrollTo({ top: position.scrollY, behavior: "instant" });
  } catch {
    // 损坏或不可读取的位置记录不应阻止用户浏览榜单。
  }
}
