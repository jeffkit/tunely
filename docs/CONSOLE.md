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

**部署方（admin，持 admin key）**
1. 签发邀请码：控制台 `/admin` 或 `POST /api/console/admin/invites`
2. （可选）在 `/admin` 禁用用户

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
| console 会话密钥 | 复用 server 既有 secret 文件机制 | HMAC 签名 cookie `console_session`（7 天） |

## 安全边界

- 密码 scrypt 存储；登录失败固定 ~1s 延迟（防爆破）。
- 会话为无状态 HMAC cookie：`HttpOnly; SameSite=Lax`，`Secure` 交由 TLS 终止层追加。
- 租户只能触碰 `owner` 是自己的隧道，越权一律 404（不泄露存在性）。
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
