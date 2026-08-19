# ADR 0003：数据库队列 Worker 与持久化 SSE 事件

- 状态：Accepted
- 日期：2026-08-19

## 背景

旅行 Agent 会串行调用模型、天气、攻略和地图服务，单次运行可能持续几十秒。若在 HTTP 请求进程中用 FastAPI `BackgroundTasks` 执行，进程重启会丢任务，也无法由独立进程消费或稳定重放进度。

## 决策

- 保留 `POST /v1/agent/invoke` 作为同步开发接口。
- 会话化接口只写入 `PENDING` AgentRun，并立即返回 `202` 和 `run_id`。
- 独立 `python -m app.worker` 进程通过条件更新原子领取任务。
- LangChain Callback 将模型和 Tool 生命周期写入 `run_events`。
- SSE 读取持久化事件，事件序号作为 SSE `id`，支持 `Last-Event-ID` 断线续传。
- 本地默认 SQLite，数据库抽象通过 `DATABASE_URL` 为迁移 PostgreSQL 保留边界。

## 后果

API 与耗时执行生命周期解耦，前端刷新后仍可恢复进度；不需要 Redis 即可完成单机演示。但 SQLite 数据库队列不是多机生产队列，目前也没有任务租约、运行中 Worker 崩溃回收或指数退避。进入生产阶段前应迁移 PostgreSQL/Alembic，并根据吞吐量评估 Redis、RabbitMQ 或托管任务队列。
