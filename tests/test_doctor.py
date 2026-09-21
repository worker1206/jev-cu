"""doctor 分类诊断 + LLM 连通性探测。全 mock，不访问网络、不打印凭证。"""
import io
import json
import urllib.error

import pytest
from typer.testing import CliRunner

from core import brain_llm
from universal import cli

runner = CliRunner()


def http_error(code, body=b"err", url="https://api.invalid/v1/models"):
    return urllib.error.HTTPError(url, code, "err", {}, io.BytesIO(body))


def sender_result(status=200, final_url="https://api.invalid/v1/models",
                  body=b'{"models":[{"name":"jev-latest"},{"name":"jev-preview"}]}'):
    def _send(request, timeout):
        return status, final_url, body
    return _send


def sender_raises(exc):
    def _send(request, timeout):
        raise exc
    return _send


# ---------------------------------------------------------------- 纯分类函数
@pytest.mark.parametrize("code,expected", [
    (401, ("auth_failed", cli.EXIT_AUTH)),
    (403, ("auth_failed", cli.EXIT_AUTH)),
    (429, ("service_unavailable", cli.EXIT_UNAVAILABLE)),
    (500, ("service_unavailable", cli.EXIT_UNAVAILABLE)),
    (503, ("service_unavailable", cli.EXIT_UNAVAILABLE)),
    (400, ("unknown", cli.EXIT_ERROR)),
    (404, ("unknown", cli.EXIT_ERROR)),
])
def test_classify_http_status(code, expected):
    assert cli.classify_http_status(code) == expected


def test_classify_exception_covers_four_kinds():
    assert cli.classify_exception(urllib.error.URLError("net")) == \
        ("network_unreachable", cli.EXIT_UNREACHABLE)
    assert cli.classify_exception(http_error(503)) == \
        ("service_unavailable", cli.EXIT_UNAVAILABLE)
    assert cli.classify_exception(http_error(401)) == ("auth_failed", cli.EXIT_AUTH)
    assert cli.classify_exception(ValueError("not json"),
                                 final_url="https://console.invalid/login?x=1") == \
        ("misconfigured_base_url", cli.EXIT_MISCONFIG)
    assert cli.classify_exception(ValueError("not json")) == ("unknown", cli.EXIT_ERROR)


def test_category_exit_table_is_complete():
    assert cli.CATEGORY_EXIT["ok"] == cli.EXIT_OK
    assert cli.CATEGORY_EXIT["missing_key"] == cli.EXIT_CONFIG
    assert cli.CATEGORY_EXIT["misconfigured_base_url"] == cli.EXIT_MISCONFIG
    assert cli.CATEGORY_EXIT["auth_failed"] == cli.EXIT_AUTH
    assert cli.CATEGORY_EXIT["service_unavailable"] == cli.EXIT_UNAVAILABLE
    assert cli.CATEGORY_EXIT["network_unreachable"] == cli.EXIT_UNREACHABLE


# ---------------------------------------------------------------- Jev 探测四类
def test_probe_jev_ok():
    report = cli.probe_jev("https://api.invalid", "test-key", sender=sender_result())
    assert report["ok"] is True
    assert report["category"] == "ok"
    assert report["exit_code"] == cli.EXIT_OK
    assert report["models"] == ["jev-latest", "jev-preview"]


def test_probe_jev_misconfigured_console_host():
    report = cli.probe_jev(
        "https://console.invalid", "test-key",
        sender=sender_result(final_url="https://console.invalid/login?returnTo=%2Fv1%2Fmodels",
                             body=b"<!DOCTYPE html><html><head>"))
    assert report["category"] == "misconfigured_base_url"
    assert report["exit_code"] == cli.EXIT_MISCONFIG
    assert "api.typesafe.ai" in report["hint"]
    assert report["ok"] is False


