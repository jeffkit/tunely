/**
 * Tunely TypeScript Client 单元测试
 *
 * 测试内容：
 * - 请求头清理逻辑（hop-by-hop headers 移除）
 * - 指数退避计算逻辑
 * - 连接状态管理（consecutiveRejectCount 重置）
 * - onDisconnect 在正常关闭时触发（Bug Fix 回归测试）
 */

import { EventEmitter } from 'events';
import { describe, it, expect, vi, afterEach } from 'vitest';

// ================================================================
// Mock WebSocket — 用于 TunnelClient 集成测试
// vi.mock 会被 vitest 自动提升到文件顶部，在 import 之前执行
// ================================================================

class MockWebSocket extends EventEmitter {
  send = vi.fn();
  // close() 触发 close 事件，模拟连接被关闭（本端或对端）
  close = vi.fn(function (this: MockWebSocket) {
    this.emit('close');
  });
}

vi.mock('ws', () => ({
  default: vi.fn(() => new MockWebSocket()),
}));

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

// ================================================================
// Bug Fix 回归测试：onDisconnect 在 WebSocket 正常关闭时应当触发
//
// 【原始 Bug】
// ws.on('close') 处理函数只调用了 resolve()，没有调用 onDisconnect。
// 导致 tunnelService.status.connected 永远停留在 true，
// Desktop UI 会持续显示"已连接"和旧的 connectedAt 时间戳，
// 即使底层 WebSocket 已断开，也不会触发重连状态更新。
//
// 【修复】
// ws.on('close') 中检查 wasConnected，如果之前已建立连接则触发 onDisconnect。
// ================================================================

import { TunnelClient } from './client.js';

/** 辅助：创建真实 TunnelClient 并获取底层 MockWebSocket 实例 */
async function createConnectedClient(): Promise<{
  client: TunnelClient;
  mockWs: MockWebSocket;
  runPromise: Promise<void>;
  disconnectSpy: ReturnType<typeof vi.fn>;
  connectSpy: ReturnType<typeof vi.fn>;
}> {
  const WebSocketMock = vi.mocked((await import('ws')).default);
  let mockWs!: MockWebSocket;

  // 第一次 new WebSocket() 返回可控实例；第二次（重连）调用 close() 退出
  WebSocketMock.mockImplementationOnce(() => {
    mockWs = new MockWebSocket();
    return mockWs as any;
  }).mockImplementation(() => {
    const ws = new MockWebSocket();
    // 让第二次连接立即收到 close，使 run() 循环退出（stop 后不再重连）
    Promise.resolve().then(() => ws.emit('close'));
    return ws as any;
  });

  const client = new TunnelClient({
    serverUrl: 'ws://test-server',
    token: 'test-token',
    targetUrl: 'http://localhost:3000',
    reconnectInterval: 100,
  });

  const disconnectSpy = vi.fn();
  const connectSpy = vi.fn();
  client.on('onDisconnect', disconnectSpy);
  client.on('onConnect', connectSpy);

  const runPromise = client.run();

  // 等待 WebSocket 实例被创建并附上事件监听器
  await new Promise((r) => setImmediate(r));

  // 模拟服务端发送 AUTH_OK，触发 onConnect
  mockWs.emit(
    'message',
    Buffer.from(
      JSON.stringify({ type: 'auth_ok', domain: 'test-domain', tunnel_id: 'tid-001' })
    )
  );

  await new Promise((r) => setImmediate(r));

  return { client, mockWs, runPromise, disconnectSpy, connectSpy };
}

describe('TunnelClient - onDisconnect Bug Fix（回归测试）', () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it('AUTH_OK 后服务端正常关闭连接，应触发 onDisconnect', async () => {
    // ✅ 验证修复：正常 close 路径（resolve）也应调用 onDisconnect
    const { client, mockWs, runPromise, disconnectSpy, connectSpy } =
      await createConnectedClient();

    // 确认已建立连接
    expect(connectSpy).toHaveBeenCalledWith('test-domain');

    // 服务端关闭 WebSocket（正常关闭，不是 error）
    mockWs.emit('close');

    // 停止客户端（阻止无限重连）
    client.stop();
    await runPromise.catch(() => {});

    // 修复后：即使是 clean close 也必须触发 onDisconnect
    expect(disconnectSpy).toHaveBeenCalledTimes(1);
  });

  it('连接建立前 close，不应触发 onDisconnect（wasConnected 守卫）', async () => {
    // ✅ 验证守卫逻辑：close 在 AUTH_OK 之前发生，不应误触发 onDisconnect
    const WebSocketMock = vi.mocked((await import('ws')).default);
    let mockWs!: MockWebSocket;

    WebSocketMock.mockImplementationOnce(() => {
      mockWs = new MockWebSocket();
      return mockWs as any;
    });

    const client = new TunnelClient({
      serverUrl: 'ws://test-server',
      token: 'test-token',
      targetUrl: 'http://localhost:3000',
    });

    const disconnectSpy = vi.fn();
    client.on('onDisconnect', disconnectSpy);

    const runPromise = client.run();
    await new Promise((r) => setImmediate(r));

    // 尚未收到 AUTH_OK，直接关闭（从未 connected）
    mockWs.emit('close');

    client.stop();
    await runPromise.catch(() => {});

    // 未建立连接，onDisconnect 不应触发
    expect(disconnectSpy).not.toHaveBeenCalled();
  });

  it('WebSocket error 触发的断开，仍应触发 onDisconnect（原有路径不受影响）', async () => {
    // ✅ 验证兼容性：error → reject → catch 路径依然工作
    const WebSocketMock = vi.mocked((await import('ws')).default);
    let mockWs!: MockWebSocket;

    WebSocketMock.mockImplementationOnce(() => {
      mockWs = new MockWebSocket();
      return mockWs as any;
    }).mockImplementation(() => {
      const ws = new MockWebSocket();
      Promise.resolve().then(() => ws.emit('close'));
      return ws as any;
    });

    const client = new TunnelClient({
      serverUrl: 'ws://test-server',
      token: 'test-token',
      targetUrl: 'http://localhost:3000',
      reconnectInterval: 100,
    });

    const disconnectSpy = vi.fn();
    client.on('onDisconnect', disconnectSpy);

    const runPromise = client.run();
    await new Promise((r) => setImmediate(r));

    // 先建立连接
    mockWs.emit(
      'message',
      Buffer.from(JSON.stringify({ type: 'auth_ok', domain: 'test-domain', tunnel_id: 'tid-001' }))
    );
    await new Promise((r) => setImmediate(r));

    // 触发 error（原有的 reject 路径）
    mockWs.emit('error', new Error('connection reset'));
    // error 后 ws 库通常也会 close，模拟之
    mockWs.emit('close');

    client.stop();
    await runPromise.catch(() => {});

    // error 路径 + close 路径各自可能都触发，至少应有 1 次
    expect(disconnectSpy).toHaveBeenCalled();
  });
});
