# ADR 0006：OpenTelemetry 与可重复运行时基准

- 状态：Accepted
- 日期：2026-08-27

## 背景

异步 Agent 请求跨越 FastAPI、PostgreSQL 队列、独立 Worker、LLM 与多个 Tool。仅靠进程日志无法回答一个 Run 在哪里排队、为何变慢、是否重复执行，以及 Worker 租约回收后旧执行者是否仍能写回。

## 决策

使用 OpenTelemetry Python SDK 采集 Trace 与 Metrics，通过 OTLP/HTTP 发送到 OpenTelemetry Collector。Collector 将 Trace 转发到 Jaeger，将 Metrics 暴露给 Prometheus。

Run 入队时保存 W3C Trace Context carrier，Worker 领取后提取 carrier 并创建 consumer span，从而跨越数据库队列边界。HTTP、SQLAlchemy、HTTPX 与 Requests 使用官方 instrumentation；Session、Run、Worker、LLM 和 Tool 使用低基数业务 span/metric。

不采集 prompt、Tool 输入、模型输出、密钥、Cookie 或数据库连接串。`service.instance.id` 使用进程启动时生成的随机 UUID，避免暴露设备名。Run ID 只用于 Trace 属性，不作为 Metric label。

Prometheus recording rules 计算滚动 5 分钟 API P50/P95、Tool 成功率、活跃 Worker 和待执行 Run。Histogram 使用显式延迟桶，避免默认桶无法描述较长 Agent 执行。

性能与并发正确性使用自写基准而不是虚构数字。基准连接名字强制以 `_benchmark` 结尾的专用 PostgreSQL 数据库，通过真实 HTTP 和真实 Worker 领取路径验证：

1. 2 与 4 Worker 并发领取；
2. 正常 Run 不重复执行；
3. Worker 领取后中断，租约到期由另一 Worker 回收；
4. 旧 Worker 写回被 fencing 拒绝；
5. 输出原始 JSON 报告、吞吐量与 nearest-rank P50/P95。

外部 LLM/Tool 在调度基准中替换为固定延迟 stub。这样测量的是 API、PostgreSQL 队列与 Worker runtime，而不是第三方网络。真实 LLM/Tool 延迟和费用应另设端到端基准，不能与本报告混用。

## 后果

- 可以从 API 返回的 Trace ID 定位跨进程调用链。
- 可以量化 API、队列、执行、Tool、LLM、重试、租约回收、Worker 数量和 backlog。
- Compose 增加 Collector、Jaeger、Prometheus 三个服务，并增加一定内存与存储开销。
- Trace 与 Metric 是 at-least-once telemetry，不用于替代业务数据库中的最终状态。
- 当前 recording rules 不等同于正式 SLO；上线前仍需配置告警阈值、数据保留期、认证和 TLS。

## 验证

2026-08-28 的最终受控探针得到一条 45-span Trace，实际包含消息 API、Session、Run 入队、Run 执行、LLM、Tool 与 PostgreSQL。Collector 同时收到 LLM 24 input / 12 output / 36 total Token 以及 Tool、Run 指标。

同日 2/4 Worker 基准共执行 200 个正常 Run，重复执行数为 0；中断任务由另一 Worker回收且旧 Worker 写回被拒绝。具体数字保存在 `docs/benchmarks/2026-08-27-runtime-benchmark.json`，README 只摘录该报告，不手工估算。
