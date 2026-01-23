import { useState } from 'react'
import { Modal } from 'antd'
import { TunnelList } from '../components/TunnelList'
import { TunnelForm } from '../components/TunnelForm'
import { useTunnels } from '../hooks/useTunnels'
import type { Tunnel, CreateTunnelRequest, UpdateTunnelRequest } from '../types'

interface TunnelsProps {
  onViewLogs?: (domain: string) => void
}

export function Tunnels({ onViewLogs }: TunnelsProps = {}) {
  const {
    tunnels,
    loading,
    createTunnel,
    updateTunnel,
    deleteTunnel,
    regenerateToken,
  } = useTunnels()

  const [formOpen, setFormOpen] = useState(false)
  const [editingTunnel, setEditingTunnel] = useState<Tunnel | null>(null)

  const handleCreate = () => {
    setEditingTunnel(null)
    setFormOpen(true)
  }

  const handleEdit = (tunnel: Tunnel) => {
    setEditingTunnel(tunnel)
    setFormOpen(true)
  }

  const handleFormSuccess = () => {
    setFormOpen(false)
    setEditingTunnel(null)
  }

  const handleSubmit = async (data: CreateTunnelRequest | UpdateTunnelRequest) => {
    if (editingTunnel) {
      await updateTunnel(editingTunnel.domain, data as UpdateTunnelRequest)
    } else {
      const result = await createTunnel(data as CreateTunnelRequest)
      // 显示 Token
      Modal.info({
        title: '隧道创建成功',
        width: 600,
        content: (
          <div>
            <p>域名: <code>{result.domain}</code></p>
            <p>Token: <code>{result.token}</code></p>
            <p style={{ color: '#ff4d4f', marginTop: 16 }}>
              ⚠️ 请妥善保管 Token，它不会再次显示！
            </p>
          </div>
        ),
        okText: '已保存',
      })
    }
  }

  return (
    <div>
      <TunnelList
        tunnels={tunnels}
        loading={loading}
        onCreate={handleCreate}
        onEdit={handleEdit}
        onDelete={deleteTunnel}
        onRegenerateToken={regenerateToken}
        onViewLogs={onViewLogs}
      />
      <TunnelForm
        open={formOpen}
        tunnel={editingTunnel}
        onCancel={() => {
          setFormOpen(false)
          setEditingTunnel(null)
        }}
        onSuccess={handleFormSuccess}
        onSubmit={handleSubmit}
      />
    </div>
  )
}
