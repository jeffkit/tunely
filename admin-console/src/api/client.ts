import axios, { AxiosError } from 'axios'
import axiosRetry from 'axios-retry'
import type {
  Tunnel,
  CreateTunnelRequest,
  UpdateTunnelRequest,
  ServerInfo,
  CheckAvailabilityResponse,
  RegenerateTokenResponse,
  TunnelLogsResponse,
} from '../types'
import { ApiError, NetworkError, TimeoutError } from '../types/errors'
import { API_CONFIG } from '../constants'

// 支持环境变量配置 API 地址
// VITE_API_BASE_URL: 完整的 API 基础 URL（如 https://tunely.example.com/api）
// 如果未设置，则使用相对路径（通过 Vite 代理或 Nginx）
// 如果设置了 base path，需要包含在路径中
// 优先使用后端配置管理中的配置
import { getCurrentBackendConfig } from '../utils/backendConfig'

const BASE_PATH = import.meta.env.BASE_URL || ''
const getStoredApiBaseUrl = () => {
  // 优先使用后端配置管理中的配置
  const backendConfig = getCurrentBackendConfig()
  if (backendConfig?.baseUrl) {
    return backendConfig.baseUrl
  }
  
  // 兼容旧配置方式
  const stored = localStorage.getItem('tunely_api_base_url')
  if (stored) return stored
  return import.meta.env.VITE_API_BASE_URL || `${BASE_PATH.replace(/\/$/, '')}/api`
}

// 从后端配置或 localStorage 获取 API Key
function getApiKey(): string | null {
  // 优先使用后端配置管理中的配置
  const backendConfig = getCurrentBackendConfig()
  if (backendConfig?.apiKey) {
    return backendConfig.apiKey
  }
  
  // 兼容旧配置方式
  return localStorage.getItem('tunely_api_key')
}

// 设置 API Key
export function setApiKey(apiKey: string | null) {
  if (apiKey) {
    localStorage.setItem('tunely_api_key', apiKey)
  } else {
    localStorage.removeItem('tunely_api_key')
  }
}

// 动态创建 client，支持运行时更新 baseURL
function createClient() {
  const baseURL = getStoredApiBaseUrl()
  const newClient = axios.create({
    baseURL: baseURL,
    headers: {
      'Content-Type': 'application/json',
    },
    timeout: API_CONFIG.TIMEOUT,
  })

  // 配置请求重试机制
  axiosRetry(newClient, {
    retries: API_CONFIG.MAX_RETRIES,
    retryDelay: axiosRetry.exponentialDelay, // 指数退避延迟（1s, 2s, 4s...）
    retryCondition: (error: AxiosError) => {
      // 只对网络错误和 5xx 错误重试
      // 不对 4xx 客户端错误重试（如认证失败、参数错误等）
      return (
        axiosRetry.isNetworkOrIdempotentRequestError(error) ||
        (error.response?.status !== undefined && error.response.status >= 500)
      )
    },
    onRetry: (retryCount, error, requestConfig) => {
      console.log(`请求重试 (${retryCount}/3):`, requestConfig.url, error.message)
    },
  })

  // 请求拦截器：添加 API Key
  newClient.interceptors.request.use((config) => {
    const apiKey = getApiKey()
    if (apiKey) {
      config.headers['x-api-key'] = apiKey
    }
    return config
  })

  // 响应拦截器：处理错误
  newClient.interceptors.response.use(
    (response) => response,
    (error: AxiosError) => {
      // 网络错误
      if (!error.response) {
        if (error.code === 'ECONNABORTED' || error.message?.includes('timeout')) {
          return Promise.reject(new TimeoutError(undefined, error))
        }
        return Promise.reject(new NetworkError(undefined, error))
      }

      // API 认证错误，清除 API Key
      if (error.response.status === 401) {
        setApiKey(null)
      }

      // 转换为 ApiError
      const apiError = ApiError.fromAxiosError(error)
      return Promise.reject(apiError)
    }
  )

  return newClient
}

let client = createClient()

// 导出函数用于更新 API Base URL（兼容旧版本）
export function updateApiBaseUrl(newBaseUrl: string) {
  if (newBaseUrl.trim()) {
    localStorage.setItem('tunely_api_base_url', newBaseUrl.trim())
  } else {
    localStorage.removeItem('tunely_api_base_url')
  }
  // 重新创建 client（但不会立即生效，需要刷新页面）
  client = createClient()
}

// 重新创建 client（用于切换后端后立即生效）
export function refreshClient() {
  client = createClient()
}

export const api = {
  // 服务器信息
  async getServerInfo(): Promise<ServerInfo> {
    const response = await client.get('/info')
    return response.data
  },

  // 隧道管理
  async listTunnels(): Promise<Tunnel[]> {
    const response = await client.get('/tunnels')
    return response.data
  },

  async getTunnel(domain: string): Promise<Tunnel> {
    const response = await client.get(`/tunnels/${domain}`)
    return response.data
  },

  async createTunnel(data: CreateTunnelRequest): Promise<Tunnel & { token: string }> {
    const response = await client.post('/tunnels', data)
    return response.data
  },

  async updateTunnel(domain: string, data: UpdateTunnelRequest): Promise<Tunnel> {
    const response = await client.put(`/tunnels/${domain}`, data)
    return response.data
  },

  async deleteTunnel(domain: string): Promise<void> {
    await client.delete(`/tunnels/${domain}`)
  },

  async regenerateToken(domain: string): Promise<RegenerateTokenResponse> {
    const response = await client.post(`/tunnels/${domain}/regenerate-token`)
    return response.data
  },

  async checkAvailability(name: string): Promise<CheckAvailabilityResponse> {
    const response = await client.get('/tunnels/check-availability', {
      params: { name },
    })
    return response.data
  },

  // 请求历史
  async getTunnelLogs(
    domain: string,
    limit: number = 100,
    offset: number = 0
  ): Promise<TunnelLogsResponse> {
    const response = await client.get(`/tunnels/${domain}/logs`, {
      params: { limit, offset },
    })
    return response.data
  },
}
