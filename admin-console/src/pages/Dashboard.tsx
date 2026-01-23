import { useState, useEffect } from 'react'
import { message } from 'antd'
import { TunnelStats } from '../components/TunnelStats'
import { SystemInfo } from '../components/SystemInfo'
import { api } from '../api/client'
import type { ServerInfo } from '../types'
import { ApiError } from '../types/errors'
import { useTunnels } from '../hooks/useTunnels'

export function Dashboard() {
  const { tunnels } = useTunnels()
  const [serverInfo, setServerInfo] = useState<ServerInfo | null>(null)
  const [infoLoading, setInfoLoading] = useState(false)

  useEffect(() => {
    const loadServerInfo = async () => {
      setInfoLoading(true)
      try {
        const info = await api.getServerInfo()
        setServerInfo(info)
      } catch (err) {
        const error = err instanceof ApiError ? err : new ApiError('加载失败', 500, String(err))
        console.error('加载服务器信息失败:', error.getDetailMessage())
        message.error(error.getUserMessage())
      } finally {
        setInfoLoading(false)
      }
    }
    loadServerInfo()
  }, [])

  return (
    <div>
      <h1 style={{ marginBottom: 24 }}>仪表盘</h1>
      <TunnelStats tunnels={tunnels} />
      <SystemInfo info={serverInfo} loading={infoLoading} />
    </div>
  )
}
