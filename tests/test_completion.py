"""shell 补全：脚本与命令面一致、安装位置正确。"""

from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from flash_input import cli, completion


@pytest.fixture()
def parser():
    return cli.build_parser()


def subcommand_names(parser) -> list[str]:
    return [str(entry["name"]) for entry in completion.commands(parser)]


def test_bash_script_mentions_every_subcommand(parser):
    text = completion.bash_script(parser)
    for name in subcommand_names(parser):
        assert name in text
    assert "complete -F _finput_complete finput" in text


def test_zsh_script_has_compdef(parser):
    text = completion.zsh_script(parser)
    assert text.startswith("#compdef finput")
    assert "_describe" in text


def test_fish_script_completes_subcommands(parser):
    text = completion.fish_script(parser)
    assert "complete -c finput" in text
    assert "__fish_use_subcommand" in text


def test_config_keys_are_completable_and_skip_version(parser):
    keys = completion.config_keys()
    assert "asr.model" in keys and "ocr.trigger" in keys
    assert "version" not in keys


def test_detect_shell_explicit_and_from_env(monkeypatch):
    assert completion.detect_shell("zsh") == "zsh"
    monkeypatch.setenv("SHELL", "/usr/bin/fish")
    assert completion.detect_shell() == "fish"
    with pytest.raises(completion.CompletionError):
        completion.detect_shell("csh")


@pytest.mark.parametrize(
    "shell,suffix",
    [
        ("bash", "bash-completion/completions/finput"),
        ("zsh", "zsh/site-functions/_finput"),
        ("fish", "fish/completions/finput.fish"),
    ],
)
def test_install_and_uninstall_write_standard_paths(tmp_path, parser, monkeypatch, shell, suffix):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    path = completion.install(parser, shell, home=tmp_path)
    assert str(path).endswith(suffix)
    assert path.is_file()
    assert completion.uninstall(shell, home=tmp_path) is True
    assert not path.exists()
    assert completion.uninstall(shell, home=tmp_path) is False


def test_cli_completion_prints_script(tmp_path, parser, monkeypatch):
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = cli.cmd_completion(cli_argparse_ns({"target": "bash", "shell": None}))
    assert code == cli.EXIT_OK
    assert "complete -F" in buffer.getvalue()


def cli_argparse_ns(values):
    class _Args:
        def __init__(self, data):
            self.__dict__.update(data)

    return _Args(values)
