import re
from dataclasses import dataclass, field

TAG_RE = re.compile(r"(?<!\S)#([A-Za-z0-9_-]+)(?!\S)")


def normalize_tag(tag: str) -> str:
    return tag.strip().lstrip("#").lower()


def split_tags(title: str) -> tuple[str, list[str]]:
    """Pull ``#tag`` words out of a title. Returns (clean title, tags)."""
    tags: list[str] = []
    for m in TAG_RE.finditer(title):
        tag = m.group(1).lower()
        if tag not in tags:
            tags.append(tag)
    clean = TAG_RE.sub(" ", title)
    return " ".join(clean.split()), tags


@dataclass
class Task:
    id: int
    title: str
    done: bool = False
    tags: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "done": self.done, "tags": list(self.tags)}

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        return cls(
            id=int(data["id"]),
            title=str(data["title"]),
            done=bool(data.get("done", False)),
            tags=[str(t) for t in data.get("tags", [])],
        )
