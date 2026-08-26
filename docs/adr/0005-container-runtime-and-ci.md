# ADR 0005：容器运行时与 PostgreSQL CI

- 状态：已接受
- 日期：2026-08-26

## 背景

Agent 运行时包含 FastAPI、异步 Worker、Alembic 迁移和 PostgreSQL。只容器化数据库仍要求开发者手工安装依赖、按正确顺序启动进程，也无法证明 PostgreSQL 特有的任务领取逻辑在持续集成中真实执行。

## 决策

1. 使用一个锁定依赖、非 root 运行的应用镜像，通过不同 command 分别承担 migration、API 和 Worker 角色。
2. Compose 等待 PostgreSQL 健康，再运行一次性 migration；只有 migration 成功退出，API 与 Worker 才能启动。
3. API 提供容器健康检查；Worker 依靠 restart policy 和结构化启动日志暴露运行状态。
4. 密钥和连接串只从运行时环境注入，构建上下文排除 `.env`、测试缓存、Git 元数据和本地资源。
5. GitHub Actions 使用数据库名以 `_test` 结尾的 PostgreSQL service container，先验证迁移，再分别执行普通测试与 PostgreSQL 集成测试，最后构建应用镜像。

## 结果

- 本地可以用一条 Compose 命令启动完整运行栈，部署顺序可重复。
- API、Worker 和 migration 使用同一构建产物，降低环境漂移。
- PostgreSQL 的 `FOR UPDATE SKIP LOCKED` 并发领取测试在 CI 中不会因缺少连接串而跳过。
- migration 是部署门禁；失败时应用不会在不兼容的 Schema 上启动。
- Compose 面向本地开发和单机演示，不替代生产环境的编排、Secret 管理、TLS、备份和滚动发布能力。
