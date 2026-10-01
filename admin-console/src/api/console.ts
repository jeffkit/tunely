/**
 * 多租户控制台 API 封装
 * 契约：docs/CONSOLE_MULTITENANT.md §5，统一前缀 /api/console，错误 {"error": {code, message}}。
 *
 * 会话：HttpOnly cookie（同源自动携带；配置了跨源后端地址时显式 withCredentials）。
 * 前端不持久化任何凭据（不写 localStorage/token）。管理端点按契约使用 admin key，
 * 若后端配置管理中已设置则随 x-api-key 附带，已登录 admin 会话 cookie 亦同时携带。
 */
import axios, { AxiosError, type AxiosInstance } from 'axios'
import axiosRetry from 'axios-retry'
import type {
  AdminCreateInviteRequest,
  AdminInviteCreated,
  AdminSetUserDisabledRequest,
  AdminUser,
  ConsoleLoginRequest,
  ConsoleLoginResponse,
  ConsoleMe,
  ConsoleRegisterRequest,
  ConsoleRegisterResponse,
  EntryInfo,
  TenantTunnel,
  TenantTunnelCreated,
} from '../types/console'
import { ConsoleApiError, NetworkError, TimeoutError } from '../types/errors'
import { API_CONFIG } from '../constants'
import { getCurrentBackendConfig } from '../utils/backendConfig'
import { getStoredApiKey } from './client'

/**
 * 控制台 API 基地址：沿用现有封装的后端配置优先级（后端配置 > 旧 localStorage > 环境变量 > 同源 /api）
 */
function getConsoleBaseURL(): string {
  const backendConfig = getCurrentBackendConfig()
  if (backendConfig?.baseUrl) {
    return `${backendConfig.baseUrl.replace(/\/+$/, '')}/console`
  }

  const stored = localStorage.getItem('tunely_api_base_url')
  if (stored) {
    return `${stored.replace(/\/+$/, '')}/console`
  }

  const basePath = (import.meta.env.BASE_URL || '').replace(/\/+$/, '')
  return import.meta.env.VITE_API_BASE_URL || `${basePath}/api/console`
}

function createConsoleClient(): AxiosInstance {
  const client = axios.create({
    baseURL: getConsoleBaseURL(),
    headers: {
      'Content-Type': 'application/json',
    },
    timeout: API_CONFIG.TIMEOUT,
    // cookie 会话：同源请求本就携带；跨源后端配置时需要该项才能带上 Set-Cookie
    withCredentials: true,
  })

  // 网络错误与 5xx 重试（与现有 client 行为一致；不重试 4xx）
  axiosRetry(client, {
    retries: API_CONFIG.MAX_RETRIES,
    retryDelay: axiosRetry.exponentialDelay,
    retryCondition: (error: AxiosError) => {
      return (
        axiosRetry.isNetworkOrIdempotentRequestError(error) ||
        (error.response?.status !== undefined && error.response.status >= 500)
      )
    },
  })

  // 请求拦截器：管理端点按契约附带 admin key（如有配置）
  client.interceptors.request.use((config) => {
    const apiKey = getStoredApiKey()
    if (apiKey) {
      config.headers['x-api-key'] = apiKey
    }
    return config
  })

  // 响应拦截器：统一转 ConsoleApiError（注意：401 不清除 admin key，控制台会话与 admin key 互不相干）
  client.interceptors.response.use(
    (response) => response,
    (error: AxiosError) => {
      if (!error.response) {
        if (error.code === 'ECONNABORTED' || error.message?.includes('timeout')) {
          return Promise.reject(new TimeoutError(undefined, error))
        }
        return Promise.reject(new NetworkError(undefined, error))
      }
      return Promise.reject(ConsoleApiError.fromAxiosError(error))
    }
  )

  return client
}

const client = createConsoleClient()

export const consoleApi = {
  // ---- 会话 ----

  /** POST /api/console/register：邀请码注册 → 201 {username, role} */
  async register(data: ConsoleRegisterRequest): Promise<ConsoleRegisterResponse> {
    const response = await client.post('/register', data)
    return response.data
  },

  /** POST /api/console/login：登录 → 200 {username, role} + Set-Cookie；失败 401（服务端延迟 1s） */
  async login(data: ConsoleLoginRequest): Promise<ConsoleLoginResponse> {
    const response = await client.post('/login', data)
    return response.data
  },

  /** POST /api/console/logout：登出 → 204 + 清 cookie */
  async logout(): Promise<void> {
    await client.post('/logout')
  },

  /** GET /api/console/me：当前会话用户；disabled → 403 */
  async me(): Promise<ConsoleMe> {
    const response = await client.get('/me')
    return response.data
  },

  // ---- 租户隧道 ----

  /** GET /api/console/tunnels：我的隧道列表（不含 token） */
  async listMyTunnels(): Promise<TenantTunnel[]> {
    const response = await client.get('/tunnels')
    return response.data
  },

  /** POST /api/console/tunnels：{prefix} → 201 {domain, token}（token 明文仅此处与 rotate 返回） */
  async createMyTunnel(prefix: string): Promise<TenantTunnelCreated> {
    const response = await client.post('/tunnels', { prefix })
    return response.data
  },

  /** POST /api/console/tunnels/{domain}/rotate-token → 201 {domain, token}，旧 token 立即失效 */
  async rotateTunnelToken(domain: string): Promise<TenantTunnelCreated> {
    const response = await client.post(`/tunnels/${encodeURIComponent(domain)}/rotate-token`)
    return response.data
  },

  /** DELETE /api/console/tunnels/{domain} → 204，在线连接断开 */
  async deleteMyTunnel(domain: string): Promise<void> {
    await client.delete(`/tunnels/${encodeURIComponent(domain)}`)
  },

  /** GET /api/console/entry：当前部署的接入说明（qr JSON 原样用于二维码编码） */
  async getEntry(): Promise<EntryInfo> {
    const response = await client.get('/entry')
    return response.data
  },

  // ---- 管理端（契约：admin key 鉴权） ----

  /** GET /api/console/admin/users：用户列表 */
  async adminListUsers(): Promise<AdminUser[]> {
    const response = await client.get('/admin/users')
    return response.data
  },

  /** POST /api/console/admin/users：禁用/启用用户 */
  async adminSetUserDisabled(data: AdminSetUserDisabledRequest): Promise<void> {
    await client.post('/admin/users', data)
  },

  /** POST /api/console/admin/invites：{max_uses?, expires_days?} → {code} */
  async adminCreateInvite(data: AdminCreateInviteRequest): Promise<AdminInviteCreated> {
    const response = await client.post('/admin/invites', data)
    return response.data
  },
}
