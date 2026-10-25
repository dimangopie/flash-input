"""shell 补全：从 `build_parser()` 现生成 bash / zsh / fish 脚本。

为什么不手写静态脚本：命令面一直在变，手写的那份必然过期。这里每次调用都重新
遍历 argparse 的 parser，子命令、嵌套子命令和选项永远与 `finput -h` 一致。

用法：

    finput completion             # 打印当前 $SHELL 的补全脚本
    finput completion zsh         # 打印指定 shell 的脚本
    finput completion install     # 写进标准目录，新终端自动生效
    finput completion uninstall

安装位置（XDG 标准，不需要 root）：

    bash  $XDG_DATA_HOME/bash-completion/completions/finput
    zsh   $XDG_DATA_HOME/zsh/site-functions/_finput
    fish  $XDG_CONFIG_HOME/fish/completions/finput.fish
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from . import config as config_mod

PROG_FALLBACK = "finput"
SHELLS = ("bash", "zsh", "fish")


class CompletionError(RuntimeError):
    """补全脚本生成/安装失败（例如不认识的 shell）。"""


def detect_shell(shell: str | None = None) -> str:
    """优先用显式指定，其次从 `$SHELL` 猜；认不出来按 bash 处理。"""
    if shell:
        name = shell.strip().lower()
        if name in SHELLS:
            return name
        raise CompletionError(f"不支持的 shell：{shell}（只支持 {' / '.join(SHELLS)}）")
    current = Path(os.environ.get("SHELL", "")).name.lower()
    for name in SHELLS:
        if name in current:
            return name
    return "bash"


def _subparser_map(action: argparse.Action) -> dict[str, argparse.ArgumentParser]:
    return dict(action.choices.items())  # type: ignore[attr-defined]


def _options(parser: argparse.ArgumentParser) -> list[str]:
    out: list[str] = []
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            continue
        out.extend(action.option_strings)
    return out


def commands(parser: argparse.ArgumentParser) -> list[dict[str, object]]:
    """``[{name, help, options, children}]``，children 用于 config / service 这类二级命令。"""
    result: list[dict[str, object]] = []
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for name, child in _subparser_map(action).items():
            kids: list[dict[str, str]] = []
            for kid_action in child._actions:
                if not isinstance(kid_action, argparse._SubParsersAction):
                    continue
                for kid_name, kid_child in _subparser_map(kid_action).items():
                    kids.append({"name": kid_name, "help": kid_child.description or ""})
            result.append(
                {
                    "name": name,
                    "help": _help_of(action, name),
                    "options": _options(child),
                    "children": kids,
                }
            )
    return result


def _help_of(action: argparse.Action, name: str) -> str:
    for choice in getattr(action, "_choices_actions", []):
        if choice.dest == name:
            return choice.help or ""
    return ""


def config_keys() -> list[str]:
    """`config set` 的补全候选：扁平化的点号键名。"""
    keys: list[str] = []

    def walk(prefix: str, node: dict) -> None:
        for key, value in node.items():
            dotted = f"{prefix}{key}"
            if isinstance(value, dict):
                walk(dotted + ".", value)
            else:
                keys.append(dotted)

    walk("", config_mod.DEFAULTS)
    # `version` 是配置的元数据，不是能用 `config set` 改的项
    return [key for key in keys if key != "version"]


def _words(entry: dict[str, object]) -> list[str]:
    words = [str(kid["name"]) for kid in entry["children"]]  # type: ignore[union-attr]
    words += [opt for opt in entry["options"] if not opt.startswith("-h")]  # type: ignore[union-attr]
    return words


def bash_script(parser: argparse.ArgumentParser) -> str:
    prog = parser.prog
    lines = [
        f"# {prog} bash 补全（生成：{prog} completion bash；安装：{prog} completion install）",
        f"_{prog}_complete() {{",
        "    local cur prev cmd word i",
        "    COMPREPLY=()",
        '    cur="${COMP_WORDS[COMP_CWORD]}"',
        '    prev="${COMP_WORDS[COMP_CWORD-1]}"',
        "",
        '    case "$prev" in',
        '        --config) COMPREPLY=($(compgen -f -- "$cur")); return ;;',
        f'        --shell)  COMPREPLY=($(compgen -W "{" ".join(SHELLS)}" -- "$cur")); return ;;',
        '        --layout) COMPREPLY=($(compgen -W "reading box" -- "$cur")); return ;;',
        '        --image)  COMPREPLY=($(compgen -f -- "$cur")); return ;;',
        "    esac",
        "",
        '    cmd=""',
        "    for ((i = 1; i < COMP_CWORD; i++)); do",
        '        word="${COMP_WORDS[i]}"',
        '        case "$word" in',
        "            -c|--config|-v|--verbose) ((i++)) ;;",
        "            -*) ;;",
        '            *) cmd="$word"; break ;;',
        "        esac",
        "    done",
        "",
        '    case "$cmd" in',
        f'        "") COMPREPLY=($(compgen -W "{" ".join(str(e["name"]) for e in commands(parser))} --version --config --verbose --help" -- "$cur")) ;;',
    ]
    for entry in commands(parser):
        words = _words(entry)
        lines.append(f'        {entry["name"]}) COMPREPLY=($(compgen -W "{" ".join(words)}" -- "$cur")) ;;')
    # config set 补键名
    lines.extend(
        [
            "    esac",
            "",
            '    if [ "$cmd" = "config" ] && { [ "$prev" = "set" ] || [ "${COMP_WORDS[2]}" = "set" ]; }; then',
            f'        COMPREPLY=($(compgen -W "{" ".join(config_keys())}" -- "$cur"))',
            "    fi",
            "}",
            f"complete -F _{prog}_complete {prog}",
            "",
        ]
    )
    return "\n".join(lines)


def zsh_script(parser: argparse.ArgumentParser) -> str:
    prog = parser.prog
    entries = commands(parser)
    descr = " ".join(f"'{e['name']}:{str(e['help']).replace(chr(39), '')}'" for e in entries)
    cases: list[str] = []
    for entry in entries:
        words = _words(entry)
        escaped = " ".join(w for w in words if not w.startswith("-"))
        if escaped:
            cases.append(f"        {entry['name']}) _values '{entry['name']}' {escaped} ;;")
    return "\n".join(
        [
            f"#compdef {prog}",
            f"# {prog} zsh 补全（生成：{prog} completion zsh）",
            "",
            f"_{prog}() {{",
            "    local -a cmds",
            f"    cmds=({descr})",
            "    _arguments -C \\",
            "        '--config[配置文件路径]:file:_files' \\",
            "        '--version[版本]' \\",
            "        '-v[调试日志]' \\",
            "        '1:命令:->cmd' \\",
            "        '*::参数:->args'",
            "    case $state in",
            "        cmd) _describe -t commands '命令' cmds ;;",
            "        args)",
            "            case $words[1] in",
            *cases,
            "            esac ;;",
            "    esac",
            "}",
            f'_{prog} "$@"',
            "",
        ]
    )


def fish_script(parser: argparse.ArgumentParser) -> str:
    prog = parser.prog
    lines = [f"# {prog} fish 补全（生成：{prog} completion fish）", f"complete -c {prog} -f"]
    for entry in commands(parser):
        desc = str(entry["help"]).replace("'", "")
        lines.append(f"complete -c {prog} -n '__fish_use_subcommand' -a {entry['name']} -d '{desc}'")
        lines.extend(
            f"complete -c {prog} -n '__fish_seen_subcommand_from {entry['name']}' "
            f"-a {kid['name']} -d '{str(kid['help']).replace(chr(39), '')}'"
            for kid in entry["children"]  # type: ignore[union-attr]
        )
    lines.append(f"complete -c {prog} -n '__fish_seen_subcommand_from config' -a '{config_keys()[0]}'")
    lines.extend(
        [
            f"complete -c {prog} -l config -r -d '配置文件路径'",
            f"complete -c {prog} -s v -l verbose -d '调试日志'",
            "",
        ]
    )
    return "\n".join(lines)


def script(parser: argparse.ArgumentParser, shell: str) -> str:
    name = detect_shell(shell)
    if name == "bash":
        return bash_script(parser)
    if name == "zsh":
        return zsh_script(parser)
    return fish_script(parser)


def target(shell: str, home: Path | None = None) -> Path:
    """补全脚本的标准安装位置。"""
    name = detect_shell(shell)
    home = Path(home or Path.home())
    data = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    conf = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    if name == "bash":
        return data / "bash-completion" / "completions" / "finput"
    if name == "zsh":
        return data / "zsh" / "site-functions" / "_finput"
    return conf / "fish" / "completions" / "finput.fish"


def install(parser: argparse.ArgumentParser, shell: str, home: Path | None = None) -> Path:
    path = target(shell, home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(script(parser, shell), encoding="utf-8")
    except OSError as exc:
        raise CompletionError(f"写入补全脚本失败：{exc}") from exc
    return path


def uninstall(shell: str, home: Path | None = None) -> bool:
    path = target(shell, home)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise CompletionError(f"删除补全脚本失败：{exc}") from exc


def hint(shell: str) -> str:
    """安装后怎么让它生效。"""
    name = detect_shell(shell)
    if name == "zsh":
        return (
            "zsh 需要让 fpath 包含 $XDG_DATA_HOME/zsh/site-functions（多数发行版的 zsh 默认如此）；"
            "否则在 ~/.zshrc 里加 fpath+=($XDG_DATA_HOME/zsh/site-functions) 后重开终端"
        )
    if name == "fish":
        return "fish 会自动加载 ~/.config/fish/completions/，重开终端或执行 `source` 即可"
    return "bash 会自动加载 ~/.local/share/bash-completion/completions/（bash-completion 包），重开终端生效"
