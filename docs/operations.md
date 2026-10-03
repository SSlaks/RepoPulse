# RepoPulse 运维操作

本文档描述生产部署、数据库备份/恢复和镜像回退。所有命令都固定使用主
`docker-compose.yml`；运维 CLI 会显式传入 `--env-file` 和 `--project-name`，不会自动
加载 `docker-compose.override.yml`。生产环境的 `TRUSTED_PROXY_TOKEN` 和
`INTERNAL_SERVICE_TOKEN` 必须使用相互独立、至少 32 个字符的随机值。开发环境由
override 文件提供固定的本地 token。

## 公网域名、Nginx 与证书

RepoPulse 只对 `repopulse.slak7.cn` 提供公网服务。`NEXT_PUBLIC_SITE_HOST` 必须只填写这一个
已解析到生产服务器的 DNS 主机名，不能填写 URL、多个域名或备用域名。Nginx 的 80 端口
默认 server 对不匹配的 Host 返回 444，443 端口默认 server 使用
`ssl_reject_handshake on` 拒绝未知 SNI 的 TLS 握手，并对其他 Host 的请求返回 444；仅目标主机名的 HTTP server 会重定向到 HTTPS，
仅目标主机名的 HTTPS server 会按模板中的代理超时、请求体限制、内部路径隔离和可信代理头
规则转发到 `127.0.0.1:3000`。`slak7.cn`、`www.slak7.cn` 等备用域名不配置重定向或应用
server；其 DNS 记录已清理（2026-10-03），不再指向生产服务器。

渲染 `deploy/nginx.conf.template` 时必须保留模板所示的受限变量列表，只替换
`${NEXT_PUBLIC_SITE_HOST}` 和 `${TRUSTED_PROXY_TOKEN}`，避免 `envsubst` 改写 Nginx 的
`$host`、`$request_uri`、`$remote_addr` 等运行时变量。渲染结果包含 token，权限应限制为
仅 Nginx 服务可读，并在 reload 前执行 `nginx -t`。

当前 Let's Encrypt 证书 lineage 为 `repopulse.slak7.cn`，Nginx 使用：

```text
/etc/letsencrypt/live/repopulse.slak7.cn/fullchain.pem
/etc/letsencrypt/live/repopulse.slak7.cn/privkey.pem
```

生产证书由 Certbot `standalone` authenticator 续期。现有
`/etc/letsencrypt/renewal-hooks/pre/10-stop-nginx` 和
`/etc/letsencrypt/renewal-hooks/post/90-start-nginx` 会在续期前后停止、启动 Nginx，以释放
HTTP-01 所需端口；应保留这两个钩子，并用 `certbot renew --dry-run` 检查续期链路和 Nginx
恢复情况。

更换公开域名时按以下顺序操作，避免配置引用不存在的证书或意外接管备用域名：

1. 先确认新域名的 DNS 已指向生产服务器，并确定它是唯一要服务的 Host。
2. 使用 standalone 模式为新域名单独签发证书；签发期间按现有停启方式释放端口，并确认
   `/etc/letsencrypt/live/<新域名>/fullchain.pem` 与 `privkey.pem` 已生成。
3. 将 `NEXT_PUBLIC_SITE_HOST` 改为新域名，仍使用受限 `envsubst` 列表重新渲染配置；执行
   `nginx -t` 成功后再 reload，并验证目标域名可用、未知 Host 仍被拒绝。
4. 确认新域名稳定后，再由用户单独决定旧域名 DNS 和旧证书 lineage 的清理；不要在同一
   次切换中为旧域名增加猜测性的重定向。

## 日常备份

备份是 PostgreSQL 16 custom-format 二进制归档，旁边会写入 JSON 元数据。元数据只含
数据库名、SHA-256、归档大小、镜像标识、Alembic 版本和 UTC 时间，不包含密码、连接 URL
或其他密钥。归档会先写入同目录临时文件，`pg_restore --list` 成功后再原子改名；失败
时不会留下可被误用的成功备份。

```bash
python3 scripts/repopulse_ops.py backup \
  --env-file .env.production \
  --project-name repopulse \
  --daily --backup-dir backups/daily --keep 7
```

Windows Task Scheduler 的“程序或脚本”填 `py`，参数填：

```text
-3 scripts\repopulse_ops.py backup --env-file .env.production --project-name repopulse --daily --backup-dir backups\daily --keep 7
```

Linux cron 示例（每天 UTC 02:15，仅成功的 daily 归档参与七份保留）：

```cron
15 2 * * * cd /srv/repopulse && flock -n /run/lock/repopulse-backup.lock /usr/bin/python3 scripts/repopulse_ops.py backup --env-file .env.production --project-name repopulse --daily --backup-dir backups/daily --keep 7 >>/var/log/repopulse-backup.log 2>&1
```

`deploy` 的前置备份和 `restore` 的恢复前备份不会执行 daily 清理，因此不会因为保留
策略删除事故前的恢复点。备份内容来自 PostgreSQL；浏览器 IndexedDB 中的 README 翻译
不会进入归档，头像缓存也不会进入归档。恢复后可由 Worker 的头像 warmup 任务重新构建
头像缓存，浏览器翻译需要在浏览器端重新生成或重新导入。

## 首次部署和发布

先准备同一个 Git SHA 构建的后端、前端镜像，例如
`repopulse-backend:0123456789abcdef...` 和 `repopulse-frontend:0123456789abcdef...`，再执行：

