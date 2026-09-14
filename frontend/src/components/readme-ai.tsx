"use client";

import Link from "next/link";
import { CalendarClock, Languages, LoaderCircle, RefreshCw, Settings, Sparkles, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { MarkdownContent } from "@/components/markdown-content";
import { findProvider } from "@/lib/ai/catalog";
import { requestReadme } from "@/lib/ai/client";
import { MAX_README_CHARS, type AiCredentials, type AiEvent, type ReadmeMode, type TranslationRecord } from "@/lib/ai/contracts";
import { fingerprintMarkdown, getReadmeTranslation, saveReadmeTranslation } from "@/lib/ai/readme-storage";
import { activeCredentials } from "@/lib/ai/storage";

const subscribeLocation = () => () => undefined;
const settingsLocation = () => `/settings/ai?returnTo=${encodeURIComponent(window.location.pathname + window.location.search + "#readme-heading")}`;
const serverSettingsLocation = () => "/settings/ai";

type BusyMode = ReadmeMode | null;
type StorageStatus = "loading" | "ready" | "error";
type PersistenceStatus = "saved" | "unsaved" | null;
type ProgressEvent = Extract<AiEvent, { type: "progress" }>;

interface ReadmeAiProps {
  markdown: string;
  imageBaseUrl: string;
  repository: string;
}

interface SummaryState {
  text: string;
  modelName: string;
}

interface PendingGeneration {
  id: number;
  mode: ReadmeMode;
  signature: string;
  controller: AbortController;
}

const initialProgress: ProgressEvent = { type: "progress", completed: 0, total: 1, message: "准备生成", indeterminate: true };

export function ReadmeAi({ markdown, imageBaseUrl, repository }: ReadmeAiProps) {
  const [summary, setSummary] = useState<SummaryState | null>(null);
  const [translation, setTranslation] = useState<TranslationRecord | null>(null);
  const [persistenceStatus, setPersistenceStatus] = useState<PersistenceStatus>(null);
  const [persistenceError, setPersistenceError] = useState("");
  const [sourceFingerprint, setSourceFingerprint] = useState<string | null>(null);
  const [translated, setTranslated] = useState(false);
  const [busy, setBusy] = useState<BusyMode>(null);
  const [error, setError] = useState("");
  const [missing, setMissing] = useState(false);
  const [modelLabel, setModelLabel] = useState("");
  const [storageStatus, setStorageStatus] = useState<StorageStatus>("loading");
  const [storageError, setStorageError] = useState("");
  const [progress, setProgress] = useState<ProgressEvent>(initialProgress);
  const settingsHref = useSyncExternalStore(subscribeLocation, settingsLocation, serverSettingsLocation);
  const pending = useRef<PendingGeneration | null>(null);
  const generationId = useRef(0);
  const loadId = useRef(0);

  const invalidatePendingWork = useCallback(() => {
    loadId.current++;
    generationId.current++;
    pending.current?.controller.abort();
    pending.current = null;
  }, []);

  const refreshCredentials = useCallback(() => {
    let credentials: AiCredentials | null = null;
    try { credentials = activeCredentials(); } catch { /* The generate action reports invalid saved settings. */ }
    const signature = credentialsSignature(credentials);
    const current = pending.current;
    if (current && current.signature !== signature) {
      current.controller.abort();
      pending.current = null;
      generationId.current++;
      setBusy(null);
      setError("模型配置已改变，已取消之前的生成。");
    }
    setModelLabel(credentials ? modelName(credentials) : "");
  }, []);

  const loadStoredTranslation = useCallback(async (clearRecord: boolean) => {
    const requestId = ++loadId.current;
    setStorageStatus("loading");
    setStorageError("");
    if (clearRecord) {
      setTranslation(null);
      setPersistenceStatus(null);
      setPersistenceError("");
      setSourceFingerprint(null);
    }
    try {
      const fingerprint = await fingerprintMarkdown(markdown);
      if (requestId !== loadId.current) return;
      setSourceFingerprint(fingerprint);
      const record = await getReadmeTranslation(repository);
      if (requestId !== loadId.current) return;
      setTranslation(record);
      setPersistenceStatus(record ? "saved" : null);
      setPersistenceError("");
      setStorageStatus("ready");
    } catch (cause) {
      if (requestId !== loadId.current) return;
      const message = cause instanceof Error ? cause.message : "无法读取翻译记录。";
      setStorageError(message);
      setStorageStatus("error");
    }
  }, [markdown, repository]);

  useEffect(() => {
    let active = true;
    queueMicrotask(() => { if (active) refreshCredentials(); });
    window.addEventListener("focus", refreshCredentials);
    window.addEventListener("storage", refreshCredentials);
    window.addEventListener("repopulse-ai-change", refreshCredentials);
    return () => {
      active = false;
      window.removeEventListener("focus", refreshCredentials);
      window.removeEventListener("storage", refreshCredentials);
      window.removeEventListener("repopulse-ai-change", refreshCredentials);
    };
  }, [refreshCredentials]);

  useEffect(() => {
    invalidatePendingWork();
    let active = true;
    queueMicrotask(() => {
      if (!active) return;
      setBusy(null);
      setSummary(null);
      setTranslated(false);
      setError("");
      void loadStoredTranslation(true);
    });
    return () => {
      active = false;
      invalidatePendingWork();
    };
  }, [invalidatePendingWork, loadStoredTranslation]);

  const isActive = (operation: PendingGeneration): boolean => (
    pending.current?.id === operation.id && generationId.current === operation.id && !operation.controller.signal.aborted
  );

  async function generate(mode: ReadmeMode, allowStorageFailure = false): Promise<void> {
    if (pending.current) return;
    if (mode === "translation" && storageStatus !== "ready" && !(allowStorageFailure && storageStatus === "error")) {
      setError(storageError || "翻译记录尚未读取完成，请重试读取记录后再翻译。");
      return;
    }
    if (mode === "translation" && !sourceFingerprint) {
      setError("原文版本尚未准备好，请重试读取记录。");
      return;
    }
    if (markdown.length > MAX_README_CHARS) {
      setError("README 超过 100,000 字符，暂不支持处理。");
      return;
    }
    let credentials: AiCredentials | null = null;
    try { credentials = activeCredentials(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "模型配置无效。"); setMissing(true); return; }
    if (!credentials) { setMissing(true); return; }

    const operation: PendingGeneration = {
      id: ++generationId.current,
      mode,
      signature: credentialsSignature(credentials),
      controller: new AbortController(),
    };
    pending.current = operation;
    const currentModelName = modelName(credentials);
    setMissing(false);
    setError("");
    setModelLabel(currentModelName);
    setBusy(mode);
    setProgress({ type: "progress", completed: 0, total: 1, message: "正在连接模型", indeterminate: true });
    try {
      const signal = AbortSignal.any([operation.controller.signal, AbortSignal.timeout(600_000)]);
      const generated = await requestReadme(credentials, markdown, mode, signal, (event) => {
        if (isActive(operation)) setProgress(event);
      });
      if (!isActive(operation)) return;
      if (mode === "summary" && generated.mode === "summary") {
        setSummary({ text: generated.summary, modelName: currentModelName });
        return;
      }
      if (mode === "translation" && generated.mode === "translation" && sourceFingerprint) {
        await finishTranslation(operation, generated.translation, currentModelName, sourceFingerprint);
      }
    } catch (cause) {
      if (!isActive(operation)) return;
      setError(generationError(operation.controller, cause));
    } finally {
      if (isActive(operation)) {
        pending.current = null;
        setBusy(null);
      }
    }
  }

  async function finishTranslation(operation: PendingGeneration, translatedMarkdown: string, currentModelName: string, fingerprint: string): Promise<void> {
    const record: TranslationRecord = {
      repository,
      translation: translatedMarkdown,
      sourceFingerprint: fingerprint,
      generatedAt: new Date().toISOString(),
      modelName: currentModelName,
      imageBaseUrl,
    };
    setProgress((current) => ({ ...current, message: "正在保存翻译记录" }));
    try {
      await saveReadmeTranslation(record, operation.controller.signal);
      if (isActive(operation)) {
        setTranslation(record);
        setTranslated(true);
        setPersistenceStatus("saved");
        setPersistenceError("");
      }
    } catch (cause) {
      if (isActive(operation)) {
        setTranslation(record);
        setTranslated(true);
        setPersistenceStatus("unsaved");
        setPersistenceError(`译文已生成，但翻译记录保存失败：${cause instanceof Error ? cause.message : "请检查浏览器存储权限。"}`);
      }
    }
  }

  function handleTranslationTab(): void {
    if (translation) { setTranslated(true); return; }
    if (busy) return;
    if (storageStatus === "error") { setError(storageError || "无法读取翻译记录，请先重试读取记录。"); return; }
    void generate("translation");
  }

  function cancelGeneration(): void {
    if (!pending.current) return;
    pending.current.controller.abort();
    pending.current = null;
    generationId.current++;
    setBusy(null);
    setError("已取消生成。");
  }

  const staleTranslation = Boolean(translation && sourceFingerprint && translation.sourceFingerprint !== sourceFingerprint);
  const content = translated && translation ? translation.translation : markdown;
  const contentImageBaseUrl = translated && translation ? translation.imageBaseUrl : imageBaseUrl;
  const summaryModel = summary?.modelName || modelLabel || "当前模型";

  return (
    <>
      <div className="readme-ai-toolbar">
        <div className="ai-readme-tabs" aria-label="文档语言">
          <button className={!translated ? "selected" : ""} aria-pressed={!translated} onClick={() => setTranslated(false)}>原文</button>
          <button disabled={storageStatus === "loading" || (Boolean(busy) && !translation)} className={translated ? "selected" : ""} aria-pressed={translated} onClick={handleTranslationTab}><Languages size={15} />中文译文</button>
        </div>
        <button className="ai-button ai-primary" disabled={Boolean(busy) || !markdown.trim()} onClick={() => void generate("summary")}>
          {busy === "summary" ? <LoaderCircle className="ai-spin" size={16} /> : <Sparkles size={16} />}
          {busy === "summary" ? "生成中" : summary ? "重新总结" : "总结翻译"}
        </button>
        {translation ? <button className="ai-button" disabled={Boolean(busy)} onClick={() => void generate("translation", storageStatus === "error")}><RefreshCw size={15} />重新翻译</button> : null}
        <Link className="ai-icon-button" href={settingsHref} aria-label="模型设置" title="模型设置"><Settings size={17} /></Link>
      </div>
      {missing ? <p className="ai-readme-notice" role="status">请先配置模型，再发起 AI 操作。<Link href={settingsHref}>前往设置</Link></p> : null}
      {storageStatus === "error" && !translation ? <div className="ai-readme-notice ai-readme-storage-error" role="alert"><span>翻译记录读取失败：{storageError || "请稍后重试。"}</span><div><button className="ai-button" disabled={Boolean(busy)} onClick={() => void loadStoredTranslation(false)}>重试读取记录</button><button className="ai-button" disabled={Boolean(busy)} onClick={() => void generate("translation", true)}>继续翻译（仅本页）</button></div></div> : null}
      {staleTranslation ? <p className="ai-readme-notice" role="status">原文已更新，可重新翻译。当前显示的译文来自 {formatGeneratedAt(translation?.generatedAt)}。</p> : null}
      {translation && !staleTranslation && persistenceStatus === "saved" ? <p className="ai-readme-record" role="status"><CalendarClock size={14} />已保存译文 · {translation.modelName} · {formatGeneratedAt(translation.generatedAt)}</p> : null}
      {translation && persistenceStatus === "unsaved" ? <p className="ai-readme-notice ai-readme-storage-warning" role="alert">{persistenceError || "当前译文仅保留在本页，尚未保存。"}</p> : null}
      {busy ? <ProgressCard mode={busy} progress={progress} onCancel={cancelGeneration} /> : null}
      {error ? <p className="ai-error ai-readme-notice" role="alert">{error}</p> : null}
      <article className="readme-content">
        {summary ? <section className="ai-summary" aria-label="AI 摘要"><div className="ai-summary-title"><strong><Sparkles size={17} />AI 项目摘要</strong><small>{summaryModel} · AI 生成，请结合原文核对</small></div><MarkdownContent content={summary.text} imageBaseUrl={imageBaseUrl} /></section> : null}
        <MarkdownContent content={content} imageBaseUrl={contentImageBaseUrl} />
      </article>
    </>
  );
}

function ProgressCard({ mode, progress, onCancel }: { mode: ReadmeMode; progress: ProgressEvent; onCancel: () => void }) {
  const isIndeterminate = progress.indeterminate || progress.message.includes("连接");
  const completed = Math.min(progress.total, Math.max(0, progress.completed));
  const percent = (completed / progress.total) * 100;
  return (
    <div className="ai-readme-progress-card" role="status" aria-live="polite" aria-busy="true">
      <div className="ai-readme-progress-icon" aria-hidden="true"><LoaderCircle className="ai-spin" size={20} /></div>
      <div className="ai-readme-progress-copy"><strong>{mode === "translation" ? "正在翻译 README" : "正在生成项目摘要"}</strong><span>{progress.message}</span></div>
      <div className="ai-readme-progress-meter">
        <div className={`ai-readme-progress-track${isIndeterminate ? " indeterminate" : ""}`} role="progressbar" aria-label={mode === "translation" ? "翻译进度" : "摘要生成进度"} aria-valuemin={0} aria-valuemax={progress.total} {...(isIndeterminate ? {} : { "aria-valuenow": completed })}>
          <span style={isIndeterminate ? undefined : { width: `${percent}%` }} />
        </div>
        <small>{isIndeterminate ? "正在等待模型响应" : `${completed} / ${progress.total}`}</small>
      </div>
      <button className="ai-button ai-readme-cancel" onClick={onCancel}><X size={15} />取消</button>
    </div>
  );
}

function credentialsSignature(credentials: AiCredentials | null): string {
  return credentials ? `${credentials.provider}\u0000${credentials.model}\u0000${credentials.apiKey}` : "";
}

function modelName(credentials: AiCredentials): string {
  return findProvider(credentials.provider)?.models.find((item) => item.id === credentials.model)?.name ?? credentials.model;
}

function generationError(controller: AbortController, cause: unknown): string {
  if (controller.signal.aborted) return "已取消生成。";
  if (cause instanceof Error && cause.name === "TimeoutError") return "生成超时，请稍后重试。";
  if (cause instanceof Error && cause.name !== "TypeError") return cause.message;
  return "网络连接失败，请稍后重试。";
}

function formatGeneratedAt(value: string | undefined): string {
  if (!value) return "此前";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "此前" : new Intl.DateTimeFormat("zh-CN", { dateStyle: "short", timeStyle: "short" }).format(date);
}
