import logging
import os

import pytest

from app import config
from app.config import ConfigError, Settings

SECRET = "sk-test-SUPERSECRET-1234567890"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in config.KEY_NAMES:
        monkeypatch.delenv(name.upper(), raising=False)
        monkeypatch.delenv(f"{name.upper()}_FILE", raising=False)


def make(**kwargs) -> Settings:
    return Settings(_env_file=None, **kwargs)


def write_key(path, content, mode=0o600):
    path.write_text(content)
    os.chmod(path, mode)
    return path


def test_key_read_from_file(tmp_path):
    f = write_key(tmp_path / "key.txt", SECRET)
    assert make(nebius_api_key_file=f).get_key("nebius_api_key") == SECRET


def test_whitespace_stripped(tmp_path):
    f = write_key(tmp_path / "key.txt", f"  \n{SECRET}\r\n\n")
    assert make(tavily_api_key_file=f).get_key("tavily_api_key") == SECRET


def test_relative_path_resolves_against_project_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    write_key(tmp_path / "key_nebius.txt", SECRET)
    assert make(nebius_api_key_file="key_nebius.txt").get_key("nebius_api_key") == SECRET


def test_tilde_expands_to_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".config" / "info_checker").mkdir(parents=True)
    write_key(tmp_path / ".config" / "info_checker" / "key_tavily.txt", SECRET)
    s = make(tavily_api_key_file="~/.config/info_checker/key_tavily.txt")
    assert s.get_key("tavily_api_key") == SECRET


def test_tilde_path_error_shows_expanded_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(ConfigError) as exc:
        make(nebius_api_key_file="~/.config/info_checker/missing.txt").get_key("nebius_api_key")
    assert str(tmp_path / ".config" / "info_checker" / "missing.txt") in str(exc.value)


def test_plain_variable_still_works():
    assert make(nebius_api_key=SECRET).get_key("nebius_api_key") == SECRET


def test_unset_key_is_none_and_require_raises():
    s = make()
    assert s.get_key("google_factcheck_api_key") is None
    with pytest.raises(ConfigError, match="GOOGLE_FACTCHECK_API_KEY"):
        s.require_key("google_factcheck_api_key")


def test_missing_file_error_names_setting_and_path(tmp_path):
    missing = tmp_path / "nope.txt"
    with pytest.raises(ConfigError) as exc:
        make(nebius_api_key_file=missing).get_key("nebius_api_key")
    assert "NEBIUS_API_KEY_FILE" in str(exc.value)
    assert str(missing) in str(exc.value)


def test_empty_file_error(tmp_path):
    f = write_key(tmp_path / "key.txt", "   \n")
    with pytest.raises(ConfigError, match="empty"):
        make(tavily_api_key_file=f).get_key("tavily_api_key")


def test_both_set_error(tmp_path):
    f = write_key(tmp_path / "key.txt", SECRET)
    with pytest.raises(ConfigError, match="Both NEBIUS_API_KEY and NEBIUS_API_KEY_FILE"):
        make(nebius_api_key=SECRET, nebius_api_key_file=f)


def test_both_set_error_via_environment(tmp_path, monkeypatch):
    f = write_key(tmp_path / "key.txt", SECRET)
    monkeypatch.setenv("TAVILY_API_KEY", SECRET)
    monkeypatch.setenv("TAVILY_API_KEY_FILE", str(f))
    with pytest.raises(ConfigError, match="TAVILY_API_KEY_FILE"):
        make()


def test_errors_and_logs_never_contain_key(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    f = write_key(tmp_path / "key.txt", SECRET, mode=0o644)
    messages = []

    for build in (
        lambda: make(nebius_api_key=SECRET, nebius_api_key_file=f),
        lambda: make(tavily_api_key=SECRET, tavily_api_key_file=f),
    ):
        with pytest.raises(ConfigError) as exc:
            build()
        messages += [str(exc.value), repr(exc.value)]

    s = make(nebius_api_key_file=f)
    s.get_key("nebius_api_key")  # triggers the permission warning
    messages += [repr(s), str(s), caplog.text]

    for m in messages:
        assert SECRET not in m


def test_warns_on_group_readable_file(tmp_path, caplog):
    f = write_key(tmp_path / "key.txt", SECRET, mode=0o640)
    make(nebius_api_key_file=f).get_key("nebius_api_key")
    assert "chmod 600" in caplog.text


def test_no_warning_for_private_file(tmp_path, caplog):
    f = write_key(tmp_path / "key.txt", SECRET, mode=0o600)
    make(nebius_api_key_file=f).get_key("nebius_api_key")
    assert "chmod" not in caplog.text
