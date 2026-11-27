# SPDX-License-Identifier: Apache-2.0
"""SELinux denials: parse ``avc: denied`` lines, pick the one that matters, and write the
minimal allow rule (what ``audit2allow`` does, in Python, never with wildcards or extra
permissions) plus the ``.te`` file that declares the source domain.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

_AVC = re.compile(r"avc:\s+denied\s+\{\s*(?P<perms>[^}]*)\}\s+for\s+(?P<rest>.*)")
_FIELD = re.compile(r"(\w+)=(\"[^\"]*\"|\S+)")
_CONTEXT = re.compile(r"^u:(?:r|object_r):([\w.-]+):")
_SERVICE_EXIT = re.compile(r"Service '([^']+)' \(pid (\d+)\) (?:exited|received signal|killed|crashed)")
_DOMAIN = re.compile(r"^[A-Za-z_][\w]*$")

#: Target types that are a generic label: allowing them gives far more than the one file needs.
GENERIC_TARGETS = frozenset({
    "unlabeled", "default_prop", "device", "vendor_file", "system_file", "sysfs", "proc", "tmpfs", "rootfs",
    "vendor_data_file", "system_data_file", "default_android_service", "default_android_hwservice",
    "default_android_vndservice", "vendor_default_prop", "shell_data_file", "fuse", "sdcardfs",
})
_APP_DOMAINS = ("untrusted_app", "isolated_app", "ephemeral_app", "sdk_sandbox")


@dataclass
class Denial:
    line: int
    perms: tuple[str, ...]
    source: str
    target: str
    tclass: str
    permissive: str = ""
    comm: str = ""
    pid: str = ""
    name: str = ""
    raw: str = ""
    path: str = ""


@dataclass
class Group:
    source: str
    target: str
    tclass: str
    perms: list[str] = field(default_factory=list)
    count: int = 0
    first: int = 0
    enforced: bool = False
    comms: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    paths: list[str] = field(default_factory=list)
    devs: list[str] = field(default_factory=list)
    pids: set[str] = field(default_factory=set)
    score: float = 0.0
    why: list[str] = field(default_factory=list)

    def rule(self) -> str:
        """The minimal allow rule: exactly the denied permissions, no wildcards, no macros."""
        target = "self" if self.target == self.source else self.target
        perms = sorted(set(self.perms))
        body = perms[0] if len(perms) == 1 else "{ " + " ".join(perms) + " }"
        return f"allow {self.source} {target}:{self.tclass} {body};"

    def context(self) -> str:
        return f"scontext={self.source} tcontext={self.target} tclass={self.tclass} {{ {' '.join(sorted(set(self.perms)))} }}"


def parse_line(number: int, text: str) -> Denial | None:
    match = _AVC.search(text)
    if not match:
        return None
    fields = {key: value.strip('"') for key, value in _FIELD.findall(match.group("rest"))}
    source = _CONTEXT.match(fields.get("scontext", ""))
    target = _CONTEXT.match(fields.get("tcontext", ""))
    tclass = fields.get("tclass", "")
    perms = tuple(p for p in match.group("perms").split() if re.fullmatch(r"[\w]+", p))
    if not (source and target and tclass and perms):
        return None
    return Denial(number, perms, source.group(1), target.group(1), tclass, fields.get("permissive", ""),
                  fields.get("comm", ""), fields.get("pid", ""), fields.get("name", "") or fields.get("path", ""),
                  text.strip(), fields.get("path", "") + ("|" + fields["dev"] if fields.get("dev") else ""))


def collect(lines: Iterable[tuple[int, str]]) -> tuple[list[Denial], dict[str, str]]:
    """Every denial, and the services init saw exit (pid -> service name)."""
    denials: list[Denial] = []
    exited: dict[str, str] = {}
    for number, text in lines:
        if "avc:" in text:
            found = parse_line(number, text)
            if found is not None:
                denials.append(found)
        elif "Service '" in text:
            match = _SERVICE_EXIT.search(text)
            if match:
                exited[match.group(2)] = match.group(1)
    return denials, exited


def group(denials: list[Denial], exited: dict[str, str] | None = None, focus: str = "") -> list[Group]:
    """Denials grouped by (source, target, class), the blocking ones first.

    Ranking: enforced (permissive=0) beats permissive; a denial of a process init saw exit, or of
    the domain/process named in ``focus``, beats the rest; then how often it happened.
    """
    exited = exited or {}
    focus = focus.strip().lower()
    groups: dict[tuple[str, str, str], Group] = {}
    for item in denials:
        key = (item.source, item.target, item.tclass)
        entry = groups.get(key)
        if entry is None:
            entry = groups[key] = Group(item.source, item.target, item.tclass, first=item.line)
        entry.count += 1
        for perm in item.perms:
            if perm not in entry.perms:
                entry.perms.append(perm)
        if item.permissive == "0":
            entry.enforced = True
        if item.comm and item.comm not in entry.comms:
            entry.comms.append(item.comm)
        if item.name and item.name not in entry.names and len(entry.names) < 3:
            entry.names.append(item.name)
        if item.pid:
            entry.pids.add(item.pid)
        path, _, dev = item.path.partition("|")
        if path and path not in entry.paths:
            entry.paths.append(path)
        if dev and dev not in entry.devs:
            entry.devs.append(dev)
    services = {name.lower() for name in exited.values()}
    for entry in groups.values():
        if entry.enforced:
            entry.score += 10
            entry.why.append("enforced (permissive=0): this one blocks")
        else:
            entry.why.append("permissive=1: logged, not blocking")
        died = sorted({exited[pid] for pid in entry.pids if pid in exited})
        if died:
            entry.score += 6
            entry.why.append(f"its process is service {', '.join(died)}, which init saw exit")
        elif services and any(_related(comm, service) for comm in entry.comms for service in services):
            entry.score += 4
            entry.why.append("its process name matches a service init saw exit")
        if focus and (focus in entry.source.lower() or any(focus in comm.lower() for comm in entry.comms)):
            entry.score += 5
            entry.why.append(f"matches {focus!r}")
        entry.score += min(entry.count, 20) / 20
    return sorted(groups.values(), key=lambda g: (-g.score, g.first))


def _related(comm: str, service: str) -> bool:
    comm = comm.lower()
    tail = service.rsplit(".", 1)[-1]
    return bool(comm) and (comm in service or tail in comm or comm in tail)


def hints(entry: Group) -> list[str]:
    """Know-how for this denial: when the plain allow rule is the wrong fix."""
    out: list[str] = []
    if entry.target in GENERIC_TARGETS:
        out.append(f"{entry.target} is a generic label: give the object its own type (file_contexts, "
                   "property_contexts, service_contexts) and allow that type instead")
    if entry.source.startswith(_APP_DOMAINS):
        out.append(f"{entry.source} is an app domain: neverallow rules forbid most of these; use a service or a "
                   "permission instead of policy")
    if entry.tclass == "unix_stream_socket" and "connectto" in entry.perms:
        out.append("the macro form is unix_socket_connect(<source>, <socket prefix>, <target>), which also allows "
                   "writing the sock_file")
    if entry.tclass == "binder" and "call" in entry.perms:
        out.append(f"the usual form is binder_call({entry.source}, {entry.target})")
    if entry.tclass == "property_service" and "set" in entry.perms:
        out.append(f"the usual form is set_prop({entry.source}, <the property's type>)")
    if entry.tclass == "service_manager" and "find" in entry.perms:
        out.append("for a service lookup the usual form is allow <domain> <service type>:service_manager find;")
    if not entry.enforced:
        out.append("permissive: the device or domain is not enforcing, so this denial is not what fails")
    return out


def relabel(entry: Group) -> list[str]:
    """For a generic target: the label and type to add instead, as concrete lines to adapt."""
    path = entry.paths[0] if entry.paths else ""
    source = entry.source
    if entry.target == "sysfs" or "sysfs" in entry.devs or path.startswith("/sys/"):
        node = path[len("/sys"):] if path.startswith("/sys/") else path
        platform = re.match(r"^(/devices/platform/[^/]+)", node)
        stable = platform.group(1) if platform else node  # hwmon3 and similar indexes change between boots
        name = "sysfs_" + re.sub(r"[^a-z0-9]+", "_", (stable.rsplit("/", 1)[-1] or "node").lower()).strip("_")
        perms = " ".join(sorted(set(entry.perms)))
        return [
            f"instead: genfs_contexts: genfscon sysfs {stable or '<the node under /sys>'} u:object_r:{name}:s0"
            + (" (a stable parent, not the hwmonN index)" if platform and platform.group(1) != node else ""),
            f"instead: file.te: type {name}, fs_type, sysfs_type;",
            f"instead: {source}'s .te: allow {source} {name}:{entry.tclass} {{ {perms} }};",
        ]
    if path:
        name = re.sub(r"[^a-z0-9]+", "_", path.rsplit("/", 1)[-1].lower()).strip("_") + "_file"
        return [f"instead: file_contexts: {path} u:object_r:{name}:s0, type {name} in file.te, then allow {source} "
                f"{name}:{entry.tclass} {{ {' '.join(sorted(set(entry.perms)))} }};"]
    return ["instead: label the object with its own type (file_contexts, genfs_contexts, property_contexts or "
            "service_contexts) and allow that type"]


def te_files(workspace: Path, files: list[str], domain: str) -> list[tuple[str, int]]:
    """``.te`` files that declare ``type <domain>`` (path, line), nearest match first."""
    if not _DOMAIN.match(domain):
        return []
    pattern = re.compile(r"^\s*type\s+" + re.escape(domain) + r"\s*[,;]", re.M)
    out: list[tuple[str, int]] = []
    for rel in files:
        if not rel.endswith(".te"):
            continue
        try:
            text = (Path(workspace) / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = pattern.search(text)
        if match:
            out.append((rel, text.count("\n", 0, match.start()) + 1))
    return out


def already_allowed(workspace: Path, te: str, entry: Group) -> bool:
    """True when ``te`` already has an allow rule for every denied permission."""
    try:
        text = (Path(workspace) / te).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    target = "(?:self|" + re.escape(entry.target) + ")" if entry.target == entry.source else re.escape(entry.target)
    rule = re.compile(r"^\s*allow\s+" + re.escape(entry.source) + r"\s+" + target + r"\s*:\s*" + re.escape(entry.tclass)
                      + r"\s+(\{[^}]*\}|\w+)\s*;", re.M)
    granted: set[str] = set()
    for match in rule.finditer(text):
        granted.update(match.group(1).strip("{} ").split())
    return bool(granted) and set(entry.perms) <= granted
