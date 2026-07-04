"""MTCA LLM 抽象层（v0.4 / T10）

统一屏蔽 4 类 LLM 后端：Ollama / LM Studio / llama.cpp / 云端 API。
上层（打分 / 召回 / 压缩）只依赖 :class:`LLMProvider` 协议。

公共 API：
- ``LLMProvider`` Protocol
- ``OllamaProvider`` / ``LMStudioProvider`` / ``LlamaCppProvider`` / ``CloudProvider``
- ``get_provider(name='auto', config=None) -> LLMProvider``
- ``load_config(path=None) -> dict`` / ``save_config(config, path=None) -> None``
- ``DEFAULT_CONFIG`` 默认配置字典 / ``MTCA_CONFIG_PATH`` 配置路径常量

配置示例（~/.mtca/config.toml）::

    [llm]
    default = "ollama"
    fallback = "cloud"

    [ollama]
    base_url = "http://localhost:11434"
    model = "qwen3.6:35b-a3b"
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol, Union

try:
    import httpx
except ImportError as exc:  # pragma: no cover - httpx 是 T10 强制依赖
    raise ImportError(
        "T10 需要 httpx，请先执行：pip install httpx"
    ) from exc

# toml 读取：Python 3.11+ 自带 tomllib；3.10 需要 tomli
if sys.version_info >= (3, 11):
    import tomllib as _toml  # type: ignore[import-not-found]
else:  # pragma: no cover - 3.10 兼容路径
    try:
        import tomli as _toml  # type: ignore[import-untyped, import-not-found]
    except ImportError:
        _toml = None  # type: ignore[assignment]

try:
    import tomllib as _toml_writer  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - 3.10 兼容路径
    try:
        import tomli_w as _toml_writer  # type: ignore[import-untyped, import-not-found]
    except ImportError:
        _toml_writer = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 配置路径：用户主目录下 ~/.mtca/config.toml
MTCA_CONFIG_PATH: Path = Path.home() / ".mtca" / "config.toml"

# 默认探测顺序（auto 模式按此顺序逐个 is_available）
_PROBE_ORDER: tuple[str, ...] = ("ollama", "lmstudio", "llamacpp", "cloud")

# HTTP 探测 / 调用超时（秒）：保持 M1 主链路 < 50ms 不被 LLM 拖垮
_HTTP_TIMEOUT: float = 2.0
_HTTP_GENERATE_TIMEOUT: float = 30.0

# is_available 探测的 endpoint（轻量，不消耗 token）
_OLLAMA_TAGS_PATH: str = "/api/tags"
_LMSTUDIO_MODELS_PATH: str = "/v1/models"

# LLM 默认参数
_DEFAULT_TEMPERATURE: float = 0.7
_DEFAULT_MAX_TOKENS: int = 512
_EMBED_DIM_DEFAULT: int = 384  # bge-small-zh 维度


# ---------------------------------------------------------------------------
# 默认配置（缺文件 / 缺字段时兜底）
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: dict[str, Any] = {
    "llm": {
        "default": "ollama",
        "fallback": "cloud",
    },
    "ollama": {
        "base_url": "http://localhost:11434",
        "model": "qwen3.6:35b-a3b",
        "timeout": _HTTP_GENERATE_TIMEOUT,
    },
    "lmstudio": {
        "base_url": "http://localhost:1234",
        "model": "local-model",
        "timeout": _HTTP_GENERATE_TIMEOUT,
    },
    "llamacpp": {
        "model_path": "~/models/qwen2.5-7b-instruct-q4_k_m.gguf",
        "n_ctx": 4096,
        "n_threads": 8,
    },
    "cloud": {
        "provider": "openai",
        "api_base": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-4o-mini",
        "timeout": _HTTP_GENERATE_TIMEOUT,
    },
}


# ---------------------------------------------------------------------------
# 协议
# ---------------------------------------------------------------------------


class LLMProvider(Protocol):
    """LLM 抽象协议。

    所有后端（Ollama / LM Studio / llama.cpp / 云端）都需实现 3 个方法。
    上层调用方只依赖该协议，便于 mock 与切换。
    """

    name: str

    def generate(self, prompt: str, **kwargs: Any) -> str:
        """同步生成文本回复。

        参数：
            prompt: 用户输入（含上下文拼接后的最终提示）。
            **kwargs: 透传后端特定参数（temperature / max_tokens / stop...）。

        返回：
            模型生成的纯文本（不含 prompt）。

        异常：
            RuntimeError: 后端不可用 / 网络失败 / 返回格式异常。
        """
        ...

    def embed(self, text: str) -> list[float]:
        """生成嵌入向量。

        参数：
            text: 待嵌入文本。

        返回：
            浮点数向量；默认 384 维（bge-small-zh 一致）。

        异常：
            RuntimeError: 后端不支持嵌入 / 调用失败。
        """
        ...

    def is_available(self) -> bool:
        """轻量探活；用于 auto 模式探测。

        返回：
            True 表示该 provider 当前可用；False 表示不可用（应尝试下一个）。
        """
        ...


# ---------------------------------------------------------------------------
# 配置加载 / 保存
# ---------------------------------------------------------------------------


def load_config(path: Optional[Union[Path, str]] = None) -> dict[str, Any]:
    """加载配置文件，返回 dict。

    行为：
        - 文件不存在 / 解析失败：返回 ``DEFAULT_CONFIG`` 深拷贝。
        - 文件存在但缺字段：用 ``DEFAULT_CONFIG`` 兜底合并（用户值优先）。
        - 不依赖 tomllib（3.10 兼容），缺 toml 库时返回默认。

    参数：
        path: 配置文件路径；None 时使用 ``MTCA_CONFIG_PATH``。

    返回：
        配置字典（嵌套 dict 结构）。
    """
    cfg_path = Path(path) if path is not None else MTCA_CONFIG_PATH

    if _toml is None:
        # 无 toml 库时直接返回默认
        return _deep_copy(DEFAULT_CONFIG)

    if not cfg_path.exists():
        return _deep_copy(DEFAULT_CONFIG)

    try:
        with open(cfg_path, "rb") as f:
            user_cfg = _toml.load(f)
    except (OSError, ValueError):
        # 解析失败：返回默认，不抛异常（启动期不能因配置坏掉而崩）
        return _deep_copy(DEFAULT_CONFIG)

    return _merge_config(_deep_copy(DEFAULT_CONFIG), user_cfg)


def save_config(
    config: dict[str, Any],
    path: Optional[Union[Path, str]] = None,
) -> None:
    """保存配置到 toml 文件。

    参数：
        config: 配置字典。
        path: 写入路径；None 时使用 ``MTCA_CONFIG_PATH``。

    异常：
        RuntimeError: 写入失败（无 toml 写入库 / 权限不足）。
    """
    if _toml_writer is None:
        raise RuntimeError(
            "保存配置需要 tomli_w（或 Python 3.11+）；请执行：pip install tomli-w"
        )

    cfg_path = Path(path) if path is not None else MTCA_CONFIG_PATH
    try:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cfg_path, "wb") as f:
            _toml_writer.dump(config, f)
    except OSError as exc:
        raise RuntimeError(f"写入配置文件失败：{exc}") from exc


def _deep_copy(cfg: dict[str, Any]) -> dict[str, Any]:
    """浅深混合拷贝：仅拷贝顶层与第二层 dict，保留叶节点引用（叶节点皆不可变）。"""
    out: dict[str, Any] = {}
    for k, v in cfg.items():
        if isinstance(v, dict):
            out[k] = dict(v)
        else:
            out[k] = v
    return out


def _merge_config(
    base: dict[str, Any],
    override: dict[str, Any],
) -> dict[str, Any]:
    """递归合并：override 覆盖 base 的同名字段。"""
    for key, val in override.items():
        if (
            key in base
            and isinstance(base[key], dict)
            and isinstance(val, dict)
        ):
            _merge_config(base[key], val)
        else:
            base[key] = val
    return base


# ---------------------------------------------------------------------------
# 工具：从配置取值，带默认值
# ---------------------------------------------------------------------------


def _get(cfg: dict[str, Any], section: str, key: str, default: Any = None) -> Any:
    """从嵌套 dict 取值，缺则返回 default。"""
    sect = cfg.get(section)
    if not isinstance(sect, dict):
        return default
    return sect.get(key, default)


# ---------------------------------------------------------------------------
# Ollama 实现
# ---------------------------------------------------------------------------


@dataclass
class OllamaProvider:
    """Ollama 本地服务 provider（http://localhost:11434）。

    API 文档参考 https://github.com/ollama/ollama/blob/main/docs/api.md
    - ``GET /api/tags`` 探活
    - ``POST /api/generate`` 文本生成（非流式）
    - ``POST /api/embeddings`` 嵌入向量
    """

    name: str = "ollama"
    base_url: str = "http://localhost:11434"
    model: str = "qwen3.6:35b-a3b"
    timeout: float = _HTTP_GENERATE_TIMEOUT

    def __post_init__(self) -> None:
        # 去掉 base_url 末尾的 /，避免 urljoin 拼接错误
        self.base_url = self.base_url.rstrip("/")

    # ---- 协议实现 ----

    def generate(self, prompt: str, **kwargs: Any) -> str:
        """调用 POST /api/generate；返回 response 字段。"""
        url = f"{self.base_url}/api/generate"
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": kwargs.get("temperature", _DEFAULT_TEMPERATURE),
            },
        }
        if "max_tokens" in kwargs:
            payload["options"]["num_predict"] = int(kwargs["max_tokens"])
        try:
            resp = httpx.post(
                url,
                json=payload,
                timeout=self.timeout or _HTTP_GENERATE_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Ollama 生成失败：{exc}") from exc
        except ValueError as exc:
            raise RuntimeError(f"Ollama 返回非 JSON：{exc}") from exc

        if "response" not in data:
            raise RuntimeError(f"Ollama 返回缺少 response 字段：{data!r}")
        return str(data["response"])

    def embed(self, text: str) -> list[float]:
        """调用 POST /api/embeddings；返回 embedding 字段。"""
        url = f"{self.base_url}/api/embeddings"
        try:
            resp = httpx.post(
                url,
                json={"model": self.model, "prompt": text},
                timeout=self.timeout or _HTTP_GENERATE_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Ollama 嵌入失败：{exc}") from exc
        except ValueError as exc:
            raise RuntimeError(f"Ollama 返回非 JSON：{exc}") from exc

        if "embedding" not in data:
            raise RuntimeError(f"Ollama 返回缺少 embedding 字段：{data!r}")
        vec = data["embedding"]
        if not isinstance(vec, list):
            raise RuntimeError(f"Ollama embedding 不是 list：{type(vec).__name__}")
        return [float(x) for x in vec]

    def is_available(self) -> bool:
        """GET /api/tags 探活；2xx 视为可用。"""
        url = f"{self.base_url}{_OLLAMA_TAGS_PATH}"
        try:
            resp = httpx.get(url, timeout=_HTTP_TIMEOUT)
        except httpx.HTTPError:
            return False
        return 200 <= resp.status_code < 300


# ---------------------------------------------------------------------------
# LM Studio 实现（OpenAI 兼容协议）
# ---------------------------------------------------------------------------


@dataclass
class LMStudioProvider:
    """LM Studio 本地服务 provider（http://localhost:1234，OpenAI 兼容）。

    协议与 OpenAI Chat Completions / Embeddings 一致，但不需要 api_key。
    """

    name: str = "lmstudio"
    base_url: str = "http://localhost:1234"
    model: str = "local-model"
    timeout: float = _HTTP_GENERATE_TIMEOUT

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")

    def generate(self, prompt: str, **kwargs: Any) -> str:
        """调用 POST /v1/chat/completions；取首条 choice.message.content。"""
        url = f"{self.base_url}/v1/chat/completions"
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": kwargs.get("temperature", _DEFAULT_TEMPERATURE),
            "stream": False,
        }
        if "max_tokens" in kwargs:
            payload["max_tokens"] = int(kwargs["max_tokens"])
        try:
            resp = httpx.post(
                url,
                json=payload,
                timeout=self.timeout or _HTTP_GENERATE_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"LM Studio 生成失败：{exc}") from exc
        except ValueError as exc:
            raise RuntimeError(f"LM Studio 返回非 JSON：{exc}") from exc

        try:
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"LM Studio 返回格式异常：{data!r}") from exc

    def embed(self, text: str) -> list[float]:
        """调用 POST /v1/embeddings；取首条 data.embedding。"""
        url = f"{self.base_url}/v1/embeddings"
        try:
            resp = httpx.post(
                url,
                json={"model": self.model, "input": text},
                timeout=self.timeout or _HTTP_GENERATE_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"LM Studio 嵌入失败：{exc}") from exc
        except ValueError as exc:
            raise RuntimeError(f"LM Studio 返回非 JSON：{exc}") from exc

        try:
            vec = data["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"LM Studio 返回格式异常：{data!r}") from exc
        if not isinstance(vec, list):
            raise RuntimeError(f"LM Studio embedding 不是 list：{type(vec).__name__}")
        return [float(x) for x in vec]

    def is_available(self) -> bool:
        """GET /v1/models 探活。"""
        url = f"{self.base_url}{_LMSTUDIO_MODELS_PATH}"
        try:
            resp = httpx.get(url, timeout=_HTTP_TIMEOUT)
        except httpx.HTTPError:
            return False
        return 200 <= resp.status_code < 300


# ---------------------------------------------------------------------------
# llama.cpp 实现（直接调用 llama-cpp-python）
# ---------------------------------------------------------------------------


@dataclass
class LlamaCppProvider:
    """llama-cpp-python 直接调用 provider。

    与 HTTP 方案不同：llama.cpp 把模型加载到内存，无网络依赖。
    启动期较慢（数秒），但推理最快；适合长期常驻服务。

    依赖（可选）：
        pip install llama-cpp-python

    未安装时 ``is_available()`` 返回 False，且 ``generate/embed`` 抛 ImportError。
    """

    name: str = "llamacpp"
    model_path: str = "~/models/qwen2.5-7b-instruct-q4_k_m.gguf"
    n_ctx: int = 4096
    n_threads: int = 8
    _llama: Any = field(default=None, init=False, repr=False)
    _load_error: Optional[str] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        # 展开 ~ 用户目录
        self.model_path = os.path.expanduser(self.model_path)

    def _get_llama(self) -> Any:
        """懒加载 llama-cpp-python 模型；首次调用时才加载。"""
        if self._llama is not None:
            return self._llama
        if self._load_error is not None:
            raise RuntimeError(f"llama.cpp 模型加载失败：{self._load_error}")

        try:
            from llama_cpp import Llama  # type: ignore[import-untyped]
        except ImportError as exc:
            self._load_error = str(exc)
            raise RuntimeError(
                "LlamaCppProvider 需要 llama-cpp-python；"
                "请执行：pip install llama-cpp-python"
            ) from exc

        if not Path(self.model_path).exists():
            self._load_error = f"模型文件不存在：{self.model_path}"
            raise RuntimeError(self._load_error)

        try:
            self._llama = Llama(
                model_path=self.model_path,
                n_ctx=self.n_ctx,
                n_threads=self.n_threads,
            )
        except Exception as exc:  # llama-cpp 抛异常类型不可控
            self._load_error = str(exc)
            raise RuntimeError(f"llama.cpp 模型加载失败：{exc}") from exc
        return self._llama

    def generate(self, prompt: str, **kwargs: Any) -> str:
        """调用 Llama.__call__()；取首条 choice.text。"""
        llama = self._get_llama()
        try:
            result = llama(
                prompt,
                max_tokens=kwargs.get("max_tokens", _DEFAULT_MAX_TOKENS),
                temperature=kwargs.get("temperature", _DEFAULT_TEMPERATURE),
                stop=kwargs.get("stop"),
            )
        except Exception as exc:
            raise RuntimeError(f"llama.cpp 生成失败：{exc}") from exc
        try:
            return str(result["choices"][0]["text"])
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"llama.cpp 返回格式异常：{result!r}") from exc

    def embed(self, text: str) -> list[float]:
        """调用 Llama.create_embedding()；取首条 embedding。"""
        llama = self._get_llama()
        try:
            result = llama.create_embedding(text)
        except Exception as exc:
            raise RuntimeError(f"llama.cpp 嵌入失败：{exc}") from exc
        try:
            vec = result["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"llama.cpp 嵌入返回异常：{result!r}") from exc
        if not isinstance(vec, list):
            raise RuntimeError(f"llama.cpp embedding 不是 list：{type(vec).__name__}")
        return [float(x) for x in vec]

    def is_available(self) -> bool:
        """检查模型文件存在 + 可选库可导入；不实际加载（避免首次调用阻塞）。"""
        if self._load_error is not None:
            return False
        if not Path(self.model_path).exists():
            return False
        try:
            import llama_cpp  # type: ignore[import-untyped, unused-ignore]  # noqa: F401
        except ImportError:
            return False
        return True


# ---------------------------------------------------------------------------
# 云端 API 实现（OpenAI / Anthropic mock）
# ---------------------------------------------------------------------------


@dataclass
class CloudProvider:
    """云端 API provider（OpenAI 兼容 / Anthropic mock）。

    M1 阶段：仅 mock，不实调。``generate/embed`` 在未配置 api_key 时返回固定 fake 数据，
    以便上层（压缩 / 打分）跑通；M3 才接真实流量。

    字段：
        provider: 'openai' | 'anthropic' | 'mock'（mock 时不发起任何 HTTP）
        api_base: API base URL
        api_key: API key（空 = mock 模式）
        model: 模型名
        timeout: HTTP 超时（秒）
    """

    name: str = "cloud"
    provider: str = "openai"
    api_base: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    timeout: float = _HTTP_GENERATE_TIMEOUT

    def __post_init__(self) -> None:
        self.api_base = self.api_base.rstrip("/")

    # ---- 内部 ----

    def _is_mock(self) -> bool:
        """空 api_key 或 provider='mock' 时走 mock 分支。"""
        return not self.api_key or self.provider == "mock"

    def _mock_generate(self, prompt: str, **kwargs: Any) -> str:
        """返回固定 fake 回复（包含 prompt 摘要，便于测试断言）。"""
        head = prompt.strip().splitlines()[0] if prompt.strip() else ""
        head = head[:40]
        return f"[mock:{self.model}] echo: {head}"

    def _mock_embed(self, text: str) -> list[float]:
        """基于文本哈希返回稳定伪向量；维度 = _EMBED_DIM_DEFAULT。"""
        # 简单 hash 扩展：保证同 text → 同向量，便于断言
        seed = 0
        for ch in text:
            seed = (seed * 131 + ord(ch)) & 0xFFFFFFFF
        vec: list[float] = []
        x = seed
        for _ in range(_EMBED_DIM_DEFAULT):
            x = (x * 1103515245 + 12345) & 0x7FFFFFFF
            vec.append((x / 0x7FFFFFFF) - 0.5)
        return vec

    # ---- 协议实现 ----

    def generate(self, prompt: str, **kwargs: Any) -> str:
        """调用云端 /v1/chat/completions；无 api_key 时 mock。"""
        if self._is_mock():
            return self._mock_generate(prompt, **kwargs)

        url = f"{self.api_base}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": kwargs.get("temperature", _DEFAULT_TEMPERATURE),
            "stream": False,
        }
        if "max_tokens" in kwargs:
            payload["max_tokens"] = int(kwargs["max_tokens"])
        try:
            resp = httpx.post(
                url,
                json=payload,
                headers=headers,
                timeout=self.timeout or _HTTP_GENERATE_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"云端 API 生成失败：{exc}") from exc
        except ValueError as exc:
            raise RuntimeError(f"云端 API 返回非 JSON：{exc}") from exc

        try:
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"云端 API 返回格式异常：{data!r}") from exc

    def embed(self, text: str) -> list[float]:
        """调用云端 /v1/embeddings；无 api_key 时 mock。"""
        if self._is_mock():
            return self._mock_embed(text)

        url = f"{self.api_base}/embeddings"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = httpx.post(
                url,
                json={"model": self.model, "input": text},
                headers=headers,
                timeout=self.timeout or _HTTP_GENERATE_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPError as exc:
            raise RuntimeError(f"云端 API 嵌入失败：{exc}") from exc
        except ValueError as exc:
            raise RuntimeError(f"云端 API 返回非 JSON：{exc}") from exc

        try:
            vec = data["data"][0]["embedding"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"云端 API 返回格式异常：{data!r}") from exc
        if not isinstance(vec, list):
            raise RuntimeError(
                f"云端 API embedding 不是 list：{type(vec).__name__}"
            )
        return [float(x) for x in vec]

    def is_available(self) -> bool:
        """mock 模式恒为 True；真实模式探测 GET /models。"""
        if self._is_mock():
            return True
        url = f"{self.api_base}/models"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            resp = httpx.get(url, headers=headers, timeout=_HTTP_TIMEOUT)
        except httpx.HTTPError:
            return False
        return 200 <= resp.status_code < 300


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def _build_ollama(cfg: dict[str, Any]) -> OllamaProvider:
    return OllamaProvider(
        base_url=_get(cfg, "ollama", "base_url", DEFAULT_CONFIG["ollama"]["base_url"]),
        model=_get(cfg, "ollama", "model", DEFAULT_CONFIG["ollama"]["model"]),
        timeout=float(_get(cfg, "ollama", "timeout", _HTTP_GENERATE_TIMEOUT)),
    )


def _build_lmstudio(cfg: dict[str, Any]) -> LMStudioProvider:
    return LMStudioProvider(
        base_url=_get(
            cfg, "lmstudio", "base_url", DEFAULT_CONFIG["lmstudio"]["base_url"]
        ),
        model=_get(cfg, "lmstudio", "model", DEFAULT_CONFIG["lmstudio"]["model"]),
        timeout=float(
            _get(cfg, "lmstudio", "timeout", _HTTP_GENERATE_TIMEOUT)
        ),
    )


def _build_llamacpp(cfg: dict[str, Any]) -> LlamaCppProvider:
    return LlamaCppProvider(
        model_path=_get(
            cfg,
            "llamacpp",
            "model_path",
            DEFAULT_CONFIG["llamacpp"]["model_path"],
        ),
        n_ctx=int(_get(cfg, "llamacpp", "n_ctx", DEFAULT_CONFIG["llamacpp"]["n_ctx"])),
        n_threads=int(
            _get(cfg, "llamacpp", "n_threads", DEFAULT_CONFIG["llamacpp"]["n_threads"])
        ),
    )


def _build_cloud(cfg: dict[str, Any]) -> CloudProvider:
    return CloudProvider(
        provider=_get(
            cfg, "cloud", "provider", DEFAULT_CONFIG["cloud"]["provider"]
        ),
        api_base=_get(cfg, "cloud", "api_base", DEFAULT_CONFIG["cloud"]["api_base"]),
        api_key=_get(cfg, "cloud", "api_key", DEFAULT_CONFIG["cloud"]["api_key"]),
        model=_get(cfg, "cloud", "model", DEFAULT_CONFIG["cloud"]["model"]),
        timeout=float(_get(cfg, "cloud", "timeout", _HTTP_GENERATE_TIMEOUT)),
    )


_BUILDERS: dict[str, Any] = {
    "ollama": _build_ollama,
    "lmstudio": _build_lmstudio,
    "llamacpp": _build_llamacpp,
    "cloud": _build_cloud,
}


def get_provider(
    name: str = "auto",
    config: Optional[dict[str, Any]] = None,
) -> LLMProvider:
    """工厂函数：按名称或 auto 探测返回 provider 实例。

    参数：
        name: 'ollama' | 'lmstudio' | 'llamacpp' | 'cloud' | 'auto'
              'auto' 顺序探测 4 个后端，返回首个 ``is_available()=True`` 的实例；
              若全部不可用，则按 ``[llm].fallback`` 配置返回（默认 cloud）。
        config: 配置字典；None 时调用 ``load_config()``。

    返回：
        实现 ``LLMProvider`` 协议的对象。

    异常：
        ValueError: name 不在合法集合内。
        RuntimeError: 显式指定 name 时该 provider 构造失败。
    """
    cfg = config if config is not None else load_config()

    if name == "auto":
        # 1) 按 [llm].default 优先探测
        default_name = _get(cfg, "llm", "default", "ollama")
        for candidate in _ordered_probe(default_name):
            builder = _BUILDERS.get(candidate)
            if builder is None:
                continue
            try:
                provider = builder(cfg)
            except (RuntimeError, ValueError, TypeError):
                continue
            try:
                if provider.is_available():
                    return provider
            except Exception:  # 探测异常吞掉，尝试下一个
                continue

        # 2) 全部不可用 → 按 fallback 返回（即使不可用也返回实例）
        fallback = _get(cfg, "llm", "fallback", "cloud")
        builder = _BUILDERS.get(fallback) or _BUILDERS["cloud"]
        return builder(cfg)

    if name not in _BUILDERS:
        raise ValueError(
            f"未知 provider 名称：{name!r}；"
            f"合法值：{sorted(_BUILDERS.keys())} 或 'auto'"
        )

    builder = _BUILDERS[name]
    try:
        return builder(cfg)
    except (RuntimeError, ValueError, TypeError) as exc:
        raise RuntimeError(f"构造 provider '{name}' 失败：{exc}") from exc


def _ordered_probe(preferred: str) -> list[str]:
    """返回探测顺序：preferred 优先，其余按 _PROBE_ORDER 补齐。"""
    seq: list[str] = []
    if preferred in _BUILDERS:
        seq.append(preferred)
    for n in _PROBE_ORDER:
        if n not in seq:
            seq.append(n)
    return seq


# ---------------------------------------------------------------------------
# 便捷：列出所有 provider 的可用性（CLI / 调试用）
# ---------------------------------------------------------------------------


def probe_all(config: Optional[dict[str, Any]] = None) -> dict[str, bool]:
    """返回 ``{name: is_available}`` 字典，便于 CLI 输出。"""
    cfg = config if config is not None else load_config()
    out: dict[str, bool] = {}
    for name, builder in _BUILDERS.items():
        try:
            provider = builder(cfg)
            out[name] = bool(provider.is_available())
        except Exception:
            out[name] = False
    return out


__all__ = [
    "LLMProvider",
    "OllamaProvider",
    "LMStudioProvider",
    "LlamaCppProvider",
    "CloudProvider",
    "get_provider",
    "probe_all",
    "load_config",
    "save_config",
    "DEFAULT_CONFIG",
    "MTCA_CONFIG_PATH",
]