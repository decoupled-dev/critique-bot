"""First-run terminal welcome: text style, then the Edge sign-in window.

The screen is the same shape as a coding agent's first launch: a banner,
a small scene, a style list, and a live preview of a diff. Arrow keys move
the list. Enter keeps the highlighted style. ``/theme`` opens it again.
"""

from __future__ import annotations

import os
import select
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from critique_bot import log

COMMAND = "crit"
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"

_ART = (
    "      CCCCCC       ",
    "   ccCCCCCCCCCCc   ",
    "  cCCCCCCCCCCCCCc  ",
    "              CC   ",
    "            CCCCCC ",
    "                   ",
    "  bbbbbb      p    ",
    " bb ee bb   ppppp  ",
    " bbbbbbbb     p    ",
    "  bb  bb      s    ",
    "              s    ",
)
_PIXEL = {
    "C": (186, 156, 176),
    "c": (120, 86, 118),
    "b": (224, 122, 74),
    "e": (48, 28, 36),
    "p": (210, 204, 214),
    "w": (255, 252, 255),
    "s": (140, 120, 148),
}
_SAMPLE = (
    (" ", "1", "function greet() {"),
    ("-", "2", 'console.log("Hello, World!");'),
    ("+", "2", 'console.log("Hello, crit!");'),
    (" ", "3", "}"),
)


def _fg(red: int, green: int, blue: int) -> str:
    return f"\033[38;2;{red};{green};{blue}m"


def _bg(red: int, green: int, blue: int) -> str:
    return f"\033[48;2;{red};{green};{blue}m"


@dataclass(frozen=True)
class Theme:
    id: str
    label: str
    plain: str
    keyword: str
    string: str
    gutter: str
    del_fg: str
    del_bg: str
    add_fg: str
    add_bg: str


def _ansi(code: str) -> str:
    return f"\033[{code}m"


THEMES: tuple[Theme, ...] = (
    Theme(
        "auto",
        "Auto (match terminal)",
        plain="",
        keyword=_ansi("34"),
        string=_ansi("32"),
        gutter=_ansi("2"),
        del_fg=_ansi("31"),
        del_bg="",
        add_fg=_ansi("32"),
        add_bg="",
    ),
    Theme(
        "dark",
        "Dark mode",
        plain=_fg(220, 214, 224),
        keyword=_fg(180, 148, 255),
        string=_fg(126, 214, 150),
        gutter=_fg(140, 132, 148),
        del_fg=_fg(255, 196, 196),
        del_bg=_bg(92, 36, 48),
        add_fg=_fg(188, 240, 196),
        add_bg=_bg(28, 72, 48),
    ),
    Theme(
        "light",
        "Light mode",
        plain=_fg(36, 41, 47),
        keyword=_fg(130, 80, 200),
        string=_fg(10, 120, 70),
        gutter=_fg(140, 140, 150),
        del_fg=_fg(140, 32, 48),
        del_bg=_bg(255, 220, 224),
        add_fg=_fg(20, 90, 48),
        add_bg=_bg(214, 244, 222),
    ),
    Theme(
        "dark-colorblind",
        "Dark mode (colorblind-friendly)",
        plain=_fg(220, 214, 224),
        keyword=_fg(130, 180, 255),
        string=_fg(255, 196, 120),
        gutter=_fg(140, 132, 148),
        del_fg=_fg(190, 214, 255),
        del_bg=_bg(28, 52, 96),
        add_fg=_fg(255, 214, 160),
        add_bg=_bg(96, 64, 24),
    ),
    Theme(
        "light-colorblind",
        "Light mode (colorblind-friendly)",
        plain=_fg(36, 41, 47),
        keyword=_fg(20, 80, 170),
        string=_fg(150, 80, 10),
        gutter=_fg(140, 140, 150),
        del_fg=_fg(20, 60, 140),
        del_bg=_bg(214, 228, 255),
        add_fg=_fg(140, 72, 10),
        add_bg=_bg(255, 230, 196),
    ),
    Theme(
        "dark-ansi",
        "Dark mode (ANSI colors only)",
        plain=_ansi("37"),
        keyword=_ansi("34"),
        string=_ansi("32"),
        gutter=_ansi("2"),
        del_fg=_ansi("97"),
        del_bg=_ansi("41"),
        add_fg=_ansi("30"),
        add_bg=_ansi("42"),
    ),
    Theme(
        "light-ansi",
        "Light mode (ANSI colors only)",
        plain=_ansi("30"),
        keyword=_ansi("34"),
        string=_ansi("32"),
        gutter=_ansi("2"),
        del_fg=_ansi("97"),
        del_bg=_ansi("41"),
        add_fg=_ansi("30"),
        add_bg=_ansi("42"),
    ),
)


