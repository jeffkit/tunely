import { useEffect, useState } from 'react'
import { Alert, Layout, Menu, Spin, Typography } from 'antd'
import { DashboardOutlined, CloudServerOutlined, HistoryOutlined } from '@ant-design/icons'
import { Dashboard } from './pages/Dashboard'
import { Tunnels } from './pages/Tunnels'
import { RequestLogs } from './pages/RequestLogs'
import { Login } from './pages/Login'
import { Register } from './pages/Register'
import { MyTunnels } from './pages/MyTunnels'
import { Admin } from './pages/Admin'
import { BackendConfigManager } from './components/BackendConfigManager'
import { useUserActivity } from './hooks/useUserActivity'
import { useRealtime } from './hooks/useRealtime'
import { useHashRoute, navigate } from './router'
import { useSessionStore } from './store/sessionStore'
import { getCurrentBackendConfig } from './utils/backendConfig'
import { POLLING_CONFIG, BACKEND_CONFIG_CHANGED_EVENT } from './constants'
import type { MenuProps } from 'antd'

const { Header, Content, Sider } = Layout

/** 全局轮询默认间隔（旧版管理台用） */
const POLLING_DEFAULT_INTERVAL = POLLING_CONFIG.DEFAULT_INTERVAL

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

/** 后端配置变更事件：旧版管理台据此重新判断是否已配置 admin key（定义见 constants） */

/** 旧版 admin key 管理台是否已配置 key（方案 A 后 key 不进浏览器，属可选显式入口） */
function hasLegacyApiKey(): boolean {
  // 旧 localStorage 方式先行判断（window.localStorage 在浏览器与测试环境均可用）
  const storedKey = window.localStorage?.getItem('tunely_api_key')
  if (storedKey) {
    return true
  }
  try {
    return Boolean(getCurrentBackendConfig()?.apiKey)
  } catch {
    // 后端配置不可读时视为未配置
    return false
  }
}

/**
 * 默认路由会话探测：GET /api/console/me
 * 有会话 → #/tunnels；无会话 → #/login（不再默认进入旧版 admin key 管理台）
 */
function SessionGate() {
  const { status, fetchMe } = useSessionStore()

  useEffect(() => {
    if (status === 'unknown') {
      void fetchMe()
    }
  }, [status, fetchMe])

  useEffect(() => {
    if (status === 'authed') {
      navigate('/tunnels', { replace: true })
    } else if (status === 'anonymous') {
      navigate('/login', { replace: true })
    }
  }, [status])

  return (
    <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <div style={{ textAlign: 'center' }}>
        <Spin />
        <Typography.Text type="secondary" style={{ display: 'block', marginTop: 12 }}>
          正在进入控制台...
        </Typography.Text>
      </div>
    </div>
  )
}

/**
 * 既有 admin key 管理台（部署方视角，显式经 #/legacy 访问）。
 * 方案 A 后 admin key 不进浏览器：未配置 key 时不渲染仪表盘、不自动发起任何 API 请求
 * （消除 401 噪音），仅提示先配置；配置保存后经事件重新判定。
 */
function LegacyAdminConsole() {
  const [selectedKey, setSelectedKey] = useState('dashboard')
  const [selectedTunnelDomain, setSelectedTunnelDomain] = useState<string | null>(null)
  const [keyReady, setKeyReady] = useState(() => hasLegacyApiKey())

  useEffect(() => {
    const onConfigChanged = () => {
      setKeyReady(hasLegacyApiKey())
    }
    window.addEventListener(BACKEND_CONFIG_CHANGED_EVENT, onConfigChanged)
    return () => window.removeEventListener(BACKEND_CONFIG_CHANGED_EVENT, onConfigChanged)
  }, [])

  // 追踪用户活跃度，自动调整轮询间隔（不发请求）
  useUserActivity()

  // 仅在已配置 admin key 时启动全局实时数据更新（自适应轮询）
  useRealtime(POLLING_DEFAULT_INTERVAL, keyReady)

  if (!keyReady) {
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
          <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
            <a href="#/login" style={{ color: 'rgba(255, 255, 255, 0.85)' }}>
              多租户控制台
            </a>
            <BackendConfigManager />
          </div>
        </Header>
        <Content style={{ padding: 48, display: 'flex', justifyContent: 'center' }}>
          <Alert
            type="info"
            showIcon
            style={{ maxWidth: 560 }}
            message="旧版管理台需要 admin key"
            description={
              <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                <span>
                  该页面供部署方以 admin key 管理服务器（key 仅供服务器脚本与显式配置，不参与多租户控制台会话）。
                  未配置时本页不会自动请求任何接口。
                </span>
                <span>
                  请点击右上角「后端配置」添加后端与 admin key；多租户控制台请
                  <Typography.Link href="#/login">前往登录</Typography.Link>。
                </span>
              </div>
            }
          />
        </Content>
      </Layout>
    )
  }

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
        <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
          <a href="#/login" style={{ color: 'rgba(255, 255, 255, 0.85)' }}>
            多租户控制台
          </a>
          <BackendConfigManager />
        </div>
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

/**
 * 多租户控制台 v2（契约 CONSOLE_MULTITENANT.md §6 v1.1）：
 * /login /register（邀请码）/tunnels（租户主页）/admin（仅 admin 入口）
 * 默认路径为会话探测（→ /tunnels 或 /login）；旧版 admin key 管理台移至 #/legacy 显式访问。
 */
function App() {
  const path = useHashRoute()

  switch (path) {
    case '/login':
      return <Login />
    case '/register':
      return <Register />
    case '/tunnels':
      return <MyTunnels />
    case '/admin':
      return <Admin />
    case '/legacy':
      return <LegacyAdminConsole />
    default:
      return <SessionGate />
  }
}

export default App
