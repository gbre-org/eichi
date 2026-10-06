"""Optional user configuration for eichi.

The config file is OPTIONAL — eichi works with zero configuration. The
config file is useful if you want to declare a set of corpora that you
re-index on a schedule, and have a single `eichi index` invocation walk
all of them.

File location (first match wins):
  1. ``$EICHI_CONFIG`` env var (absolute path to TOML file).
  2. ``$XDG_CONFIG_HOME/eichi/eichi.toml`` if XDG_CONFIG_HOME is set.
  3. ``~/.config/eichi/eichi.toml``.

Format (TOML):

    # eichi.toml — declare named corpora to index.
    [[corpus]]
    name = "notes"
    path = "~/Documents/notes"
    extensions = ["md", "txt"]

    [[corpus]]
    name = "code-readmes"
    path = "~/repos"
    extensions = ["md"]

See ``eichi.toml.example`` in the repo root.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

if sys.version_info >= (3, 11):
    import tomllib as _toml
else:  # pragma: no cover
    import tomli as _toml  # type: ignore[no-redef]


@dataclass
class Corpus:
    name: str
    # ``path`` is required for filesystem corpora (walked by
    # ``eichi index <path>``) but OPTIONAL for connector corpora, which
    # resolve their source via the connector's own config (``options``).
    path: Optional[Path] = None
    extensions: List[str] = field(default_factory=list)
    # Connector module to use. ``None`` means ``name`` itself is a
    # built-in connector name (e.g. ``claude-jsonl``). Setting it lets
    # several differently-named corpora share one connector (e.g. two
    # ``http-conversation`` sources), each indexed under its own name.
    connector: Optional[str] = None
    # Extra per-corpus keys (anything besides name/path/extensions/
    # connector). Passed straight through to a connector's
    # ``iter_documents(config=...)``. Empty for plain filesystem corpora.
    options: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Config:
    corpora: List[Corpus] = field(default_factory=list)


def config_path() -> Path:
    """Resolve the config file path (may not exist on disk)."""
    override = os.environ.get("EICHI_CONFIG")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "eichi" / "eichi.toml"


def load(path: Optional[Path] = None) -> Config:
    """Load the config file. Returns an empty :class:`Config` if absent."""
    p = path or config_path()
    if not p.exists():
        return Config()
    with open(p, "rb") as fh:
        data = _toml.load(fh)
    # Known connector names — a path-less corpus block is valid iff its
    # ``name`` matches a built-in connector (those resolve their source
    # via connector config, not a filesystem path). Imported lazily so a
    # connector import error never breaks plain config loading.
    try:
        from .connectors import REGISTRY as _CONNECTORS

        connector_names = set(_CONNECTORS)
    except Exception:  # pragma: no cover — defensive
        connector_names = set()

    _RESERVED = {"name", "path", "extensions", "connector"}
    corpora: List[Corpus] = []
    for raw in data.get("corpus", []) or []:
        name = raw.get("name")
        cpath = raw.get("path")
        if not name:
            continue
        # A filesystem corpus needs a path; a connector corpus does not.
        connector = raw.get("connector")
        if connector is not None and str(connector) not in connector_names:
            continue  # unknown connector: skip rather than index nothing
        if not cpath and connector is None and str(name) not in connector_names:
            continue
        options = {k: v for k, v in raw.items() if k not in _RESERVED}
        corpora.append(
            Corpus(
                name=str(name),
                path=Path(os.path.expanduser(str(cpath))) if cpath else None,
                extensions=[str(x).lstrip(".") for x in raw.get("extensions", [])],
                connector=str(connector) if connector is not None else None,
                options=options,
            )
        )
    return Config(corpora=corpora)