def test_probe_jev_misconfigured_via_redirect_httperror():
    exc = urllib.error.HTTPError(
        "https://console.invalid/v1/models", 307, "redirect",
        {"Location": "https://console.invalid/login?returnTo=%2Fv1%2Fmodels"}, io.BytesIO(b""))
    report = cli.probe_jev("https://console.invalid", "test-key", sender=sender_raises(exc))
    assert report["category"] == "misconfigured_base_url"
    assert report["exit_code"] == cli.EXIT_MISCONFIG
    assert report["location"].endswith("%2Fv1%2Fmodels")


def test_probe_jev_auth_failed():
    report = cli.probe_jev("https://api.invalid", "bad-key",
                           sender=sender_raises(http_error(401, b'{"detail":"bad key"}')))
    assert report["category"] == "auth_failed"
    assert report["exit_code"] == cli.EXIT_AUTH
    assert report["http"] == 401
    assert "JEV_API_KEY" in report["hint"]


def test_probe_jev_service_unavailable():
    report = cli.probe_jev("https://api.invalid", "test-key",
                           sender=sender_raises(http_error(503, b"no healthy upstream")))
    assert report["category"] == "service_unavailable"
    assert report["exit_code"] == cli.EXIT_UNAVAILABLE
    assert report["http"] == 503
    assert "no healthy upstream" in report["error"]
    assert "服务端故障" in report["hint"]


def test_probe_jev_network_unreachable():
    report = cli.probe_jev("https://api.invalid", "test-key",
                           sender=sender_raises(urllib.error.URLError("nodename nor servname")))
    assert report["category"] == "network_unreachable"
    assert report["exit_code"] == cli.EXIT_UNREACHABLE
    assert "网络不可达" in report["hint"]


def test_probe_jev_never_raises_on_weird_error():
    report = cli.probe_jev("https://api.invalid", "test-key",
                           sender=sender_raises(RuntimeError("很怪的错误")))
    assert report["category"] == "unknown"
    assert report["exit_code"] == cli.EXIT_ERROR
    assert "很怪的错误" in report["error"]


# ---------------------------------------------------------------- doctor CLI 端到端
def _prepare_doctor_env(monkeypatch, tmp_path, sender):
    monkeypatch.chdir(tmp_path)                 # 避开仓库里的真实 .env
    monkeypatch.setenv("JEV_API_KEY", "test-key")
    monkeypatch.setenv("JEV_BASE_URL", "https://api.invalid")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setattr(cli, "_PROBE_SENDER", sender)


def test_doctor_cli_exit5_on_service_unavailable(monkeypatch, tmp_path):
    _prepare_doctor_env(monkeypatch, tmp_path, sender_raises(http_error(503, b"no healthy upstream")))

    result = runner.invoke(cli.app, ["doctor"])
    payload = json.loads(result.stdout)

    assert result.exit_code == cli.EXIT_UNAVAILABLE
    assert payload["diagnosis_code"] == "service_unavailable"
    assert payload["probe"]["http"] == 503
    assert payload["llm_probe"]["category"] == "not_configured"
    assert "test-key" not in result.stdout          # 绝不回显凭证


def test_doctor_cli_exit4_on_auth_failed(monkeypatch, tmp_path):
    _prepare_doctor_env(monkeypatch, tmp_path, sender_raises(http_error(403, b"forbidden")))

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == cli.EXIT_AUTH
    assert json.loads(result.stdout)["diagnosis_code"] == "auth_failed"


def test_doctor_cli_exit3_on_wrong_host(monkeypatch, tmp_path):
    _prepare_doctor_env(monkeypatch, tmp_path,
                        sender_result(final_url="https://api.invalid/login", body=b"<html>"))

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == cli.EXIT_MISCONFIG
    assert json.loads(result.stdout)["diagnosis_code"] == "misconfigured_base_url"


def test_doctor_cli_exit6_on_unreachable(monkeypatch, tmp_path):
    _prepare_doctor_env(monkeypatch, tmp_path, sender_raises(urllib.error.URLError("dns fail")))

    result = runner.invoke(cli.app, ["doctor"])

    assert result.exit_code == cli.EXIT_UNREACHABLE
    assert json.loads(result.stdout)["diagnosis_code"] == "network_unreachable"


