# Personal AI configuration

The settings page is `/settings/ai`. Credentials stay in the browser's `repopulse-ai-v1` localStorage entry. Only a user-initiated test, summary, or translation sends them to same-origin Next.js endpoints; they are forwarded to a fixed vendor endpoint and are never intentionally logged or persisted on the server. This is not encrypted browser storage. Use HTTPS and trusted devices in production. Vendor data retention policies still apply.

`POST /api/ai/test` accepts `{ provider, model, apiKey }`. `POST /api/ai/readme` additionally accepts `markdown` and `mode: "summary" | "translation"` (defaulting to `summary`) and returns newline-delimited JSON progress, a mode-specific result, or an error event. Summary mode returns only a concise Chinese project introduction with at most three key points. Translation mode returns only the complete translated Markdown. Failed validation uses a JSON error response. No arbitrary endpoints or model names are accepted. Qwen uses the mainland China endpoint and requires a matching API key.

Full translation preserves Markdown block order, copies standalone code/HTML/reference blocks verbatim, checks code and link destinations, and rejects empty/truncated responses. Documents are limited to 100,000 UTF-16 code units and individual translatable blocks to 16,000. The task deadline is 10 minutes; each upstream request has a 90-second timeout. Cancellation propagates to upstream fetch and an in-progress IndexedDB write. No automatic paid retries occur.

Successful translations are stored in the browser's `repopulse-readme-ai` IndexedDB database by full repository name. Each record contains the translated Markdown, source SHA-256 fingerprint, generation time, model display name, and Markdown image base URL; it never contains the API key. A restored record does not select the Chinese view automatically. Source changes mark the record as stale without deleting or regenerating it, and model changes do not invalidate it. A failed or cancelled retranslation preserves the previous record. If persistence fails, the new translation remains readable in the mounted page with an explicit warning.

Deployment must allow streaming responses and a 600-second request duration. Reverse proxies must not buffer NDJSON or record request bodies/credential headers. No database migration or Python worker changes are required.

## Model sources

Recommended presets and explicit per-model capabilities are maintained separately in `src/lib/ai/catalog.ts`; verify IDs, endpoint compatibility, parameters and regional availability before adding an adapted model. Discovery never expands the server compatibility registry. Official documentation consulted during implementation:

- OpenAI: https://developers.openai.com/api/docs/models/gpt-4.1-mini and https://developers.openai.com/api/docs/models/gpt-4.1
- DeepSeek: https://api-docs.deepseek.com/ and https://api-docs.deepseek.com/guides/thinking_mode/
- Gemini: https://ai.google.dev/gemini-api/docs/models and https://ai.google.dev/api/generate-content
- Claude: https://platform.claude.com/docs/en/models/overview
- Qwen: https://help.aliyun.com/zh/model-studio/qwen-api-reference

Availability depends on the user's vendor account. Tests use mocked vendor responses and do not establish live account access.

## Verification

Run `npm run lint`, `npm run typecheck`, and `npm run build` from `frontend`.
Run `npx playwright test --config playwright.ai.config.ts --project server` for isolated vendor/translation tests.
For the full suite, start the frontend on `127.0.0.1:3001` and a backend with demo data on port 8000, then run `npx playwright test --config playwright.ai.config.ts`. Browser tests mock only the AI responses; they exercise the real settings and README pages. Traces are disabled for this suite so credentials cannot accidentally appear in retained network traces. Test credentials are placeholders.

## Model discovery and compatibility (reviewed 2026-09-14)

`POST /api/ai/models` accepts `{ provider, apiKey }` and returns `{ models: [{ id, name, supported }], source: "vendor" | "preset", queriedAt: ISO8601 }`. The request uses the same origin, JSON validation and public error envelope as generation. Credentials are forwarded only in headers to fixed official hosts. Responses use `Cache-Control: no-store`. Queries have a 30-second total deadline, cancellation, no retries, a 100-page/10,000-model bound and repeated-cursor detection. An empty list is a successful empty discovery, not proof of unavailable account access.

- OpenAI: `GET https://api.openai.com/v1/models`; [reference](https://developers.openai.com/api/reference/resources/models/methods/list).
- DeepSeek: `GET https://api.deepseek.com/models`; [reference](https://api-docs.deepseek.com/api/list-models/).
- Claude: `GET https://api.anthropic.com/v1/models`, with `after_id` pagination; [reference](https://platform.claude.com/docs/en/api/models/list).
- Gemini: `GET https://generativelanguage.googleapis.com/v1beta/models`, with `pageToken` pagination; normalize the `models/` prefix; [reference](https://ai.google.dev/api/models).
- Qwen: mainland China presets only; no account discovery call. The API reports `source: preset` if explicitly requested.

All ten existing preset IDs were checked against the linked official sources. They remain a small supported subset, not a complete/current flagship catalogue. OpenAI GPT-4.1 models use Chat Completions with `store: false`; DeepSeek presets explicitly disable thinking; Gemini presets use generateContent with minimal thinking; Claude presets use Messages; Qwen Plus disables thinking, while Qwen Max omits that parameter. Token caps and parameter policies are explicit per model, not inferred from a model-name prefix. Changes require request-shape tests and documentation review; mocked tests establish adapter behavior, not live account access.

The settings page distinguishes adapted presets, vendor-returned models and a successful manual generation test. Unknown returned models are shown disabled. Presets missing from a query remain selectable for testing and are labelled as not returned. Queries never select a different model and do not trigger generation. Results remain in component memory only; editing a key, deleting a configuration or changing providers aborts discovery and discards late responses. Refresh failures leave presets available.

The `repopulse-ai-v1` storage shape is unchanged. Storage parsing accepts old model names, while save/test/translation enforce compatibility. An obsolete selection is retained with a reselection prompt; README generation directs the user to settings. No migration silently replaces a user's model or erases other provider configurations. Connection test success is cleared on credential or model changes and does not establish future availability.
