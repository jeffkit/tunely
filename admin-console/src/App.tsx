import { useState } from 'react'
import { Layout, Menu } from 'antd'
import { DashboardOutlined, CloudServerOutlined, HistoryOutlined } from '@ant-design/icons'
import { Dashboard } from './pages/Dashboard'
import { Tunnels } from './pages/Tunnels'
import { RequestLogs } from './pages/RequestLogs'
import { BackendConfigManager } from './components/BackendConfigManager'
import { useUserActivity } from './hooks/useUserActivity'
import { useRealtime } from './hooks/useRealtime'
import type { MenuProps } from 'antd'

const { Header, Content, Sider } = Layout

type MenuItem = Required<MenuProps>['items'][number]

const menuItems: MenuItem[] = [
  {
    key: 'dashboard',
    icon: <DashboardOutlined />,
    label: '仪表盘',
  },
  {
    key: 'tunnels',
    icon: <CloudServerOutlined />,
    label: '隧道管理',
  },
  {
    key: 'logs',
    icon: <HistoryOutlined />,
    label: '请求历史',
  },
]

function App() {
  const [selectedKey, setSelectedKey] = useState('dashboard')
  const [selectedTunnelDomain, setSelectedTunnelDomain] = useState<string | null>(null)

  // 追踪用户活跃度，自动调整轮询间隔
  useUserActivity()

  // 启动全局实时数据更新（自适应轮询）
  useRealtime()

  return (
    <Layout style={{ minHeight: '100vh' }}>
      <Header
        style={{
          background: '#001529',
          padding: '0 24px',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
        }}
      >
        <div style={{ color: '#fff', fontSize: 18, fontWeight: 'bold' }}>Tunely Server - 管理台</div>
        <BackendConfigManager />
      </Header>
      <Layout>
        <Sider width={200} style={{ background: '#fff' }}>
          <Menu
            mode="inline"
            selectedKeys={[selectedKey]}
            items={menuItems}
            onClick={({ key }) => {
              setSelectedKey(key)
              // 切换到其他页面时，清除选中的隧道域名
              if (key !== 'logs') {
                setSelectedTunnelDomain(null)
              }
            }}
            style={{ height: '100%', borderRight: 0 }}
          />
        </Sider>
        <Layout style={{ padding: '24px' }}>
          <Content
            style={{
              background: '#fff',
              padding: 24,
              margin: 0,
              minHeight: 280,
            }}
          >
            {selectedKey === 'dashboard' && <Dashboard />}
            {selectedKey === 'tunnels' && (
              <Tunnels onViewLogs={(domain) => {
                setSelectedTunnelDomain(domain)
                setSelectedKey('logs')
              }} />
            )}
            {selectedKey === 'logs' && <RequestLogs tunnelDomain={selectedTunnelDomain} />}
          </Content>
        </Layout>
      </Layout>
    </Layout>
  )
}

export default App
