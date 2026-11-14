"""Text for a terminal: what the command line prints, made safe to show.

File names, error messages and anything else that may come from outside (a log file named by
whoever wrote it, a key inside a downloaded dataset) can hold control characters: an escape
sequence could rewrite the terminal's title or earlier lines, and a bidirectional override could
make a line read differently from its bytes. :func:`printable` shows each such character as an
escape instead, so a line on screen always says what its bytes say.
"""

import re
import unicodedata

import typer

# Control characters other than newline and tab, DEL and the C1 controls.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
# Unicode categories shown escaped: format characters (bidirectional overrides, zero-width
# characters) and line or paragraph separators.
_INVISIBLE = frozenset({"Cf", "Zl", "Zp"})


def _escape(character: str) -> str:
    code = ord(character)
    return f"\\x{code:02x}" if code <= 0xFF else f"\\u{code:04x}"  # noqa: PLR2004 -- one byte


def printable(text: str) -> str:
    """``text`` with every control, format and separator character except newline and tab
    shown as a ``\\xNN`` or ``\\uNNNN`` escape."""
    text = _CONTROL.sub(lambda match: _escape(match.group()), text)
    if text.isascii():
        return text
    return "".join(_escape(c) if unicodedata.category(c) in _INVISIBLE else c for c in text)


def echo(message: str = "", err: bool = False, nl: bool = True) -> None:
    """Print ``message`` like :func:`typer.echo`, through :func:`printable`."""
    typer.echo(printable(message), err=err, nl=nl)
