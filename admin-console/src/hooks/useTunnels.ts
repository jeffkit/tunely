/**
 * 隧道管理 Hook
 * 
 * 这个 hook 已经迁移到使用 Zustand 全局状态管理，
 * 避免多个组件重复请求和状态不一致问题。
 */
import { useTunnelStore } from '../store/tunnelStore'

export function useTunnels() {
  return useTunnelStore()
}
