"use client";

import { Search, SlidersHorizontal, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { fetchFilters } from "@/lib/api";
import { formatNumber } from "@/lib/format";
import type { FilterResponse, RankingFilters } from "@/lib/types";

const PAGE_SIZE_OPTIONS = [15, 25, 50];
const MIN_STAR_OPTIONS = [0, 100, 1000, 10000];

interface RankingFilterControlsProps {
  filters: RankingFilters;
  initialOptions: FilterResponse;
  initialError?: string;
  searchValue: string;
  onSearchValueChange: (value: string) => void;
  onChange: (changes: Partial<RankingFilters>) => void;
}

export function RankingFilterControls({ filters, initialOptions, initialError, searchValue, onSearchValueChange, onChange }: RankingFilterControlsProps) {
  const [options, setOptions] = useState(initialOptions);
  const [error, setError] = useState(initialError ?? "");
  const [loading, setLoading] = useState(false);
  const pending = useRef<AbortController | null>(null);

  useEffect(() => () => {
    pending.current?.abort();
    pending.current = null;
  }, []);

  async function retryOptions() {
    if (pending.current) return;
    const controller = new AbortController();
    pending.current = controller;
    setLoading(true);
    try {
      const payload = await fetchFilters(true, { signal: controller.signal });
      if (pending.current !== controller) return;
      setOptions(payload);
      setError("");
    } catch (cause) {
      if (pending.current === controller && !controller.signal.aborted) {
        setError(cause instanceof Error ? cause.message : "筛选选项加载失败，请重试。");
      }
    } finally {
      if (pending.current === controller) {
        pending.current = null;
        setLoading(false);
      }
    }
  }

  const selected = [
    { key: "language", label: "语言", value: filters.language },
    { key: "topic", label: "主题", value: filters.topic },
    { key: "minStars", label: "最低 Star", value: filters.minStars ? `${formatNumber(filters.minStars)}+ Star` : undefined },
    { key: "q", label: "搜索", value: filters.q },
  ] as const;
  const active = selected.filter((item) => item.value);
  const optionsUnavailable = Boolean(error) || loading;

  return (
    <>
      <div className="filter-bar">
        <div className="filter-title"><SlidersHorizontal size={16} />筛选</div>
        <select aria-label="编程语言" value={filters.language ?? ""} disabled={optionsUnavailable}
          onChange={(event) => onChange({ language: event.target.value || undefined, page: 1 })}>
          <option value="">全部语言</option>
          {filters.language && !options.languages.some((item) => item.value === filters.language)
            ? <option value={filters.language}>{filters.language}</option> : null}
          {options.languages.map((item) => <option key={item.value} value={item.value}>{item.label} ({item.count})</option>)}
        </select>
        <select aria-label="项目主题" value={filters.topic ?? ""} disabled={optionsUnavailable}
          onChange={(event) => onChange({ topic: event.target.value || undefined, page: 1 })}>
          <option value="">全部主题</option>
          {filters.topic && !options.topics.some((item) => item.value === filters.topic)
            ? <option value={filters.topic}>{filters.topic}</option> : null}
          {options.topics.map((item) => <option key={item.value} value={item.value}>{item.label} ({item.count})</option>)}
        </select>
        <select aria-label="最低 Star" value={filters.minStars ?? 0}
          onChange={(event) => onChange({ minStars: Number(event.target.value), page: 1 })}>
          {filters.minStars && !MIN_STAR_OPTIONS.includes(filters.minStars)
            ? <option value={filters.minStars}>{formatNumber(filters.minStars)}+ Star</option> : null}
          {MIN_STAR_OPTIONS.map((value) => <option key={value} value={value}>{value ? `${formatNumber(value)}+ Star` : "不限 Star"}</option>)}
        </select>
        <select aria-label="每页数量" value={filters.limit ?? 15}
          onChange={(event) => onChange({ limit: Number(event.target.value), page: 1 })}>
          {PAGE_SIZE_OPTIONS.map((size) => <option key={size} value={size}>每页 {size} 条</option>)}
        </select>
        <form className="search-form" onSubmit={(event) => {
          event.preventDefault();
          onChange({ q: searchValue.trim() || undefined, page: 1 });
        }}>
          <Search size={16} aria-hidden="true" />
          <input value={searchValue} onChange={(event) => onSearchValueChange(event.target.value)}
            placeholder="搜索仓库或简介" aria-label="搜索仓库或简介" />
          <button type="submit">搜索</button>
        </form>
      </div>
      {error || loading ? (
        <div className="filter-options-status" role={loading ? "status" : "alert"}>
          <div><strong>{loading ? "正在重试筛选选项…" : "筛选选项暂时不可用"}</strong>
            <p>{loading ? "语言与主题选项加载中，当前筛选条件已保留。" : error}</p></div>
          <button type="button" disabled={loading} onClick={() => void retryOptions()}>
            {loading ? "正在重试" : "重试筛选选项"}
          </button>
        </div>
      ) : null}
      {active.length ? (
        <div className="active-filters" role="group" aria-label="已选筛选条件">
          <span>已选条件</span>
          {active.map((item) => <button className="filter-chip" key={item.key} type="button"
            aria-label={`移除${item.label}：${item.value}`} title={`${item.label}：${item.value}`}
            onClick={() => onChange({ [item.key]: item.key === "minStars" ? 0 : undefined, page: 1 })}>
            <span>{item.label}：{item.value}</span><X size={13} aria-hidden="true" />
          </button>)}
          <button className="clear-filters" type="button" onClick={() => onChange({ language: undefined, topic: undefined, minStars: 0, q: undefined, page: 1 })}>清空筛选</button>
        </div>
      ) : null}
    </>
  );
}
