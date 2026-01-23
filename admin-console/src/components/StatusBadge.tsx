import { Badge } from 'antd'

interface StatusBadgeProps {
  connected: boolean
  enabled: boolean
}

export function StatusBadge({ connected, enabled }: StatusBadgeProps) {
  if (!enabled) {
    return <Badge status="default" text="已禁用" />
  }
  if (connected) {
    return <Badge status="success" text="在线" />
  }
  return <Badge status="error" text="离线" />
}
