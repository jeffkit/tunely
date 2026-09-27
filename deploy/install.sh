#!/usr/bin/env bash
# tunely 客户端一键安装（Linux x86_64 / arm64，musl 静态二进制 + systemd）
#
# 用法（在目标机器上以 root 执行）:
#   curl -sL https://dsht.agentstudio.cc/install.sh | sudo bash -s -- \
#     --token tun_xxx --target http://127.0.0.1:PORT [--name <实例名>] [--server wss://...]
#
# token 在 dsht 管理台（/console/）创建隧道后获取。
set -euo pipefail

BASE="https://dsht.agentstudio.cc"
TOKEN="" TARGET="http://127.0.0.1:8080" NAME="client" SERVER="wss://dsht.agentstudio.cc/ws/tunnel"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --token)  TOKEN="$2";  shift 2 ;;
    --target) TARGET="$2"; shift 2 ;;
    --server) SERVER="$2"; shift 2 ;;
    --name)   NAME="$2";   shift 2 ;;
    *) echo "未知参数: $1"; exit 1 ;;
  esac
done

[[ -z "$TOKEN" ]] && { echo "错误: 缺少 --token（管理台创建隧道后获取）"; exit 1; }
[[ $EUID -ne 0 ]] && { echo "错误: 请用 sudo 运行"; exit 1; }

case "$(uname -m)" in
  x86_64)          ARCH="amd64" ;;
  aarch64 | arm64) ARCH="arm64" ;;
  *) echo "不支持的架构: $(uname -m)"; exit 1 ;;
esac

echo "==> 下载 tunely 客户端 (linux-$ARCH)"
curl -fsSL "$BASE/downloads/tunely-linux-$ARCH" -o /usr/local/bin/tunely
chmod 755 /usr/local/bin/tunely

echo "==> 写入配置 /etc/tunely/$NAME.env"
mkdir -p /etc/tunely
cat > "/etc/tunely/$NAME.env" <<ENV
TUNELY_TOKEN=$TOKEN
TUNELY_SERVER=$SERVER
TUNELY_TARGET=$TARGET
ENV
chmod 640 "/etc/tunely/$NAME.env"

echo "==> 安装 systemd 服务 tunely-$NAME"
cat > "/etc/systemd/system/tunely-$NAME.service" <<UNIT
[Unit]
Description=Tunely tunnel client ($NAME)
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=/etc/tunely/$NAME.env
ExecStart=/usr/local/bin/tunely connect
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now "tunely-$NAME"

sleep 2
systemctl --no-pager -l status "tunely-$NAME" | head -5 || true
echo
echo "完成: tunely-$NAME 已安装并启动（凭据: /etc/tunely/$NAME.env）"
echo "查看状态: systemctl status tunely-$NAME / tunely status 需另配状态文件"
