/**
 * 后端配置管理工具
 */

export interface BackendConfig {
  id: string
  name: string
  baseUrl: string
  apiKey: string
}

const STORAGE_KEY = 'tunely_backend_configs'
const CURRENT_BACKEND_KEY = 'tunely_current_backend_id'

/**
 * 获取所有后端配置
 */
export function getAllBackendConfigs(): BackendConfig[] {
  const stored = localStorage.getItem(STORAGE_KEY)
  if (!stored) return []
  try {
    return JSON.parse(stored)
  } catch {
    return []
  }
}

/**
 * 保存所有后端配置
 */
export function saveAllBackendConfigs(configs: BackendConfig[]): void {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(configs))
}

/**
 * 添加或更新后端配置
 */
export function saveBackendConfig(config: BackendConfig): void {
  const configs = getAllBackendConfigs()
  const index = configs.findIndex(c => c.id === config.id)
  if (index >= 0) {
    configs[index] = config
  } else {
    configs.push(config)
  }
  saveAllBackendConfigs(configs)
}

/**
 * 删除后端配置
 */
export function deleteBackendConfig(id: string): void {
  const configs = getAllBackendConfigs()
  const filtered = configs.filter(c => c.id !== id)
  saveAllBackendConfigs(filtered)
  
  // 如果删除的是当前后端，切换到第一个或清空
  const currentId = getCurrentBackendId()
  if (currentId === id) {
    if (filtered.length > 0) {
      setCurrentBackendId(filtered[0].id)
    } else {
      localStorage.removeItem(CURRENT_BACKEND_KEY)
    }
  }
}

/**
 * 获取当前使用的后端 ID
 */
export function getCurrentBackendId(): string | null {
  return localStorage.getItem(CURRENT_BACKEND_KEY)
}

/**
 * 设置当前使用的后端 ID
 */
export function setCurrentBackendId(id: string | null): void {
  if (id) {
    localStorage.setItem(CURRENT_BACKEND_KEY, id)
  } else {
    localStorage.removeItem(CURRENT_BACKEND_KEY)
  }
}

/**
 * 获取当前使用的后端配置
 */
export function getCurrentBackendConfig(): BackendConfig | null {
  const currentId = getCurrentBackendId()
  if (!currentId) {
    // 如果没有设置当前后端，尝试使用第一个配置
    const configs = getAllBackendConfigs()
    if (configs.length > 0) {
      return configs[0]
    }
    return null
  }
  const configs = getAllBackendConfigs()
  return configs.find(c => c.id === currentId) || null
}

/**
 * 生成新的后端配置 ID
 */
export function generateBackendId(): string {
  return `backend_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`
}