def stdio_is_tty() -> bool:
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return False


def theme_by_id(theme_id: str) -> Theme:
    for theme in THEMES:
        if theme.id == theme_id:
            return theme
    return THEMES[1]


def render_welcome(*, cursor: int, committed: int, syntax: bool) -> str:
    """The welcome frame. ``cursor`` is the highlighted style; the preview uses it."""
    from critique_bot import __version__

    theme = THEMES[cursor]
    lines = [
        _fg(217, 119, 87) + "✻ " + _RESET + _BOLD + f"Welcome to {COMMAND}" + _RESET + _DIM + f" v{__version__}" + _RESET,
        _DIM + ("·" * 48) + _RESET,
        "",
        *_art_lines(),
        _DIM + ("·" * 48) + _RESET,
        "",
        _BOLD + "Let's get started." + _RESET,
        "",
        _BOLD + "Choose the text style that looks best with your terminal" + _RESET,
        _DIM + "To change this later, run /theme" + _RESET,
        "",
        *_menu(cursor, committed),
        "",
        _DIM + ("─" * 48) + _RESET,
        *_sample(theme, syntax),
        _DIM + ("─" * 48) + _RESET,
        _DIM + _syntax_footer(syntax) + _RESET,
    ]
    return "\n".join(lines) + "\n"


def pick_theme(
    *,
    selected: str = "dark",
    syntax: bool = True,
    read_key: Callable[[], str] | None = None,
    write: Callable[[str], None] | None = None,
) -> tuple[str, bool] | None:
    """Let the user highlight a style. Enter returns ``(theme id, syntax on)``."""
    ids = [theme.id for theme in THEMES]
    if selected not in ids:
        selected = "dark"
    cursor = ids.index(selected)
    committed = cursor
    reader = read_key or _read_key
    clear = write is None and stdio_is_tty()

    def emit(text: str) -> None:
        if write is not None:
            write(text)
            return
        log.print_safe(text, end="", flush=True)

    try:
        if clear:
            emit("\033[?25l")
        while True:
            frame = render_welcome(cursor=cursor, committed=committed, syntax=syntax)
            if clear:
                emit("\033[H\033[2J")
            emit(frame)
            key = reader()
            if key == "up":
                cursor = max(0, cursor - 1)
            elif key == "down":
                cursor = min(len(THEMES) - 1, cursor + 1)
            elif key == "syntax":
                syntax = not syntax
            elif key == "enter":
                return THEMES[cursor].id, syntax
            elif key == "cancel":
                return None
    finally:
        if clear:
            emit("\033[?25h")


def ask_login(
    *,
    prompt: Callable[[str], str] | None = None,
    write: Callable[[str], None] | None = None,
) -> bool:
    """Ask before opening Edge. Enter means yes."""
    _say(write, "")
    _say(write, f"Sign in with Microsoft Edge so {COMMAND} can use the chat.")
    _say(write, "The login window closes after the chat box is ready.")
    try:
        line = (prompt or input)("Open the login window? [Y/n] ")
    except EOFError:
        return False
    return line.strip().lower() not in {"n", "no"}


def reopen_theme(workspace: Path) -> None:
    """``/theme`` from the agent prompt. A pipe skips the screen."""
    if not stdio_is_tty():
        log.print_safe(
            f"Open a terminal and run {COMMAND}, then type /theme.",
            file=sys.stderr,
        )
        return
    from critique_bot.bot_home import find_bot_home, update_settings

    home = find_bot_home(workspace)
    selected = "dark"
    syntax = True
    if home is not None:
        raw = home.settings.get("theme")
        if isinstance(raw, str) and raw:
            selected = raw
        if home.settings.get("syntax_preview") is False:
            syntax = False
    chosen = pick_theme(selected=selected, syntax=syntax)
    if chosen is None or home is None:
        return
    theme_id, syntax_on = chosen
    update_settings(home, theme=theme_id, syntax_preview=syntax_on)


def _say(write: Callable[[str], None] | None, text: str) -> None:
    if write is not None:
        write(text + "\n")
        return
    log.print_safe(text, flush=True)


