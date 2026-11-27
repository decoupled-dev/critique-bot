import argparse
import sys

from .store import TaskNotFound, TaskStore


def format_task(task) -> str:
    mark = "x" if task.done else " "
    line = f"[{mark}] {task.id:>3}  {task.title}"
    if task.tags:
        line += " [" + ", ".join(task.tags) + "]"
    return line


def main(argv=None, out=None) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="taskq")
    parser.add_argument("--file", default="todo.json")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_add = sub.add_parser("add")
    p_add.add_argument("title")
    p_list = sub.add_parser("list")
    p_list.add_argument("--all", action="store_true", help="include finished tasks")
    p_list.add_argument("--tag", help="only tasks with this tag")
    p_done = sub.add_parser("done")
    p_done.add_argument("id", type=int)
    args = parser.parse_args(argv)

    store = TaskStore.load(args.file)
    if args.cmd == "add":
        task = store.add(args.title)
        store.save(args.file)
        print(f"added {task.id}", file=out)
    elif args.cmd == "list":
        for task in store.list(include_done=args.all, tag=args.tag):
            print(format_task(task), file=out)
    elif args.cmd == "done":
        try:
            store.complete(args.id)
        except TaskNotFound:
            print(f"no task {args.id}", file=sys.stderr)
            return 1
        store.save(args.file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
