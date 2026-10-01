/**
 * WS-Tunnel TypeScript Client
 *
 * 提供 WebSocket 隧道客户端功能
 */

export * from './protocol.js';
export * from './framing.js';
export * from './client.js';
export {
  parseProxyUrl,
  validateProxy,
  proxyFromEnv,
  resolveProxy,
  serverHostPort,
  PROXY_ENV_KEYS,
} from './proxy.js';
