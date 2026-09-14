export const AI_PROVIDERS = [
  { id: "deepseek", name: "DeepSeek", logo: "deepseek", models: [
    { id: "deepseek-flash", name: "DeepSeek Flash" },
    { id: "deepseek-v4-pro", name: "DeepSeek V4 Pro" },
  ] },
  { id: "openai", name: "OpenAI", logo: "openai", models: [
    { id: "gpt-4.1-mini", name: "GPT-4.1 mini" },
    { id: "gpt-4.1", name: "GPT-4.1" },
  ] },
  { id: "gemini", name: "Gemini", logo: "gemini", models: [
    { id: "gemini-3.1-flash-lite", name: "Gemini 3.1 Flash-Lite" },
    { id: "gemini-3.5-flash", name: "Gemini 3.5 Flash" },
  ] },
  { id: "claude", name: "Claude", logo: "claude", models: [
    { id: "claude-haiku-4-5-20251001", name: "Claude Haiku 4.5" },
    { id: "claude-sonnet-5", name: "Claude Sonnet 5" },
  ] },
  { id: "qwen", name: "通义千问", logo: "qwen", models: [
    { id: "qwen-plus", name: "Qwen Plus（中国内地）" },
    { id: "qwen-max", name: "Qwen Max（中国内地）" },
  ] },
] as const;

export type ProviderId = typeof AI_PROVIDERS[number]["id"];
export function findProvider(id: string) {
  return AI_PROVIDERS.find((provider) => provider.id === id);
}
export function isModelSupported(provider: string, model: string): boolean {
  return Boolean(findModelCapability(provider, model));
}

export const PROVIDER_DOCS: Record<ProviderId, string> = {
  openai: "https://developers.openai.com/api/docs/models/all",
  deepseek: "https://api-docs.deepseek.com/api/list-models/",
  gemini: "https://ai.google.dev/gemini-api/docs/models",
  claude: "https://platform.claude.com/docs/en/models/overview",
  qwen: "https://help.aliyun.com/zh/model-studio/qwen-api-reference",
};

type ModelCapability = {
  api: "chat" | "messages" | "generateContent";
  maxOutputTokens: number;
  parameters: Record<string, unknown>;
  docs: string;
};

// Explicitly reviewed IDs only. Vendor discovery never expands this compatibility registry.
const MODEL_CAPABILITIES: Record<ProviderId, Record<string, ModelCapability>> = {
  openai: {
    "gpt-4.1-mini": { api: "chat", maxOutputTokens: 32768, parameters: { store: false }, docs: "https://developers.openai.com/api/docs/models/gpt-4.1-mini" },
    "gpt-4.1": { api: "chat", maxOutputTokens: 32768, parameters: { store: false }, docs: "https://developers.openai.com/api/docs/models/gpt-4.1" },
  },
  deepseek: {
    "deepseek-flash": { api: "chat", maxOutputTokens: 12000, parameters: { thinking: { type: "disabled" } }, docs: "https://api-docs.deepseek.com/guides/thinking_mode/" },
    "deepseek-v4-pro": { api: "chat", maxOutputTokens: 12000, parameters: { thinking: { type: "disabled" } }, docs: "https://api-docs.deepseek.com/guides/thinking_mode/" },
  },
  gemini: {
    "gemini-3.1-flash-lite": { api: "generateContent", maxOutputTokens: 65536, parameters: { thinkingConfig: { thinkingLevel: "minimal" } }, docs: "https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite" },
    "gemini-3.5-flash": { api: "generateContent", maxOutputTokens: 65536, parameters: { thinkingConfig: { thinkingLevel: "minimal" } }, docs: "https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash" },
  },
  claude: {
    "claude-haiku-4-5-20251001": { api: "messages", maxOutputTokens: 64000, parameters: {}, docs: PROVIDER_DOCS.claude },
    "claude-sonnet-5": { api: "messages", maxOutputTokens: 128000, parameters: {}, docs: PROVIDER_DOCS.claude },
  },
  qwen: {
    "qwen-plus": { api: "chat", maxOutputTokens: 8192, parameters: { enable_thinking: false }, docs: PROVIDER_DOCS.qwen },
    "qwen-max": { api: "chat", maxOutputTokens: 8192, parameters: {}, docs: PROVIDER_DOCS.qwen },
  },
};

export function findModelCapability(provider: string, model: string): ModelCapability | undefined {
  const known = findProvider(provider);
  if (!known) return undefined;
  const models = MODEL_CAPABILITIES[known.id];
  return Object.hasOwn(models, model) ? models[model] : undefined;
}
