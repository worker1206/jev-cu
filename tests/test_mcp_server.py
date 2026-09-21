"""universal/mcp_server 契约：工具恒返回 JSON 字符串、绝不抛异常；缺 mcp 时清晰降级。

本文件不 import mcp，因此在 Python 3.9（mcp 装不上）与 3.11 上都能跑。
"""
import json

from core import loop as loop_mod
from universal import mcp_server


def test_status_returns_json_string_never_raises(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = mcp_server.status("ghost")
    assert isinstance(out, str), "MCP 工具必须返回字符串"
    payload = json.loads(out)                      # 必须是合法 JSON
    assert payload["status"] == "not_found"
    assert payload["schema_version"] == "1"


def test_status_wraps_internal_failure_as_json(monkeypatch):
    def boom(*args, **kwargs):
        raise ValueError("日志读坏了")

    monkeypatch.setattr(loop_mod, "summarize_log", boom)
    payload = json.loads(mcp_server.status("s1"))
    assert payload["status"] == "error"
    assert payload["error"]["type"] == "ValueError"
    assert "日志读坏了" in payload["error"]["message"]


def test_browse_wraps_loop_failure_as_json(monkeypatch):
    def boom(**kwargs):
        raise RuntimeError("内部崩了")

    monkeypatch.setattr(loop_mod, "run", boom)
    out = mcp_server.browse("任务", "https://example.invalid", 3)
    assert isinstance(out, str)
    payload = json.loads(out)
    assert payload["status"] == "error"
    assert payload["error"]["type"] == "RuntimeError"


def test_browse_forwards_arguments_to_loop(monkeypatch):
    seen = {}

    def fake_run(**kwargs):
        seen.update(kwargs)
        return {"schema_version": "1", "status": "finished", "steps": 2}

    monkeypatch.setattr(loop_mod, "run", fake_run)
    payload = json.loads(mcp_server.browse("搜索人工智能", "https://example.invalid", 7))

    assert payload["status"] == "finished"
    assert seen["task"] == "搜索人工智能"
    assert seen["url"] == "https://example.invalid"
    assert seen["max_steps"] == 7


def test_browse_defaults_to_25_steps(monkeypatch):
    seen = {}
    monkeypatch.setattr(loop_mod, "run", lambda **kw: seen.update(kw) or {"status": "ok"})
    mcp_server.browse("任务")
    assert seen["max_steps"] == 25


def test_main_returns_2_when_python_too_old(monkeypatch, capsys):
    monkeypatch.setattr(mcp_server, "MIN_PYTHON", (99, 0))
    code = mcp_server.main()
    assert code == 2
    err = capsys.readouterr().err
    assert "pip install mcp" in err
    assert "99.0" in err


def test_main_returns_2_with_hint_when_mcp_missing(monkeypatch, capsys):
    def no_mcp():
        raise ImportError("No module named 'mcp'")

    monkeypatch.setattr(mcp_server, "MIN_PYTHON", (3, 0))   # 先让版本守卫放行
    monkeypatch.setattr(mcp_server, "build_server", no_mcp)
    code = mcp_server.main()
    assert code == 2
    err = capsys.readouterr().err
    assert "pip install mcp" in err
    assert "No module named 'mcp'" in err          # 如实带出底层错误


def test_main_runs_server_and_returns_0(monkeypatch):
    calls = []

    class FakeServer:
        def run(self):
            calls.append("run")

    monkeypatch.setattr(mcp_server, "MIN_PYTHON", (3, 0))
    monkeypatch.setattr(mcp_server, "build_server", lambda: FakeServer())

    assert mcp_server.main() == 0
    assert calls == ["run"]


def test_main_returns_1_when_server_run_fails(monkeypatch, capsys):
    class BrokenServer:
        def run(self):
            raise RuntimeError("stdio 断了")

    monkeypatch.setattr(mcp_server, "MIN_PYTHON", (3, 0))
    monkeypatch.setattr(mcp_server, "build_server", lambda: BrokenServer())

    assert mcp_server.main() == 1
    assert "stdio 断了" in capsys.readouterr().err


def test_hint_mentions_current_python_version():
    hint = mcp_server.mcp_hint()
    assert "pip install mcp" in hint
    assert "%d.%d.%d" % mcp_server.sys.version_info[:3] in hint


def test_json_output_is_utf8_readable_not_escaped():
    out = mcp_server.status("中文会话")
    assert "中文会话" in out, "JSON 不应把中文转成 \\uXXXX 转义"
