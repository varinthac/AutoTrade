"""`.env` parse/edit/write for `scripts/autotrade_gui.py`'s Settings tab --
pure logic, no tkinter import, so it's independently testable.

Parses on a line-by-line basis, splitting on `\\n` (not `str.splitlines()`,
which would discard whether the file ends with a trailing newline) so that
`parse_env(text).render() == text` holds byte-for-byte with no `.set()`
calls -- comment/blank lines are kept verbatim, `KEY=VALUE` lines are
reconstructed from their parsed (key, value) pair.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from autotrade.common.config import REPO_ROOT

DEFAULT_ENV_PATH = REPO_ROOT / ".env"
DEFAULT_ENV_EXAMPLE_PATH = REPO_ROOT / ".env.example"

SECRET_KEYS: frozenset[str] = frozenset({
    "MT5_PASSWORD",
    "ANTHROPIC_API_KEY",
    "FINNHUB_API_KEY",
    "FMP_API_KEY",
    "EODHD_API_TOKEN",
    "RAPIDAPI_KEY",
    "ALPHAVANTAGE_API_KEY",
    "TELEGRAM_BOT_TOKEN",
})

_KEY_VALUE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


@dataclass
class _Line:
    raw: str
    key: str | None = None
    value: str | None = None


@dataclass
class EnvDocument:
    lines: list[_Line] = field(default_factory=list)

    def get(self, key: str) -> str | None:
        for line in self.lines:
            if line.key == key:
                return line.value
        return None

    def set(self, key: str, value: str) -> None:
        for line in self.lines:
            if line.key == key:
                line.value = value
                return
        new_line = _Line(raw="", key=key, value=value)
        if self.lines and self.lines[-1].key is None and self.lines[-1].raw == "":
            # Last element is the empty string left by a trailing "\n" in the
            # original text (see parse_env) -- insert before it so the
            # trailing newline is preserved rather than duplicated.
            self.lines.insert(len(self.lines) - 1, new_line)
        else:
            self.lines.append(new_line)

    def keys(self) -> list[str]:
        return [line.key for line in self.lines if line.key is not None]

    def render(self) -> str:
        parts = [f"{line.key}={line.value}" if line.key is not None else line.raw for line in self.lines]
        return "\n".join(parts)


def parse_env(text: str) -> EnvDocument:
    doc = EnvDocument()
    for part in text.split("\n"):
        if part.startswith("#") or part.strip() == "":
            doc.lines.append(_Line(raw=part))
            continue
        match = _KEY_VALUE_RE.match(part)
        if match:
            doc.lines.append(_Line(raw=part, key=match.group(1), value=match.group(2)))
        else:
            doc.lines.append(_Line(raw=part))
    return doc


def validate_field(key: str, value: str) -> str | None:
    """Only `MT5_LOGIN` is validated -- `common/config.py`'s
    `load_mt5_credentials` does `int(login)` on it and would crash on
    anything else. Every other field is intentionally unvalidated here,
    matching this codebase's existing minimal-validation stance."""
    if key == "MT5_LOGIN":
        if value == "" or value.isdigit():
            return None
        return "MT5_LOGIN must be blank or a number"
    return None


def write_env_atomic(path: Path, content: str) -> None:
    """Writes via a temp file in the SAME directory as `path` (not the system
    temp dir, which may be on a different drive and would make os.replace a
    non-atomic cross-filesystem copy) then os.replace()s it in -- a crash,
    kill, or disk-full mid-write leaves the original `.env` untouched instead
    of truncated/corrupted."""
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)


def ensure_env_exists(env_path: Path | None = None, example_path: Path | None = None) -> bool:
    env_path = env_path or DEFAULT_ENV_PATH
    example_path = example_path or DEFAULT_ENV_EXAMPLE_PATH
    if env_path.exists():
        return False
    shutil.copy(example_path, env_path)
    return True
