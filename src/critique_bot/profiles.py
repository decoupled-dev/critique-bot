# SPDX-License-Identifier: Apache-2.0
"""Model profiles: what differs between model families, kept as data instead of code paths.

A profile says how crit should talk to one family of models: native or prompted tool
calls, the sampling the model card recommends, whether reasoning goes back into the
history, edit format, and context and output limits. Built-in profiles ship in
``critique_bot/profiles/*.json``; a person's own go in ``~/.crit/profiles/*.json``
(:func:`critique_bot.config.crit_home`). A user profile with a built-in's ``id`` overrides
just the fields it sets; one with a new ``id`` adds a profile.

Resolution (:func:`resolve`): ``local.profile`` in config.json when it names a known
profile, else the profile whose ``match`` glob fits the model name best (user profiles
before built-ins), else ``generic``. A name is matched as given and by its last path part,
so ``qwen/qwen3.6-27b`` (OpenRouter), ``Qwen/Qwen3.6-27B`` (Hugging Face) and ``qwen3.6:27b``
(Ollama) all find the Qwen3.6 profile.

Server presets (``profiles/servers/*.json``) describe how to launch each server for agent
use; ``crit doctor model`` prints them. The doctor's probe results are cached in
``~/.crit/cache/doctor.json`` (:func:`cached_probe`); ``tool_mode: auto`` trusts them.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from critique_bot import log

__all__ = [
    "Profile",
    "ProfileError",
    "ServerPreset",
    "TOOL_MODES",
    "builtin_dir",
    "cached_probe",
    "doctor_cache_path",
    "effective_tool_mode",
    "load_profiles",
    "resolve",
    "save_probe",
    "server_presets",
    "user_dir",
]

TOOL_NATIVE, TOOL_PROMPTED, TOOL_AUTO = "native", "prompted", "auto"
TOOL_MODES = (TOOL_NATIVE, TOOL_PROMPTED, TOOL_AUTO)
EDIT_FORMATS = ("str_replace", "whole_file", "apply_patch")
#: Sampling keys crit sends. temperature/top_p/presence_penalty are OpenAI fields; top_k,
#: min_p and repetition_penalty are extensions llama.cpp, vLLM, SGLang and OpenRouter accept.
SAMPLING_KEYS = ("temperature", "top_p", "top_k", "min_p", "presence_penalty", "repetition_penalty")
GENERIC = "generic"
_INT_FIELDS = ("effective_context", "tool_output_cap_tokens", "shell_timeout_s", "max_tokens")
_FIELDS = frozenset({
    "id", "extends", "match", "tool_mode", "sampling", "preserve_thinking", "edit_format", "edit_fallback",
    *_INT_FIELDS, "parallel_tool_calls", "parsers", "notes", "source",
})


class ProfileError(ValueError):
    """A profile file that cannot be used: bad JSON, an unknown value, a wrong type."""


@dataclass(frozen=True)
class Profile:
    """One resolved profile. Fields left out of the JSON are None (crit's own default applies)."""

    id: str
    match: tuple[str, ...] = ()
    tool_mode: str = TOOL_AUTO
    #: (key, value) pairs from ``sampling``, in SAMPLING_KEYS order.
    sampling: tuple[tuple[str, float], ...] = ()
    #: Keep the model's reasoning on assistant messages in the history (``reasoning_content``).
    #: False never echoes it back (R1-style models must not see it again).
    preserve_thinking: bool = False
    edit_format: str | None = None
    edit_fallback: str | None = None
    #: Tokens the model works well within; crit sizes chats to the smaller of this and the server's window.
    effective_context: int | None = None
    tool_output_cap_tokens: int | None = None
    shell_timeout_s: int | None = None
    #: ``max_tokens`` when ``local.max_tokens`` is 0.
    max_tokens: int | None = None
    #: Sent as ``parallel_tool_calls`` with native tools when set; None sends nothing.
    parallel_tool_calls: bool | None = None
    #: Server name -> (option, value) pairs for the launch line, e.g. vllm -> tool_call_parser.
    parsers: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = ()
    notes: str = ""
    source: str = ""
    #: Where it came from: "builtin", "user", or "builtin+user" (a user override).
    origin: str = "builtin"

    def sampling_dict(self) -> dict[str, float]:
        return dict(self.sampling)

    def parsers_for(self, server: str) -> dict[str, str]:
        return dict(next((pairs for name, pairs in self.parsers if name == server), ()))

    def matches(self, model: str) -> int:
        """How well ``model`` fits this profile's globs: 0 for no match, else the longest
        matching glob's count of literal characters (more specific wins)."""
        best = 0
        for name in _candidates(model):
            for glob in self.match:
                if fnmatch.fnmatchcase(name, glob.lower()):
                    best = max(best, len(glob.replace("*", "").replace("?", "")) + 1)
        return best


@dataclass(frozen=True)
class ServerPreset:
    """How to run one model server for agent use (``profiles/servers/<id>.json``)."""

    id: str
    name: str
    data: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def launch(self) -> str:
        return str(self.data.get("launch") or "")

    def launch_line(self, model: str, profile: Profile | None = None, context: int | None = None) -> str:
        """The launch command with the model, context and the profile's parsers filled in."""
        line = self.launch.replace("{model}", model or "MODEL").replace("{context}", str(context or 32768))
        extra = []
        for option, value in (profile.parsers_for(self.id).items() if profile else ()):
            extra.append(f"--{option.replace('_', '-')} {value}")
        line = line.replace("{parsers}", " ".join(extra)).strip()
        return re.sub(r"  +", " ", line)


def builtin_dir() -> Path:
    return Path(__file__).resolve().parent / "profiles"


def user_dir() -> Path:
    from critique_bot.config import crit_home

    return crit_home() / "profiles"


def doctor_cache_path() -> Path:
    from critique_bot.config import crit_home

    return crit_home() / "cache" / "doctor.json"


# --------------------------------------------------------------------------- validation


def _number(where: str, value: Any, low: float | None = None, high: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProfileError(f"{where} must be a number, not {value!r}")
    if (low is not None and value < low) or (high is not None and value > high):
        raise ProfileError(f"{where} is {value}; it must be between {low} and {high}")
    return value


def _validate(raw: Any, where: str) -> dict[str, Any]:
    """The checked fields of one profile object (still raw values, before inheritance)."""
    if not isinstance(raw, dict):
        raise ProfileError(f"{where}: a profile must be a JSON object")
    pid = raw.get("id")
    if not isinstance(pid, str) or not pid.strip():
        raise ProfileError(f"{where}: a profile needs an \"id\"")
    where = f"{where} ({pid})"
    unknown = sorted(set(raw) - _FIELDS)
    if unknown:
        raise ProfileError(f"{where}: unknown field(s) {', '.join(unknown)}")
    out: dict[str, Any] = {"id": pid.strip()}
    if "extends" in raw:
        if not isinstance(raw["extends"], str):
            raise ProfileError(f"{where}: extends must be a profile id")
        out["extends"] = raw["extends"]
    if "match" in raw:
        match = raw["match"]
        if isinstance(match, str):
            match = [match]
        if not isinstance(match, list) or not all(isinstance(g, str) and g.strip() for g in match):
            raise ProfileError(f"{where}: match must be a list of name globs")
        out["match"] = tuple(g.strip() for g in match)
    if "tool_mode" in raw:
        if raw["tool_mode"] not in TOOL_MODES:
            raise ProfileError(f"{where}: tool_mode must be one of {', '.join(TOOL_MODES)}")
        out["tool_mode"] = raw["tool_mode"]
    if "sampling" in raw:
        sampling = raw["sampling"]
        if not isinstance(sampling, dict):
            raise ProfileError(f"{where}: sampling must be an object")
        bad = sorted(set(sampling) - set(SAMPLING_KEYS))
        if bad:
            raise ProfileError(f"{where}: unknown sampling key(s) {', '.join(bad)}")
        limits = {"temperature": (0, 2), "top_p": (0, 1), "top_k": (-1, 1000), "min_p": (0, 1),
                  "presence_penalty": (-2, 2), "repetition_penalty": (0, 2)}
        checked = {}
        for key, value in sampling.items():
            checked[key] = _number(f"{where}: sampling.{key}", value, *limits[key])
            if key == "top_k" and not float(value).is_integer():
                raise ProfileError(f"{where}: sampling.top_k must be a whole number")
            if key == "top_k":
                checked[key] = int(value)
        out["sampling"] = checked
    for key in ("preserve_thinking", "parallel_tool_calls"):
        if key in raw:
            if not isinstance(raw[key], bool):
                raise ProfileError(f"{where}: {key} must be true or false")
            out[key] = raw[key]
    for key in ("edit_format", "edit_fallback"):
        if key in raw:
            if raw[key] not in EDIT_FORMATS:
                raise ProfileError(f"{where}: {key} must be one of {', '.join(EDIT_FORMATS)}")
            out[key] = raw[key]
    for key in _INT_FIELDS:
        if key in raw:
            value = raw[key]
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ProfileError(f"{where}: {key} must be a positive whole number")
            if key == "effective_context" and value < 1024:
                raise ProfileError(f"{where}: effective_context must be at least 1024")
            out[key] = value
    if "parsers" in raw:
        parsers = raw["parsers"]
        if not isinstance(parsers, dict) or not all(
            isinstance(v, dict) and all(isinstance(o, str) and isinstance(x, str) for o, x in v.items())
            for v in parsers.values()
        ):
            raise ProfileError(f"{where}: parsers must map a server id to {{option: value}} strings")
        out["parsers"] = parsers
    for key in ("notes", "source"):
        if key in raw:
            if not isinstance(raw[key], str):
                raise ProfileError(f"{where}: {key} must be text")
            out[key] = raw[key]
    return out


def _read_file(path: Path) -> list[dict[str, Any]]:
    """The profile objects in one file: one object, a list, or ``{"profiles": [...]}``."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProfileError(f"{path}: {exc}") from exc
    if isinstance(data, dict) and isinstance(data.get("profiles"), list):
        data = data["profiles"]
    items = data if isinstance(data, list) else [data]
    return [_validate(item, path.name) for item in items]


def _freeze(fields: dict[str, Any], origin: str) -> Profile:
    sampling = fields.get("sampling") or {}
    parsers = fields.get("parsers") or {}
    return Profile(
        id=fields["id"],
        match=tuple(fields.get("match") or ()),
        tool_mode=fields.get("tool_mode", TOOL_AUTO),
        sampling=tuple((k, sampling[k]) for k in SAMPLING_KEYS if k in sampling),
        preserve_thinking=bool(fields.get("preserve_thinking", False)),
        edit_format=fields.get("edit_format"),
        edit_fallback=fields.get("edit_fallback"),
        effective_context=fields.get("effective_context"),
        tool_output_cap_tokens=fields.get("tool_output_cap_tokens"),
        shell_timeout_s=fields.get("shell_timeout_s"),
        max_tokens=fields.get("max_tokens"),
        parallel_tool_calls=fields.get("parallel_tool_calls"),
        parsers=tuple(sorted((name, tuple(sorted(opts.items()))) for name, opts in parsers.items())),
        notes=fields.get("notes", ""),
        source=fields.get("source", ""),
        origin=origin,
    )


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in over.items():
        if key in ("sampling", "parsers") and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


# --------------------------------------------------------------------------- loading


_cache: dict[str, Any] = {}


def _files(folder: Path) -> list[Path]:
    try:
        return sorted(p for p in folder.glob("*.json") if p.is_file())
    except OSError:
        return []


def load_profiles(*, user: Path | None = None, builtin: Path | None = None) -> list[Profile]:
    """Every profile: user ones first (in file order), then the built-ins.

    A broken user file is skipped with a warning; a broken built-in raises (the tests catch
    it before a release). ``extends`` copies the named profile's fields, then overrides them.
    """
    builtin = builtin or builtin_dir()
    user = user or user_dir()
    key = f"{builtin}|{user}"
    stamp = tuple((str(p), p.stat().st_mtime_ns) for p in _files(user) + _files(builtin))
    cached = _cache.get(key)
    if cached and cached[0] == stamp:
        return cached[1]
    raw_builtin: dict[str, dict[str, Any]] = {}
    for path in _files(builtin):
        for fields in _read_file(path):
            if fields["id"] in raw_builtin:
                raise ProfileError(f"{path.name}: profile id {fields['id']!r} is defined twice")
            raw_builtin[fields["id"]] = fields
    raw_user: dict[str, dict[str, Any]] = {}
    for path in _files(user):
        try:
            for fields in _read_file(path):
                raw_user[fields["id"]] = fields
        except ProfileError as exc:
            log.warn(f"profile {exc}; skipping that file")

    def expand(fields: dict[str, Any], table: dict[str, dict[str, Any]], seen: tuple[str, ...]) -> dict[str, Any]:
        parent = fields.get("extends")
        if not parent:
            return fields
        if parent in seen:
            raise ProfileError(f"profile {fields['id']!r}: extends loops through {parent!r}")
        base = table.get(parent) or raw_builtin.get(parent)
        if base is None:
            raise ProfileError(f"profile {fields['id']!r} extends unknown profile {parent!r}")
        base = expand(base, table, (*seen, fields["id"]))
        merged = _merge(base, fields)
        merged.pop("extends", None)
        merged["id"] = fields["id"]
        if "match" not in fields:
            merged["match"] = ()  # a size override matches by its own globs, not its parent's
        return merged

    builtins = {pid: expand(f, raw_builtin, ()) for pid, f in raw_builtin.items()}
    profiles: list[Profile] = []
    for pid, fields in raw_user.items():
        try:
            if pid in builtins:
                profiles.append(_freeze(_merge(builtins[pid], expand(fields, raw_user, ())), "builtin+user"))
            else:
                profiles.append(_freeze(expand(fields, raw_user, ()), "user"))
        except ProfileError as exc:
            log.warn(f"{exc}; skipping it")
    overridden = {p.id for p in profiles}
    profiles += [_freeze(f, "builtin") for pid, f in builtins.items() if pid not in overridden]
    _cache[key] = (stamp, profiles)
    return profiles


def _candidates(model: str) -> list[str]:
    """The model name as given and its last path part, lowercased, without a ``.gguf`` suffix."""
    name = model.strip().lower()
    if name.endswith(".gguf"):
        name = name[:-5]
    out = [name]
    tail = re.split(r"[/\\]", name)[-1]
    if tail and tail != name:
        out.append(tail)
    return out


def resolve(model: str, wanted: str = "", *, profiles: list[Profile] | None = None) -> Profile:
    """The profile for ``model``: ``wanted`` (``local.profile``) when known, else the best match
    (user profiles win a tie), else ``generic``. An unknown ``wanted`` warns and falls through."""
    profiles = profiles if profiles is not None else load_profiles()
    by_id = {p.id: p for p in profiles}
    if wanted:
        if wanted in by_id:
            return by_id[wanted]
        log.warn(f"local.profile {wanted!r} is not a known profile; choosing one by the model name")
    best: tuple[tuple[int, int, int], Profile] | None = None
    for index, profile in enumerate(profiles):
        if profile.id == GENERIC or not model:
            continue
        score = profile.matches(model)
        if score:
            # most specific glob first; on a tie a user profile, then the earlier one
            rank = (score, 1 if profile.origin != "builtin" else 0, -index)
            if best is None or rank > best[0]:
                best = (rank, profile)
    if best is not None:
        return best[1]
    return by_id.get(GENERIC) or Profile(id=GENERIC)


# --------------------------------------------------------------------------- server presets


def server_presets(folder: Path | None = None) -> list[ServerPreset]:
    folder = folder or builtin_dir() / "servers"
    presets = []
    for path in _files(folder):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProfileError(f"{path}: {exc}") from exc
        if not isinstance(data, dict) or not data.get("id"):
            raise ProfileError(f"{path.name}: a server preset needs an \"id\"")
        presets.append(ServerPreset(id=str(data["id"]), name=str(data.get("name") or data["id"]), data=data))
    return presets


# --------------------------------------------------------------------------- doctor cache


def _cache_key(url: str, model: str) -> str:
    return f"{url.rstrip('/')}|{model}"


def _read_cache(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def cached_probe(url: str, model: str, *, path: Path | None = None) -> dict[str, Any] | None:
    """The last ``crit doctor model`` result for this server and model, or None."""
    entry = _read_cache(path or doctor_cache_path()).get(_cache_key(url, model))
    return entry if isinstance(entry, dict) else None


def save_probe(url: str, model: str, result: dict[str, Any], *, path: Path | None = None) -> Path:
    """Store a doctor result (owner-only file), keyed by server and model."""
    from critique_bot.output import private_dir

    path = path or doctor_cache_path()
    private_dir(path.parent)
    data = _read_cache(path)
    data[_cache_key(url, model)] = {**result, "when": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
    return path


def effective_tool_mode(profile: Profile, url: str, model: str, *, cache: Path | None = None) -> str:
    """``native`` or ``prompted``: the profile's mode, and for ``auto`` the doctor's cached
    recommendation for this server and model (native when there is none)."""
    if profile.tool_mode != TOOL_AUTO:
        return profile.tool_mode
    found = cached_probe(url, model, path=cache)
    if found and found.get("recommend") in (TOOL_NATIVE, TOOL_PROMPTED):
        return str(found["recommend"])
    return TOOL_NATIVE
