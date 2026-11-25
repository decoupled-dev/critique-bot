"""The role crit plays in a repository: a persona the system prompt is shaped around.

The first ``crit`` in a folder picks one from the project's files (:func:`detect_role`)
and stores it as ``"role"`` in ``.crit/settings.json``; ``/role`` and ``--role`` change it. Each role's
model-facing text is a ``<<<ROLE_...>>>`` section of ``prompts/agent.txt``
(see :func:`section_key`); this module only knows the names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Role:
    id: str
    label: str
    summary: str
    #: Built-in skills sent with every task in this role (on top of the "skills" setting).
    skills: tuple[str, ...] = ()


GENERIC = "generic"
CODE_REVIEWER = "code-reviewer"

ROLES: tuple[Role, ...] = (
    Role(
        "code-reviewer",
        "Code Reviewer",
        "Reads diffs and files for defects, security, missing tests, and conventions; reports findings with the smallest fix.",
    ),
    Role(
        "aosp-expert",
        "AOSP Expert",
        "Android platform work: frameworks, system services, HALs, SELinux, Soong builds, vendor partitions, Automotive.",
        skills=("aosp",),
    ),
    Role(
        "expert-developer",
        "Expert Developer",
        "Complete, production-grade changes that follow the codebase's own patterns, with tests and a passing build.",
    ),
    Role(
        "ai-expert",
        "AI Expert",
        "Software that uses ML models and LLM APIs: prompts, tool calling, agents, retrieval, evaluation, on-device inference.",
    ),
    Role(
        "test-engineer",
        "Test Engineer",
        "Tests that prove behavior: the project's framework, boundaries and error paths, a failing test first for a bug.",
    ),
    Role(
        "android-app-engineer",
        "Android App Engineer",
        "Android apps with Kotlin, Jetpack, Compose, Hilt, coroutines, and Gradle; lifecycle, permissions, and process death.",
        skills=("android",),
    ),
    Role(
        GENERIC,
        "Generic",
        "No extra persona: the standard crit instructions only.",
    ),
)

ROLE_IDS: tuple[str, ...] = tuple(role.id for role in ROLES)
_BY_KEY = {re.sub(r"[^a-z0-9]", "", role.id): role for role in ROLES}
_ALIASES = {
    "reviewer": "code-reviewer",
    "review": "code-reviewer",
    "codereview": "code-reviewer",
    "aosp": "aosp-expert",
    "platform": "aosp-expert",
    "developer": "expert-developer",
    "dev": "expert-developer",
    "expert": "expert-developer",
    "ai": "ai-expert",
    "ml": "ai-expert",
    "llm": "ai-expert",
    "test": "test-engineer",
    "tester": "test-engineer",
    "testing": "test-engineer",
    "qa": "test-engineer",
    "android": "android-app-engineer",
    "androidapp": "android-app-engineer",
    "androidengineer": "android-app-engineer",
    "app": "android-app-engineer",
    "default": GENERIC,
    "none": GENERIC,
    "general": GENERIC,
}


def normalize_role(raw: object) -> str | None:
    """The role id for a setting, flag, or command word, or ``None`` when it names no role.

    ``AOSPExpert``, ``aosp-expert``, ``aosp_expert``, ``AOSP Expert``, and ``aosp``
    all mean ``aosp-expert``.
    """
    key = re.sub(r"[^a-z0-9]", "", str(raw or "").lower())
    if not key:
        return None
    role = _BY_KEY.get(key)
    if role is not None:
        return role.id
    alias = _ALIASES.get(key)
    return alias


def role_by_id(role_id: object) -> Role:
    """The role for an id (any spelling :func:`normalize_role` accepts); Generic when unknown."""
    found = normalize_role(role_id)
    for role in ROLES:
        if role.id == found:
            return role
    return ROLES[-1]


def role_label(role_id: object) -> str:
    return role_by_id(role_id).label


def role_skills(role_id: object) -> list[str]:
    """Skills a role pins for every task. Generic and unknown roles pin none."""
    return list(role_by_id(role_id).skills)


def section_key(role_id: object) -> str:
    """The ``prompts/agent.txt`` section that holds this role's instructions, e.g. ``ROLE_AOSP_EXPERT``."""
    return "ROLE_" + role_by_id(role_id).id.upper().replace("-", "_")


# --------------------------------------------------------------------------- picking a role from the files

#: Files or folders whose presence says what a project is (checked at the root and one level down).
_ANDROID_APP_MARKERS = ("AndroidManifest.xml", "app/src/main/AndroidManifest.xml", "src/main/AndroidManifest.xml")
_AI_PACKAGES = ("openai", "anthropic", "torch", "transformers", "langchain", "llama_index", "llama-index", "tensorflow", "onnxruntime", "vllm")
_SKIP_DIRS = frozenset({".git", ".crit", ".bot", "node_modules", "build", "out", ".gradle", ".venv", "venv", "__pycache__", "dist", "target"})


def detect_role(root: object) -> str:
    """A role for the project at ``root`` from its files; Generic when nothing clearly fits.

    AOSP (a platform tree: ``.repo``, ``build/soong``, ``Android.bp`` at the top) > an Android
    app (an ``AndroidManifest.xml`` or the Android Gradle plugin) > AI (an ML or LLM package in
    the Python requirements) > Generic. Only names and small build files are read.
    """
    from pathlib import Path

    base = Path(str(root))
    if not base.is_dir():
        return GENERIC
    exists = lambda rel: (base / rel).exists()  # noqa: E731
    if exists(".repo") or exists("build/soong") or (exists("Android.bp") and exists("frameworks/base")):
        return "aosp-expert"
    if any(exists(rel) for rel in _ANDROID_APP_MARKERS):
        return "android-app-engineer"
    for name in ("build.gradle", "build.gradle.kts", "app/build.gradle", "app/build.gradle.kts"):
        text = _small_text(base / name)
        if "com.android.application" in text or "com.android.library" in text or "android {" in text:
            return "android-app-engineer"
    try:
        children = [child for child in base.iterdir() if child.is_dir() and child.name not in _SKIP_DIRS]
    except OSError:
        children = []
    for child in children[:200]:
        if (child / "src" / "main" / "AndroidManifest.xml").exists() or (child / "AndroidManifest.xml").exists():
            return "android-app-engineer"
    deps = " ".join(_small_text(base / name) for name in ("requirements.txt", "pyproject.toml", "setup.cfg", "environment.yml")).lower()
    if any(re.search(rf"(^|[\s\"'\[,]){re.escape(pkg)}([\s\"'<>=~!\],;\[]|$)", deps, re.M) for pkg in _AI_PACKAGES):
        return "ai-expert"
    return GENERIC


def _small_text(path) -> str:
    try:
        if path.is_file() and path.stat().st_size <= 512_000:
            return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    return ""
