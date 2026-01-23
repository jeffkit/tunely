export interface Tunnel {
  domain: string
  name: string | null
  description: string | null
  enabled: boolean
  connected: boolean
  token?: string | null
  created_at: string | null
  last_connected_at: string | null
  total_requests: number
}

export interface CreateTunnelRequest {
  domain: string
  name?: string | null
  description?: string | null
}

export interface UpdateTunnelRequest {
  name?: string | null
  description?: string | null
  enabled?: boolean | null
}

export interface ServerInfo {
  name: string
  version: string
  domain: {
    pattern: string
    customizable: string
    suffix: string
  }
  websocket: {
    url: string
  }
  protocols: string[]
}

export interface CheckAvailabilityResponse {
  available: boolean
  name: string
  reason: string | null
}

export interface RegenerateTokenResponse {
  domain: string
  token: string
}

export interface TunnelRequestLog {
  id: number
  timestamp: string
  tunnel_domain: string
  method: string
  path: string
  request_headers: Record<string, string> | null
  request_body: string | null
  status_code: number | null
  response_headers: Record<string, string> | null
  response_body: string | null
  error: string | null
  duration_ms: number
}

export interface TunnelLogsResponse {
  total: number
  logs: TunnelRequestLog[]
}

// 导出错误类型
export * from './errors'
