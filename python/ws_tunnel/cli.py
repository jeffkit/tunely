"""
WS-Tunnel 命令行工具

使用示例:
    # 启动客户端
    ws-tunnel connect --token tun_xxx --target http://localhost:8080

    # 使用配置文件
    ws-tunnel connect --config tunnel.yaml

    # 管理隧道
    ws-tunnel tunnel create my-agent
    ws-tunnel tunnel list
    ws-tunnel tunnel delete my-agent
"""

import asyncio
import logging
import sys
from pathlib import Path

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from .client import TunnelClient
from .config import TunnelClientConfig

console = Console()


def setup_logging(verbose: bool = False) -> None:
    """配置日志"""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True)],
    )


@click.group()
@click.version_option(version="0.1.0")
def main():
    """WS-Tunnel - WebSocket 透明反向代理隧道"""
    pass


@main.command()
@click.option(
    "--server",
    "-s",
    default="ws://localhost:8000/ws/tunnel",
    help="服务端 WebSocket URL",
)
@click.option("--token", "-t", required=True, help="隧道令牌")
@click.option(
    "--target",
    "-T",
    default="http://localhost:8080",
    help="本地目标服务 URL",
)
@click.option("--reconnect", "-r", default=5.0, help="重连间隔（秒）")
@click.option("--verbose", "-v", is_flag=True, help="详细日志")
def connect(server: str, token: str, target: str, reconnect: float, verbose: bool):
    """连接到隧道服务器"""
    setup_logging(verbose)

    console.print(f"[bold blue]WS-Tunnel Client[/bold blue]")
    console.print(f"  服务端: {server}")
    console.print(f"  目标: {target}")
    console.print()

    config = TunnelClientConfig(
        server_url=server,
        token=token,
        target_url=target,
        reconnect_interval=reconnect,
    )
    client = TunnelClient(config=config)

    def on_connect():
        console.print(f"[green]✓[/green] 已连接: domain={client.domain}")

    def on_disconnect():
        console.print(f"[yellow]![/yellow] 连接断开")

    client.on_connect(on_connect)
    client.on_disconnect(on_disconnect)

    try:
        asyncio.run(client.run())
    except KeyboardInterrupt:
        console.print("\n[dim]已停止[/dim]")
        sys.exit(0)


@main.group()
def tunnel():
    """管理隧道"""
    pass


@tunnel.command("create")
@click.argument("domain")
@click.option("--name", "-n", help="隧道名称")
@click.option("--description", "-d", help="隧道描述")
@click.option("--server", "-s", default="http://localhost:8000", help="服务端 URL")
@click.option("--api-key", "-k", help="管理 API 密钥")
def tunnel_create(
    domain: str, name: str, description: str, server: str, api_key: str
):
    """创建隧道"""
    import httpx

    headers = {}
    if api_key:
        headers["x-api-key"] = api_key

    try:
        response = httpx.post(
            f"{server}/api/tunnels",
            json={"domain": domain, "name": name, "description": description},
            headers=headers,
        )

        if response.status_code == 201 or response.status_code == 200:
            data = response.json()
            console.print(f"[green]✓[/green] 隧道已创建")
            console.print(f"  域名: {data['domain']}")
            console.print(f"  令牌: [bold]{data['token']}[/bold]")
            console.print()
            console.print("[dim]使用以下命令连接:[/dim]")
            console.print(f"  ws-tunnel connect --token {data['token']} --target http://localhost:8080")
        else:
            console.print(f"[red]✗[/red] 创建失败: {response.text}")
            sys.exit(1)

    except Exception as e:
        console.print(f"[red]✗[/red] 请求失败: {e}")
        sys.exit(1)


@tunnel.command("list")
@click.option("--server", "-s", default="http://localhost:8000", help="服务端 URL")
@click.option("--api-key", "-k", help="管理 API 密钥")
def tunnel_list(server: str, api_key: str):
    """列出所有隧道"""
    import httpx

    headers = {}
    if api_key:
        headers["x-api-key"] = api_key

    try:
        response = httpx.get(f"{server}/api/tunnels", headers=headers)

        if response.status_code == 200:
            tunnels = response.json()

            if not tunnels:
                console.print("[dim]没有隧道[/dim]")
                return

            table = Table(title="隧道列表")
            table.add_column("域名", style="cyan")
            table.add_column("名称")
            table.add_column("状态")
            table.add_column("连接")
            table.add_column("请求数", justify="right")

            for t in tunnels:
                status = "[green]启用[/green]" if t["enabled"] else "[red]禁用[/red]"
                connected = "[green]●[/green]" if t["connected"] else "[dim]○[/dim]"
                table.add_row(
                    t["domain"],
                    t.get("name") or "-",
                    status,
                    connected,
                    str(t.get("total_requests", 0)),
                )

            console.print(table)
        else:
            console.print(f"[red]✗[/red] 请求失败: {response.text}")
            sys.exit(1)

    except Exception as e:
        console.print(f"[red]✗[/red] 请求失败: {e}")
        sys.exit(1)


@tunnel.command("delete")
@click.argument("domain")
@click.option("--server", "-s", default="http://localhost:8000", help="服务端 URL")
@click.option("--api-key", "-k", help="管理 API 密钥")
@click.option("--yes", "-y", is_flag=True, help="跳过确认")
def tunnel_delete(domain: str, server: str, api_key: str, yes: bool):
    """删除隧道"""
    import httpx

    if not yes:
        if not click.confirm(f"确定删除隧道 {domain}?"):
            return

    headers = {}
    if api_key:
        headers["x-api-key"] = api_key

    try:
        response = httpx.delete(f"{server}/api/tunnels/{domain}", headers=headers)

        if response.status_code == 200:
            console.print(f"[green]✓[/green] 隧道已删除: {domain}")
        else:
            console.print(f"[red]✗[/red] 删除失败: {response.text}")
            sys.exit(1)

    except Exception as e:
        console.print(f"[red]✗[/red] 请求失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
