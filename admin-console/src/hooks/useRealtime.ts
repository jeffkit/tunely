import { useEffect } from 'react'
import { useTunnelStore } from '../store/tunnelStore'
import { POLLING_CONFIG } from '../constants'

/**
 * 实时更新隧道状态
 * 通过轮询方式获取最新状态
 *
 * 使用全局状态管理，避免多个组件重复轮询。
 * enabled=false 时不启动轮询、不发起任何请求（如旧版管理台未配置 admin key 时）。
 */
export function useRealtime(
  interval: number = POLLING_CONFIG.DEFAULT_INTERVAL,
  enabled: boolean = true
) {
  const { startPolling, stopPolling } = useTunnelStore()

  useEffect(() => {
    if (!enabled) {
      return
    }

    startPolling(interval)

    return () => {
      stopPolling()
    }
  }, [enabled, interval, startPolling, stopPolling])
}
