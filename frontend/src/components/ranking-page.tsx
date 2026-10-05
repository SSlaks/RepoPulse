import { cookies } from "next/headers";

import { RankingExplorer } from "@/components/ranking-explorer";
import { fetchFilters, fetchRankings } from "@/lib/api";
import { METHODOLOGY_COOKIE } from "@/lib/preferences";
import type { FilterResponse, Period, RankingFilters } from "@/lib/types";

type SearchParams = Record<string, string | string[] | undefined>;

const EMPTY_FILTERS: FilterResponse = { languages: [], topics: [] };

export async function RankingPage({ searchParams }: { searchParams: Promise<SearchParams> }) {
  const [params, cookieStore] = await Promise.all([searchParams, cookies()]);
  const filters = parseFilters(params);
  const [rankingResult, filterResult] = await Promise.allSettled([
    fetchRankings(filters),
    fetchFilters(),
  ]);

  return (
    <RankingExplorer
      initialData={rankingResult.status === "fulfilled" ? rankingResult.value : null}
      filterOptions={filterResult.status === "fulfilled" ? filterResult.value : EMPTY_FILTERS}
      initialFilters={filters}
      initialFilterError={filterResult.status === "rejected"
        ? filterResult.reason instanceof Error ? filterResult.reason.message : "筛选选项暂时不可用，请重试。"
        : undefined}
      showOnboarding={!cookieStore.has(METHODOLOGY_COOKIE)}
      initialError={
        rankingResult.status === "rejected"
          ? rankingResult.reason instanceof Error
            ? rankingResult.reason.message
            : "数据服务暂时不可用"
          : undefined
      }
    />
  );
}

function parseFilters(params: SearchParams): RankingFilters {
  const rawPeriod = firstValue(params.period);
  const numericPeriod = Number(rawPeriod);
  const period: Period = numericPeriod === 1 || numericPeriod === 14 || numericPeriod === 30 ? numericPeriod : 7;
  const minStars = Math.max(0, Number(firstValue(params.minStars)) || 0);
  const page = Math.max(1, Number(firstValue(params.page)) || 1);
  const numericLimit = Number(firstValue(params.limit));
  const limit = numericLimit === 25 || numericLimit === 50 ? numericLimit : 15;
  return {
    period,
    language: firstValue(params.language),
    topic: firstValue(params.topic),
    minStars,
    q: firstValue(params.q),
    page,
    limit,
  };
}

function firstValue(value: string | string[] | undefined): string | undefined {
  return Array.isArray(value) ? value[0] : value;
}
