# Tunely Server - 管理台

Tunely Server 的独立管理控制台，提供可视化的隧道管理功能。

## 功能特性

- 📊 **仪表盘** - 查看系统概览和统计信息
- 🔌 **隧道管理** - 创建、编辑、删除隧道
- 🔄 **实时状态** - 自动刷新隧道连接状态
- 🔑 **Token 管理** - 重新生成隧道 Token
- 📈 **统计信息** - 查看请求数、在线状态等

## 快速开始

### 安装依赖

```bash
cd packages/ws-tunnel/admin-console
pnpm install
```

### 开发模式

```bash
pnpm dev
```

管理台默认在 `http://localhost:3001` 启动（可通过环境变量 `VITE_PORT` 修改）。

### 构建生产版本

```bash
pnpm build
```

构建产物在 `dist/` 目录。

### 类型检查

```bash
pnpm typecheck
```

## 配置

### API 地址

管理台支持两种方式配置后端 API 地址：

#### 方式1: 使用 Vite 代理（开发环境推荐）

创建 `.env` 文件：

```bash
# 开发服务器端口（默认 3001）
VITE_PORT=3001

# 连接到本地 tunely server
VITE_API_TARGET=http://localhost:8000

# 连接到 Pro 服务器
VITE_API_TARGET=http://21.6.243.90:8000
```

然后启动开发服务器：

```bash
pnpm dev
```

#### 方式2: 直接指定 API 基础 URL

适用于开发和生产环境：

```bash
# 开发环境
VITE_API_BASE_URL=http://21.6.243.90:8000/api pnpm dev

# 生产环境构建
VITE_API_BASE_URL=https://tunely.example.com/api pnpm build
```

**注意**：如果设置了 `VITE_API_BASE_URL`，将直接使用该地址，忽略代理配置。

### API Key

管理台需要在 Header 中提供 `x-api-key` 来访问受保护的 API。

1. 在管理台顶部输入 API Key
2. API Key 会保存在浏览器的 localStorage 中
3. 所有 API 请求会自动携带该 Key

## 使用说明

### 1. 设置 API Key

首次使用时，在页面顶部输入 tunely server 的 `admin_api_key`，点击"设置"。

### 2. 查看仪表盘

- 总隧道数
- 在线隧道数
- 总请求数统计
- 系统信息（版本、域名配置等）

### 3. 管理隧道

在"隧道管理"页面可以：

- **创建隧道**：点击"创建隧道"按钮，输入域名、名称、描述
- **编辑隧道**：点击列表中的"编辑"按钮，修改名称、描述、启用状态
- **删除隧道**：点击"删除"按钮（需确认）
- **重新生成 Token**：点击"重新生成 Token"按钮，获取新 Token（旧 Token 将失效）

### 4. 实时状态

管理台每 5 秒自动刷新一次隧道状态，显示：
- 在线/离线状态
- 最后连接时间
- 请求数统计

## 技术栈

- **React 18** - UI 框架
- **TypeScript** - 类型安全
- **Vite** - 构建工具
- **Ant Design 5** - UI 组件库
- **Axios** - HTTP 客户端
- **Day.js** - 日期处理

## 项目结构

```
admin-console/
├── src/
│   ├── api/          # API 客户端
│   ├── components/   # 可复用组件
│   ├── hooks/       # React Hooks
│   ├── pages/       # 页面组件
│   ├── types/       # TypeScript 类型定义
│   ├── utils/       # 工具函数
│   ├── App.tsx      # 主应用组件
│   └── main.tsx     # 入口文件
├── public/          # 静态资源
└── package.json
```

## 开发

### 添加新功能

1. 在 `src/types/index.ts` 中定义类型
2. 在 `src/api/client.ts` 中添加 API 方法
3. 在 `src/components/` 中创建组件
4. 在 `src/pages/` 中组合页面

### 代码规范

- 使用 TypeScript 严格模式
- 遵循 React Hooks 最佳实践
- 使用 Ant Design 组件库
- 保持代码简洁和可维护

## 部署

### 静态部署

构建后的 `dist/` 目录可以部署到任何静态文件服务器：

```bash
# 构建
pnpm build

# 部署 dist/ 目录到服务器
```

### Nginx 配置示例

```nginx
server {
    listen 80;
    server_name admin.tunely.example.com;

    root /path/to/admin-console/dist;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    location /api {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

## 许可证

与主项目保持一致。
