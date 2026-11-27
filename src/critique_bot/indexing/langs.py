# SPDX-License-Identifier: Apache-2.0
"""Which language (or build/config format) a file is, from its name."""

from __future__ import annotations

from pathlib import PurePosixPath

BY_SUFFIX = {
    ".py": "python",
    ".pyi": "python",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".hxx": "cpp",
    ".cs": "csharp",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".aidl": "aidl",
    ".hal": "hidl",
    ".xml": "xml",
    ".rc": "rc",
    ".prop": "prop",
    ".sysprop": "sysprop",
    ".te": "sepolicy",
    ".mk": "make",
    ".cmake": "cmake",
    ".gradle": "gradle",
    ".bzl": "bazel",
}

BY_NAME = {
    "android.bp": "blueprint",
    "cmakelists.txt": "cmake",
    "build": "bazel",
    "build.bazel": "bazel",
    "workspace": "bazel",
    "build.gradle.kts": "gradle",
    "settings.gradle.kts": "gradle",
    "property_contexts": "sepolicy",
    "file_contexts": "sepolicy",
    "service_contexts": "sepolicy",
    "hwservice_contexts": "sepolicy",
    "seapp_contexts": "sepolicy",
}

#: Languages whose definitions, calls and imports the index extracts.
CODE = frozenset({"python", "java", "kotlin", "go", "rust", "c", "cpp", "csharp", "javascript", "typescript", "aidl", "hidl"})
#: Formats only scanned for strings, keys and module definitions.
CONFIG = frozenset({"xml", "rc", "prop", "sysprop", "sepolicy", "blueprint", "gradle", "cmake", "bazel", "make"})


def language_of(path: str) -> str:
    """``python``, ``java``, ..., a config format, or ``text``."""
    name = PurePosixPath(path.replace("\\", "/")).name.lower()
    if name in BY_NAME:
        return BY_NAME[name]
    if name.endswith("_contexts"):
        return "sepolicy"
    if name.endswith(".gradle.kts"):
        return "gradle"
    suffix = PurePosixPath(name).suffix
    return BY_SUFFIX.get(suffix, "text")
