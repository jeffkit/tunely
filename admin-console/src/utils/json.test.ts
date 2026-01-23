/**
 * JSON 工具函数单元测试
 */
import { describe, it, expect } from 'vitest'
import { isJsonString, formatJsonString, safeJsonParse } from './json'

describe('json utils', () => {
  describe('isJsonString', () => {
    it('should return true for valid JSON objects', () => {
      expect(isJsonString('{"name":"test"}')).toBe(true)
      expect(isJsonString('{"a":1,"b":2}')).toBe(true)
    })

    it('should return true for valid JSON arrays', () => {
      expect(isJsonString('[1,2,3]')).toBe(true)
      expect(isJsonString('["a","b"]')).toBe(true)
    })

    it('should return false for non-JSON strings', () => {
      expect(isJsonString('hello world')).toBe(false)
      expect(isJsonString('123')).toBe(false)
      expect(isJsonString('')).toBe(false)
    })

    it('should handle double-encoded JSON strings', () => {
      expect(isJsonString('"{\\\"name\\\":\\\"test\\\"}"')).toBe(true)
    })

    it('should handle invalid JSON', () => {
      expect(isJsonString('{invalid}')).toBe(false)
      expect(isJsonString('{"unclosed"')).toBe(false)
    })
  })

  describe('formatJsonString', () => {
    it('should format valid JSON objects', () => {
      const input = '{"name":"test","age":30}'
      const output = formatJsonString(input)
      expect(output).toContain('"name": "test"')
      expect(output).toContain('"age": 30')
    })

    it('should format valid JSON arrays', () => {
      const input = '[1,2,3]'
      const output = formatJsonString(input)
      expect(output).toContain('1')
      expect(output).toContain('2')
      expect(output).toContain('3')
    })

    it('should handle double-encoded JSON', () => {
      //  测试场景：一个 JSON 字符串被再次编码为 JSON 字符串
      // 实际场景：后端返回的 response_body 是一个字符串 "{\"message\":\"hello\"}"
      const input = '{"message":"hello"}'
      const output = formatJsonString(input)
      expect(output).toContain('"message": "hello"')
    })

    it('should handle Unicode escape sequences', () => {
      const input = '{"text":"\\u4e2d\\u6587"}'
      const output = formatJsonString(input)
      expect(output).toContain('中文')
    })

    it('should return original string for non-JSON', () => {
      const input = 'plain text'
      const output = formatJsonString(input)
      expect(output).toBe(input)
    })

    it('should handle empty strings', () => {
      expect(formatJsonString('')).toBe('')
      expect(formatJsonString('   ')).toBe('')
    })

    it('should use custom indentation', () => {
      const input = '{"a":1}'
      const output = formatJsonString(input, 4)
      expect(output).toContain('    "a"')
    })
  })

  describe('safeJsonParse', () => {
    it('should parse valid JSON', () => {
      const result = safeJsonParse<{ name: string }>('{"name":"test"}')
      expect(result).toEqual({ name: 'test' })
    })

    it('should return default value for invalid JSON', () => {
      const result = safeJsonParse('{invalid}', { default: true })
      expect(result).toEqual({ default: true })
    })

    it('should handle double-encoded JSON', () => {
      const result = safeJsonParse<{ msg: string }>('"{\\\"msg\\\":\\\"hi\\\"}"')
      expect(result).toEqual({ msg: 'hi' })
    })

    it('should return null by default for invalid JSON', () => {
      const result = safeJsonParse('not json')
      expect(result).toBeNull()
    })

    it('should handle empty strings', () => {
      const result = safeJsonParse('', { empty: true })
      expect(result).toEqual({ empty: true })
    })
  })
})
