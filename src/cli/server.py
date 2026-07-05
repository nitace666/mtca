'''MTCA Server 启动入口（src/cli/server.py — T_A2 模块化）

从根目录 mtca-server.py 迁移而来；保持原 CLI 子命令 + 默认行为不变。
pyproject.toml [project.scripts] 指向 cli.server:main。
'''
import os

# 可选埋点：仅当 MTCA_TRACE=1 + 3 个凭证齐全时才启用
# 默认不安装 syncause_tracer；用户想启用自行 pip install syncause-tracer
if os.environ.get('MTCA_TRACE') == '1':
    from syncause_tracer import initialize
    initialize(
        api_key=os.environ['SYNCAUSE_API_KEY'],
        proxy=os.environ['SYNCAUSE_PROXY'],
        app_name='MTCA',
        project_id=os.environ['SYNCAUSE_PROJECT_ID'],
    )

import click
try:
    from src.store.sqlite import MTCA_DB_PATH, get_stats, init_db
except ImportError:
    from store.sqlite import MTCA_DB_PATH, get_stats, init_db


@click.group()
def main():
    '''MTCA - Multi-Tier Context Architecture'''
    pass


@main.command()
def init():
    '''初始化数据库（创建 schema）'''
    click.echo(f'Initializing MTCA at {MTCA_DB_PATH}')
    init_db()
    click.echo('✓ 数据库已初始化')


@main.command()
def status():
    '''查看 MTCA 状态'''
    stats = get_stats()
    click.echo(f'MTCA DB: {MTCA_DB_PATH}')
    click.echo(f'  会话数: {stats.get("sessions", 0)}')
    click.echo(f'  消息数: {stats.get("messages", 0)}')
    click.echo(f'  段落数: {stats.get("segments", 0)}')


@main.command()
def start():
    '''启动 MTCA 服务（M1 阶段是 stub）'''
    click.echo('MTCA Server 已启动（M1 stub 模式）')
    click.echo('M2 会接 MCP Server + REST API')
    click.echo('按 Ctrl+C 停止')


if __name__ == '__main__':
    main()
