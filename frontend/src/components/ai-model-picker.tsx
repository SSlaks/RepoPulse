"use client";

import { useEffect, useRef, useState } from "react";
import { findProvider, isModelSupported, PROVIDER_DOCS, type ProviderId } from "@/lib/ai/catalog";
import { modelListRequestSchema, type ModelList } from "@/lib/ai/contracts";
import { fetchAiModels } from "@/lib/ai/client";

export function AiModelPicker({ providerId, apiKey, model, onChange }: {
  providerId: ProviderId; apiKey: string; model: string; onChange: (model: string) => void;
}) {
  const [result, setResult] = useState<ModelList | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const pending = useRef<AbortController | null>(null);
  useEffect(() => () => { pending.current?.abort(); pending.current = null; }, []);
  const provider = findProvider(providerId);
  if (!provider) return null;
  const supported = provider.models.filter((item) => isModelSupported(providerId, item.id));
  const other = result?.models.filter((item) => !isModelSupported(providerId, item.id) && item.id !== model) ?? [];
  const returned = new Set(result?.models.map((item) => item.id));

  async function refresh() {
    if (pending.current) return;
    const parsed = modelListRequestSchema.safeParse({ provider: providerId, apiKey });
    if (!parsed.success) { setError("请先填写有效的 API Key。"); return; }
    const controller = new AbortController(); pending.current = controller;
    setBusy(true); setError(""); setResult(null);
    try {
      const next = await fetchAiModels(parsed.data, AbortSignal.any([controller.signal, AbortSignal.timeout(35_000)]));
      if (pending.current === controller) setResult(next);
    } catch (cause) {
      if (pending.current === controller) setError(controller.signal.aborted ? "已取消模型查询。" : cause instanceof Error && cause.name !== "TypeError" ? cause.message : "获取模型列表失败，请稍后重试。");
    } finally {
      if (pending.current === controller) { pending.current = null; setBusy(false); }
    }
  }

  return <div className="ai-model-picker">
    <p className="ai-cost-note">这里配置厂商 API 模型，列表不等同于官网聊天产品的模型选择器。<a href={PROVIDER_DOCS[providerId]} target="_blank" rel="noreferrer">查看官方模型文档</a></p>
    <label htmlFor="ai-model">选择模型</label>
    <select id="ai-model" value={model} onChange={(event) => onChange(event.target.value)}>
      {!isModelSupported(providerId, model) ? <option value={model} disabled>{model}（需重新选择）</option> : null}
      <optgroup label="已适配模型">
        {supported.map((item) => <option key={item.id} value={item.id}>{item.name}{result ? returned.has(item.id) ? " · 厂商已返回" : " · 本次未返回" : " · 推荐预设"}</option>)}
      </optgroup>
      {other.length ? <optgroup label="其他厂商模型（尚未适配）">{other.map((item) => <option key={item.id} value={item.id} disabled>{item.name} · {item.id}（未适配）</option>)}</optgroup> : null}
    </select>
    <p className="ai-cost-note">预设和厂商返回均不代表当前账号一定可调用；请手动测试连接。未适配模型暂不可选择。</p>
    {!isModelSupported(providerId, model) ? <p className="ai-error">已保存的模型需重新选择；密钥和其他厂商配置仍保留。</p> : null}
    {providerId === "qwen" ? <p className="ai-cost-note">来源：官方文档维护的中国内地预设。本次未查询账号模型列表。</p> : <div className="ai-model-refresh">
      <button type="button" className="ai-button" disabled={busy} onClick={refresh}>{busy ? "正在获取模型列表…" : "获取模型列表"}</button>
      {busy ? <button type="button" className="ai-button" onClick={() => pending.current?.abort()}>取消查询</button> : null}
      {result ? <p className="ai-cost-note" role="status">来源：厂商 API · 查询时间：{new Date(result.queriedAt).toLocaleString()} · {result.models.length ? `返回 ${result.models.length} 个模型，尚未验证实际调用。` : "未返回模型，仍可使用预设进行测试。"}</p> : null}
      {error ? <p className="ai-error" role="alert">{error} 已保留推荐预设。</p> : null}
    </div>}
  </div>;
}
