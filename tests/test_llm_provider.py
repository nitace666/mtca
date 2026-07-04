"""src/llm/provider.py 测试：覆盖 LLMProvider 协议 + 4 个后端 + 工厂。

测试覆盖（≥ 5 用例，全部 mock httpx，无真实网络）：

1.  test_provider_protocol         Protocol 结构（generate/embed/is_available）
2.  test_ollama_generate           Ollama POST /api/generate 解析
3.  test_lmstudio_generate         LM Studio POST /v1/chat/completions 解析
4.  test_get_provider_auto_detect  工厂 auto 模式顺序探测
5.  test_config_parsing            load_config 缺文件 / 合并 / 兜底
6.  test_cloud_provider_mock       CloudProvider 无 api_key 时走 mock
7.  test_llamacpp_optional_import  LlamaCppProvider 缺库时 is_available=False
8.  test_get_provider_invalid_name 工厂非法名称抛 ValueError
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from src.llm.provider import (
    DEFAULT_CONFIG,
    LMStudioProvider,
    LlamaCppProvider,
    LLMProvider,
    MTCA_CONFIG_PATH,
    OllamaProvider,
    CloudProvider,
    get_provider,
    load_config,
    probe_all,
)


# ---------------------------------------------------------------------------
# mock 工具：构造 httpx Response
# ---------------------------------------------------------------------------


def _mock_response(
    status_code: int = 200,
    json_data: Any = None,
) -> httpx.Response:
    """构造一个 httpx.Response 用于 mock side_effect。"""
    request = httpx.Request("POST", "http://test.local")
    if json_data is None:
        return httpx.Response(status_code, request=request)
    return httpx.Response(status_code, json=json_data, request=request)


def _mock_transport(handler):
    """构造一个 httpx.MockTransport，把 handler 注入到 httpx.Client/httpx.post。"""
    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# 测试 1：Protocol 结构
# ---------------------------------------------------------------------------


def test_provider_protocol() -> None:
    """OllamaProvider 应实现 LLMProvider 协议全部 3 个方法。"""
    p: LLMProvider = OllamaProvider()
    # 协议字段
    assert hasattr(p, "name")
    assert hasattr(p, "generate")
    assert hasattr(p, "embed")
    assert hasattr(p, "is_available")
    # 类型标注存在即可（运行时 duck-typing）
    assert callable(p.generate)
    assert callable(p.embed)
    assert callable(p.is_available)


# ---------------------------------------------------------------------------
# 测试 2：Ollama generate
# ---------------------------------------------------------------------------


def test_ollama_generate(monkeypatch: pytest.MonkeyPatch) -> None:
    """OllamaProvider.generate 应正确解析 /api/generate 的 response 字段。"""

    def fake_post(url: str, *args: Any, **kwargs: Any) -> httpx.Response:
        # 验证 endpoint 与 payload
        assert url.endswith("/api/generate")
        body = kwargs.get("json", {})
        assert body["model"] == "qwen3.6:35b-a3b"
        assert body["stream"] is False
        assert "prompt" in body and body["prompt"] == "hello"
        return _mock_response(
            200,
            {"response": "hi from ollama", "done": True},
        )

    monkeypatch.setattr(httpx, "post", fake_post)

    p = OllamaProvider(model="qwen3.6:35b-a3b")
    out = p.generate("hello")
    assert out == "hi from ollama"


def test_ollama_is_available_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """OllamaProvider.is_available 在 200 时返回 True。"""

    def fake_get(url: str, *args: Any, **kwargs: Any) -> httpx.Response:
        assert url.endswith("/api/tags")
        return _mock_response(200, {"models": []})

    monkeypatch.setattr(httpx, "get", fake_get)
    assert OllamaProvider().is_available() is True


def test_ollama_is_available_false_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OllamaProvider.is_available 在网络异常时返回 False。"""

    def fake_get(url: str, *args: Any, **kwargs: Any) -> httpx.Response:
        raise httpx.ConnectError("nope")

    monkeypatch.setattr(httpx, "get", fake_get)
    assert OllamaProvider().is_available() is False


def test_ollama_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    """OllamaProvider.embed 应返回 list[float]，长度与后端一致。"""

    def fake_post(url: str, *args: Any, **kwargs: Any) -> httpx.Response:
        return _mock_response(200, {"embedding": [0.1, 0.2, 0.3, 0.4]})

    monkeypatch.setattr(httpx, "post", fake_post)
    vec = OllamaProvider().embed("text")
    assert vec == [0.1, 0.2, 0.3, 0.4]
    assert all(isinstance(x, float) for x in vec)


# ---------------------------------------------------------------------------
# 测试 3：LM Studio generate
# ---------------------------------------------------------------------------


def test_lmstudio_generate(monkeypatch: pytest.MonkeyPatch) -> None:
    """LMStudioProvider.generate 应解析 OpenAI 风格 chat/completions。"""

    def fake_post(url: str, *args: Any, **kwargs: Any) -> httpx.Response:
        assert url.endswith("/v1/chat/completions")
        body = kwargs.get("json", {})
        assert body["model"] == "local-model"
        assert body["messages"] == [{"role": "user", "content": "ping"}]
        return _mock_response(
            200,
            {
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "pong"}}
                ]
            },
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    p = LMStudioProvider()
    assert p.generate("ping") == "pong"


