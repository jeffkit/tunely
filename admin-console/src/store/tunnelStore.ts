import { create } from 'zustand'
import { message } from 'antd'
import { api } from '../api/client'
import type { Tunnel, CreateTunnelRequest, UpdateTunnelRequest } from '../types'
import { ApiError } from '../types/errors'
import { POLLING_CONFIG } from '../constants'

interface TunnelState {
  tunnels: Tunnel[]
  loading: boolean
  error: string | null
  lastFetchTime: number | null
  lastUserActivityTime: number | null
  
  // Actions
  loadTunnels: () => Promise<void>
  createTunnel: (data: CreateTunnelRequest) => Promise<Tunnel & { token: string }>
  updateTunnel: (domain: string, data: UpdateTunnelRequest) => Promise<Tunnel>
  deleteTunnel: (domain: string) => Promise<void>
  regenerateToken: (domain: string) => Promise<{ domain: string; token: string }>
  
  // 实时更新控制
  startPolling: (interval?: number, adaptive?: boolean) => void
  stopPolling: () => void
  markUserActivity: () => void
}

let pollingTimer: number | null = null
let currentPollingInterval: number = POLLING_CONFIG.DEFAULT_INTERVAL

export const useTunnelStore = create<TunnelState>((set, get) => ({
  tunnels: [],
  loading: false,
  error: null,
  lastFetchTime: null,
  lastUserActivityTime: Date.now(),

  loadTunnels: async () => {
    set({ loading: true, error: null })
    try {
      const data = await api.listTunnels()
      set({ 
        tunnels: data, 
        loading: false,
        lastFetchTime: Date.now()
      })
    } catch (err) {
      const error = err instanceof ApiError ? err : new ApiError('加载失败', 500, String(err))
      set({ error: error.getDetailMessage(), loading: false })
      message.error(error.getUserMessage())
    }
  },

  createTunnel: async (data: CreateTunnelRequest) => {
    get().markUserActivity() // 标记用户活跃
    try {
      const result = await api.createTunnel(data)
      await get().loadTunnels()
      message.success(`隧道 ${result.domain} 创建成功`)
      return result
    } catch (err) {
      const error = err instanceof ApiError ? err : new ApiError('创建失败', 500, String(err))
      message.error(error.getUserMessage())
      throw error
    }
  },

  updateTunnel: async (domain: string, data: UpdateTunnelRequest) => {
    get().markUserActivity() // 标记用户活跃
    try {
      const result = await api.updateTunnel(domain, data)
      await get().loadTunnels()
      message.success(`隧道 ${domain} 更新成功`)
      return result
    } catch (err) {
      const error = err instanceof ApiError ? err : new ApiError('更新失败', 500, String(err))
      message.error(error.getUserMessage())
      throw error
    }
  },

  deleteTunnel: async (domain: string) => {
    get().markUserActivity() // 标记用户活跃
    try {
      await api.deleteTunnel(domain)
      await get().loadTunnels()
      message.success(`隧道 ${domain} 删除成功`)
    } catch (err) {
      const error = err instanceof ApiError ? err : new ApiError('删除失败', 500, String(err))
      message.error(error.getUserMessage())
      throw error
    }
  },

  regenerateToken: async (domain: string) => {
    try {
      const result = await api.regenerateToken(domain)
      return result
    } catch (err) {
      const error = err instanceof ApiError ? err : new ApiError('重新生成失败', 500, String(err))
      message.error(error.getUserMessage())
      throw error
    }
  },

  startPolling: (interval: number = POLLING_CONFIG.DEFAULT_INTERVAL, adaptive: boolean = true) => {
    // 清除现有定时器
    if (pollingTimer !== null) {
      clearInterval(pollingTimer)
    }

    // 立即加载一次
    get().loadTunnels()

    // 自适应轮询逻辑
    if (adaptive) {
      const adaptivePolling = () => {
        const state = get()
        const now = Date.now()
        const timeSinceLastActivity = state.lastUserActivityTime
          ? now - state.lastUserActivityTime
          : Infinity

        // 根据用户活跃度调整轮询间隔
        // 5 分钟内有活动：使用活跃间隔 (3s)
        // 5-15 分钟：使用默认间隔 (5s)
        // 15 分钟以上：使用不活跃间隔 (10s)
        let newInterval: number
        if (timeSinceLastActivity < 5 * 60 * 1000) {
          newInterval = POLLING_CONFIG.ACTIVE_INTERVAL
        } else if (timeSinceLastActivity < 15 * 60 * 1000) {
          newInterval = POLLING_CONFIG.DEFAULT_INTERVAL
        } else {
          newInterval = POLLING_CONFIG.INACTIVE_INTERVAL
        }

        // 如果间隔改变，重新设置定时器
        if (newInterval !== currentPollingInterval) {
          currentPollingInterval = newInterval
          get().stopPolling()
          get().startPolling(newInterval, true)
          console.log(`轮询间隔已调整为 ${newInterval}ms`)
        }

        // 执行轮询
        get().loadTunnels()
      }

      currentPollingInterval = POLLING_CONFIG.ACTIVE_INTERVAL
      pollingTimer = window.setInterval(adaptivePolling, currentPollingInterval)
    } else {
      // 非自适应模式，使用固定间隔
      currentPollingInterval = interval
      pollingTimer = window.setInterval(() => {
        get().loadTunnels()
      }, interval)
    }
  },

  stopPolling: () => {
    if (pollingTimer !== null) {
      clearInterval(pollingTimer)
      pollingTimer = null
    }
  },

  markUserActivity: () => {
    set({ lastUserActivityTime: Date.now() })
  },
}))
