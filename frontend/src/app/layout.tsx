import type { Metadata } from "next";
import { Inter, Noto_Sans_SC } from "next/font/google";
import { headers } from "next/headers";
import Script from "next/script";

import { SiteFooter } from "@/components/site-footer";
import { SiteHeader } from "@/components/site-header";
import { THEME_STORAGE_KEY } from "@/lib/preferences";

import "./globals.css";

const inter = Inter({ subsets: ["latin"], variable: "--font-inter", display: "swap" });
const notoSansSC = Noto_Sans_SC({
  subsets: ["latin"],
  variable: "--font-noto-sans-sc",
  display: "swap",
});

const THEME_INITIALIZER = `
  (function () {
    var savedTheme = null;
    try {
      savedTheme = localStorage.getItem("${THEME_STORAGE_KEY}");
    } catch (error) {
      savedTheme = null;
    }
    var theme = savedTheme === "light" || savedTheme === "dark"
      ? savedTheme
      : (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
  })();
`;

export const metadata: Metadata = {
  metadataBase: new URL(process.env.NEXT_PUBLIC_SITE_URL ?? "http://localhost:3000"),
  title: { default: "RepoPulse · 开源项目增长榜", template: "%s · RepoPulse" },
  description: "追踪 GitHub 开源项目最近 1、7、14、30 天的 Star 净增长。",
  openGraph: {
    title: "RepoPulse · 开源项目增长榜",
    description: "用可解释的数据，发现正在增长的开源项目。",
    type: "website",
    locale: "zh_CN",
  },
};

export default async function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  const nonce = (await headers()).get("x-nonce") ?? undefined;

  return (
    <html lang="zh-CN" data-scroll-behavior="smooth" suppressHydrationWarning>
      <body className={`${inter.variable} ${notoSansSC.variable}`}>
        <SiteHeader />
        {children}
        <SiteFooter />
        <Script id="theme-initializer" nonce={nonce} strategy="beforeInteractive">
          {THEME_INITIALIZER}
        </Script>
      </body>
    </html>
  );
}
