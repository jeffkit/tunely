import { useEffect } from 'react'
import { useTunnelStore } from '../store/tunnelStore'
import { POLLING_CONFIG } from '../constants'

/**
 * 实时更新隧道状态
 * 通过轮询方式获取最新状态
 * 
 * 使用全局状态管理，避免多个组件重复轮询。
 */
export function useRealtime(interval: number = POLLING_CONFIG.DEFAULT_INTERVAL) {
  const { startPolling, stopPolling } = useTunnelStore()

  useEffect(() => {
    startPolling(interval)
    
    return () => {
      stopPolling()
    }
  }, [interval, startPolling, stopPolling])
}
