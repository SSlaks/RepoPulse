import { z } from "zod";
import { findProvider, isModelSupported } from "./catalog";

export const MAX_README_CHARS = 100_000;
export const storedCredentialsSchema = z.object({
  provider: z.string().min(1).max(100),
  model: z.string().min(1).max(256),
  apiKey: z.string().trim().min(1).max(4096).regex(/^[\x21-\x7e]+$/),
}).strict();
export const credentialsSchema = storedCredentialsSchema.refine((value) => isModelSupported(value.provider, value.model));
export const modelListRequestSchema = storedCredentialsSchema.omit({ model: true })
  .refine((value) => Boolean(findProvider(value.provider)));
export const modelListResponseSchema = z.object({
  models: z.array(z.object({ id: z.string().min(1).max(256), name: z.string().min(1).max(256), supported: z.boolean() })).max(10000),
  source: z.enum(["vendor", "preset"]),
  queriedAt: z.string().datetime(),
});
export type ModelList = z.infer<typeof modelListResponseSchema>;
export type ModelListRequest = z.infer<typeof modelListRequestSchema>;
export const readmeModeSchema = z.enum(["summary", "translation"]);
export const readmeRequestSchema = credentialsSchema.safeExtend({
  markdown: z.string().min(1).max(MAX_README_CHARS).refine((value) => value.trim().length > 0),
  mode: readmeModeSchema.default("summary"),
});
export type AiCredentials = z.infer<typeof credentialsSchema>;
export type ReadmeMode = z.infer<typeof readmeModeSchema>;
export const summaryResultSchema = z.object({ mode: z.literal("summary"), summary: z.string().trim().min(1).max(1000) }).strict();
export const translationResultSchema = z.object({ mode: z.literal("translation"), translation: z.string().min(1).max(600000) }).strict();
export const resultSchema = z.discriminatedUnion("mode", [summaryResultSchema, translationResultSchema]);
export type SummaryResult = z.infer<typeof summaryResultSchema>;
export type TranslationResult = z.infer<typeof translationResultSchema>;
export type AiResult = SummaryResult | TranslationResult;
export const translationRecordSchema = z.object({
  repository: z.string().trim().min(1).max(512),
  translation: z.string().min(1).max(600000),
  sourceFingerprint: z.string().regex(/^[a-f0-9]{64}$/),
  generatedAt: z.string().datetime(),
  modelName: z.string().trim().min(1).max(256),
  imageBaseUrl: z.string().trim().min(1).max(2048),
}).strict();
export type TranslationRecord = z.infer<typeof translationRecordSchema>;
export const summaryRecordSchema = z.object({
  repository: z.string().trim().min(1).max(512),
  summary: z.string().trim().min(1).max(1000),
  sourceFingerprint: z.string().regex(/^[a-f0-9]{64}$/),
  generatedAt: z.string().datetime(),
  modelName: z.string().trim().min(1).max(256),
}).strict();
export type SummaryRecord = z.infer<typeof summaryRecordSchema>;
export const eventSchema = z.discriminatedUnion("type", [
  z.object({ type: z.literal("progress"), completed: z.number().nonnegative(), total: z.number().positive(), message: z.string(), indeterminate: z.boolean().optional() }),
  z.object({ type: z.literal("result"), result: resultSchema }),
  z.object({ type: z.literal("error"), error: z.object({ code: z.string(), message: z.string() }) }),
]);
export type AiEvent = z.infer<typeof eventSchema>;
