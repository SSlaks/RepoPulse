import { expect, test } from "@playwright/test";

import { serializeJsonLd } from "@/lib/json-ld";
import { buildContentSecurityPolicy, HSTS_HEADER_VALUE, STATIC_SECURITY_HEADERS } from "@/lib/security-headers";

test("JSON-LD serialization cannot terminate the script element", async ({ page }) => {
  const payload = "</script><script>globalThis.__jsonLdXss = true</script>&\u2028\u2029>";
  const serialized = serializeJsonLd({ description: payload });

  expect(serialized).not.toContain("</script>");
  expect(serialized).toContain("\\u003c/script\\u003e");
  expect(serialized).toContain("\\u0026");
  expect(serialized).toContain("\\u2028");
  expect(serialized).toContain("\\u2029");
  expect(JSON.parse(serialized)).toEqual({ description: payload });

  await page.setContent(`<script id="metadata" type="application/ld+json">${serialized}</script>`);
  const executed = await page.evaluate(
    () => Boolean((globalThis as { __jsonLdXss?: boolean }).__jsonLdXss),
  );
  expect(executed).toBe(false);
  expect(await page.locator("#metadata").textContent()).toBe(serialized);
});

test("production CSP and static headers contain the required restrictions", () => {
  const policy = buildContentSecurityPolicy("test-nonce", false);

  expect(policy).toContain("script-src 'self' 'nonce-test-nonce' 'strict-dynamic'");
  expect(policy).not.toContain("'unsafe-eval'");
  expect(policy).toContain("object-src 'none'");
  expect(policy).toContain("base-uri 'self'");
  expect(policy).toContain("frame-ancestors 'none'");
  expect(policy).toContain("form-action 'self'");
  expect(STATIC_SECURITY_HEADERS["X-Content-Type-Options"]).toBe("nosniff");
  expect(STATIC_SECURITY_HEADERS["Referrer-Policy"]).toBe("strict-origin-when-cross-origin");
  expect(STATIC_SECURITY_HEADERS["Permissions-Policy"]).toContain("camera=()");
  expect(HSTS_HEADER_VALUE).toBe("max-age=63072000; includeSubDomains; preload");
});

test("HTML responses use a request-specific CSP nonce", async ({ page }) => {
  const firstResponse = await page.goto("/settings/ai");
  const firstPolicy = firstResponse?.headers()["content-security-policy"];
  expect(firstPolicy).toBeTruthy();

  const nonce = await page.locator("script[nonce]").first().evaluate((script) => (script as HTMLScriptElement).nonce);
  expect(nonce).toBeTruthy();
  expect(firstPolicy).toContain(`'nonce-${nonce}'`);
  expect(firstResponse?.headers()["x-content-type-options"]).toBe("nosniff");
  expect(firstResponse?.headers()["referrer-policy"]).toBe("strict-origin-when-cross-origin");
  expect(firstResponse?.headers()["permissions-policy"]).toContain("camera=()");

  const secondResponse = await page.reload();
  const secondPolicy = secondResponse?.headers()["content-security-policy"];
  expect(secondPolicy).toBeTruthy();
  expect(secondPolicy).not.toBe(firstPolicy);
});
