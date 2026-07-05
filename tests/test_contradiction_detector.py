"""src/lifecycle/contradiction_detector.py 测试（T22）

测试覆盖（用户 prompt 要求的 8 条）：

1. test_detect_contradiction_finds_candidates   同 topic + active + 90 天内 + ≤10 段候选
2. test_detect_contradiction_within_90_days     90 天外的旧段不进候选
3. test_detect_contradiction_max_10             11+ 段时只取 10 段
4. test_apply_supersede_writes_relation         apply 后 segment_relations 有 supersedes 行
5. test_apply_supersede_marks_a                 apply 后 A 段的 superseded_by / supersedes_count 更新
6. test_user_override_accept                    accept → 等价于 apply_supersede
7. test_user_override_branch                    branch → 写 related_to，不动 superseded_by
8. test_user_override_revoke                    revoke → 删关系 + 清空 superseded_by
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

import pytest

from src.lifecycle.contradiction_detector import (
    CONTRADICTION_WINDOW_DAYS,
    CONTENT_PREVIEW_CHARS,
    MAX_CANDIDATES,
    VALID_ACTIONS,
    VALID_LEVELS,
    apply_supersede,
    build_prompt,
    detect_contradiction,
    find_candidates,
    parse_verdict,
    user_override,
)
from src.l0.session_writer import create_session
from src.store.sqlite import execute, query


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    """当前毫秒时间戳。"""
    return int(time.time() * 1000)


def _days_ago_ms(days: float) -> int:
    """N 天前的毫秒时间戳。"""
    return _now_ms() - int(days * 24 * 60 * 60 * 1000)


def _insert_segment(
    path: Path,
    topic_label: str = "漫剧方案",
    silence_state: str = "active",
    start_days_ago: int = 30,
    fog_anchor: str = "漫剧方案",
    superseded_by: Any = None,
) -> str:
    """直接 INSERT 一条 segments 记录（绕过 L0-骨架触发器）。"""
    sid = create_session(path=path)
    seg_id = str(uuid.uuid4())
    start_at = _days_ago_ms(start_days_ago)
    end_at = start_at + 60_000
    execute(
        "INSERT INTO segments "
        "(segment_id, session_id, start_msg_seq, end_msg_seq, "
        "start_at, end_at, topic_label, fog_anchor, "
        "silence_state, superseded_by) "
        "VALUES (?, ?, 1, 1, ?, ?, ?, ?, ?, ?)",
        (
            seg_id, sid,
            start_at, end_at,
            topic_label, fog_anchor,
            silence_state, superseded_by,
        ),
        path=path,
    )
    return seg_id


class MockProvider:
    """可配置的 mock LLM provider：按 prompt 关键字返回 JSON。"""

    def __init__(
        self,
        contradicts: bool = True,
        level: str = "major",
        reason: str = "mock 一致判定",
    ) -> None:
        self.contradicts = bool(contradicts)
        self.level = level
        self.reason = reason
        self.calls: list[str] = []
        self.name = "mock"

    def generate(self, prompt: str, **kwargs: Any) -> str:
        self.calls.append(prompt)
        payload = {
            "contradicts": self.contradicts,
            "level": self.level,
            "reason": self.reason,
        }
        return json.dumps(payload, ensure_ascii=False)

    def embed(self, text: str) -> list[float]:  # pragma: no cover - 不走此路径
        return []

    def is_available(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# 测试 1：find_candidates 返回符合条件的段
# ---------------------------------------------------------------------------


def test_detect_contradiction_finds_candidates(mtca_db: Path) -> None:
    """同 topic + active + 90 天内 + ≤10 → 全部返回；超期 / 非 active 排除。"""
    # 主段（"new"）
    new_id = _insert_segment(mtca_db, topic_label="漫剧方案", silence_state="active")

    # 3 个候选（同 topic + active + 30 天内）
    cand_ids = [
        _insert_segment(mtca_db, topic_label="漫剧方案", silence_state="active",
                        start_days_ago=10)
        for _ in range(3)
    ]
    # 1 个非 active（dormant）→ 不应进候选
    _insert_segment(mtca_db, topic_label="漫剧方案", silence_state="dormant")
    # 1 个不同 topic → 不应进候选
    _insert_segment(mtca_db, topic_label="烹饪方案", silence_state="active")

    provider = MockProvider(contradicts=False, level="")
    results = detect_contradiction(new_id, provider=provider, path=mtca_db)

    assert len(results) == 3
    returned_b = {r["b_id"] for r in results}
    assert returned_b == set(cand_ids)
    for r in results:
        assert r["a_id"] == new_id
        # provider 强制非矛盾 → level 必须空
        assert r["contradicts"] is False
        assert r["level"] == ""
    assert len(provider.calls) == 3


# ---------------------------------------------------------------------------
# 测试 2：90 天外的旧段不进候选
# ---------------------------------------------------------------------------


def test_detect_contradiction_within_90_days(mtca_db: Path) -> None:
    """100 天前的段不参与矛盾检测（窗口外）。"""
    new_id = _insert_segment(mtca_db, topic_label="漫剧方案", start_days_ago=5)
    # 100 天前（超 90 天窗口）
    _insert_segment(mtca_db, topic_label="漫剧方案", start_days_ago=100)
    # 80 天前（窗口内）
    in_window_id = _insert_segment(
        mtca_db, topic_label="漫剧方案", start_days_ago=80,
    )

    provider = MockProvider()
    results = detect_contradiction(new_id, provider=provider, path=mtca_db)

    returned_b = {r["b_id"] for r in results}
    assert returned_b == {in_window_id}
    assert len(results) == 1


def test_find_candidates_respects_window_days(mtca_db: Path) -> None:
    """find_candidates 直接传 window_days=30 也应过滤 50 天前的段。"""
    new_id = _insert_segment(mtca_db, topic_label="漫剧方案", start_days_ago=5)
    _insert_segment(mtca_db, topic_label="漫剧方案", start_days_ago=50)
    close_id = _insert_segment(mtca_db, topic_label="漫剧方案", start_days_ago=10)

    rows = find_candidates(
        new_id, path=mtca_db, window_days=30,
    )
    assert {r["segment_id"] for r in rows} == {close_id}


# ---------------------------------------------------------------------------
# 测试 3：候选不超过 MAX_CANDIDATES（10）
# ---------------------------------------------------------------------------


def test_detect_contradiction_max_10(mtca_db: Path) -> None:
    """15 个候选时，detect 只取最近 10 个；其余不参与。"""
    new_id = _insert_segment(mtca_db, topic_label="漫剧方案", start_days_ago=1)
    # 15 个候选，start_days_ago 递减 → 所有都进候选池；按 start_at DESC 取 10
    cand_ids = []
    for i in range(15):
        seg = _insert_segment(
            mtca_db, topic_label="漫剧方案",
            start_days_ago=5 + i,  # 6..20 天前
        )
        cand_ids.append(seg)

    provider = MockProvider()
    results = detect_contradiction(new_id, provider=provider, path=mtca_db)

    assert len(results) == MAX_CANDIDATES  # == 10
    # 取的是最近的 10 个（start_days_ago = 6..15）
    expected_b = set(cand_ids[:MAX_CANDIDATES])
    returned_b = {r["b_id"] for r in results}
    assert returned_b == expected_b


# ---------------------------------------------------------------------------
# 测试 4：apply_supersede 写入 segment_relations 行
# ---------------------------------------------------------------------------


def test_apply_supersede_writes_relation(mtca_db: Path) -> None:
    """apply_supersede 后 relations 表有 1 行 (a→b, 'supersedes')，auto_created=1。"""
    a_id = _insert_segment(mtca_db, topic_label="漫剧方案", start_days_ago=60)
    b_id = _insert_segment(mtca_db, topic_label="漫剧方案", start_days_ago=1)

    result = apply_supersede(a_id, b_id, path=mtca_db)
    assert result["a_id"] == a_id
    assert result["b_id"] == b_id
    assert result["type"] == "supersedes"
    assert result["auto_created"] == 1
    assert result["relation_id"]

    rows = query(
        "SELECT * FROM segment_relations WHERE relation_id = ?",
        (result["relation_id"],),
        path=mtca_db,
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["relation_type"] == "supersedes"
    assert row["seg_a_id"] == a_id
    assert row["seg_b_id"] == b_id
    assert int(row["auto_created"]) == 1
    assert int(row["created_at"]) > 0


def test_apply_supersede_self_raises(mtca_db: Path) -> None:
    """a_id == b_id 时 apply_supersede 抛 ValueError。"""
    a_id = _insert_segment(mtca_db)
    with pytest.raises(ValueError):
        apply_supersede(a_id, a_id, path=mtca_db)


# ---------------------------------------------------------------------------
# 测试 5：apply_supersede 标记 A 段
# ---------------------------------------------------------------------------


def test_apply_supersede_marks_a(mtca_db: Path) -> None:
    """apply 后 A 段的 superseded_by = b_id，supersedes_count + 1。"""
    a_id = _insert_segment(mtca_db, topic_label="漫剧方案", start_days_ago=60)
    b_id = _insert_segment(mtca_db, topic_label="漫剧方案", start_days_ago=1)

    # 预值
    from src.l0.segment_writer import get_segment
    a0 = get_segment(a_id, path=mtca_db)
    pre_count = int(a0.get("supersedes_count") or 0)

    apply_supersede(a_id, b_id, path=mtca_db)

    a1 = get_segment(a_id, path=mtca_db)
    assert a1["superseded_by"] == b_id
    assert int(a1["supersedes_count"]) == pre_count + 1

    # 审计
    events = query(
        "SELECT event_type, reason FROM score_events "
        "WHERE segment_id = ? AND event_type = 'supersede_log'",
        (a_id,),
        path=mtca_db,
    )
    assert len(events) == 1
    assert "apply_supersede" in events[0]["reason"]


# ---------------------------------------------------------------------------
# 测试 6：user_override 'accept' → 等价于 apply_supersede
# ---------------------------------------------------------------------------


def test_user_override_accept(mtca_db: Path) -> None:
    """accept 必须调用 apply_supersede 的全部副作用（relation + superseded_by）。"""
    a_id = _insert_segment(mtca_db, topic_label="漫剧方案", start_days_ago=60)
    b_id = _insert_segment(mtca_db, topic_label="漫剧方案", start_days_ago=1)

    result = user_override(a_id, "accept", b_id=b_id, path=mtca_db)
    assert result["action"] == "accept"
    assert result["a_id"] == a_id
    assert result["b_id"] == b_id
    assert result["relation_id"]

    # relations 表有 1 行 supersedes
    rels = query(
        "SELECT * FROM segment_relations "
        "WHERE seg_a_id = ? AND seg_b_id = ?",
        (a_id, b_id),
        path=mtca_db,
    )
    assert len(rels) == 1
    assert rels[0]["relation_type"] == "supersedes"
    assert int(rels[0]["auto_created"]) == 1  # user_override 也用 auto 标记

    from src.l0.segment_writer import get_segment
    a1 = get_segment(a_id, path=mtca_db)
    assert a1["superseded_by"] == b_id


def test_user_override_accept_requires_b(mtca_db: Path) -> None:
    """accept 不带 b_id 必须抛 ValueError。"""
    a_id = _insert_segment(mtca_db)
    with pytest.raises(ValueError):
        user_override(a_id, "accept", path=mtca_db)


def test_user_override_accept_case_insensitive(mtca_db: Path) -> None:
    """action='ACCEPT' / ' Accept ' 大小写、空格归一。"""
    a_id = _insert_segment(mtca_db, topic_label="漫剧方案", start_days_ago=60)
    b_id = _insert_segment(mtca_db, topic_label="漫剧方案", start_days_ago=1)
    r = user_override(a_id, " ACCEPT ", b_id=b_id, path=mtca_db)
    assert r["action"] == "accept"


# ---------------------------------------------------------------------------
# 测试 7：user_override 'branch' → related_to，不动 superseded_by
# ---------------------------------------------------------------------------


def test_user_override_branch(mtca_db: Path) -> None:
    """branch 写 related_to 关系，suppressed_by 不动。"""
    a_id = _insert_segment(mtca_db, topic_label="漫剧方案", start_days_ago=60)
    b_id = _insert_segment(mtca_db, topic_label="漫剧方案", start_days_ago=1)

    from src.l0.segment_writer import get_segment
    a_before = get_segment(a_id, path=mtca_db)
    pre_super = a_before["superseded_by"]

    result = user_override(a_id, "branch", b_id=b_id, path=mtca_db)
    assert result["action"] == "branch"
    assert result["relation_id"]

    rels = query(
        "SELECT * FROM segment_relations "
        "WHERE seg_a_id = ? AND seg_b_id = ?",
        (a_id, b_id),
        path=mtca_db,
    )
    assert len(rels) == 1
    assert rels[0]["relation_type"] == "related_to"
    assert int(rels[0]["auto_created"]) == 0  # user manual

    a_after = get_segment(a_id, path=mtca_db)
    # superseded_by 不变（branch 不是 supersede）
    assert a_after["superseded_by"] == pre_super

    # branch 审计
    events = query(
        "SELECT event_type FROM score_events "
        "WHERE segment_id = ? AND event_type = 'branch_log'",
        (a_id,),
        path=mtca_db,
    )
    assert len(events) == 1


def test_user_override_branch_requires_b(mtca_db: Path) -> None:
    """branch 不带 b_id 必须抛 ValueError。"""
    a_id = _insert_segment(mtca_db)
    with pytest.raises(ValueError):
        user_override(a_id, "branch", path=mtca_db)


# ---------------------------------------------------------------------------
# 测试 8：user_override 'revoke' → 删关系 + 清空 superseded_by
# ---------------------------------------------------------------------------


def test_user_override_revoke(mtca_db: Path) -> None:
    """revoke 清掉 supersede 关系，superseded_by 清空；至少写一条审计。"""
    a_id = _insert_segment(mtca_db, topic_label="漫剧方案", start_days_ago=60)
    b_id = _insert_segment(mtca_db, topic_label="漫剧方案", start_days_ago=1)

    # 先建 supersede 关系
    apply_supersede(a_id, b_id, path=mtca_db)
    from src.l0.segment_writer import get_segment
    mid = get_segment(a_id, path=mtca_db)
    assert mid["superseded_by"] == b_id

    # 再 revoke
    result = user_override(a_id, "revoke", b_id=b_id, path=mtca_db)
    assert result["action"] == "revoke"
    assert result["deleted_relations"] == 1

    rels = query(
        "SELECT * FROM segment_relations "
        "WHERE seg_a_id = ? OR seg_b_id = ?",
        (a_id, b_id),
        path=mtca_db,
    )
    assert len(rels) == 0

    a_after = get_segment(a_id, path=mtca_db)
    assert a_after["superseded_by"] is None

    # 审计
    events = query(
        "SELECT event_type FROM score_events "
        "WHERE segment_id = ? AND event_type = 'revoke_log'",
        (a_id,),
        path=mtca_db,
    )
    assert len(events) == 1


def test_user_override_revoke_no_b_clears_all(mtca_db: Path) -> None:
    """revoke 不带 b_id：清掉所有与 a_id 相关的 supersedes/related_to。"""
    a_id = _insert_segment(mtca_db, topic_label="漫剧方案", start_days_ago=60)
    b1_id = _insert_segment(mtca_db, topic_label="漫剧方案", start_days_ago=1)
    b2_id = _insert_segment(mtca_db, topic_label="漫剧方案", start_days_ago=2)

    apply_supersede(a_id, b1_id, path=mtca_db)
    user_override(a_id, "branch", b_id=b2_id, path=mtca_db)

    result = user_override(a_id, "revoke", path=mtca_db)
    assert result["action"] == "revoke"
    assert result["deleted_relations"] == 2

    rels = query(
        "SELECT * FROM segment_relations WHERE seg_a_id = ? OR seg_b_id = ?",
        (a_id, a_id),
        path=mtca_db,
    )
    assert len(rels) == 0


# ---------------------------------------------------------------------------
# 额外：边界与辅助函数
# ---------------------------------------------------------------------------


def test_user_override_invalid_action(mtca_db: Path) -> None:
    """非法 action 抛 ValueError。"""
    a_id = _insert_segment(mtca_db)
    with pytest.raises(ValueError):
        user_override(a_id, "maybe", b_id="x" * 36, path=mtca_db)


def test_user_override_invalid_segment(mtca_db: Path) -> None:
    """空 / None / 非字符串 segment_id 抛 ValueError。"""
    with pytest.raises(ValueError):
        user_override("", "accept", b_id="x" * 36, path=mtca_db)
    with pytest.raises(ValueError):
        user_override(None, "accept", b_id="x" * 36, path=mtca_db)  # type: ignore[arg-type]


def test_constants_export() -> None:
    """常量与 V0.4_PIVOT.md §3.7 对齐。"""
    assert CONTRADICTION_WINDOW_DAYS == 90
    assert MAX_CANDIDATES == 10
    assert CONTENT_PREVIEW_CHARS == 500
    assert set(VALID_LEVELS) == {"minor", "major", "full"}
    assert set(VALID_ACTIONS) == {"accept", "branch", "revoke"}


def test_parse_verdict_codes_and_fence() -> None:
    """parse_verdict 接受 JSON / code-fence / 中文别名。"""
    # 1) 纯 JSON
    v = parse_verdict('{"contradicts": true, "level": "major", "reason": "冲突"}')
    assert v["contradicts"] is True
    assert v["level"] == "major"
    assert v["reason"] == "冲突"

    # 2) code-fence
    v = parse_verdict('```json\n{"contradicts": true, "level": "full"}\n```')
    assert v["level"] == "full"

    # 3) 中文别名
    v = parse_verdict('{"contradicts": "是", "level": "严重"}')
    assert v["contradicts"] is True
    assert v["level"] == "full"

    # 4) 解析失败 → fallback
    v = parse_verdict("invalid")
    assert v["contradicts"] is False
    assert v["level"] == ""

    # 5) 含噪声的混合文本 → 提取首个 {...}
    v = parse_verdict('think ... {"contradicts": false, "level": ""} ... done')
    assert v["contradicts"] is False
    assert v["level"] == ""


def test_build_prompt_truncates_long_content(mtca_db: Path) -> None:
    """长内容截断到 CONTENT_PREVIEW_CHARS。"""
    long_label = "漫剧方案 " * 200  # ≈ 1200 字
    a_id = _insert_segment(mtca_db, topic_label="漫剧方案", fog_anchor=long_label)
    b_id = _insert_segment(
        mtca_db, topic_label="漫剧方案", fog_anchor="另一种思路",
        start_days_ago=1,
    )
    a_seg, b_seg = (None, None)
    from src.l0.segment_writer import get_segment
    a_seg = get_segment(a_id, path=mtca_db)
    b_seg = get_segment(b_id, path=mtca_db)
    prompt = build_prompt(a_seg, b_seg)
    assert "段落 A 说" in prompt
    assert "段落 B 说" in prompt
    assert "是否矛盾" in prompt

    # prompt 中 A 部分不会超过 500 + 模板头 ≈ < 600
    a_part = prompt.split("\n")[0]
    assert len(a_part) <= 60 + CONTENT_PREVIEW_CHARS