def test_lmstudio_is_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """LMStudioProvider.is_available 探测 /v1/models。"""

    def fake_get(url: str, *args: Any, **kwargs: Any) -> httpx.Response:
        assert url.endswith("/v1/models")
        return _mock_response(200, {"data": []})

    monkeypatch.setattr(httpx, "get", fake_get)
    assert LMStudioProvider().is_available() is True


def test_lmstudio_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    """LMStudioProvider.embed 解析 OpenAI 风格 embeddings。"""

    def fake_post(url: str, *args: Any, **kwargs: Any) -> httpx.Response:
        return _mock_response(
            200,
            {"data": [{"index": 0, "embedding": [0.5, 0.5]}]},
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    vec = LMStudioProvider().embed("x")
    assert vec == [0.5, 0.5]


# ---------------------------------------------------------------------------
# 测试 4：工厂 auto 探测
# ---------------------------------------------------------------------------


def test_get_provider_auto_detect(monkeypatch: pytest.MonkeyPatch) -> None:
    """auto 模式：按 [llm].default 优先；该后端不可用时跳到下一个。"""
    # 1. ollama 不可用（探测失败）
    # 2. lmstudio 可用（应被选中）
    state = {"ollama_calls": 0, "lmstudio_calls": 0}

    def fake_get(url: str, *args: Any, **kwargs: Any) -> httpx.Response:
        if "/api/tags" in url:
            state["ollama_calls"] += 1
            raise httpx.ConnectError("down")
        if "/v1/models" in url:
            state["lmstudio_calls"] += 1
            return _mock_response(200, {"data": []})
        return _mock_response(404)

    monkeypatch.setattr(httpx, "get", fake_get)

    cfg = {
        "llm": {"default": "ollama", "fallback": "cloud"},
        "ollama": {
            "base_url": "http://localhost:11434",
            "model": "qwen3.6:35b-a3b",
        },
        "lmstudio": {
            "base_url": "http://localhost:1234",
            "model": "local-model",
        },
        "llamacpp": DEFAULT_CONFIG["llamacpp"],
        "cloud": DEFAULT_CONFIG["cloud"],
    }
    p = get_provider("auto", config=cfg)
    assert p.name == "lmstudio"
    assert state["ollama_calls"] >= 1
    assert state["lmstudio_calls"] >= 1


def test_get_provider_explicit_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """显式 name='ollama' 时直接构造该 provider（不等探测）。"""

    def fake_post(url: str, *args: Any, **kwargs: Any) -> httpx.Response:
        return _mock_response(200, {"response": "ok"})

    monkeypatch.setattr(httpx, "post", fake_post)
    p = get_provider("ollama", config=DEFAULT_CONFIG)
    assert isinstance(p, OllamaProvider)
    assert p.generate("x") == "ok"


def test_get_provider_invalid_name() -> None:
    """非法 name 应抛 ValueError。"""
    with pytest.raises(ValueError) as exc_info:
        get_provider("not_a_provider", config=DEFAULT_CONFIG)
    assert "未知 provider" in str(exc_info.value)


def test_get_provider_fallback_when_all_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全部不可用时按 [llm].fallback 返回（cloud 默认 mock 模式可用）。"""

    def fake_get(url: str, *args: Any, **kwargs: Any) -> httpx.Response:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_get)

    p = get_provider("auto", config=DEFAULT_CONFIG)
    # cloud mock 模式 is_available 恒为 True，应被选中
    assert isinstance(p, CloudProvider)


# ---------------------------------------------------------------------------
# 测试 5：config 解析
# ---------------------------------------------------------------------------


def test_config_parsing(tmp_path: Path) -> None:
    """load_config 缺文件返回默认；用户值覆盖默认；缺字段用默认补。"""
    # 1. 文件不存在 → DEFAULT_CONFIG 深拷贝
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg["llm"]["default"] == "ollama"
    assert cfg["ollama"]["base_url"] == "http://localhost:11434"
    assert cfg["ollama"]["model"] == "qwen3.6:35b-a3b"

    # 2. 部分用户配置 → 与默认合并，用户值优先
    cfg_path = tmp_path / "user.toml"
    cfg_path.write_text(
        '[llm]\ndefault = "cloud"\n[ollama]\nmodel = "llama3:8b"\n',
        encoding="utf-8",
    )
    cfg = load_config(cfg_path)
    assert cfg["llm"]["default"] == "cloud"          # 用户覆盖
    assert cfg["ollama"]["model"] == "llama3:8b"     # 用户覆盖
    # base_url 用户没写，仍是默认
    assert cfg["ollama"]["base_url"] == "http://localhost:11434"
    # lmstudio 用户没写，应有默认值
    assert cfg["lmstudio"]["base_url"] == "http://localhost:1234"


def test_config_corrupted_returns_default(tmp_path: Path) -> None:
    """损坏的 toml 文件不应抛异常，应返回默认配置。"""
    cfg_path = tmp_path / "bad.toml"
    cfg_path.write_text("this is = not valid toml [[[", encoding="utf-8")
    cfg = load_config(cfg_path)
    assert cfg == load_config(tmp_path / "missing.toml")
    assert cfg["llm"]["default"] == "ollama"


def test_config_path_constant() -> None:
    """MTCA_CONFIG_PATH 应指向 ~/.mtca/config.toml。"""
    assert MTCA_CONFIG_PATH == Path.home() / ".mtca" / "config.toml"
    assert MTCA_CONFIG_PATH.name == "config.toml"


# ---------------------------------------------------------------------------
# 测试 6：CloudProvider mock
# ---------------------------------------------------------------------------


def test_cloud_provider_mock() -> None:
    """CloudProvider 无 api_key 时 generate/embed 走 mock 分支。"""
    p = CloudProvider(api_key="")
    # mock is_available 恒为 True
    assert p.is_available() is True
    # mock generate 返回含 prompt 头部的固定格式
    out = p.generate("hello world\nsecond line")
    assert out.startswith("[mock:")
    assert "echo:" in out
    # mock embed 返回 384 维
    vec = p.embed("text")
    assert isinstance(vec, list)
    assert len(vec) == 384
    assert all(isinstance(x, float) for x in vec)


def test_cloud_provider_mock_embed_stable() -> None:
    """mock embed 应对同 text 返回稳定向量（hash 确定性）。"""
    p = CloudProvider(api_key="")
    a = p.embed("hello")
    b = p.embed("hello")
    assert a == b


# ---------------------------------------------------------------------------
# 测试 7：LlamaCppProvider 可选依赖
# ---------------------------------------------------------------------------


def test_llamacpp_optional_import(tmp_path: Path) -> None:
    """LlamaCppProvider 在无模型文件 / 无 llama_cpp 库时 is_available=False。"""
    fake_path = tmp_path / "no-such-model.gguf"
    p = LlamaCppProvider(model_path=str(fake_path))
    # 文件不存在 → False（不依赖 llama_cpp 是否安装）
    assert p.is_available() is False


def test_llamacpp_generate_no_library(tmp_path: Path, monkeypatch) -> None:
    """LlamaCppProvider 在缺库时 generate 抛 RuntimeError。"""
    # 创建假模型文件使 is_available 路径不抛
    fake_path = tmp_path / "fake.gguf"
    fake_path.write_bytes(b"GGUF\x00\x00\x00\x00")
    # 强制 ImportError
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "llama_cpp":
            raise ImportError("simulated missing")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    p = LlamaCppProvider(model_path=str(fake_path))
    with pytest.raises(RuntimeError) as exc_info:
        p.generate("hi")
    assert "llama-cpp-python" in str(exc_info.value) or "llama.cpp" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 测试 8：probe_all
# ---------------------------------------------------------------------------


def test_probe_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """probe_all 应返回所有 4 个 provider 的可用性字典。"""
    # ollama 探测失败；lmstudio 成功；llamacpp 模型不存在；cloud mock True
    def fake_get(url: str, *args: Any, **kwargs: Any) -> httpx.Response:
        if "/api/tags" in url:
            raise httpx.ConnectError("down")
        if "/v1/models" in url:
            return _mock_response(200, {"data": []})
        return _mock_response(200, {"data": []})

    monkeypatch.setattr(httpx, "get", fake_get)
    result = probe_all(config=DEFAULT_CONFIG)
    assert set(result.keys()) == {"ollama", "lmstudio", "llamacpp", "cloud"}
    assert result["ollama"] is False
    assert result["lmstudio"] is True
    assert result["llamacpp"] is False   # 模型文件不存在
    assert result["cloud"] is True       # mock 模式恒可用


# ---------------------------------------------------------------------------
# 测试 9：generate 异常路径
# ---------------------------------------------------------------------------


def test_ollama_generate_raises_on_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP 4xx/5xx 应被 raise_for_status 触发，最终包成 RuntimeError。"""

    def fake_post(url: str, *args: Any, **kwargs: Any) -> httpx.Response:
        return _mock_response(500, {"error": "boom"})

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(RuntimeError) as exc_info:
        OllamaProvider().generate("x")
    assert "Ollama" in str(exc_info.value)


def test_ollama_generate_raises_on_missing_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """后端 200 但缺 response 字段 → RuntimeError。"""

    def fake_post(url: str, *args: Any, **kwargs: Any) -> httpx.Response:
        return _mock_response(200, {"done": True})  # 缺 response

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(RuntimeError) as exc_info:
        OllamaProvider().generate("x")
    assert "缺少 response" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 测试 10：工厂失败构造
# ---------------------------------------------------------------------------


def test_get_provider_explicit_cloud_mock_works() -> None:
    """显式 name='cloud' 时返回 CloudProvider（mock 模式无需网络）。"""
    p = get_provider("cloud", config=DEFAULT_CONFIG)
    assert isinstance(p, CloudProvider)
    assert p.is_available() is True
    assert "echo:" in p.generate("hi")