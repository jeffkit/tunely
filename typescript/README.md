# tunely

WebSocket 隧道客户端 - 让内网服务可被外网访问。

## 安装

```bash
npm install tunely
# 或
pnpm add tunely
```

## 命令行使用

```bash
# 连接到隧道服务器
tunely connect --server ws://your-server/ws/tunnel --token tun_xxxxx --target http://localhost:8080

# 查看帮助
tunely --help
```

## SDK 使用

```typescript
import { TunnelClient } from 'tunely';

const client = new TunnelClient({
  serverUrl: 'ws://localhost:8000/ws/tunnel',
  token: 'tun_xxxxx',
  targetUrl: 'http://localhost:8080',
});

// 设置回调
client.on('onConnect', (domain) => {
  console.log(`已连接: ${domain}`);
});

client.on('onDisconnect', () => {
  console.log('连接断开');
});

// 启动
await client.run();
```

## 配置选项

| 选项 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `serverUrl` | string | - | 服务端 WebSocket URL |
| `token` | string | - | 隧道令牌 |
| `targetUrl` | string | - | 本地目标服务 URL |
| `reconnectInterval` | number | 5000 | 重连间隔（毫秒） |
| `maxReconnectAttempts` | number | 0 | 最大重连次数（0 = 无限） |
| `requestTimeout` | number | 300000 | 请求超时（毫秒） |
| `proxy` | string | - | 出站 HTTP CONNECT 代理（客户端 → server 的 WS 经此转发，见下） |

### 代理出站（HTTP CONNECT，0.5.0+）

跨境直连受限时，客户端 → server 的 WS 可经 HTTP CONNECT 代理转发
（经 `https-proxy-agent` 建 CONNECT 隧道，wss 的 TLS 在隧道内部完成，端到端加密不变）：

```typescript
const client = new TunnelClient({
  serverUrl: 'wss://your-server/ws/tunnel',
  token: 'tun_xxxxx',
  targetUrl: 'http://127.0.0.1:3080',
  proxy: 'http://proxy.lan:7890',
});
```

CLI / 配置文件形态：

```toml
# client.toml（与 rust/python 同一份）
proxy = "http://proxy.lan:7890"
```

```bash
# 环境变量回退（HTTPS_PROXY > https_proxy > ALL_PROXY > all_proxy）
export HTTPS_PROXY=http://proxy.lan:7890
tunely connect
```

- 优先级：**配置 > env > 无**（注意与 TUNELY_* 的 CLI > env > file 顺序不同：
  站点级配置覆盖部署环境注入的通用代理变量）。
- **作用域**：只作用于「客户端 → server」的 WS 出站；转发目标（target）的
  流量语义不变，不经代理。
- **v1 仅支持 HTTP CONNECT 代理，SOCKS 明确不支持**（非法配置构造期即报错）；
  对代理本身走 TLS（`https://` 代理）与代理认证（userinfo）暂不支持。

## TCP 隧道模式

当服务端配置了 `tcp_listen_port` 时，客户端自动支持 TCP 模式（与 Python 客户端行为一致）：
服务端把外部 TCP 连接的原始字节经 WebSocket 转发，客户端直连 `targetUrl` 解析出的 `host:port`。
HTTP 模式的请求-响应转发不受影响。

TCP 模式按原始字节透传，不做任何应用层解析，适合 WebSocket 升级、gRPC 等需要
`Host`/`Origin` 原样到达后端的场景。使用方式与 HTTP 模式完全相同：

```bash
# 服务端配置 tcp_listen_port=9080 后，外部访问 :9080 即到达本地 8080
tunely connect --server wss://your-server/ws/tunnel --token tun_xxxxx --target http://127.0.0.1:8080
```

## License

MIT
