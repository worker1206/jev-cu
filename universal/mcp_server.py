"""FastMCP stdio server：把 jev-cu 暴露成协议级能力，不绑定任何宿主。

- 工具：browse(task, start_url, max_steps) / status(session_id)
- 永远返回 JSON 字符串，绝不抛异常（失败也要让调用方拿到结构化内容）
- mcp 包缺失时（例如 Python < 3.10，mcp 要求 >= 3.10）给出清晰提示并以退出码 2 结束，
  不打印 traceback；本模块在 mcp 缺失时依然可以被安全 import。
"""
from __future__ import annotations

import json
import sys

SCHEMA_VERSION = "1"
MIN_PYTHON = (3, 10)

BROWSE_DOC = ("在浏览器中执行一个多步任务：Jev 每步判断，低 margin 时 LLM 兜底，"
              "危险动作需人工确认。返回 JSON 字符串（含 session_id 与决策日志路径）。")
STATUS_DOC = "读取某个 session_id 的决策日志汇总，返回 JSON 字符串。"


def mcp_hint(exc=None):
    """mcp 不可用时的清晰提示（含当前 Python 版本与两种修法）。"""
    lines = [
        "jev-cu-mcp 不可用：需要 Python >= %d.%d 且安装 mcp 包。" % MIN_PYTHON,
        "当前 Python：%d.%d.%d" % sys.version_info[:3],
        "修法之一：用 Python >= 3.10 的环境执行  pip install mcp",
        "修法之二：若只能用当前解释器，则改用 CLI（jev-cu run ...）或直接调用 core.loop.run()。",
    ]
    if exc is not None:
        lines.append("底层错误：%s" % exc)
    return "\n".join(lines) + "\n"


def _json(payload):
    return json.dumps(payload, ensure_ascii=False)


def _error(status, etype, message, **extra):
    payload = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "error": {"type": str(etype), "message": str(message)[:300]},
    }
    payload.update(extra)
    return _json(payload)


def browse(task, start_url=None, max_steps=25):
    """BROWSE_DOC"""
    try:
        from core import loop as loop_mod
        steps = int(max_steps or 25)
        result = loop_mod.run(task=task, url=start_url, max_steps=steps)
        return _json(result)
    except Exception as exc:
        return _error("error", type(exc).__name__, exc, task=task, start_url=start_url)


def status(session_id):
    """STATUS_DOC"""
    try:
        from core import loop as loop_mod
        return _json(loop_mod.summarize_log(session_id))
    except Exception as exc:
        return _error("error", type(exc).__name__, exc, session_id=str(session_id))


def build_server():
    """构建 FastMCP server。mcp 缺失时抛 ImportError，由 main() 转成清晰提示。"""
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("jev-cu")
    server.tool()(browse)
    server.tool()(status)
    return server


def main():
    if sys.version_info < MIN_PYTHON:
        sys.stderr.write(mcp_hint())
        return 2
    try:
        server = build_server()
    except ImportError as exc:
        sys.stderr.write(mcp_hint(exc))
        return 2
    except Exception as exc:                      # 构建失败也不抛裸异常
        sys.stderr.write("jev-cu-mcp 启动失败：%s\n" % exc)
        return 1
    try:
        server.run()
    except Exception as exc:
        sys.stderr.write("jev-cu-mcp 运行中断：%s\n" % exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
