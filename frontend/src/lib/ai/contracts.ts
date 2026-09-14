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
export const readmeRequestSchema = credentialsSchema.safeExtend({
  markdown: z.string().min(1).max(MAX_README_CHARS).refine((value) => value.trim().length > 0),
});
export type AiCredentials = z.infer<typeof credentialsSchema>;
export const resultSchema = z.object({ summary: z.string().trim().min(1).max(12000), translation: z.string().min(1).max(600000) });
export type AiResult = z.infer<typeof resultSchema>;
export const eventSchema = z.discriminatedUnion("type", [
  z.object({ type: z.literal("progress"), completed: z.number(), total: z.number(), message: z.string() }),
  z.object({ type: z.literal("result"), result: resultSchema }),
  z.object({ type: z.literal("error"), error: z.object({ code: z.string(), message: z.string() }) }),
]);
export type AiEvent = z.infer<typeof eventSchema>;
