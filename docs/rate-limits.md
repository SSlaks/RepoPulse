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

Docker Compose 开发 override 将前端服务设置为 `ENVIRONMENT=development`。本机直连前端时可以没有
可信代理身份，但只要配置了 `INTERNAL_SERVICE_TOKEN`，仍会调用同一个 Redis 协调器并使用独立的
`internal` 身份额度；该 token 只在服务端环境变量中使用。生产构建默认保持严格模式，生产或其他
显式部署环境缺少 token 时返回 `503`。当 `ENVIRONMENT` 未明确为 `development` 或 `test` 时，生产
构建也会要求可信代理身份。单独运行 `next dev` 且未设置部署环境时才保留本地开发回退。

超限返回 `429` 和整数秒 `Retry-After`，同时返回 `Cache-Control: no-store`。Redis 不可用
时需要返回 `503`，不要在应用内存中降级为另一套额度。README 先读取已有缓存，只有缓存未命中
才申请租约；AI 在创建流之前申请租约，流结束或取消后释放。前端不会自动重试付费的模型请求。

README Worker 还有一层 GitHub core 配额保护：每批先探测真实 `rate_limit`，并在每个 HTTP
请求前保留 `README_QUOTA_RESERVE`（默认 1000）和低速单并发间隔。配额、二级限流和其他应用
可能共享同一账号 Token；这些保护不代表 RepoPulse 拥有独立或额外的 GitHub 配额。等待状态
写入 `job_runs.progress.resume_at`，任务退出后由 Beat/恢复调度续跑，不在进程内无限 sleep。

生产 Nginx 配置应使用 [nginx.conf.template](../deploy/nginx.conf.template) 并在部署时通过
`envsubst` 写入最终配置。模板会覆盖专用身份头并清空来客的转发头；API 和内部协调器只应
通过前端容器的内部网络访问。生产环境必须设置相互独立且至少 32 个字符的
`TRUSTED_PROXY_TOKEN` 与 `INTERNAL_SERVICE_TOKEN`。缺少可信身份时，新的回源请求返回
`503`，不会降级成服务端或容器 IP。
