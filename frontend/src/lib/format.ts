const compactNumber = new Intl.NumberFormat("zh-CN", {
  notation: "compact",
  maximumFractionDigits: 1,
});

const fullNumber = new Intl.NumberFormat("zh-CN");

export function formatCompact(value: number): string {
  return compactNumber.format(value);
}

export function formatNumber(value: number): string {
  return fullNumber.format(value);
}

export function formatSignedNumber(value: number, compact = false): string {
  if (value === 0) return "0";
  const formatted = compact ? formatCompact(value) : formatNumber(value);
  return value > 0 ? `+${formatted}` : formatted;
}

export function formatPercent(value: number | null): string {
  if (value === null) return "--";
  return `${(value * 100).toFixed(value >= 0.1 ? 1 : 2)}%`;
}

export function formatDate(value: string | null, includeTime = false): string {
  if (!value) return "暂无";
  const normalizedValue = /(?:Z|[+-]\d{2}:\d{2})$/i.test(value)
    ? value
    : `${value}Z`;
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    ...(includeTime ? { hour: "2-digit", minute: "2-digit", hour12: false } : {}),
  }).format(new Date(normalizedValue));
}

export function rankMovement(rank: number, previousRank: number | null): number | null {
  return previousRank === null ? null : previousRank - rank;
}
