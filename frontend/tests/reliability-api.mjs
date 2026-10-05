import { createServer } from "node:http";

const rules = new Map();
const counts = new Map();
const stamp = "2026-09-30T00:00:00Z";

function defaultPayload(url) {
  if (url.pathname === "/api/v1/filters") return { languages: [], topics: [] };
  if (url.pathname === "/api/v1/rankings") {
    const period = Number(url.searchParams.get("period") ?? 7);
    return {
      data: [{ rank: 1, previous_rank: 1, full_name: "fixture/initial-ranking", owner: "fixture", owner_github_id: null,
        name: "initial-ranking", description: "可靠性回归测试", language: "TypeScript", topics: [], total_stars: 100,
        star_delta: 20, growth_rate: 0.25, baseline_available: true, last_updated_at: stamp,
        github_url: "https://github.com/fixture/initial-ranking" }],
      meta: { period_days: period, as_of: stamp, baseline_at: stamp, generated_at: stamp, coverage: 1,
        total: 1, page: Number(url.searchParams.get("page") ?? 1), limit: 15, data_mode: "live" },
    };
  }
  if (url.pathname.endsWith("/snapshots")) return {
    repository: "fixture/chart", range: url.searchParams.get("range"),
    data: [{ captured_at: "2026-09-20T00:00:00Z", stars_count: 100, forks_count: 1 },
      { captured_at: stamp, stars_count: 140, forks_count: 1 }],
  };
  if (url.pathname.endsWith("/readme")) return {
    repository: "fixture/chart", path: "README.md", content: "# 测试文档", html_url: "https://github.com/fixture/chart/blob/main/README.md",
  };
  const [owner, name] = url.pathname.split("/").slice(-2);
  return { full_name: `${owner}/${name}`, owner, name, owner_github_id: null, description: "可靠性回归仓库",
    html_url: `https://github.com/${owner}/${name}`, language: "TypeScript", topics: [], license_name: "MIT",
    stars_count: 140, forks_count: 1, open_issues_count: 0, pushed_at: stamp, github_created_at: stamp,
    first_tracked_at: stamp, last_seen_at: stamp, history_available_from: stamp };
}

function json(response, status, payload) {
  response.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" });
  response.end(JSON.stringify(payload));
}

const server = createServer(async (request, response) => {
  const url = new URL(request.url, "http://127.0.0.1:18081");
  if (url.pathname === "/__health") return json(response, 200, { ready: true });
  if (url.pathname === "/__control" && request.method === "POST") {
    let body = "";
    for await (const chunk of request) body += chunk;
    const control = JSON.parse(body);
    rules.set(control.path, control);
    return json(response, 200, { configured: true });
  }
  if (url.pathname === "/__counts") return json(response, 200, Object.fromEntries(counts));
  counts.set(url.pathname, (counts.get(url.pathname) ?? 0) + 1);
  const rule = rules.get(url.pathname);
  if (rule?.disconnect) return request.socket.destroy();
  if (rule?.delayMs) await new Promise((resolve) => setTimeout(resolve, rule.delayMs));
  json(response, rule?.status ?? 200, rule?.body ?? defaultPayload(url));
});

server.listen(18081, "127.0.0.1");
for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => { server.closeAllConnections(); server.close(() => process.exit(0)); });
}
