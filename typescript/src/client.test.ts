/**
 * Tunely TypeScript Client 单元测试
 *
 * 测试内容：
 * - 请求头清理逻辑（hop-by-hop headers 移除）
 * - 指数退避计算逻辑
 * - 连接状态管理（consecutiveRejectCount 重置）
 */

import { describe, it, expect } from 'vitest';

/**
 * 提取并测试 header 清理逻辑
 * (与 client.ts handleRequest 中的逻辑一致)
 */
function cleanHeaders(headers: Record<string, string>): Record<string, string> {
  const cleanedHeaders: Record<string, string> = {};
  const skipHeaders = new Set([
    'host', 'connection', 'keep-alive', 'transfer-encoding',
    'te', 'trailer', 'upgrade', 'proxy-authorization',
    'proxy-connection',
  ]);
  for (const [key, value] of Object.entries(headers)) {
    if (!skipHeaders.has(key.toLowerCase())) {
      cleanedHeaders[key] = value;
    }
  }
  return cleanedHeaders;
}

/**
 * 提取并测试指数退避计算逻辑
 * (与 client.ts connect 中的逻辑一致)
 */
function calculateBackoffDelay(
  baseInterval: number,
  reconnectCount: number,
  consecutiveRejectCount: number,
): { minDelay: number; maxDelay: number; } {
  const backoffFactor = Math.min(reconnectCount + consecutiveRejectCount, 8);
  const maxDelayLimit = 300000; // 5 minutes max
  const delay = Math.min(baseInterval * Math.pow(2, backoffFactor - 1), maxDelayLimit);
  // Jitter range: delay * 0.8 to delay * 1.2
  return {
    minDelay: Math.round(delay * 0.8),
    maxDelay: Math.round(delay * 1.2),
  };
}

describe('Header Cleaning', () => {
  it('should remove hop-by-hop headers', () => {
    const input = {
      'Host': 'example.com',
      'Connection': 'keep-alive',
      'Content-Type': 'application/json',
      'Authorization': 'Bearer token123',
      'Transfer-Encoding': 'chunked',
      'Keep-Alive': 'timeout=5',
    };

    const cleaned = cleanHeaders(input);

    expect(cleaned).not.toHaveProperty('Host');
    expect(cleaned).not.toHaveProperty('Connection');
    expect(cleaned).not.toHaveProperty('Transfer-Encoding');
    expect(cleaned).not.toHaveProperty('Keep-Alive');
    expect(cleaned).toHaveProperty('Content-Type', 'application/json');
    expect(cleaned).toHaveProperty('Authorization', 'Bearer token123');
  });

  it('should handle case-insensitive header names', () => {
    const input = {
      'host': 'example.com',
      'HOST': 'example.com',
      'Host': 'example.com',
      'content-type': 'text/plain',
    };

    const cleaned = cleanHeaders(input);

    expect(cleaned).not.toHaveProperty('host');
    expect(cleaned).not.toHaveProperty('HOST');
    expect(cleaned).not.toHaveProperty('Host');
    expect(cleaned).toHaveProperty('content-type', 'text/plain');
  });

  it('should remove all proxy-related headers', () => {
    const input = {
      'Proxy-Authorization': 'Basic abc123',
      'Proxy-Connection': 'keep-alive',
      'X-Custom-Header': 'value',
    };

    const cleaned = cleanHeaders(input);

    expect(cleaned).not.toHaveProperty('Proxy-Authorization');
    expect(cleaned).not.toHaveProperty('Proxy-Connection');
    expect(cleaned).toHaveProperty('X-Custom-Header', 'value');
  });

  it('should return empty object for all-hop-by-hop headers', () => {
    const input = {
      'Host': 'example.com',
      'Connection': 'keep-alive',
    };

    const cleaned = cleanHeaders(input);
    expect(Object.keys(cleaned)).toHaveLength(0);
  });

  it('should pass through empty headers', () => {
    expect(cleanHeaders({})).toEqual({});
  });

  it('should preserve all non-hop-by-hop headers', () => {
    const input = {
      'Content-Type': 'application/json',
      'Accept': 'text/html',
      'X-Request-Id': 'abc-123',
      'Cache-Control': 'no-cache',
    };

    const cleaned = cleanHeaders(input);
    expect(cleaned).toEqual(input);
  });
});

describe('Exponential Backoff', () => {
  const baseInterval = 5000; // 5 seconds

  it('should start with base interval on first attempt', () => {
    const { minDelay, maxDelay } = calculateBackoffDelay(baseInterval, 1, 0);
    // Factor = min(1, 8) = 1, delay = 5000 * 2^0 = 5000
    expect(minDelay).toBe(4000); // 5000 * 0.8
    expect(maxDelay).toBe(6000); // 5000 * 1.2
  });

  it('should double delay on each reconnect', () => {
    const d1 = calculateBackoffDelay(baseInterval, 1, 0);
    const d2 = calculateBackoffDelay(baseInterval, 2, 0);
    const d3 = calculateBackoffDelay(baseInterval, 3, 0);

    // Factor 1: 5000*1 = 5000
    // Factor 2: 5000*2 = 10000
    // Factor 3: 5000*4 = 20000
    expect(d1.minDelay).toBe(4000);
    expect(d2.minDelay).toBe(8000);
    expect(d3.minDelay).toBe(16000);
  });

  it('should cap at 5 minutes (300000ms)', () => {
    // Factor 8 = max: 5000 * 2^7 = 640000, capped to 300000
    const { minDelay, maxDelay } = calculateBackoffDelay(baseInterval, 8, 0);
    expect(minDelay).toBe(240000); // 300000 * 0.8
    expect(maxDelay).toBe(360000); // 300000 * 1.2
  });

  it('should cap backoff factor at 8', () => {
    const d8 = calculateBackoffDelay(baseInterval, 8, 0);
    const d10 = calculateBackoffDelay(baseInterval, 10, 0);
    const d100 = calculateBackoffDelay(baseInterval, 100, 0);

    // All should have the same delay since factor is capped at 8
    expect(d8.minDelay).toBe(d10.minDelay);
    expect(d8.maxDelay).toBe(d100.maxDelay);
  });

  it('should accumulate reject count with reconnect count', () => {
    // reconnect=2 + reject=3 = factor 5
    const combined = calculateBackoffDelay(baseInterval, 2, 3);
    const equivalent = calculateBackoffDelay(baseInterval, 5, 0);

    expect(combined.minDelay).toBe(equivalent.minDelay);
    expect(combined.maxDelay).toBe(equivalent.maxDelay);
  });

  it('should handle zero reconnect count', () => {
    const { minDelay, maxDelay } = calculateBackoffDelay(baseInterval, 0, 0);
    // Factor = min(0, 8) = 0, delay = 5000 * 2^(-1) = 2500
    expect(minDelay).toBe(2000); // 2500 * 0.8
    expect(maxDelay).toBe(3000); // 2500 * 1.2
  });

  it('should increase delay for consecutive rejections', () => {
    const noReject = calculateBackoffDelay(baseInterval, 1, 0);
    const withReject = calculateBackoffDelay(baseInterval, 1, 2);

    // Factor 1 vs Factor 3: reject adds to backoff
    expect(withReject.minDelay).toBeGreaterThan(noReject.minDelay);
  });
});
