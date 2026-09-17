# Windows 运维演练

演练必须使用独立 Compose project、独立 env 文件和临时备份目录。不要把演练的
`--project-name` 改成生产项目名；这样可以避免触碰生产 PostgreSQL volume 和 Redis。

在 PowerShell 中执行：

```powershell
Copy-Item .env.example .env.ops-drill
$sha = (git rev-parse HEAD).Substring(0, 40)
$env:BACKEND_IMAGE = "repopulse-backend:$sha"
$env:FRONTEND_IMAGE = "repopulse-frontend:$sha"
$env:GIT_SHA = $sha

docker compose --env-file .env.ops-drill --project-name repopulse-drill -f docker-compose.yml config --quiet
docker compose --env-file .env.ops-drill --project-name repopulse-drill -f docker-compose.yml build --build-arg VCS_REF=$sha
docker compose --env-file .env.ops-drill --project-name repopulse-drill -f docker-compose.yml up -d postgres redis
docker compose --env-file .env.ops-drill --project-name repopulse-drill -f docker-compose.yml up --force-recreate --exit-code-from migrate migrate

py -3 scripts\repopulse_ops.py backup --env-file .env.ops-drill --project-name repopulse-drill --daily --backup-dir .\artifacts\drill-backups --keep 7
py -3 scripts\repopulse_ops.py restore --env-file .env.ops-drill --project-name repopulse-drill --dump .\artifacts\drill-backups\repopulse-daily-<timestamp>.dump --backup-dir .\artifacts\drill-backups\pre-restore --record-dir .\artifacts\drill-records
```

核对输出中的 `pg_restore --list`、SHA-256、Alembic 版本和三类记录数。恢复默认使用新
数据库名；如果要演练覆盖，必须改用一个只属于 `repopulse-drill` 的明确数据库名并同时
传入相同的 `--confirm-existing` 值。恢复完成后 API、Worker、Beat 保持停止，演练人员
应检查记录再手工启动 drill 服务。

演练结束时只清理这个独立 project 及其 volume：

```powershell
docker compose --env-file .env.ops-drill --project-name repopulse-drill -f docker-compose.yml down -v
Remove-Item -LiteralPath .env.ops-drill
```

生产演练前应在隔离主机复核 env、project name、备份目录和镜像 SHA。不要使用
`docker compose down -v` 清理生产 project；生产 volume 只允许按明确的恢复流程处理。
