""".env 定位与加载的优先级、以及在输出里"只记路径不记值"的约定。

重点：仓库根 .env 优先；不加载 HOME_DIR 及以上；doctor/调试日志只出现路径，绝不出现任何值。
"""
import json
import os

import pytest
from typer.testing import CliRunner

from universal import cli

runner = CliRunner()


def write_env(directory, content):
    path = directory / ".env"
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------- 优先级
def test_child_env_wins_over_parent(tmp_path, monkeypatch):
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    write_env(parent, "JEV_TEST_MARK=parent\n")
    child_path = write_env(child, "JEV_TEST_MARK=child\n")

    monkeypatch.delenv("JEV_TEST_MARK", raising=False)
    monkeypatch.setattr(cli, "HOME_DIR", str(tmp_path / "home"))

    loaded = cli.load_env(start=str(child))

    assert loaded == str(child_path)
    assert cli.find_env_file(start=str(child)) == str(child_path)
    assert os.environ["JEV_TEST_MARK"] == "child"


def test_parent_env_found_when_child_missing(tmp_path, monkeypatch):
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    parent_path = write_env(parent, "JEV_TEST_MARK=parent\n")

    monkeypatch.delenv("JEV_TEST_MARK", raising=False)
    monkeypatch.setattr(cli, "HOME_DIR", str(tmp_path / "home"))

    assert cli.load_env(start=str(child)) == str(parent_path)
    assert os.environ["JEV_TEST_MARK"] == "parent"


def test_repo_root_wins_over_deeper_cwd(tmp_path, monkeypatch):
    """仓库根 .env 优先于 cwd（及其上溯）——避免"随手在别处跑"时误加载无关 .env。"""
    repo = tmp_path / "repo"
    deep = repo / "sub" / "deep"
    deep.mkdir(parents=True)
    repo_path = write_env(repo, "JEV_TEST_MARK=repo\n")
    write_env(deep, "JEV_TEST_MARK=deep\n")

    monkeypatch.delenv("JEV_TEST_MARK", raising=False)
    monkeypatch.setattr(cli, "HOME_DIR", str(tmp_path / "home"))
    monkeypatch.setattr(cli, "PROJECT_ROOT", str(repo))
    monkeypatch.chdir(deep)

    assert cli.find_env_file() == str(repo_path)
    assert cli.load_env() == str(repo_path)
    assert os.environ["JEV_TEST_MARK"] == "repo"


def test_home_dotenv_is_never_loaded(tmp_path, monkeypatch):
    """~/.env 与本项目无关，绝不能被自动带进来。"""
    home = tmp_path / "home"
    proj = home / "work" / "proj"
    proj.mkdir(parents=True)
    write_env(home, "JEV_TEST_MARK=home\n")

    monkeypatch.delenv("JEV_TEST_MARK", raising=False)
    monkeypatch.setattr(cli, "HOME_DIR", str(home))
    monkeypatch.setattr(cli, "PROJECT_ROOT", str(tmp_path / "nonexistent-repo"))
    monkeypatch.chdir(proj)

    assert cli.find_env_file(start=str(proj)) is None
    assert cli.load_env(start=str(proj)) is None
    assert "JEV_TEST_MARK" not in os.environ


def test_existing_environment_wins_over_env_file(tmp_path, monkeypatch):
    """load_env 是 setdefault 语义：已有的环境变量不会被 .env 覆盖。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    write_env(repo, "JEV_TEST_MARK=from-file\n")
    monkeypatch.setenv("JEV_TEST_MARK", "from-env")
    monkeypatch.setattr(cli, "PROJECT_ROOT", str(repo))

    cli.load_env()

    assert os.environ["JEV_TEST_MARK"] == "from-env"


# ---------------------------------------------------------------- 只记路径，不记值
def test_doctor_reports_env_path_but_never_the_value(tmp_path, monkeypatch):
    secret = "FAKEKEY-do-not-print-2195"
    env_path = write_env(tmp_path, "JEV_API_KEY=%s\n" % secret)

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    monkeypatch.setenv("JEV_BASE_URL", "https://api.invalid")
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setattr(cli, "PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(cli, "_PROBE_SENDER",
                        lambda request, timeout: (200, "https://api.invalid/v1/models",
                                                  b'{"models":[{"name":"jev-latest"}]}'))

    result = runner.invoke(cli.app, ["doctor"])
    payload = json.loads(result.stdout)

    assert result.exit_code == cli.EXIT_OK
    assert payload["env_file"] == str(env_path)        # 路径如实输出
    assert payload["env_file_found"] is True
    assert payload["jev_api_key"] == "present"
    assert secret not in result.stdout                 # 值绝不出现
    assert secret not in result.output                 # 含 stderr 的合并输出也不出现


def test_debug_log_records_path_not_values(tmp_path, monkeypatch, capsys):
    secret = "FAKEKEY-debug-check"
    env_path = write_env(tmp_path, "JEV_API_KEY=%s\n" % secret)

    monkeypatch.delenv("JEV_API_KEY", raising=False)
    monkeypatch.setenv("JEV_DEBUG", "1")
    monkeypatch.setattr(cli, "PROJECT_ROOT", str(tmp_path))

    loaded = cli.load_env()
    err = capsys.readouterr().err

    assert loaded == str(env_path)
    assert str(env_path) in err                        # 调试日志含路径
    assert secret not in err                           # 但不含值


def test_debug_log_is_silent_by_default(monkeypatch, capsys):
    monkeypatch.delenv("JEV_DEBUG", raising=False)
    cli.debug_log("已加载 .env：/tmp/example/.env")
    assert capsys.readouterr().err == ""


def test_debug_log_marks_which_path_when_enabled(monkeypatch, capsys):
    monkeypatch.setenv("JEV_DEBUG", "1")
    cli.debug_log("已加载 .env：/tmp/example/.env")
    err = capsys.readouterr().err
    assert "/tmp/example/.env" in err
    assert err.startswith("[jev-cu]")
