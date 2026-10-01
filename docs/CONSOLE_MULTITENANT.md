# 多租户自助控制台（Console v2）设计契约 v1.1

> 状态：v1.1（2026-10-01 机主拍板管理台鉴权 = 方案 A：admin 角色会话）。本文件是并行实现的**唯一权威契约**；
> 实现细节（代码放置、内部函数命名）各实现者自定，但 API 形状、数据模型、安全决策不得偏离。
> 背景讨论见会话记录：甲模式 = 租户自带后端服务（如 DSH），tunely 部署方只出租隧道。
> **v1.1 变更**：管理端点鉴权从「仅 admin key」改为「admin key **或** role=admin 会话」；
> 邀请码支持签发 admin 角色；管理员可提升/降级用户角色。前端 /admin 改用会话，不再向前端下发 admin key。

## 1. 目标与非目标

**目标**
- tunely 内置通用「多租户自助控制台」：租户经邀请注册 → 登录 → 自助创建/删除自己的隧道。
- 认证退位：控制台**只管隧道可达性**，绝不代理/注入后端服务（如 DSH）的认证；每个后端服务的 token/cookie 归服务自己。
- 吃掉 crypto-ops 的一次性个人门户（后续迁移单独执行，不在本契约范围）。

**非目标**
- 不做支付/计量计费；不做邮箱验证；不做 OAuth 三方登录。
- 不改变数据面协议（ws/tunnel 控制面、tcp/udp 透传零改动）。
- 不做 nginx 层租户门禁（auth_request 挂隧道域名）——留给部署方可选，本仓不实现。

## 2. 角色与术语

| 角色 | 说明 |
|---|---|
| admin | 部署方账号：`role='admin'` 的 users 行（经 admin 邀请码注册，或由既有 admin 提升）；另可持 admin key 供服务器脚本使用 |
| tenant（租户） | 受邀注册的用户；只能操作 owner 是自己的隧道 |

## 3. 数据模型（新增 + 迁移）

```
users        id, username(unique, 3-32, [a-z0-9_-]), password_hash(scrypt),
             role('admin'|'tenant'), disabled(bool, default false), created_at
invites      code(unique, 可读格式如 dsh-xxxxxx), role('tenant'|'admin', default 'tenant'),
             created_by, max_uses(int, default 1), used_count(int, default 0), expires_at(nullable), created_at
tunnels      新增列 owner_id(FK users.id, nullable)  —— NULL 视为 admin/遗留所有
```

- 通过现有 alembic 体系出迁移；旧库升级后行为不变（admin key 通道零影响）。
- 密码哈希用 stdlib `hashlib.scrypt`（n=2^14, r=8, p=1，随机 16B 盐，格式 `scrypt$n$r$p$salt$hash` hex 存储）。

## 4. 会话

- 无状态 HMAC 签名 cookie（不建 session 表）：`console_session`，载荷 `{u: username, r: role, exp}`，
  HMAC-SHA256；密钥来源：server 既有 secret 文件机制，无则随 admin key 一并提供配置项。
- Cookie 属性：`HttpOnly; SameSite=Lax; Path=/`，`Secure` 交给部署层（TLS 终止处加），Max-Age 7 天。
- 登录失败固定延迟 ~1s（防爆破，对齐既有 portal 经验）。

## 5. API 契约（全部 JSON；挂载进现有 FastAPI app）

统一前缀 `/api/console`。错误统一 `{"error": {"code", "message"}}`，HTTP 状态语义化。

| 方法/路径 | 鉴权 | 语义 |
|---|---|---|
| `POST /api/console/register` | 邀请码 | `{username,password,invite_code}` → 201 `{username,role}`；邀请码校验（有效/未过期/未用尽）后 used_count+1 |
| `POST /api/console/login` | 无 | `{username,password}` → 200 `{username,role}` + Set-Cookie；失败 401（延迟 1s） |
| `POST /api/console/logout` | 会话 | 204 + 清 cookie |
| `GET  /api/console/me` | 会话 | `{username,role,disabled}`；disabled → 403 |
| `GET  /api/console/tunnels` | 会话 | 我的隧道列表：`[{domain, created_at, online, last_seen_at, bytes_in, bytes_out}]`（**不含 token**） |
| `POST /api/console/tunnels` | 会话 | `{prefix}` → 201 `{domain, token}`；**token 明文仅此处与 rotate 返回**；超出配额 409 `quota_exceeded`；域名冲突 409 |
| `POST /api/console/tunnels/{domain}/rotate-token` | 会话+所有权 | 201 `{domain, token}`（旧 token 立即失效，连接被踢） |
| `DELETE /api/console/tunnels/{domain}` | 会话+所有权 | 204；在线连接断开 |
| `GET  /api/console/entry` | 会话 | 当前部署的接入说明模板：`{entry_base, qr: {v,kind:"dsh-tunnel",url,desktop,note}, desktop_connect}`，供前端渲染二维码 |

