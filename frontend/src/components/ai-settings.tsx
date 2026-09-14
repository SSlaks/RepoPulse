"use client";

import Image from "next/image";
import Link from "next/link";
import { ArrowLeft, Check, Eye, EyeOff, LoaderCircle, Save, ShieldCheck, Trash2, Wifi, X } from "lucide-react";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { AiModelPicker } from "./ai-model-picker";
import { AI_PROVIDERS, findProvider } from "@/lib/ai/catalog";
import { credentialsSchema } from "@/lib/ai/contracts";
import { testAiConnection } from "@/lib/ai/client";
import { emptySettings, loadAiSettings, storeAiSettings, type AiSettings } from "@/lib/ai/storage";

const subscribe = () => () => undefined;
const clientSnapshot = () => true;
const serverSnapshot = () => false;

export function AiSettings({ returnTo }: { returnTo: string | null }) {
  const mounted = useSyncExternalStore(subscribe, clientSnapshot, serverSnapshot);
  return mounted ? <AiSettingsForm returnTo={returnTo} /> : <main className="shell ai-settings-page" aria-busy="true"><h1>大模型设置</h1><p>正在读取配置…</p></main>;
}

function AiSettingsForm({ returnTo }: { returnTo: string | null }) {
  const [initial] = useState(() => {
    try { return { settings: loadAiSettings(), error: "" }; }
    catch (cause) { return { settings: emptySettings(), error: (cause as Error).message }; }
  });
  const active = initial.settings.selected ? initial.settings.providers[initial.settings.selected] : null;
  const [settings, setSettings] = useState<AiSettings>(initial.settings);
  const [providerId, setProviderId] = useState<string>(active?.provider ?? AI_PROVIDERS[0].id);
  const provider = findProvider(providerId) ?? AI_PROVIDERS[0];
  const [apiKey, setApiKey] = useState(active?.apiKey ?? "");
  const [model, setModel] = useState<string>(active?.model ?? AI_PROVIDERS[0].models[0].id);
  const [pickerVersion, setPickerVersion] = useState(0);
  const [visible, setVisible] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState(initial.error);
  const pending = useRef<AbortController | null>(null);
  const drafts = useRef<Record<string, { apiKey: string; model: string }>>({});

  useEffect(() => () => pending.current?.abort(), []);

  function selectProvider(id: string) {
    pending.current?.abort(); pending.current = null; setBusy(false);
    drafts.current[providerId] = { apiKey, model };
    const selected = findProvider(id);
    if (!selected) return;
    const draft = drafts.current[id] ?? settings.providers[id];
    setPickerVersion((value) => value + 1);
    setProviderId(id); setApiKey(draft?.apiKey ?? ""); setModel(draft?.model ?? selected.models[0].id);
    setVisible(false); setError(""); setMessage("");
  }

  function save() {
    setMessage(""); setError("");
    const parsed = credentialsSchema.safeParse({ provider: providerId, model, apiKey });
    if (!parsed.success) { setError("请填写有效的 API Key 并选择模型。"); return; }
    const next = { selected: providerId, providers: { ...settings.providers, [providerId]: parsed.data } };
    try { storeAiSettings(next); setSettings(next); setMessage("已保存，当前模型将用于 README 总结翻译。"); }
    catch (cause) { setError((cause as Error).message); }
  }

  function remove() {
    setPickerVersion((value) => value + 1);
    const next = { ...settings, providers: { ...settings.providers } };
    delete next.providers[providerId];
    if (next.selected === providerId) next.selected = Object.keys(next.providers)[0] ?? null;
    try {
      storeAiSettings(next); setSettings(next); setApiKey(""); delete drafts.current[providerId];
      setMessage("已删除当前厂商配置。"); setError("");
    } catch (cause) { setError((cause as Error).message); }
  }

  async function test() {
    if (pending.current) return;
    const parsed = credentialsSchema.safeParse({ provider: providerId, model, apiKey });
    if (!parsed.success) { setError("请填写有效的 API Key 并选择模型。"); return; }
    const controller = new AbortController(); pending.current = controller;
    setBusy(true); setError(""); setMessage("");
    try {
      await testAiConnection(parsed.data, AbortSignal.any([controller.signal, AbortSignal.timeout(100_000)]));
      if (pending.current === controller) setMessage("连接成功，测试通过：当前密钥和模型已完成实际生成。");
    } catch (cause) {
      if (pending.current === controller) setError(controller.signal.aborted ? "已取消测试。" : (cause instanceof Error && cause.name !== "TypeError" ? cause.message : "连接失败，请检查网络或稍后重试。"));
    } finally { if (pending.current === controller) { pending.current = null; setBusy(false); } }
  }

  return <main className="shell ai-settings-page">
    {returnTo ? <Link className="back-link" href={returnTo}><ArrowLeft size={16} />返回项目 README</Link> : null}
    <h1>大模型设置</h1>
    <div className="ai-settings-layout">
      <aside className="ai-provider-list" aria-label="模型厂商">
        <h2>模型厂商</h2>
        {AI_PROVIDERS.map((item) => <button type="button" key={item.id} className={providerId === item.id ? "ai-provider selected" : "ai-provider"} aria-pressed={providerId === item.id} onClick={() => selectProvider(item.id)}>
          <span className="ai-provider-logo"><Image src={`/ai-providers/${item.logo}.svg`} alt="" width={28} height={28} /></span>
          <span><strong>{item.name}</strong><small>{settings.selected === item.id ? "当前使用" : settings.providers[item.id] ? "已配置" : "未配置"}</small></span>
          {providerId === item.id ? <Check size={16} /> : null}
        </button>)}
      </aside>
      <section className="ai-provider-config" aria-label={`${provider.name} 配置`}>
        <div className="ai-provider-heading"><span className="ai-provider-logo"><Image src={`/ai-providers/${provider.logo}.svg`} alt={`${provider.name} 标志`} width={32} height={32} /></span><h2>{provider.name}</h2></div>
        <form onSubmit={(event) => { event.preventDefault(); save(); }}>
          <fieldset disabled={busy}>
            <label htmlFor="ai-key">API Key</label>
            <div className="ai-key-input"><input id="ai-key" type={visible ? "text" : "password"} value={apiKey} onChange={(event) => { setApiKey(event.target.value); setPickerVersion((value) => value + 1); setMessage(""); setError(""); }} autoComplete="off" spellCheck={false} placeholder="输入你的 API Key" maxLength={4096} />
              <button type="button" className="ai-icon-button" onClick={() => setVisible(!visible)} aria-label={visible ? "隐藏密钥" : "显示密钥"} title={visible ? "隐藏密钥" : "显示密钥"}>{visible ? <EyeOff size={18} /> : <Eye size={18} />}</button></div>
            <AiModelPicker key={pickerVersion} providerId={provider.id} apiKey={apiKey} model={model} onChange={(value) => { setModel(value); setMessage(""); setError(""); }} />
            <div className="ai-config-actions"><button className="ai-button" type="button" onClick={test}><Wifi size={16} />测试连接</button><button className="ai-button ai-primary" type="submit"><Save size={16} />保存配置</button><button type="button" className="ai-icon-button ai-delete" onClick={remove} aria-label="删除当前厂商配置" title="删除当前厂商配置"><Trash2 size={17} /></button></div>
          </fieldset>
        </form>
        {busy ? <div className="ai-inline-status"><LoaderCircle className="ai-spin" size={16} />正在测试连接<button className="ai-button" onClick={() => pending.current?.abort()}><X size={14} />取消</button></div> : null}
        <p className="ai-cost-note">连接测试和总结翻译会调用所选模型，产生的费用由模型厂商收取。</p>
        {message ? <p className="ai-success" role="status">{message}</p> : null}
        {error ? <p className="ai-error" role="alert">{error}</p> : null}
      </section>
    </div>
    <p className="ai-privacy"><ShieldCheck size={18} /><span>密钥保存在当前浏览器；调用时经 RepoPulse 服务端临时转发，不写入服务端数据库或日志。请仅在可信设备上保存。</span></p>
  </main>;
}
