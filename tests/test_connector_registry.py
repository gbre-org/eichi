"""Open connector registry (entry points) and declarative connector kind."""

from __future__ import annotations

import types
from unittest.mock import patch

from eichi import connectors
from eichi import cli


class _EP:
    def __init__(self, name, obj, value="pkg.mod"):
        self.name, self._obj, self.value = name, obj, value

    def load(self):
        if isinstance(self._obj, Exception):
            raise self._obj
        return self._obj


def _build(eps):
    with patch.object(connectors._metadata, "entry_points", return_value=eps):
        return connectors.build_registry()


def _mod(kind=None):
    m = types.ModuleType("ext")
    m.iter_documents = lambda state=None, config=None: iter(())
    if kind:
        m.KIND = kind
    return m


def test_third_party_registers_with_kind():
    reg, kinds = _build([_EP("ext-chat", _mod("conversation")),
                         _EP("ext-docs", _mod())])
    assert "ext-chat" in reg and kinds["ext-chat"] == "conversation"
    assert kinds["ext-docs"] == "document"
    assert kinds["claude-jsonl"] == "conversation"


def test_bare_function_with_kind_attribute():
    def fn(state=None, config=None):
        return iter(())
    fn.KIND = "conversation"
    reg, kinds = _build([_EP("fn-chat", fn)])
    assert reg["fn-chat"] is fn and kinds["fn-chat"] == "conversation"


def test_builtin_wins_collision_and_warns(capsys):
    reg, kinds = _build([_EP("claude-jsonl", _mod("document"))])
    assert reg["claude-jsonl"] is connectors.claude_jsonl.iter_documents
    assert kinds["claude-jsonl"] == "conversation"
    assert "claude-jsonl" in capsys.readouterr().err


def test_broken_entry_points_are_skipped(capsys):
    reg, _ = _build([_EP("boom", ImportError("nope")),
                     _EP("empty", types.ModuleType("empty"))])
    err = capsys.readouterr().err
    assert "boom" not in reg and "empty" not in reg
    assert "boom" in err and "empty" in err


def test_conversation_sources_asks_connectors():
    kinds = {**connectors.KINDS, "ext-chat": "conversation", "ext-docs": "document"}
    configured = {"mine": "ext-chat", "notes": "ext-docs", "web": "http-conversation"}
    with patch.object(connectors, "KINDS", kinds), \
         patch.object(cli, "_configured_connectors", return_value=configured):
        out = cli._conversation_sources()
    assert out == ["claude-jsonl", "claude-watch-queue", "mine", "web"]