def _art_lines() -> list[str]:
    rows: list[str] = []
    for row in _ART:
        parts: list[str] = []
        for cell in row:
            color = _PIXEL.get(cell)
            if color is None:
                parts.append("  ")
            else:
                red, green, blue = color
                parts.append(f"{_bg(red, green, blue)}  {_RESET}")
        rows.append("".join(parts).rstrip())
    return rows


def _menu(cursor: int, committed: int) -> list[str]:
    lines: list[str] = []
    for index, theme in enumerate(THEMES):
        if index == cursor:
            mark = ">"
        elif index == committed:
            mark = "✓"
        else:
            mark = " "
        label = theme.label
        if index == cursor:
            label = _BOLD + label + _RESET
        lines.append(f" {mark} {label}")
    return lines


def _sample(theme: Theme, syntax: bool) -> list[str]:
    rows: list[str] = []
    for kind, number, text in _SAMPLE:
        mark = "-" if kind == "-" else "+" if kind == "+" else " "
        if kind == "-":
            plain, bg = theme.del_fg, theme.del_bg
        elif kind == "+":
            plain, bg = theme.add_fg, theme.add_bg
        else:
            plain, bg = theme.plain, ""
        gutter = _paint(f" {number:>2} {mark} ", theme.gutter if kind == " " else plain, bg)
        body = _highlight(text, theme, syntax, bg=bg, plain=plain)
        rows.append(gutter + body)
    return rows


def _highlight(text: str, theme: Theme, syntax: bool, *, bg: str, plain: str) -> str:
    if not syntax:
        return _paint(text, plain, bg)
    parts: list[str] = []
    index = 0
    while index < len(text):
        if text.startswith("function", index) and _boundary(text, index):
            parts.append(_paint("function", theme.keyword, bg))
            index += len("function")
            continue
        if text[index] == '"':
            end = text.find('"', index + 1)
            end = len(text) if end < 0 else end + 1
            parts.append(_paint(text[index:end], theme.string, bg))
            index = end
            continue
        nxt = index + 1
        while nxt < len(text) and text[nxt] != '"' and not (
            text.startswith("function", nxt) and _boundary(text, nxt)
        ):
            nxt += 1
        parts.append(_paint(text[index:nxt], plain, bg))
        index = nxt
    return "".join(parts)


def _boundary(text: str, index: int) -> bool:
    if index == 0:
        return True
    return not text[index - 1].isalnum()


def _paint(text: str, fg: str, bg: str) -> str:
    if not text:
        return ""
    if not fg and not bg:
        return text
    return f"{fg}{bg}{text}{_RESET}"


def _syntax_footer(syntax: bool) -> str:
    if syntax:
        return "Syntax theme: GitHub (ctrl+t to disable)"
    return "Syntax theme: off (ctrl+t to enable)"


def _read_key() -> str:
    if os.name == "nt":
        return _read_key_windows()
    return _read_key_posix()


def _map_key(ch: str) -> str:
    if ch in {"\r", "\n"}:
        return "enter"
    if ch == "\x03":
        raise KeyboardInterrupt
    if ch == "\x14":
        return "syntax"
    if ch in {"q", "Q"}:
        return "cancel"
    if ch in {"k", "K"}:
        return "up"
    if ch in {"j", "J"}:
        return "down"
    return ""


def _read_key_windows() -> str:
    import msvcrt

    ch = msvcrt.getwch()
    if ch in {"\x00", "\xe0"}:
        nxt = msvcrt.getwch()
        return {"H": "up", "P": "down"}.get(nxt, "")
    return _map_key(ch)


def _read_key_posix() -> str:
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return _decode_key(lambda: _read_byte(fd), lambda: bool(select.select([fd], [], [], 0.05)[0]))
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _read_byte(fd: int) -> str:
    """One byte straight from the descriptor.

    ``sys.stdin.read(1)`` would pull the whole escape sequence into Python's
    buffer, and ``select`` on the descriptor would then see nothing left, so
    an arrow key looked like a lone Esc (cancel).
    """
    data = os.read(fd, 1)
    return data.decode("latin-1") if data else ""


def _decode_key(read: Callable[[], str], ready: Callable[[], bool]) -> str:
    ch = read()
    if ch != "\x1b":
        return _map_key(ch)
    if not ready():
        return "cancel"
    nxt = read()
    if nxt not in {"[", "O"} or not ready():
        return ""
    arrow = read()
    return {"A": "up", "B": "down"}.get(arrow, "")
