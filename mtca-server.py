#!/usr/bin/env python3
"""
MTCA Server 启动入口（M1 阶段占位）

M1 阶段只提供 CLI，不提供真正的 server。
M2 会替换成 MCP Server + REST API。

用法：
    python mtca-server.py start        # 启动 MTCA 进程
    python mtca-server.py stop         # 停止
    python mtca-server.py status       # 状态
    python mtca-server.py init         # 初始化 DB
"""
import sys
import click
from src.store.sqlite import init_db, MTCA_DB_PATH

@click.group()
def main():
    """MTCA - Multi-Tier Context Architecture"""
    pass

@main.command()
def init():
    """初始化数据库（创建 schema）"""
    click.echo(f"Initializing MTCA at {MTCA_DB_PATH}")
    init_db()
    click.echo("✓ 数据库已初始化")

@main.command()
def status():
    """查看 MTCA 状态"""
    from src.store.sqlite import get_stats
    stats = get_stats()
    click.echo(f"MTCA DB: {MTCA_DB_PATH}")
    click.echo(f"  会话数: {stats.get('sessions', 0)}")
    click.echo(f"  消息数: {stats.get('messages', 0)}")
    click.echo(f"  段落数: {stats.get('segments', 0)}")

@main.command()
def start():
    """启动 MTCA 服务（M1 阶段是 stub）"""
    click.echo("MTCA Server 已启动（M1 stub 模式）")
    click.echo("M2 会接 MCP Server + REST API")
    click.echo("按 Ctrl+C 停止")

if __name__ == "__main__":
    main()
