export function rankingFixture(name: string, period = 7) {
  return {
    data: [{
      rank: 1, previous_rank: 1, full_name: `fixture/${name}`, owner: "fixture",
      owner_github_id: null, name, description: "可靠性回归测试", language: "TypeScript",
      topics: [], total_stars: 100, star_delta: 20, growth_rate: 0.25,
      baseline_available: true, last_updated_at: "2026-09-30T00:00:00Z",
      github_url: `https://github.com/fixture/${name}`,
    }],
    meta: {
      period_days: period, as_of: "2026-09-30T00:00:00Z", baseline_at: "2026-09-23T00:00:00Z",
      generated_at: "2026-09-30T00:00:00Z", coverage: 1, total: 1, page: 1, limit: 15, data_mode: "live",
    },
  };
}

export function snapshotsFixture(stars: number[], range = "90d") {
  return {
    repository: "fixture/chart", range,
    data: stars.map((stars_count, index) => ({
      captured_at: `2026-09-${String(20 + index).padStart(2, "0")}T00:00:00Z`, stars_count, forks_count: 1,
    })),
  };
}
