# 访问限流与租约

RepoPulse 的回源接口使用 Redis 中央协调器。每个请求在实际回源前取得一个短租约，
请求结束、取消或租约失效时释放。Redis Lua 脚本在同一事务内清理过期租约、检查滑动窗口
和并发数，因此多个 API 或 Next 实例不会各自放大额度。

| policy | 单个身份窗口额度 | 单个身份并发 | 全局并发 | 租约 |
| --- | ---: | ---: | ---: | ---: |
| `readme` | 30 次/60 秒 | 2 | 8 | 60 秒 |
| `ai_generate` | 6 次/60 秒 | 1 | 4 | 60 秒 |
| `ai_probe` | 20 次/60 秒 | 2 | 4 | 60 秒 |

`/internal/limits/acquire`、`renew`、`release` 是只供 Next 服务调用的 FastAPI 内部接口，
要求 `X-Internal-Service-Token`。有访问者上下文时还必须携带由反向代理覆盖的
`X-RepoPulse-Client-IP` 和 `X-RepoPulse-Proxy-Token`；没有访问者上下文的受信内部调用使用
独立的 `internal` 身份额度。`X-Forwarded-For` 和 `X-Real-IP` 不参与身份判断。无论采用哪种
身份，内部调用仍受全局并发上限约束。租约 owner 使用随机 token，续租不会复活已过期 owner，
释放其他 owner 是幂等的。

超限返回 `429` 和整数秒 `Retry-After`，同时返回 `Cache-Control: no-store`。Redis 不可用
时需要返回 `503`，不要在应用内存中降级为另一套额度。README 先读取已有缓存，只有缓存未命中
才申请租约；AI 在创建流之前申请租约，流结束或取消后释放。前端不会自动重试付费的模型请求。

生产 Nginx 配置应使用 [nginx.conf.template](../deploy/nginx.conf.template) 并在部署时通过
`envsubst` 写入最终配置。模板会覆盖专用身份头并清空来客的转发头；API 和内部协调器只应
通过前端容器的内部网络访问。生产环境必须设置相互独立且至少 32 个字符的
`TRUSTED_PROXY_TOKEN` 与 `INTERNAL_SERVICE_TOKEN`。缺少可信身份时，新的回源请求返回
`503`，不会降级成服务端或容器 IP。
