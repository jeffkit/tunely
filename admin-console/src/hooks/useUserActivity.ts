/**
 * 用户活跃度追踪 Hook
 * 
 * 监听用户的鼠标和键盘活动，自动标记用户活跃状态
 */
import { useEffect, useRef } from 'react'
import { useTunnelStore } from '../store/tunnelStore'

export function useUserActivity() {
  const { markUserActivity } = useTunnelStore()
  const throttleTimerRef = useRef<number | null>(null)

  useEffect(() => {
    // 节流函数：避免频繁调用
    const throttledMarkActivity = () => {
      if (throttleTimerRef.current !== null) {
        return // 已经在节流期间，跳过
      }

      markUserActivity()

      // 设置节流计时器（1 秒内只标记一次）
      throttleTimerRef.current = window.setTimeout(() => {
        throttleTimerRef.current = null
      }, 1000)
    }

    // 监听的事件类型
    const events = ['mousedown', 'mousemove', 'keydown', 'scroll', 'touchstart']

    // 添加事件监听器
    events.forEach((event) => {
      window.addEventListener(event, throttledMarkActivity, { passive: true })
    })

    // 清理函数
    return () => {
      events.forEach((event) => {
        window.removeEventListener(event, throttledMarkActivity)
      })
      if (throttleTimerRef.current !== null) {
        clearTimeout(throttleTimerRef.current)
      }
    }
  }, [markUserActivity])
}
