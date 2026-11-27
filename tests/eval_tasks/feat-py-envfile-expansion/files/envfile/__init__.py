"""Parser for .env files."""

from .parser import EnvFileError, load, parse

__all__ = ["EnvFileError", "load", "parse"]
