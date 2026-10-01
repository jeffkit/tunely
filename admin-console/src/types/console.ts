/**
 * 多租户控制台类型定义
 * 契约：docs/CONSOLE_MULTITENANT.md §5（API 形状不得偏离）
 */

export type ConsoleRole = 'admin' | 'tenant'

/** GET /api/console/me 响应 */
export interface ConsoleMe {
  username: string
  role: ConsoleRole
  disabled?: boolean
}

/** POST /api/console/register 请求（邀请码必填） */
export interface ConsoleRegisterRequest {
  username: string
  password: string
  invite_code: string
}

/** POST /api/console/register 响应 */
export interface ConsoleRegisterResponse {
  username: string
  role: ConsoleRole
}

/** POST /api/console/login 请求 */
export interface ConsoleLoginRequest {
  username: string
  password: string
}

/** POST /api/console/login 响应（会话经 Set-Cookie 下发，前端不持有凭据） */
export interface ConsoleLoginResponse {
  username: string
  role: ConsoleRole
}

/** GET /api/console/tunnels 列表项（契约：不含 token） */
export interface TenantTunnel {
  domain: string
  created_at: string | null
  online: boolean
  last_seen_at: string | null
  bytes_in: number
  bytes_out: number
}

/** POST /api/console/tunnels 与 rotate-token 响应（token 明文仅这两处返回） */
export interface TenantTunnelCreated {
  domain: string
  token: string
}

/** GET /api/console/entry 返回的 qr 对象（前端原样编码为二维码，不做改写） */
export interface EntryQr {
  v: number
  kind: string
  url: string
  desktop: string
  note?: string
}

/** GET /api/console/entry 响应 */
export interface EntryInfo {
  entry_base: string
  qr: EntryQr
  desktop_connect: string
}

/** GET /api/console/admin/users 列表项 */
export interface AdminUser {
  id: number
  username: string
  role: ConsoleRole
  disabled: boolean
  created_at: string | null
  tunnel_count?: number
}

/** PATCH /api/console/admin/users/{username} 请求（契约 v1.1：禁用/降级自己 → 409 self_lockout） */
export interface AdminUpdateUserRequest {
  disabled?: boolean
  role?: ConsoleRole
}

/** POST /api/console/admin/invites 请求（契约 v1.1：role 默认 tenant，签发 admin 邀请前端需二次确认） */
export interface AdminCreateInviteRequest {
  max_uses?: number
  expires_days?: number
  role?: ConsoleRole
}

/** POST /api/console/admin/invites 响应 */
export interface AdminInviteCreated {
  code: string
  max_uses?: number
  expires_at?: string | null
}
