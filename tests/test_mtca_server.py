"""tests/test_mtca_server.py — Stage A1 红绿测试。

验证 src/cli/server.py 在没有 MTCA_TRACE=1 时不会调 syncause_tracer.initialize()。
"""
import os
import importlib
import sys
from unittest import mock

import pytest


def test_no_env_means_no_initialize_call(monkeypatch):
    """未设 MTCA_TRACE=1 时，绝不调用 syncause_tracer.initialize。"""
    # 1. 清掉所有相关 env
    for k in ("MTCA_TRACE", "SYNCAUSE_API_KEY", "SYNCAUSE_PROXY", "SYNCAUSE_PROJECT_ID"):
        monkeypatch.delenv(k, raising=False)
    
    # 2. mock syncause_tracer 让它被调就报错
    fake_syncause = mock.MagicMock()
    fake_syncause.initialize = mock.MagicMock(side_effect=AssertionError(
        "BUG: initialize() called without MTCA_TRACE=1"
    ))
    
    with mock.patch.dict(sys.modules, {"syncause_tracer": fake_syncause}):
        # 3. 直接 exec 文件（不是 import 模块）
        if "mtca_server" in sys.modules:
            del sys.modules["mtca_server"]
        spec = importlib.util.spec_from_file_location(
            "mtca_server", "src/cli/server.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    
    # 4. 断言没被调
    fake_syncause.initialize.assert_not_called()


def test_with_env_calls_initialize(monkeypatch):
    """设 MTCA_TRACE=1 + 3 个凭证 → 应调 initialize。"""
    monkeypatch.setenv("MTCA_TRACE", "1")
    monkeypatch.setenv("SYNCAUSE_API_KEY", "fake-key")
    monkeypatch.setenv("SYNCAUSE_PROXY", "wss://test/ws")
    monkeypatch.setenv("SYNCAUSE_PROJECT_ID", "test-uuid")
    
    fake_syncause = mock.MagicMock()
    with mock.patch.dict(sys.modules, {"syncause_tracer": fake_syncause}):
        if "mtca_server" in sys.modules:
            del sys.modules["mtca_server"]
        spec = importlib.util.spec_from_file_location(
            "mtca_server", "src/cli/server.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    
    fake_syncause.initialize.assert_called_once()
    kwargs = fake_syncause.initialize.call_args.kwargs
    assert kwargs["api_key"] == "fake-key"
    assert kwargs["proxy"] == "wss://test/ws"
    assert kwargs["project_id"] == "test-uuid"
    assert kwargs["app_name"] == "MTCA"
