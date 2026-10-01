/**
 * 代理出站配置面单测（与 rust/src/proxy.rs、rust/src/config.rs 同规格）
 *
 * - parseProxyUrl / validateProxy：http 形态、宽容输入、SOCKS/https/userinfo 拒绝
 * - proxyFromEnv：HTTPS_PROXY > https_proxy > ALL_PROXY > all_proxy
 * - resolveProxy：配置 > env > 无；非法值抛错
 * - serverHostPort：ws/wss 端口缺省补齐
 */

import { describe, it, expect } from 'vitest';
import {
  parseProxyUrl,
  validateProxy,
  proxyFromEnv,
  resolveProxy,
  serverHostPort,
  PROXY_ENV_KEYS,
} from './proxy.js';

describe('parseProxyUrl', () => {
  it('解析 http://host:port', () => {
    expect(parseProxyUrl('http://127.0.0.1:7890')).toEqual({
      host: '127.0.0.1',
      port: 7890,
      protocol: 'http',
    });
  });

  it('http 端口缺省 80', () => {
    expect(parseProxyUrl('http://proxy.corp.example')).toEqual({
      host: 'proxy.corp.example',
      port: 80,
      protocol: 'http',
    });
  });

  it('无 scheme 的宽容输入按 http 处理', () => {
    expect(parseProxyUrl('127.0.0.1:7890')).toMatchObject({ host: '127.0.0.1', port: 7890 });
    expect(parseProxyUrl('localhost')).toMatchObject({ host: 'localhost', port: 80 });
  });

  it('IPv6 host', () => {
    expect(parseProxyUrl('http://[::1]:7897')).toMatchObject({ host: '[::1]', port: 7897 });
  });

  it('拒绝 SOCKS（v1 明确不支持）', () => {
    for (const input of ['socks5://127.0.0.1:1080', 'socks5h://p:1080', 'socks4://p:1080']) {
      expect(() => parseProxyUrl(input)).toThrow(/SOCKS/);
    }
  });

  it('拒绝 https:// 代理（v1 未实现对代理本身的 TLS）', () => {
    expect(() => parseProxyUrl('https://proxy:8443')).toThrow(/http:\/\/host:port/);
  });

  it('拒绝代理认证 userinfo', () => {
    expect(() => parseProxyUrl('http://user:pass@proxy:8080')).toThrow(/认证/);
  });

  it('拒绝空值与垃圾输入', () => {
    expect(() => parseProxyUrl('')).toThrow();
    expect(() => parseProxyUrl('   ')).toThrow();
    expect(() => parseProxyUrl('http://')).toThrow();
  });
});

describe('validateProxy', () => {
  it('合法值原样返回（去首尾空白）', () => {
    expect(validateProxy(' http://127.0.0.1:7890 ')).toBe('http://127.0.0.1:7890');
  });

  it('非法值抛错', () => {
    expect(() => validateProxy('socks5://x:1080')).toThrow(/SOCKS/);
  });
});

describe('proxyFromEnv（HTTPS_PROXY > https_proxy > ALL_PROXY > all_proxy）', () => {
  it('大写 HTTPS_PROXY 优先', () => {
    expect(
      proxyFromEnv({
        HTTPS_PROXY: 'http://upper:1',
        https_proxy: 'http://lower:2',
        ALL_PROXY: 'http://all:3',
      })
    ).toBe('http://upper:1');
  });

  it('小写 https_proxy 次之', () => {
    expect(
      proxyFromEnv({
        https_proxy: 'http://lower:2',
        ALL_PROXY: 'http://all:3',
        all_proxy: 'http://all-lower:4',
      })
    ).toBe('http://lower:2');
  });

  it('ALL_PROXY 第三、all_proxy 兜底', () => {
    expect(proxyFromEnv({ ALL_PROXY: 'http://all:3', all_proxy: 'http://all-lower:4' })).toBe(
      'http://all:3'
    );
    expect(proxyFromEnv({ all_proxy: 'http://all-lower:4' })).toBe('http://all-lower:4');
  });

  it('空白串视为未设置', () => {
    expect(proxyFromEnv({ HTTPS_PROXY: '   ', ALL_PROXY: 'http://all:3' })).toBe('http://all:3');
  });

  it('全部未设置返回 null', () => {
    expect(proxyFromEnv({})).toBeNull();
    expect(proxyFromEnv({ HTTPS_PROXY: '' })).toBeNull();
  });

  it('键序与 rust PROXY_ENV_KEYS 同规格', () => {
    expect([...PROXY_ENV_KEYS]).toEqual(['HTTPS_PROXY', 'https_proxy', 'ALL_PROXY', 'all_proxy']);
  });
});

describe('resolveProxy（配置 > env > 无）', () => {
  it('配置文件优先于 env（与 TUNELY_* 相反的明确语义）', () => {
    expect(
      resolveProxy({
        fileProxy: 'http://file-proxy:7890',
        env: { HTTPS_PROXY: 'http://env-proxy:3128' },
      })
    ).toBe('http://file-proxy:7890');
  });

  it('无配置时回退 env', () => {
    expect(resolveProxy({ env: { HTTPS_PROXY: 'http://env-proxy:3128' } })).toBe(
      'http://env-proxy:3128'
    );
  });

  it('两处都没有 → null（直连）', () => {
    expect(resolveProxy({ env: {} })).toBeNull();
    expect(resolveProxy({})).toBeNull();
  });

  it('配置为空白串视同未配置 → 回退 env', () => {
    expect(
      resolveProxy({ fileProxy: '   ', env: { ALL_PROXY: 'http://all:3' } })
    ).toBe('http://all:3');
  });

  it('非法值（SOCKS）无论来自配置还是 env 都抛错', () => {
    expect(() => resolveProxy({ fileProxy: 'socks5://x:1080' })).toThrow(/SOCKS/);
    expect(() => resolveProxy({ env: { ALL_PROXY: 'socks5://x:1080' } })).toThrow(/SOCKS/);
  });
});

describe('serverHostPort（CONNECT 目标提取）', () => {
  it('显式端口', () => {
    expect(serverHostPort('ws://myhost:9000/ws/tunnel')).toEqual({ host: 'myhost', port: 9000 });
  });

  it('缺省端口按 scheme 补齐：ws→80、wss→443', () => {
    expect(serverHostPort('ws://1.2.3.4/ws/tunnel')).toEqual({ host: '1.2.3.4', port: 80 });
    expect(serverHostPort('wss://tun.example.com/ws/tunnel')).toEqual({
      host: 'tun.example.com',
      port: 443,
    });
  });

  it('非 ws/wss scheme 抛错', () => {
    expect(() => serverHostPort('https://example.com/ws/tunnel')).toThrow(/ws:\/\//);
  });

  it('垃圾输入抛错', () => {
    expect(() => serverHostPort('not a url')).toThrow();
  });
});
