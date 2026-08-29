# ADR 0007：FastAPI 同源托管 Web 产品入口

- 状态：Accepted
- 日期：2026-08-28

## 背景

项目已经具备 API、PostgreSQL Run、独立 Worker、SSE、容器化和可观测性，但使用者只能通过 OpenAPI 或测试代码观察系统。面试演示需要一个能够直接输入旅行偏好、观看 Agent 执行并理解结果来源的可见产品入口。

## 决策

在 `app/web/` 内实现无构建的单页界面，由 FastAPI 在 `/` 和 `/static__ 同源托管。当前阶段不引入 React、Node、独立前端镜像或额外部署流水线。

页面复用既有后端链路：

1. 创建 Session；
2. 提交包含城市、天数、兴趣、预算和补充偏好的 Message；
3. 获取异步 Run 与 Trace ID；
4. 通过 EventSource 消费持久化 SSE；
5. Tool 完成时展示天气候选与行程；
6. Run 结束后查询最终自然语言回答。

地图使用 Leaflet 1.9.4 稳定版和 OpenStreetMap 瓦片。景点路线由攻略 Tool 的坐标和时间轴确定，不由浏览器重新优化。Leaflet 不可用时使用原生 SVG 将经纬度投影成路线示意，确保核心顺序仍可见。

## Tool 展示契约

不把任意 Tool 输出直接写入 SSE。`build_tool_snapshot` 对已知 Tool 使用显式白名单：

- 天气：解析位置、最佳窗口、前三候选、逐日预报、来源和提示；
- 攻略：逐日 itinerary、来源页面、归属和 warnings；
- 错误：只保留状态、错误码和面向用户的消息；
- 未知 Tool：不暴露结果。

前端使用 DOM API 和 `textContent` 渲染动态内容，不执行 Tool 或模型返回的 HTML。页面只发起规划请求，不提供预订、支付或不可逆操作。

## 后果

- 仓库拥有真实可操作的全栈入口和可用于简历的产品截图。
- Docker 镜像无需增加 Node，CI 仍只有一个应用构建。
- 原生 JavaScript 适合当前页面规模；如果后续增加账号、酒店搜索、复杂表单和状态共享，应重新评估组件化前端。
- Leaflet CDN 和 OpenStreetMap 瓦片依赖公网；SVG 降级不提供真实底图。
- 预算目前只是 Agent 的规划偏好，不是实时酒店、交通或门票报价。

## 验证

- API 测试验证首页、JavaScript、CSS 均由 FastAPI 返回；
- SSE 测试验证天气快照可回放且非白名单字段不会进入浏览器；
- 单元测试验证未知 Tool 输出默认不公开；
- Docker 环境中使用真实 Chrome 无头模式访问首页并生成 `docs/images/web-product.png`。