```bash
python3 scripts/repopulse_ops.py deploy 0123456789abcdef0123456789abcdef01234567 \
  --env-file .env.production \
  --project-name repopulse \
  --backend-image registry.example/repopulse-backend:0123456789abcdef0123456789abcdef01234567 \
  --frontend-image registry.example/repopulse-frontend:0123456789abcdef0123456789abcdef01234567 \
  --backup-dir backups/predeploy
```

部署会按以下顺序执行：停止 Beat；向 Worker 发送取消消费指令并读取本机 Worker 的
`active`、`reserved`、`scheduled` 数量；在超时前排空任务；写入不自动清理的
predeploy 备份；每次用 `--force-recreate` 执行一次 `migrate` 服务；最后用
`up -d --wait --force-recreate` 重新创建 API、Worker、Beat 和前端。Worker 排空超时会
中止发布并保留 Worker 运行，CLI 不会调用会在宽限期结束后发送 SIGKILL 的
`docker stop`。发布失败不会自动回滚镜像或删除数据库。

`migrate` 是 `restart: "no"` 的一次性服务，只在 PostgreSQL 健康后执行
`alembic upgrade head`。API 的 `/api/v1/ready` 会在数据库或 Redis 不可达、异常或超过
两秒时返回 503；前端 `/api/health` 只检查 Next 进程本身。

## README 预热与状态

README 正文和刷新状态位于 PostgreSQL 的 `repository_readmes`。Redis 只提供短 TTL 的
`readme:v2:*` 加速缓存、队列去重标记和任务锁；不要用 Redis 恢复 README，也不要为此清空
共享队列或修改全局 eviction/persistence。API 冷缺时只入队 Worker 并返回 `503`/`Retry-After`，
不会在访客请求里同步访问 GitHub。已有正文在 Redis 不可用时仍从数据库返回，后台失败也不删除
旧正文。发现仓库变为 private 后，Worker 会隐藏旧正文并清理 v2 key。

Beat 默认每 10 分钟投递一个最多 100 条的 README 批次；Worker 会在快照/发现任务活跃、配额
低于保留值、任务超时或无法取得 Redis 锁时保存 `waiting` 和 `resume_at` 后退出。历史失联的
`running`/`waiting` 任务按当前时间和启动时间判断，不会永久阻塞 README。GitHub 配额可能与
同一账号的其他应用共享，多个配置 Token 也不构成独立额度。

运维查看覆盖率、预热或取消任务：

```bash
docker compose exec -T worker python -m worker.app.readme_admin status
docker compose exec -T worker python -m worker.app.readme_admin warmup --limit 100
docker compose exec -T worker python -m worker.app.readme_admin cancel --job-key '<job-key>'
```

`status` 输出仓库总数、已有正文、缺失正文、private/隐藏数量、最近任务和最早
`resume_at`。`cancel` 只设置持久化的取消标记，Worker 在下一次边界退出；周期 Beat 会在
之后按到期状态继续安排任务。部署新版本时先完成备份，再执行 `alembic upgrade head`，确认
`repository_readmes` 已创建后再启动 Worker/Beat。

## 恢复到新库

不传 `--target-db` 时，CLI 会生成一个新的 `repopulse_restore_...` 数据库名。恢复前会
停止 Beat、排空并停止 Worker、停止 API，并先生成恢复前备份；恢复失败时这些服务保持停止，
不会自动打开流量。恢复不会恢复 Redis 队列，避免把过期任务重新注入线上。

```bash
python3 scripts/repopulse_ops.py restore \
  --env-file .env.production \
  --project-name repopulse \
  --dump backups/daily/repopulse-daily-20260916T021500Z.dump \
  --backup-dir backups/pre-restore \
  --record-dir backups/restore-records
```

恢复完成后会检查 `pg_restore --list`、`alembic_version` 以及
`repositories`、`repo_snapshots`、`ranking_runs` 记录数，并写入只含验证信息的恢复记录。
需要切换应用连接到新库时，先审阅验证记录，再按发布流程显式切换配置和流量。

## 覆盖已有库

覆盖已有数据库必须同时提供目标库名和目标库名本身作为确认值。目标库会在恢复前先
备份，API 停止后才会删除并重建；目标库名不允许使用 shell 或 SQL 标识符之外的字符。

```bash
python3 scripts/repopulse_ops.py restore \
  --env-file .env.production \
  --project-name repopulse \
  --dump backups/daily/repopulse-daily-20260916T021500Z.dump \
  --target-db repopulse \
  --confirm-existing repopulse \
  --backup-dir backups/pre-restore
```

## 镜像回退

回退只切换后端和前端镜像，不执行 Alembic downgrade、不删除旧镜像，也不改动用户真实
数据库。CLI 会用目标旧镜像读取 live database 的 Alembic current/heads；操作者还必须
显式传入 `--schema-compatible`，确认已经完成旧应用与当前 schema 的兼容性审阅。

```bash
python3 scripts/repopulse_ops.py rollback 0123456789abcdef0123456789abcdef01234567 \
  --env-file .env.production \
  --project-name repopulse \
  --backend-image registry.example/repopulse-backend:0123456789abcdef0123456789abcdef01234567 \
  --frontend-image registry.example/repopulse-frontend:0123456789abcdef0123456789abcdef01234567 \
  --schema-compatible
```

回退前同样会停止 Beat、排空 Worker 并停止 API；兼容性检查或新容器健康等待失败时，
服务保持停止，旧镜像仍保留供再次操作。