配额：配置项 `console_tunnels_per_user`（默认 3，0 = 该租户禁建）。
注册开关：无有效邀请码机制即天然关闭（register 必须带邀请码）。

| `POST /api/console/admin/invites` | admin key 或 admin 会话 | `{max_uses?, expires_days?, role?('tenant'\|'admin', 默认 tenant)}` → `{code}` |
| `GET  /api/console/admin/users` | admin key 或 admin 会话 | 用户列表 `{id,username,role,disabled,created_at,tunnel_count}` |
| `PATCH /api/console/admin/users/{username}` | admin key 或 admin 会话 | `{disabled?, role?('tenant'\|'admin')?}`；不可降级/禁用自己（409 self_lockout） |

admin 会话鉴权：会话用户 `role='admin'` 且未禁用（每请求查库，禁用即时生效）。
admin key 通道保持不变（服务器脚本用）。admin 会话与 admin key 通过任一即可。

**所有权规则**：tenant 只能触碰 `owner_id == 自己` 的隧道；越权一律 404（不泄露存在性）。
admin key 通道可操作全部（现状不变）。

## 6. 前端（admin-console 扩展，React 18 + Vite + AntD 5）

新增页面/路由：
- `/login`、`/register`（带邀请码输入）
- `/tunnels`（租户主页）：隧道列表（在线状态徽标、流量）、新建（前缀输入 → 展示 `域名 + token`，
  token 明文展示一次 + 复制按钮 + 「我已保存」确认）、rotate、删除（二次确认）、接入二维码弹层
- `/admin`（仅 role=admin 可见入口）：用户列表/禁用/角色调整、邀请码签发（**含角色选择**，默认 tenant；
  签发 admin 邀请需二次确认）。**鉴权只依赖会话 cookie，不向前端下发或粘贴 admin key**
  （admin key 仅供服务器脚本；既有默认路径的 key 管理台保持原样不动）。

约定：
- API 层沿用 admin-console 现有 `src/api` 封装风格；cookie 会话（无 token 存储）。
- 二维码内容 = `GET /api/console/entry` 返回的 `qr` JSON 原样编码（`v:1, kind:"dsh-tunnel",
  url:"https://<域名>", desktop:"tunely connect --token <token> --target http://127.0.0.1:3080"`）。
- 二维码渲染用轻量零依赖方案（自绘或既有依赖内解决，不为 QR 引入重依赖——如确需库，选 `qrcode`
  并在 PR 说明理由）。
- 构建/测试遵循 admin-console 现有脚本（`npm run dev / build`，test 目录已有先例）。

## 7. 部署形态（说明，不在本契约实现范围）

- 控制台随 tunely server 同进程提供（`:8000/api/console` + 静态 console 产物），nginx 反代之。
- crypto-ops 迁移路径：nginx `/login` → 控制台 `/login`；`/dsh/` 等路由的门户 auth_request **摘除**
  （认证已退位给 DSH 自身）；迁移前需机主确认。

## 8. 验收清单

- [ ] 注册（有效/过期/用尽邀请码）、登录（对/错/延迟）、登出、me
- [ ] 租户建隧道 → token 仅创建/rotate 时可见；配额 409；域名冲突 409
- [ ] 越权访问他人隧道 → 404；admin key 通道不受影响（回归）
- [ ] rotate 后旧 token 连接被断开、新 token 可连
- [ ] 前端：登录/注册/列表/新建（token 一次性展示）/二维码/删除确认
- [ ] pytest 全绿；admin-console build 通过
