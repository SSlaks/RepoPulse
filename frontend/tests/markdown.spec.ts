import { expect, test } from "@playwright/test";

import { resolveMarkdownImageUrl, resolveMarkdownLinkUrl } from "../src/components/markdown-content";

test("README 相对链接解析到 GitHub README 所在目录", () => {
  const baseUrl = "https://github.com/acme/project/blob/main/docs/README.md";

  expect(resolveMarkdownLinkUrl("README.en.md", baseUrl)).toBe(
    "https://github.com/acme/project/blob/main/docs/README.en.md",
  );
  expect(resolveMarkdownLinkUrl("../CONTRIBUTING.md", baseUrl)).toBe(
    "https://github.com/acme/project/blob/main/CONTRIBUTING.md",
  );
});

test("README 链接保留绝对 HTTP(S) 地址和页面内锚点", () => {
  const baseUrl = "https://github.com/acme/project/blob/main/README.md";

  expect(resolveMarkdownLinkUrl("http://example.com/docs", baseUrl)).toBe("http://example.com/docs");
  expect(resolveMarkdownLinkUrl("https://example.com/docs", baseUrl)).toBe("https://example.com/docs");
  expect(resolveMarkdownLinkUrl("#installation", baseUrl)).toBe("#installation");
});

test("README 链接拒绝不安全协议", () => {
  const baseUrl = "https://github.com/acme/project/blob/main/README.md";

  expect(resolveMarkdownLinkUrl("javascript:alert(1)", baseUrl)).toBeUndefined();
  expect(resolveMarkdownLinkUrl("data:text/html,unsafe", baseUrl)).toBeUndefined();
});

test("README 图片解析为可访问的 GitHub raw 地址", () => {
  const baseUrl = "https://github.com/acme/project/blob/main/docs/README.md";

  expect(resolveMarkdownImageUrl("../assets/logo.png", baseUrl)).toBe(
    "https://github.com/acme/project/raw/main/assets/logo.png",
  );
  expect(resolveMarkdownImageUrl("https://github.com/acme/project/blob/main/logo.png", baseUrl)).toBe(
    "https://github.com/acme/project/raw/main/logo.png",
  );
  expect(resolveMarkdownImageUrl("javascript:alert(1)", baseUrl)).toBeUndefined();
});
