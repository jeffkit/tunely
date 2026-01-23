/**
 * JSON 工具函数
 * 用于处理各种格式的 JSON 字符串解析和格式化
 */

/**
 * 判断字符串是否为有效的 JSON 格式
 */
export function isJsonString(str: string): boolean {
  if (!str) return false
  const trimmed = str.trim()
  if (!trimmed) return false

  // 检查是否以 JSON 对象或数组开头
  if (!trimmed.startsWith('{') && !trimmed.startsWith('[')) {
    // 可能是 JSON 字符串（双重编码），尝试解码
    if (trimmed.startsWith('"') && trimmed.endsWith('"')) {
      try {
        const decoded = JSON.parse(trimmed)
        if (typeof decoded === 'string') {
          // 解码后还是字符串，检查是否是 JSON
          return decoded.trim().startsWith('{') || decoded.trim().startsWith('[')
        }
        return true
      } catch {
        return false
      }
    }
    return false
  }

  try {
    JSON.parse(trimmed)
    return true
  } catch {
    return false
  }
}

/**
 * 格式化 JSON 字符串
 * 
 * 支持多种情况：
 * 1. 直接是有效的 JSON
 * 2. JSON 字符串（双重编码）
 * 3. 包含 Unicode 转义字符的 JSON
 * 4. 纯 Unicode 转义字符串
 * 
 * @param str 待格式化的字符串
 * @param indent 缩进空格数，默认 2
 * @returns 格式化后的字符串
 */
export function formatJsonString(str: string, indent: number = 2): string {
  if (!str) return ''

  const trimmed = str.trim()
  if (!trimmed) return ''

  // 情况1: 直接是有效的 JSON
  try {
    const parsed = JSON.parse(trimmed)
    return JSON.stringify(parsed, null, indent)
  } catch {
    // 继续尝试其他方式
  }

  // 情况2: 是 JSON 字符串（双重编码），需要先解码字符串再解析 JSON
  // 例如: "{\"message\":\"hello\"}" -> {"message":"hello"}
  if (trimmed.startsWith('"') && trimmed.endsWith('"')) {
    try {
      let decoded: any = JSON.parse(trimmed) // 解码外层字符串
      // 递归解码，直到不再是字符串或解码失败
      while (typeof decoded === 'string') {
        try {
          const next = JSON.parse(decoded)
          decoded = next
        } catch {
          // 解码失败，停止递归
          break
        }
      }
      // 如果最终结果是对象或数组，格式化输出
      if (typeof decoded === 'object' && decoded !== null) {
        return JSON.stringify(decoded, null, indent)
      }
      // 否则返回字符串
      return decoded
    } catch {
      // 继续尝试
    }
  }

  // 情况3: 包含 Unicode 转义字符的 JSON 字符串
  if (trimmed.includes('\\u') && (trimmed.startsWith('{') || trimmed.startsWith('['))) {
    try {
      // 尝试解析包含 Unicode 转义的 JSON
      const parsed = JSON.parse(trimmed)
      return JSON.stringify(parsed, null, indent)
    } catch {
      // 继续尝试
    }
  }

  // 情况4: 纯 Unicode 转义字符串（不是 JSON 对象）
  if (trimmed.includes('\\u') && !trimmed.startsWith('{') && !trimmed.startsWith('[')) {
    try {
      // 尝试将字符串作为 JSON 字符串值解析（会解码 Unicode）
      const decoded = JSON.parse(`"${trimmed}"`)
      return decoded
    } catch {
      // 如果失败，返回原字符串
      return trimmed
    }
  }

  // 默认返回原字符串
  return trimmed
}

/**
 * 安全解析 JSON 字符串
 * 
 * @param str 待解析的字符串
 * @param defaultValue 解析失败时返回的默认值
 * @returns 解析后的对象或默认值
 */
export function safeJsonParse<T = any>(str: string, defaultValue: T | null = null): T | null {
  if (!str) return defaultValue

  try {
    let parsed: any = JSON.parse(str)
    // 递归解码，直到不再是字符串
    while (typeof parsed === 'string') {
      try {
        parsed = JSON.parse(parsed)
      } catch {
        break
      }
    }
    return parsed as T
  } catch {
    return defaultValue
  }
}