def test_doctor_cli_exit0_and_reports_llm_probe(monkeypatch, tmp_path):
    _prepare_doctor_env(monkeypatch, tmp_path, sender_result())

    result = runner.invoke(cli.app, ["doctor"])
    payload = json.loads(result.stdout)

    assert result.exit_code == cli.EXIT_OK
    assert payload["probe"]["models"] == ["jev-latest", "jev-preview"]
    assert payload["llm_fallback_configured"] is False


def test_doctor_cli_exit2_without_key(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    result = runner.invoke(cli.app, ["doctor"])
    payload = json.loads(result.stdout)

    assert result.exit_code == cli.EXIT_CONFIG
    assert payload["diagnosis_code"] == "missing_key"
    assert payload["jev_api_key"] == "missing"


# ---------------------------------------------------------------- LLM 探测
LLM_ENV = {"LLM_BASE_URL": "https://llm.invalid/v1", "LLM_API_KEY": "test-llm-key",
           "LLM_MODEL": "test-model"}


def _set_llm_env(monkeypatch):
    for key, value in LLM_ENV.items():
        monkeypatch.setenv(key, value)


def test_probe_llm_not_configured(monkeypatch):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    report = cli.probe_llm()
    assert report["category"] == "not_configured"
    assert report["configured"] is False
    assert report["ok"] is False
    assert "LLM_BASE_URL" in report["missing"]


def test_probe_llm_ok(monkeypatch):
    _set_llm_env(monkeypatch)
    seen = {}

    def transport(messages, model):
        seen["messages"] = messages
        seen["model"] = model
        return '```json\n{"act": "1"}\n```'

    report = cli.probe_llm(transport=transport)

    assert report["category"] == "ok" and report["ok"] is True
    assert report["reply"] == "1"
    assert report["latency"] is not None
    assert seen["model"] == "test-model"                    # 复用 cfg 里的模型名
    assert seen["messages"] == brain_llm.PROBE_MESSAGES      # 复用最小探测请求
    assert "test-llm-key" not in json.dumps(report)          # 不回显凭证


def test_probe_llm_auth_failed(monkeypatch):
    _set_llm_env(monkeypatch)

    def transport(messages, model):
        raise brain_llm.LlmError("LLM API 返回 401: bad key", status=401)

    report = cli.probe_llm(transport=transport)
    assert report["category"] == "auth_failed"
    assert report["ok"] is False
    assert "LLM_API_KEY" in report["hint"]


def test_probe_llm_service_unavailable(monkeypatch):
    _set_llm_env(monkeypatch)

    def transport(messages, model):
        raise brain_llm.LlmError("LLM API 返回 503: upstream", status=503)

    report = cli.probe_llm(transport=transport)
    assert report["category"] == "service_unavailable"
    assert report["http"] == 503


def test_probe_llm_unreachable(monkeypatch):
    _set_llm_env(monkeypatch)

    def transport(messages, model):
        raise urllib.error.URLError("connection refused")

    report = cli.probe_llm(transport=transport)
    assert report["category"] == "unreachable"


def test_probe_llm_bad_response(monkeypatch):
    _set_llm_env(monkeypatch)
    report = cli.probe_llm(transport=lambda messages, model: "你好，我不输出 JSON")
    assert report["category"] == "bad_response"
    assert report["ok"] is False


def test_http_transport_maps_httperror_to_llmerror(monkeypatch):
    """http_transport 必须把 HTTPError 换成带 status 的 LlmError（doctor 靠它分类）。"""
    def fake_urlopen(request, timeout=None):
        raise http_error(401, b'{"detail":"bad key"}', url="https://llm.invalid/v1/chat/completions")

    monkeypatch.setattr(brain_llm.urllib.request, "urlopen", fake_urlopen)
    cfg = {"base_url": "https://llm.invalid/v1", "api_key": "test-llm-key",
           "model": "test-model"}
    send = brain_llm.http_transport(cfg, timeout=1)

    with pytest.raises(brain_llm.LlmError) as excinfo:
        send(brain_llm.PROBE_MESSAGES, "test-model")

    assert excinfo.value.status == 401
    assert not isinstance(excinfo.value, urllib.error.HTTPError)
