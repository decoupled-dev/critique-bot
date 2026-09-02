from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Finding:
    file: str
    line: int
    column: int
    level: str
    api: str
    method: str
    receiver: str
    snippet: str
    parse_sources: list[str] = field(default_factory=list)
    enclosing_class: str = ""
    enclosing_function: str = ""
    contexts: list[str] = field(default_factory=list)
    chatty_score: int = 0
    why: str = ""

    def merge_key(self) -> tuple[str, int, str, str]:
        return (self.file, self.line, self.level, self.method)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FileError:
    file: str
    error: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class ScanStats:
    root: str
    files_scanned: int = 0
    files_with_findings: int = 0
    findings: int = 0
    parse_failures: int = 0
    bytes_scanned: int = 0
