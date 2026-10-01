/**
 * 出站 HTTP CONNECT 代理（客户端 → server 的 WS 连接经代理转发）。
 *
 * v1 范围（与 rust/src/proxy.rs 同规格）：
 * - 仅支持 HTTP CONNECT 代理（`http://host:port`，明文隧道）；**SOCKS 明确不支持**，
 *   配置了 socks 会直接报错而不是静默直连；
 * - wss:// 语义：https-proxy-agent 先与代理完成 CONNECT 建立隧道，TLS 在隧道
 *   **内部**由 ws 的 https 模块完成，端到端加密不变；
 * - 代理只作用于「客户端 → server」的 WS 出站；转发目标（target）流量语义不变。
 */

/** 代理 env 回退键（按序首个非空生效；与 rust/src/config.rs PROXY_ENV_KEYS 同序） */
export const PROXY_ENV_KEYS = [
  'HTTPS_PROXY',
  'https_proxy',
  'ALL_PROXY',
  'all_proxy',
] as const;

export interface ProxyEndpoint {
  host: string;
  port: number;
  /** 归一化后的代理 scheme（v1 恒为 'http'） */
  protocol: string;
}

/**
 * 解析代理 URL。规则与 rust parse_proxy_url 一致：
 * - `http://host:port`：v1 唯一支持的形态；端口缺省 80；
 * - 无 scheme 的 `host:port` / `host`：按 http 代理处理（宽容输入）；
 * - `socks5://` 等：抛错（v1 明确不支持 SOCKS）；
 * - `https://`（对代理本身走 TLS）：抛错（v1 未实现）；
 * - 带 userinfo：抛错（v1 不支持代理认证）。
 */
export function parseProxyUrl(input: string): ProxyEndpoint {
  const trimmed = (input ?? '').trim();
  if (!trimmed) {
    throw new Error('proxy 配置为空');
  }
  const normalized = trimmed.includes('://') ? trimmed : `http://${trimmed}`;
  let parsed: URL;
  try {
    parsed = new URL(normalized);
  } catch (e) {
    throw new Error(`proxy URL 无法解析: '${input}' (${(e as Error).message})`);
  }
  if (!parsed.hostname) {
    throw new Error(`proxy URL 缺少 host: '${input}'`);
  }
  if (parsed.username || parsed.password) {
    throw new Error(
      `proxy URL 暂不支持代理认证（user:pass@host:port）: '${input}'`
    );
  }
  const scheme = parsed.protocol.replace(/:$/, '');
  if (scheme === 'http') {
    return {
      host: parsed.hostname,
      port: parsed.port ? parseInt(parsed.port, 10) : 80,
      protocol: 'http',
    };
  }
  if (scheme.startsWith('socks')) {
    throw new Error(
      `proxy 暂不支持 SOCKS（'${input}'）：v1 仅支持 HTTP CONNECT 代理，` +
        `请改用 http://host:port 形态的代理地址`
    );
  }
  throw new Error(
    `proxy 不支持的 scheme '${scheme}': '${input}'（v1 仅支持 http://host:port）`
  );
}

/**
 * 校验代理配置（CLI/SDK 入口用）：合法时原样返回，非法抛错。
 * 传给 https-proxy-agent 的字符串保持用户原输入（其自身也接受 http:// URL）。
 */
export function validateProxy(input: string): string {
  parseProxyUrl(input);
  return (input ?? '').trim();
}

/**
 * 代理 env 回退：HTTPS_PROXY > https_proxy > ALL_PROXY > all_proxy。
 * 空白串视为未设置。返回 null 表示无代理。
 */
export function proxyFromEnv(
  env: Record<string, string | undefined>
): string | null {
  for (const key of PROXY_ENV_KEYS) {
    const value = env[key];
    if (typeof value === 'string' && value.trim()) {
      return value;
    }
  }
  return null;
}

/**
 * 代理解析（配置面入口）：**配置 > env > 无**。
 * 注意与 TUNELY_* 的 CLI > env > file 顺序不同：站点级配置覆盖部署环境
 * 注入的通用代理变量。拿到非空值即校验（SOCKS 等不支持的形态在此抛错）。
 */
export function resolveProxy(input: {
  fileProxy?: string | null;
  env?: Record<string, string | undefined>;
}): string | null {
  const raw =
    input.fileProxy && input.fileProxy.trim()
      ? input.fileProxy
      : proxyFromEnv(input.env ?? {});
  if (!raw) {
    return null;
  }
  return validateProxy(raw);
}

/**
 * 从 server URL（ws:// / wss://）提取 CONNECT 隧道的目标 host:port。
 * 端口缺省按 scheme 补齐：wss → 443，ws → 80。
 */
export function serverHostPort(serverUrl: string): { host: string; port: number } {
  let parsed: URL;
  try {
    parsed = new URL((serverUrl ?? '').trim());
  } catch (e) {
    throw new Error(`server URL 无法解析: '${serverUrl}' (${(e as Error).message})`);
  }
  if (!parsed.hostname) {
    throw new Error(`server URL 缺少 host: '${serverUrl}'`);
  }
  if (parsed.protocol === 'ws:') {
    return { host: parsed.hostname, port: parsed.port ? parseInt(parsed.port, 10) : 80 };
  }
  if (parsed.protocol === 'wss:') {
    return { host: parsed.hostname, port: parsed.port ? parseInt(parsed.port, 10) : 443 };
  }
  throw new Error(
    `server URL 应为 ws:// 或 wss://（当前 scheme '${parsed.protocol.replace(/:$/, '')}'）: '${serverUrl}'`
  );
}
