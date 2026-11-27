# SPDX-License-Identifier: Apache-2.0
"""The indexing engine (plan Stage 8): one SQLite database per project, layered.

====  ==========================================  ==============================
L0    catalog: path, size, mtime, hash, language,  ``files``, ``modules``
      the build file and module that own a file   (:mod:`.modules`)
L2    definitions (kind, signature, range,         ``symbols``, ``refs``
      container) and references by name            (:mod:`.symbols`)
L2+   string literals, log tags, resources,        ``strings`` (:mod:`.facts`)
      property keys, SELinux types
L3    imports (resolved per language), likely      ``edges`` (:mod:`.graph`)
      calls by name, inherits/implements,
      module deps
L4    commits, files per commit, co-change         ``commits``, ``commit_files``,
      pairs, blame cache                           ``cochange``, ``blame_cache``
                                                   (:mod:`.history`)
====  ==========================================  ==============================

The database lives in ``.crit/cache/index.db`` (WAL). On a ``repo``-tool tree (AOSP) a
module group (one git project such as ``packages/services/Car``) gets its own database
under ``.crit/cache/index/`` the first time a tool asks about it (:mod:`.store`).

:mod:`critique_bot.code_index` keeps its public API and writes L0-L3 during its refresh;
the tools (:mod:`.tools`) read every layer. Everything works with the standard library;
tree-sitter (the ``index`` extra) only makes definitions and calls more exact.

Later stages extend it: Stage 8b fills ``glossary`` and adds its own retrievers to
:data:`critique_bot.indexing.locate.RETRIEVERS` and its own migrations through
:func:`critique_bot.indexing.store.register_migration`.
"""
