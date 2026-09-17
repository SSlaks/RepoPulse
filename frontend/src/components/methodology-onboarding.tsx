"use client";

import {
  ArrowRight,
  CalendarRange,
  Database,
  GitCompareArrows,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { METHODOLOGY_COOKIE } from "@/lib/preferences";

const METHODOLOGY_STEPS = [
  {
    icon: Database,
    title: "候选项目",
    copy: "综合 Trending、Search 与人工种子，并排除 fork、归档和停用仓库。",
  },
  {
    icon: CalendarRange,
    title: "每日快照",
    copy: "统一按 UTC 保存 Star、Fork 等聚合数据，不收集任何 Star 用户身份。",
  },
  {
    icon: GitCompareArrows,
    title: "周期增量",
    copy: "基线必须落在目标 UTC 日期且不晚于周期边界；没有严格基线就显示历史数据不足，不使用 36 小时回退。",
  },
  {
    icon: ShieldCheck,
    title: "稳定排名",
    copy: "依次比较净增长、增长率、当前 Star 与仓库名；已发布历史榜单不会回溯重算。",
  },
] as const;

export function MethodologyOnboarding({ openInitially }: { openInitially: boolean }) {
  const [isOpen, setIsOpen] = useState(openInitially);
  const startButtonRef = useRef<HTMLButtonElement>(null);

  const completeOnboarding = useCallback(() => {
    document.cookie = `${METHODOLOGY_COOKIE}=1; Max-Age=31536000; Path=/; SameSite=Lax`;
    setIsOpen(false);
  }, []);

  useEffect(() => {
    if (!isOpen) return;

    document.body.classList.add("onboarding-open");
    startButtonRef.current?.focus();

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") completeOnboarding();
      if (event.key === "Tab") {
        event.preventDefault();
        startButtonRef.current?.focus();
      }
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => {
      document.body.classList.remove("onboarding-open");
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [completeOnboarding, isOpen]);

  if (!isOpen) return null;

  return (
    <div className="onboarding-backdrop" role="presentation">
      <section
        className="onboarding-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="onboarding-title"
        aria-describedby="onboarding-description"
      >
        <div className="onboarding-orb onboarding-orb-one" aria-hidden="true" />
        <div className="onboarding-orb onboarding-orb-two" aria-hidden="true" />

        <div className="onboarding-heading">
          <span className="onboarding-kicker">
            <Sparkles size={14} /> 欢迎来到 RepoPulse
          </span>
          <h1 id="onboarding-title">先了解数据，再发现增长。</h1>
          <p id="onboarding-description">
            RepoPulse 关注一段时间内真实发生的 Star 净增长，而不是复刻 GitHub 未公开的 Trending 算法。
          </p>
        </div>

        <div className="onboarding-steps">
          {METHODOLOGY_STEPS.map((step, index) => {
            const Icon = step.icon;
            return (
              <article key={step.title}>
                <span className="onboarding-step-number">0{index + 1}</span>
                <span className="onboarding-step-icon" aria-hidden="true">
                  <Icon size={19} />
                </span>
                <div>
                  <h2>{step.title}</h2>
                  <p>{step.copy}</p>
                </div>
              </article>
            );
          })}
        </div>

        <div className="onboarding-footer">
          <p>
            <strong>核心口径</strong>
            <code>净增长 = 截止 Star − 起点 Star</code>
          </p>
          <button ref={startButtonRef} type="button" onClick={completeOnboarding}>
            现在开始 <ArrowRight size={17} aria-hidden="true" />
          </button>
        </div>
      </section>
    </div>
  );
}
