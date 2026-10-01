/**
 * Vitest 测试环境设置
 */
import '@testing-library/jest-dom'
import { configure } from '@testing-library/react'

// findBy* 默认 1s 超时在 antd 动效下偏紧，放宽到 5s
configure({ asyncUtilTimeout: 5000 })

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
