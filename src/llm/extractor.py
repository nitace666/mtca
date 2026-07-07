"""LLM 事实提炼（C1.4）

从对话文本 / 消息列表中调用 LLM 提炼出"可被长期记忆的事实"，并（可选）写入 facts 表。

设计要点：
- prompt 模板内置；要求 LLM 输出严格 JSON 列表 ``[{"content", "confidence", "tags"}, ...]``
- 解析失败时优雅降级（返回 [] 或包装成单事实），不抛错
- 写库是可选的，调用方可先用 extract_facts 看输出再决定
- 走 LLMProvider 协议（get_provider("auto")），复用 T10 配置
- 默认 max_tokens=512，给 thinking 模型留余量（C1.0 修复）

公共 API：
- ``build_extraction_prompt(text: str) -> dict``  返回 {"system":..., "user":...}
- ``extract_facts(text: str, provider=None) -> list[dict]``
- ``extract_facts_from_messages(messages: list[dict], provider=None) -> list[dict]``
- ``extract_and_store(session_id, text, segment_id=None, provider=None, path=None) -> list[str]``
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional, Union

from src.llm.facts_store import create_fact

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 默认 confidence：纯文本包装场景下用
_DEFAULT_CONFIDENCE: float = 0.7

# 提炼用的 max_tokens（要给 LLM 留够输出 JSON 列表的余量）
_EXTRACT_MAX_TOKENS: int = 512

# smart-retry 提示词（Bug#2 修复 M2.5.8 C）：追加到 user 末尾，鼓励 LLM 重新审视
_SMART_RETRY_HINT: str = (
    "\n\n（重试 {n}/2）请再次仔细审视对话："
    "即便讨论技术问题，也可以提取出概念定义、常见陷阱、工具用法等事实命题。"
    "若确实无任何可提炼事实再返回 []，否则请尽量返回 JSON 列表。"
)

# system prompt：明确要求 JSON 输出
_SYSTEM_PROMPT: str = (
    "你是一个事实提炼助手。从用户对话中提取可被长期记忆的事实命题。"
    "输出严格的 JSON 列表，每个元素形如："
    '{"content": "事实描述（中文/英文均可，简洁）", '
    '"confidence": 0.0~1.0 的浮点数, '
    '"tags": ["标签1", "标签2"]}'
    "不要输出其它内容（不要解释、不要 markdown 包装、不要前缀后缀）。"
    "如果对话里没有可提炼的事实，返回空列表 []。"
)


# ---------------------------------------------------------------------------
# Prompt 构造
# ---------------------------------------------------------------------------


def build_extraction_prompt(text: str) -> dict[str, str]:
    """构造 LLM 提炼 prompt。

    返回 ``{"system": str, "user": str}``。
    """
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text 必须是非空字符串")
    user = (
        "请从以下对话片段中提炼事实：\n\n"
        "------\n"
        f"{text.strip()}\n"
        "------\n\n"
        "输出 JSON 列表："
    )
    return {"system": _SYSTEM_PROMPT, "user": user}


# ---------------------------------------------------------------------------
# 解析 LLM 输出
# ---------------------------------------------------------------------------


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*)```", re.DOTALL)


def _parse_facts_from_text(llm_text: str) -> list[dict]:
    """从 LLM 输出文本解析 fact 列表。

    容错策略：
    1. 优先尝试整体 parse 为 JSON 列表
    2. 尝试剥离 markdown ````json` 围栏
    3. 尝试匹配第一个 ``[...]`` 子串
    4. 全部失败 → 返回空（不抛错）
    """
    if not llm_text or not llm_text.strip():
        return []

    text = llm_text.strip()

    # 1) 直接 parse
    try:
        data = json.loads(text)
        return _coerce_facts(data)
    except (json.JSONDecodeError, ValueError, TypeError):
        pass

    # 2) 剥离 markdown fence
    m = _JSON_FENCE_RE.search(text)
    if m:
        try:
            data = json.loads(m.group(1).strip())
            return _coerce_facts(data)
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    # 3) 找第一个 [...] 子串
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(0))
            return _coerce_facts(data)
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    return []


def _extract_fact_text(item: dict) -> str:
    """从 LLM 返回的 fact dict 里提取 fact 文本。

    支持 3 种 LLM schema:
    - 标准：{"content": "..."} 或 {"fact": "..."} 或 {"text": "..."}
    - RDF 三元组：{"subject": "...", "predicate": "...", "object": "..."}
      可选 {"time": "..."} → "subject predicate object（time）"
    """
    # 1. 直接文本字段
    direct = (
        item.get("content")
        or item.get("fact")
        or item.get("text")
    )
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    # 2. RDF 三元组（qwen-heretic 等 fine-tune 模型会输出）
    subj = item.get("subject")
    pred = item.get("predicate")
    obj = item.get("object")
    if isinstance(subj, str) and isinstance(pred, str) and isinstance(obj, str):
        if subj.strip() and pred.strip() and obj.strip():
            triple = f"{subj.strip()} {pred.strip()} {obj.strip()}"
            time = item.get("time")
            if isinstance(time, str) and time.strip():
                triple += f"（{time.strip()}）"
            return triple

    return ""


def _coerce_facts(data: Any) -> list[dict]:
    """把 JSON parse 结果规整为 list[dict{content, confidence, tags}]。

    - 单 dict → 包装成 1 元素 list
    - 非 list → 返回空
    - 元素不是 dict 或缺 content → 过滤
    """
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        content = _extract_fact_text(item)
        if not content:
            continue
        try:
            conf = float(item.get("confidence", _DEFAULT_CONFIDENCE))
        except (TypeError, ValueError):
            conf = _DEFAULT_CONFIDENCE
        conf = max(0.0, min(1.0, conf))
        tags = item.get("tags") or []
        if not isinstance(tags, list):
            tags = [str(tags)]
        tags = [str(t) for t in tags if t]
        out.append({
            "content": content.strip(),
            "confidence": conf,
            "tags": tags,
        })
    return out


def _wrap_as_single_fact(llm_text: str) -> list[dict]:
    """把非 JSON 纯文本包装成单事实（保守策略）。"""
    text = llm_text.strip()
    if not text:
        return []
    # 去掉可能的 markdown 标记
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return [{
        "content": text,
        "confidence": _DEFAULT_CONFIDENCE,
        "tags": [],
    }]


# ---------------------------------------------------------------------------
# LLM 调用入口
# ---------------------------------------------------------------------------


def _get_provider(provider: Optional[Any] = None) -> Any:
    """获取 LLM provider；显式传 > 自动探测。"""
    if provider is not None:
        return provider
    from src.llm.provider import get_provider
    return get_provider("auto")


def _llm_generate(provider: Any, text: str, attempt: int = 0) -> str:
    """调用 LLM，返回 content 字符串。失败抛 RuntimeError。

    Bug#1 修复（M2.5.8 C）：手动拼接 system + 两换行 + user。
    原代码只取 prompt["user"]，system 被丢弃 -> LLM 看不到 role/schema 约束。
    改用手动拼接的原因：4 个 provider（Ollama / LMStudio / LlamaCpp / Cloud）
    都不支持 system kwargs（Ollama 单 prompt 字段；其余 messages 写死 user role）。

    参数：
        provider: LLM provider 实例
        text: 待提炼文本
        attempt: retry 计数（0=首次；>=1 时追加 smart-retry 提示鼓励 LLM 重新审视）
    """
    prompt = build_extraction_prompt(text)
    user = prompt["user"]
    if attempt >= 1:
        user = user + _SMART_RETRY_HINT.format(n=attempt)
    full_prompt = prompt["system"] + "\n\n" + user
    return provider.generate(full_prompt, max_tokens=_EXTRACT_MAX_TOKENS)


# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------


def extract_facts(
    text: str,
    provider: Optional[Any] = None,
    max_retries: int = 2,
) -> list[dict]:
    """从单段文本提炼 facts。

    Bug#2 修复（M2.5.8 C）：空结果 / 解析失败 -> 自动 retry 最多 max_retries=2 次。
    重试时 user prompt 追加 smart-retry 提示，鼓励 LLM 重新审视对话。
    解析失败 / 重试用完仍空 → 返回 []（不抛错，保持向后兼容）。

    参数：
        text: 待提炼文本
        provider: LLM provider；None 时走 auto 探测
        max_retries: 空结果 / parse 失败时最多重试次数；0 = 不重试
    """
    if not isinstance(text, str) or not text.strip():
        return []
    try:
        prov = _get_provider(provider)
    except Exception:
        return []

    for attempt in range(max_retries + 1):
        try:
            llm_text = _llm_generate(prov, text, attempt=attempt)
        except Exception:
            # 单次 LLM 失败不算终态，下一轮继续
            continue
        facts = _parse_facts_from_text(llm_text)
        if facts:
            return facts
    return []


def extract_facts_from_messages(
    messages: list[dict],
    provider: Optional[Any] = None,
    max_retries: int = 2,
) -> list[dict]:
    """从多条消息提炼 facts。

    ``messages`` 元素应含 ``role`` / ``content`` 字段；空内容被跳过。
    拼接策略：``[user] xxxxx\\n[assistant] yyyyy``。
    """
    if not messages:
        return []
    lines = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "?")
        content = m.get("content") or ""
        if not isinstance(content, str) or not content.strip():
            continue
        lines.append(f"[{role}] {content.strip()}")
    if not lines:
        return []
    text = "\n".join(lines)
    return extract_facts(text, provider=provider, max_retries=max_retries)


def extract_and_store(
    session_id: str,
    text: str,
    segment_id: Optional[str] = None,
    provider: Optional[Any] = None,
    path: Optional[Union[Path, str]] = None,
    max_retries: int = 2,
) -> list[str]:
    """提炼并直接写入 facts 表，返回 ``[fact_id, ...]``。

    空文本 / 空提炼结果 → 返回空 list，不调 LLM。
    """
    if not isinstance(text, str) or not text.strip():
        return []
    facts = extract_facts(text, provider=provider, max_retries=max_retries)
    fids: list[str] = []
    for f in facts:
        fid = create_fact(
            session_id=session_id,
            segment_id=segment_id,
            content=f["content"],
            source="extracted",
            confidence=f["confidence"],
            tags=json.dumps(f["tags"], ensure_ascii=False) if f["tags"] else None,
            path=path,
        )
        fids.append(fid)
    return fids


__all__ = [
    "build_extraction_prompt",
    "extract_facts",
    "extract_facts_from_messages",
    "extract_and_store",
]