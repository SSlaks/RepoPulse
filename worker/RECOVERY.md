# 抓取隔离与恢复运维

## 行为

- 每轮冻结应抓仓库；成功快照和请求明细持久化。同日重跑只抓缺失项。
- 404 每个 UTC 日期最多计数一次，三个日期且跨度至少 48 小时才允许隔离。
- 批量 403/404 达到 `max(10, ceil(应抓数 × 5%))` 时冻结隔离与失败确认计数。
- 网络/5xx 保留 1、2、4 秒重试；额外补抓最多两轮，等待 10、30 分钟。
- Beat 每 5 分钟恢复到期任务。限流等待服务端时间，持久化重试最多五次。
- 首次启动后六小时或 UTC 当天结束为截止时间；跨日不能补写历史实时快照。
- `partial` 表示仍有缺失，保留旧榜单；`failed` 表示认证、数据库等全局失败。
- 四周期榜单同一事务发布，失败独立重试，自动发布最多尝试五次。
- 隔离仓库按 1、3、7 天退避复查，之后每七天一次；临时失败一小时后再查。
- 复查每小时执行一次，每批最多 50 个，并发 1、每秒最多一次请求。
- 主抓取与隔离复查使用同一把分布式锁，避免同仓库并发写入；忙时等下一次调度。
- 复查成功保存当天快照、恢复 active，并从下一轮固定集合开始纳入。
- 缺失当天端点不入榜；目标日期无基线时标记 baseline_available=false。

## 首次部署

1. 确认 Worker 无执行中任务，停止 Beat。
2. 在后端工作目录执行 `alembic upgrade head`，再重新创建 API、Worker、Beat。
3. 首次默认 `REPOSITORY_AUTO_QUARANTINE=false`，记录 suspect 状态但不自动隔离。
4. 核对一个完整日周期：请求数、成功数、缺失原因、批量访问异常和凭据状态。
5. 核对完成后，在根目录 `.env` 设置 `REPOSITORY_AUTO_QUARANTINE=true`，运行
   `docker compose up -d --no-deps --force-recreate api worker beat`。

`REPOSITORY_PROBE_ENABLED=true` 控制复查，`SNAPSHOT_DEADLINE_HOURS=6` 控制截止时间。
`EXCLUDED_REPOSITORIES` 是逗号分隔、不区分大小写的人工排除清单；人工排除优先于复查。

## 状态与人工恢复

`job_runs.progress` 保存 `missing`、`failures`、`access_anomaly`、`deadline`、
`publication`、`publication_error`、`quota_retries` 等字段；`snapshot_requests` 保存逐仓库
错误和补抓次数。完成抓取不等于榜单发布成功，应检查 `publication=published`。
历史任务在迁移时不会自动重新抓取，也不会生成虚假的逐仓库成功记录。

人工解除隔离：

```powershell
docker compose exec -T worker python -m worker.app.repository_admin owner/name
```

该命令保留最后检查时间和错误，且不绕过人工排除项。隔离/复查可通过配置关闭，
不删除历史仓库和快照。不要在回退应用时对生产库运行删除新增数据的 downgrade。

采集状态检查间隔至少五分钟，仅报告启动、有效进展、完成或故障。
