/**
 * Vitest 测试环境设置
 */
import '@testing-library/jest-dom'
import { cleanup, configure } from '@testing-library/react'
import { afterEach } from 'vitest'
import { message, Modal } from 'antd'

// findBy* 默认 1s 超时在 antd 动效下偏紧；负载波动时留足余量（8s）
configure({ asyncUtilTimeout: 8000 })

// 每个用例结束后：卸载组件树并销毁 antd 静态实例（message / Modal.confirm）
// —— 它们渲染在 body 门户且持有模块级单例，不随 RTL cleanup 卸载；
//   残留节点会让后续用例 findByText 偶发命中旧文案。
// 注意：不能用清空 body 的方式清理，那会把 message 的全局容器从文档摘除，
// 导致后续 message 调用渲染进游离节点、用例稳定失败。
afterEach(() => {
  cleanup()
  message.destroy()
  Modal.destroyAll()
})

// jsdom 缺少 matchMedia / ResizeObserver / localStorage，antd 组件与既有封装依赖
if (typeof window !== 'undefined') {
  if (!window.matchMedia) {
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: (query: string) => ({
        matches: false,
        media: query,
        onchange: null,
        addListener: () => {},
        removeListener: () => {},
        addEventListener: () => {},
        removeEventListener: () => {},
        dispatchEvent: () => false,
      }),
    })
  }

  if (!window.ResizeObserver) {
    class ResizeObserverStub {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
    window.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver
  }

  if (!window.localStorage) {
    const store = new Map<string, string>()
    Object.defineProperty(window, 'localStorage', {
      configurable: true,
      value: {
        getItem: (key: string) => (store.has(key) ? (store.get(key) as string) : null),
        setItem: (key: string, value: string) => {
          store.set(key, String(value))
        },
        removeItem: (key: string) => {
          store.delete(key)
        },
        clear: () => {
          store.clear()
        },
        key: (index: number) => Array.from(store.keys())[index] ?? null,
        get length() {
          return store.size
        },
      },
    })
  }
}
