import json
import os
import tempfile
from pathlib import Path

from .models import Task, normalize_tag, split_tags


class TaskNotFound(KeyError):
    pass


class TaskStore:
    def __init__(self, tasks=None):
        self._tasks: list[Task] = list(tasks or [])

    # --- persistence -------------------------------------------------------
    @classmethod
    def load(cls, path) -> "TaskStore":
        path = Path(path)
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(Task.from_dict(d) for d in data.get("tasks", []))

    def save(self, path) -> None:
        path = Path(path)
        payload = {"version": 1, "tasks": [t.to_dict() for t in self._tasks]}
        fd, tmp = tempfile.mkstemp(dir=path.parent or ".", prefix=".taskq-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, path)

    # --- operations ---------------------------------------------------------
    def _next_id(self) -> int:
        return max((t.id for t in self._tasks), default=0) + 1

    def add(self, title: str) -> Task:
        title, tags = split_tags(title)
        if not title:
            raise ValueError("title must not be empty")
        task = Task(id=self._next_id(), title=title, tags=tags)
        self._tasks.append(task)
        return task

    def get(self, task_id: int) -> Task:
        for t in self._tasks:
            if t.id == task_id:
                return t
        raise TaskNotFound(task_id)

    def complete(self, task_id: int) -> Task:
        task = self.get(task_id)
        task.done = True
        return task

    def list(self, include_done: bool = False, tag: str | None = None) -> list[Task]:
        want = normalize_tag(tag) if tag is not None else None
        return [
            t
            for t in self._tasks
            if (include_done or not t.done) and (want is None or want in t.tags)
        ]
