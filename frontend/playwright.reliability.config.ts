import { defineConfig, devices } from "@playwright/test";

// Existing fixture-based regressions require a test identity, but this API does not authenticate.
process.env.TRUSTED_PROXY_TOKEN ??= "reliability-local-test";

// The programmable API exercises server fetches as well as same-origin browser fetches.
export default defineConfig({
  testDir: "./tests",
  timeout: 35_000,
  globalTimeout: 8 * 60_000,
  expect: { timeout: 7_000 },
  workers: 1,
  retries: 0,
  use: { baseURL: "http://127.0.0.1:13001", trace: "retain-on-failure", reducedMotion: "reduce" },
  webServer: process.env.RELIABILITY_REQUEST_ONLY === "1" ? undefined : [
    {
      command: "node tests/reliability-api.mjs",
      url: "http://127.0.0.1:18081/__health",
      reuseExistingServer: false,
      timeout: 15_000,
    },
    {
      command: "npm start -- --hostname 127.0.0.1 --port 13001",
      url: "http://127.0.0.1:13001",
      env: { ...process.env, API_BASE_URL: "http://127.0.0.1:18081", NODE_ENV: "production" },
      reuseExistingServer: false,
      timeout: 30_000,
    },
  ],
  projects: [
    { name: "request", testMatch: "api-request.spec.ts" },
    { name: "reliability-desktop", testMatch: "reliability.spec.ts", use: devices["Desktop Chrome"] },
    { name: "reliability-mobile", testMatch: "reliability.spec.ts", use: devices["Pixel 7"] },
    {
      name: "existing-desktop",
      testMatch: ["ranking.spec.ts", "completeness.spec.ts", "markdown.spec.ts"],
      grep: /增长值保留|摘要仅统计|分页支持指定|完整度随|README 相对|README 链接|README 图片|头像首次|头像重试耗尽/,
      use: devices["Desktop Chrome"],
    },
    {
      name: "existing-mobile",
      testMatch: ["ranking.spec.ts", "completeness.spec.ts", "markdown.spec.ts"],
      grep: /增长值保留|摘要仅统计|分页支持指定|完整度随|README 相对|README 链接|README 图片|头像首次|头像重试耗尽/,
      use: devices["Pixel 7"],
    },
  ],
});
