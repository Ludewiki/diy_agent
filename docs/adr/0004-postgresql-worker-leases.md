# ADR 0004：PostgreSQL、Alembic 与 Worker 租约

- 状态：Accepted
- 日期：2026-08-20
- 影响：替代 ADR 0003 中“SQLite 作为默认运行时数据库”的部分，不改写其历史记录

## 背景

SQLite 足以支持单进程 Demo，但数据库级写锁、缺少 `SKIP LOCKED` 和弱化的并发语义不适合多个 API/Worker 实例。应用还需要可审查、可回滚的 Schema 演进，不能继续在启动时调用 `create_all()`。

## 决策

1. API 与 Worker 的正式运行时统一使用 PostgreSQL，驱动为 Psycopg 3。
2. Schema 由 Alembic 管理；应用启动只检查连接，不自动创建或修改表。
3. 主键使用 UUID，结构化输出和 SSE 数据使用 JSONB。
4. Worker 使用 `SELECT ... FOR UPDATE SKIP LOCKED` 并发领取任务。
5. Run 保存 `worker_id`、租约到期时间、心跳、尝试次数和下次重试时间。
6. Worker 周期性续租；过期 Run 可由其他 Worker 回收。完成、失败和进度写入都校验租约所有者，拒绝陈旧 Worker 写回。
7. SQLite 仅用于显式注入的快速单元测试；PostgreSQL 行为由独立集成测试覆盖。

## 结果

多个 Worker 可以在不重复领取同一任务的情况下并行消费，并能回收因进程崩溃遗留的 `RUNNING` 任务。迁移历史可进入代码评审并在部署前执行。代价是本地开发新增 PostgreSQL/Docker 依赖，运维需要监控连接池、锁等待、租约时长、重试次数和失败任务。

PostgreSQL 仍不是专用消息队列。若任务量、优先级、延迟队列或跨区域需求显著增长，应通过指标重新评估 Redis、RabbitMQ 或托管任务服务，而不是无限扩展数据库轮询。
