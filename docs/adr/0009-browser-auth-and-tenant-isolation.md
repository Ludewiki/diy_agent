# ADR 0009：浏览器会话鉴权与租户数据隔离

- 状态：Accepted
- 日期：2026-09-02

## 背景

Agent Session、Message、Run 与 SSE Event 原先只通过 UUID 定位。UUID 可以降低碰撞和猜测概率，但不是授权机制；获得另一个用户的 UUID 后仍可能读取行程、监听进度或取消任务。原生 EventSource 又不便添加自定义 Authorization Header，因此浏览器鉴权必须同时兼顾 SSE。

## 决策

1. 新增 User 与 AuthSession。密码使用 Argon2id 哈希；登录凭证是高熵不透明随机 Token，数据库只保存 SHA-256 摘要。
2. 浏览器通过 `HttpOnly`、`SameSite=Lax` Cookie 携带认证 Token。生产 HTTPS 设置 `Secure`；Token 不进入 URL、JavaScript 或 `localStorage`。
3. AgentSession 必须拥有非空 `user_id`。Session、Message、Run、取消和 SSE API 均以资源 ID 与当前用户 ID 联合查询。无权限与不存在统一返回 404。
4. 所有状态变更 API 使用双提交 CSRF Token：可读 CSRF Cookie 必须与 `X-CSRF-Token` 恒定时间匹配。存在 `Origin` 请求头时，还必须通过同源或显式可信 Origin 检查。
5. AuthSession 支持到期与服务端撤销。退出登录先撤销数据库记录，再清除浏览器 Cookie。
6. 历史匿名 Session 在迁移中归属一个禁用登录的 legacy 用户，避免数据库升级丢失数据，也避免历史数据自动暴露给新用户。

## 后果

- 同源 EventSource 会自动携带 HttpOnly Cookie，因此 SSE 无需向前端脚本暴露认证令牌。
- API 客户端在执行 POST 前必须先获取 CSRF Token，并同时保存 Cookie、发送请求头。
- 跨域前端除了 CORS 外还需要明确配置可信 Origin；Cookie 认证接口不能使用通配 Origin。
- 当前实现建立了长期记忆所需的用户边界，但不会在未获授权时把历史会话或偏好注入 Agent。

## 验证

自动化测试覆盖 Cookie 属性、数据库不存储原始 Token、CSRF 缺失、恶意 Origin、退出撤销，以及用户 A 无法读取、写入、取消或监听用户 B 的 Session、Message、Run 与 SSE。
