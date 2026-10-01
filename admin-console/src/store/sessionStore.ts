/**
 * 控制台会话状态（cookie 会话的前端镜像）
 * 仅缓存 /me 的结果用于路由守卫与入口可见性判断；任何凭据都不落盘。
 */
import { create } from 'zustand'
import { consoleApi } from '../api/console'
import type { ConsoleMe } from '../types/console'

export type SessionStatus = 'unknown' | 'authed' | 'anonymous'

interface SessionState {
  me: ConsoleMe | null
  status: SessionStatus
  loading: boolean

  /** 查询当前会话；未登录/会话失效时 status → anonymous 并返回 null */
  fetchMe: () => Promise<ConsoleMe | null>
  /** 本地标记已登录（登录成功后使用） */
  setMe: (me: ConsoleMe) => void
  /** 清空本地会话状态（登出后使用） */
  clear: () => void
}

export const useSessionStore = create<SessionState>((set) => ({
  me: null,
  status: 'unknown',
  loading: false,

  fetchMe: async () => {
    set({ loading: true })
    try {
      const me = await consoleApi.me()
      set({ me, status: 'authed', loading: false })
      return me
    } catch {
      set({ me: null, status: 'anonymous', loading: false })
      return null
    }
  },

  setMe: (me: ConsoleMe) => {
    set({ me, status: 'authed' })
  },

  clear: () => {
    set({ me: null, status: 'anonymous' })
  },
}))
