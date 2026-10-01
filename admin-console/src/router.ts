/**
 * 轻量哈希路由（零依赖）
 *
 * 管理台部署在 /console/ 子路径下的静态产物，哈希路由无需服务端 history 回退配置。
 * 路由表：/login /register /tunnels /admin，默认 / 为既有 admin key 管理台。
 */
import { useEffect, useState } from 'react'

/** 读取当前哈希路径（无哈希时视为 "/"） */
export function getHashPath(): string {
  const raw = window.location.hash.replace(/^#/, '')
  const path = raw.split('?')[0]
  if (!path || path === '/') return '/'
  return path.startsWith('/') ? path : `/${path}`
}

/** 跳转到指定哈希路径；replace 时替换历史记录（需手动触发 hashchange） */
export function navigate(to: string, options: { replace?: boolean } = {}): void {
  if (options.replace) {
    const { pathname, search } = window.location
    window.history.replaceState(null, '', `${pathname}${search}#${to}`)
    window.dispatchEvent(new Event('hashchange'))
  } else {
    window.location.hash = to
  }
}

/** 订阅哈希路由变化，返回当前路径 */
export function useHashRoute(): string {
  const [path, setPath] = useState<string>(() => getHashPath())

  useEffect(() => {
    const onHashChange = () => {
      setPath(getHashPath())
    }
    window.addEventListener('hashchange', onHashChange)
    return () => window.removeEventListener('hashchange', onHashChange)
  }, [])

  return path
}
