# Issue: UV 安装 tunely 包时出现文件不完整问题

## 问题描述

日期：2026-01-27

在生产服务器上使用 `uv pip install tunely==0.2.3` 安装时，发现安装的包文件不完整，导致部分路由（如 `/api/info`）缺失。

## 问题表现

### 症状

1. **文件被截断**
   - 本地打包的 `tunely-0.2.3.tar.gz` 中 `server.py` 有 1361 行
   - PyPI 下载的包中 `server.py` 有 1361 行（正常）
   - 服务器上用 `uv` 安装后 `server.py` 只有 830 行（异常）

2. **路由缺失**
   - `_register_routes` 方法被截断
   - 缺少 `/api/info`、`/api/tunnels/{domain}/logs` 等后续注册的路由
   - MD5 校验不匹配

3. **hardlink 警告**
   ```
   warning: Failed to hardlink files; falling back to full copy. 
   This may lead to degraded performance.
   If the cache and target directories are on different filesystems, 
   hardlinking may not be supported.
   ```

### 环境信息

- **OS**: Rocky Linux (CentOS 替代)
- **Python**: 3.11
- **UV 版本**: 最新版（通过 curl 安装）
- **文件系统**: 不同挂载点（`.venv` 在本地，cache 可能在其他位置）

## 根本原因分析

### 可能的原因

1. **PyPI 包本身的问题**（已排除）
   - 从 PyPI 下载并验证，包是完整的
   - `tunely-0.2.3.tar.gz` MD5 正确

2. **UV 缓存损坏**（高可能性）
   - UV 有已知的缓存损坏问题
   - 第一次安装后缓存了不完整的文件
   - 后续安装从损坏的缓存读取

3. **网络中断**（可能性较低）
   - 下载过程中连接中断
   - 但 PyPI 包验证是完整的

4. **文件系统跨挂载问题**（可能）
   - hardlink 失败后 fallback 到 copy
   - 复制过程中出现问题

## 已知的 UV 相关问题

根据 GitHub Issues 搜索，UV 确实有一些已知问题：

1. **网络相关截断** ([#2586](https://github.com/astral-sh/uv/issues/2586))
   - HTTP 连接中断导致 wheel 文件不完整
   - 错误：`unexpected end of file`

2. **缓存损坏** ([#11043](https://github.com/astral-sh/uv/issues/11043))
   - 缓存反序列化错误
   - 使用 `--no-cache` 可以绕过

3. **Windows 文件访问错误** ([#1491](https://github.com/astral-sh/uv/issues/1491))
   - 与我们的 Linux 环境无关

4. **Hardlink 跨文件系统** ([#9500](https://github.com/astral-sh/uv/issues/9500))
   - 不会导致包损坏，只是性能问题
   - UV 会自动 fallback 到 copy

## 解决方案

### 临时解决方案（已实施）

直接从本地复制完整的 tunely 代码到服务器：

```bash
# 本地打包
cd /Users/kongjie/projects/agent-studio/tunely/python
tar czf /tmp/tunely-local.tar.gz tunely/

# 上传到服务器
scp /tmp/tunely-local.tar.gz pro:/tmp/

# 在服务器上部署
cd /tmp && tar xzf tunely-local.tar.gz
rm -rf /data/projects/as-dispatch/.venv/lib/python3.11/site-packages/tunely
cp -r tunely /data/projects/as-dispatch/.venv/lib/python3.11/site-packages/

# 重启服务
sudo systemctl restart as-dispatch
```

### 长期解决方案

#### 方案 1: 使用 --no-cache（推荐）

```bash
cd /data/projects/as-dispatch
uv pip install tunely==0.2.3 --no-cache --reinstall-package tunely
```

优点：
- 跳过缓存，直接从 PyPI 下载
- 避免缓存损坏问题

缺点：
- 安装速度稍慢

#### 方案 2: 设置 UV_LINK_MODE

```bash
export UV_LINK_MODE=copy
uv pip install tunely==0.2.3 --reinstall-package tunely
```

或在 systemd service 中配置：

```ini
[Service]
Environment="UV_LINK_MODE=copy"
```

优点：
- 避免 hardlink 相关问题
- 设置一次，全局生效

缺点：
- 性能稍差（文件复制而非 hardlink）

#### 方案 3: 从 Git 安装

```bash
# 如果有 Git 仓库
uv pip install git+https://github.com/xxx/tunely.git@v0.2.3
```

优点：
- 直接从源码安装，确保完整性

缺点：
- 需要 Git 仓库访问权限
- 安装速度较慢

#### 方案 4: 清理缓存后重装

```bash
# 清理 UV 缓存
uv cache clean tunely

# 或清理整个缓存
uv cache clean

# 重新安装
uv pip install tunely==0.2.3
```

优点：
- 解决缓存损坏问题

缺点：
- 下次可能还会出现

## 预防措施

### 1. 发布流程改进

在发布 tunely 到 PyPI 后，立即验证：

```bash
# 下载并验证
pip download tunely==0.2.3
tar -tzf tunely-0.2.3.tar.gz | grep server.py
tar -xzf tunely-0.2.3.tar.gz
wc -l tunely-0.2.3/tunely/server.py  # 应该是 1361 行
```

### 2. 部署流程改进

在 `as-dispatch` 的 systemd service 中添加环境变量：

```ini
[Service]
Environment="UV_LINK_MODE=copy"
Environment="UV_NO_CACHE=1"  # 可选
```

### 3. 监控和验证

部署后验证包完整性：

```bash
# 检查文件行数
wc -l /data/projects/as-dispatch/.venv/lib/python3.11/site-packages/tunely/server.py

# 检查路由是否正常
curl http://127.0.0.1:8083/api/info
```

### 4. 回退方案

保留一份完整的 tunely 代码在服务器上：

```bash
# 在服务器上保留备份
cp -r /tmp/tunely /data/projects/tunely-backup/

# 需要时快速恢复
cp -r /data/projects/tunely-backup/tunely \
  /data/projects/as-dispatch/.venv/lib/python3.11/site-packages/
```

## 后续行动

- [ ] 重新发布 tunely 0.2.4 到 PyPI，确保包完整
- [ ] 在服务器上使用 `--no-cache` 重新安装并验证
- [ ] 在 `as-dispatch` systemd service 中添加 `UV_LINK_MODE=copy`
- [ ] 更新部署文档，添加验证步骤
- [ ] 考虑向 UV 项目报告这个问题（如果能稳定复现）

## 参考链接

- UV Issues: https://github.com/astral-sh/uv/issues
- UV Documentation: https://github.com/astral-sh/uv
- Related Issues:
  - https://github.com/astral-sh/uv/issues/2586 (网络截断)
  - https://github.com/astral-sh/uv/issues/11043 (缓存损坏)
  - https://github.com/astral-sh/uv/issues/9500 (hardlink 问题)

## 相关文件

- `/data/projects/as-dispatch/.venv/lib/python3.11/site-packages/tunely/server.py`
- `/data/projects/as-dispatch/alembic/`
- `/data/projects/hitl/website/tun-console/`
