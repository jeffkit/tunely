# 多租户自助控制台（Console v2）

> 设计契约：[`CONSOLE_MULTITENANT.md`](CONSOLE_MULTITENANT.md)（权威）。本文是部署与使用指南，
> 实现合入后如有出入，以代码 + 契约为准并回改本文。

## 这是什么

tunely 内置的多租户控制台：部署方（admin）签发邀请码，租户自助注册并创建自己的隧道。
**认证退位**原则：控制台只管隧道可达性；每个后端服务（如 DSH）的认证归服务自己
（各自的 token / 会话 cookie），控制台不做任何代理注入。

```
租户桌面（自己的 DSH + tunely connect）
   │  出站 wss（tunnel token）
   ▼
tunely server（多租户控制台 + 数据面）
   ▲  https://<租户域名>
租户手机（DSH App / 浏览器）
```

## 角色与流程

**部署方（admin 账号）**
1. 初始化（仅一次）：用服务器 admin key 签发一枚 **role=admin** 邀请码 → 注册自己的管理员账号
2. 日常：登录控制台 `/admin` 签发邀请码（可选角色）、管理用户——全程会话鉴权，admin key 不进浏览器

**租户（tenant）**
1. `/register` 用邀请码注册用户名/密码
2. `/tunnels` 创建隧道 → 得到 `域名 + token`（token 明文仅此一次展示）
3. 桌面侧连接：`tunely connect --token <token> --target http://127.0.0.1:3080`
4. 手机侧：控制台隧道页的「接入二维码」→ DSH App 扫码即配对（二维码为 JSON
   `{"v":1,"kind":"dsh-tunnel","url":"https://<域名>"}`）
5. token 泄露时在控制台 rotate（旧连接立即断开）

## 配置项

| 配置 | 默认 | 说明 |
|---|---|---|
| `console_tunnels_per_user` | `3` | 每租户可建隧道数；`0` = 禁止创建 |
| `WS_TUNNEL_CONSOLE_SESSION_SECRET`（或 `*_FILE`） | 未配置时进程内随机（重启会话失效并告警） | HMAC 签名 cookie `console_session`（7 天） |
| 登录失败延迟 | `1.0s`（`console_login_failure_delay`） | 防爆破 |

部署提示：
- 独立 `tunely serve` 启动时数据库用 `-D/--database` 指定；未显式给出时回退
  `WS_TUNNEL_DATABASE_URL`，再回退内置默认（解析链：CLI > env > 默认）。
- alembic 已是主依赖：全新部署可不跑迁移（建表即最新）；**存量库升级必须跑**
  `alembic upgrade head`（在 `python/` 目录下用 console script，勿用 `python -m alembic`
  ——本地 `alembic/` 迁移目录会遮蔽包）。
- admin-console 静态产物由 nginx 挂到 `/console/` 之类路径（哈希路由，无需回退配置）。

## 安全边界

- 密码 scrypt 存储；登录失败固定 ~1s 延迟（防爆破）。
- 会话为无状态 HMAC cookie：`HttpOnly; SameSite=Lax`，`Secure` 交由 TLS 终止层追加。
- 租户只能触碰 `owner` 是自己的隧道，越权一律 404（不泄露存在性）。
- 管理面鉴权（契约 v1.1）：admin key **或** `role=admin` 会话二选一；admin 会话每请求查库，
  禁用/降级即时生效；`self_lockout` 禁止对当前登录账号禁用/降级。
- admin key 通道（`/api/tunnels` 等）行为不变，回归由测试守住。

## 部署

- 控制台随 tunely server 同进程提供：`/api/console/*` + admin-console 静态产物。
- nginx 反代静态与 API 即可；**不要**在隧道域名的 server 块上再挂门户式 auth_request
  （认证已退位给各后端服务）。
- 从旧的个人门户（crypto-ops portal）迁移的步骤与风险，见下节。

## 从一次性门户迁移（crypto-ops → Console v2）

| 现在（个人门户） | 迁移后 |
|---|---|
| 单用户单密码 + nginx cookie 注入 DSH 会话 | 控制台多租户登录；**不再注入**，DSH 认证走自己的 token/cookie |
| 服务目录手写 HTML | 控制台隧道列表 + 接入二维码 |
| token 手工发放 | 自助创建 / rotate |

迁移前置条件：目标 DSH 实例可被租户用 token 配对直达（App 已支持 token 配对双轨）。
迁移执行需部署方（机主）确认后另行操作。
