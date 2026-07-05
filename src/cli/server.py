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

try:
    from src.llm.config_store import (
        db_set_setting,
        sync_db_to_toml,
    )
except ImportError:
    from llm.config_store import (
        db_set_setting,
        sync_db_to_toml,
    )


_VALID_PROVIDERS: tuple[str, ...] = ("ollama", "lmstudio", "llamacpp", "cloud")


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
    # T33 启动时自动备份
    try:
        from src.store.backup import backup_now
        bk = backup_now()
        click.echo(f'✓ 已自动备份 → {bk.name}')
    except Exception as exc:
        click.echo(f'⚠ 自动备份失败：{exc}', err=True)


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


@main.command()
@click.option(
    "--provider", "-p",
    type=click.Choice(_VALID_PROVIDERS, case_sensitive=False),
    required=True,
    help="LLM 后端：ollama / lmstudio / llamacpp / cloud",
)
@click.option(
    "--base-url", "-u",
    default=None,
    help="LLM 后端 base URL（如 http://localhost:8083）",
)
@click.option(
    "--model", "-m",
    default=None,
    help="LLM 模型标识（如 qwen36-heretic-q8）",
)
@click.option(
    "--enable-thinking/--no-enable-thinking",
    default=False,
    help="是否启用 thinking 模式（Qwen3 推理模型；默认 False）",
)
def llm_set(
    provider: str,
    base_url: str | None,
    model: str | None,
    enable_thinking: bool,
) -> None:
    """设置 LLM 后端配置，写入 DB 并同步到 TOML。"""
    provider = provider.lower()
    db_set_setting("llm.default", provider, path=MTCA_DB_PATH)
    if base_url is not None:
        db_set_setting(f"{provider}.base_url", base_url, path=MTCA_DB_PATH)
    if model is not None:
        db_set_setting(f"{provider}.model", model, path=MTCA_DB_PATH)
    db_set_setting(
        f"{provider}.enable_thinking",
        "True" if enable_thinking else "False",
        path=MTCA_DB_PATH,
    )
    sync_db_to_toml(path=MTCA_DB_PATH)
    click.echo(f"✓ LLM 配置已写入 DB + TOML（provider={provider}）")
    if base_url:
        click.echo(f"  base_url: {base_url}")
    if model:
        click.echo(f"  model: {model}")
    click.echo(f"  enable_thinking: {enable_thinking}")


if __name__ == '__main__':
    main()
