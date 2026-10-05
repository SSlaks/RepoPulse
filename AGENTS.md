# Agent Operating Rules

## 模型分工

- Sol（gpt-6.1-sol）负责复杂规划、难题攻关，以及最后的测试、审查与完善。
- DeepSeek Flash（deepseek-v4.1-flash）负责较为重复的执行任务与代码撰写。
- 允许按上述职责委派子代理；同一文件的修改必须串行交接。
- 影响业务口径或架构的变更交回规划方（Sol）调整计划后再实施。
- 指定模型不可用时明确报告，不静默替换模型。
- 完成后汇总实现内容、测试结果及尚未验证的事项。

## Git 提交规范

- Git 提交信息的标题和正文使用简体中文；技术名称、文件路径及 `feat`、`fix`、`docs` 等类型前缀可保留英文。
- 标题简要说明本次变更，正文按需记录实现内容与验证结果。

## Long-running collection tasks

- After starting a long-running collection, build, sync, or migration task, report only startup, meaningful milestone progress, completion, or failure.
- Do not poll the task continuously or call the model repeatedly while waiting.
- Use a progress check interval of at least 5 minutes unless the user explicitly requests closer monitoring.
- Do not repeat unchanged status updates.
- Prefer tasks with explicit timeout, cancellation, retry, and failure states.
- At completion, cancellation, or failure, perform one final status check and report the result.

## 生产部署信息

- 唯一公开站点：https://repopulse.slak7.cn；Nginx 仅为该主机名提供 RepoPulse，其他 Host 会被拒绝
- 备用域名 `slak7.cn`、`www.slak7.cn` 不承载 RepoPulse；其 A 记录已于 2026-10-03 清理，不再解析到本机
- 服务器：腾讯云东京节点 43.153.181.59（AS132203 Tencent Cloud），Ubuntu + Nginx + Let's Encrypt
- 运行方式：Docker Compose 全栈；frontend 仅绑定 127.0.0.1:3000，由 Nginx 反代至 443
- 发布/备份/回滚：scripts/repopulse_ops.py（deploy / backup / restore / rollback）
- 生产环境变量：服务器上的 .env.production（不入库）
- 服务器与部署信息于 2026-09-27 核实；服务器或公开域名变更时更新本节。
