"""Phase-1 self-improvement: learned-correction hints injected into the PRIMARY
extraction pass (Presto/Scribe), flag-gated by LEARN_FEWSHOT_PRIMARY.

Pure-logic tests: the DB aggregate (frequently_corrected_fields) is monkeypatched
so no Postgres is needed — the gating, rendering, shadow-mode, and fail-safe
behaviour are what matters here.
"""
from v11.learn import fewshot as fs


_FIELDS = [("exchange_rate", 6), ("origin", 4), ("declaration_date", 3)]


def _fake_fields(monkeypatch, fields=_FIELDS):
    monkeypatch.setattr(fs, "frequently_corrected_fields",
                        lambda *a, **k: list(fields))


def test_flag_off_returns_empty(monkeypatch):
    _fake_fields(monkeypatch)
    monkeypatch.delenv("LEARN_FEWSHOT_PRIMARY", raising=False)
    monkeypatch.delenv("LEARN_FEWSHOT_SHADOW", raising=False)
    assert fs.primary_hint_block() == ""


def test_flag_on_injects_attention_list(monkeypatch):
    _fake_fields(monkeypatch)
    monkeypatch.setenv("LEARN_FEWSHOT_PRIMARY", "1")
    monkeypatch.delenv("LEARN_FEWSHOT_SHADOW", raising=False)
    block = fs.primary_hint_block()
    assert "exchange_rate" in block and "origin" in block and "declaration_date" in block
    assert "correct" in block.lower()          # it's framed as guidance


def test_attention_block_is_values_free(monkeypatch):
    # Global attention list must NOT carry any specific corrected VALUE (would
    # mislead a different document). Only field names.
    _fake_fields(monkeypatch)
    block = fs.attention_block()
    assert "exchange_rate" in block
    assert "6" not in block and "4" not in block   # counts/values not leaked


def test_shadow_mode_observes_but_does_not_inject(monkeypatch):
    _fake_fields(monkeypatch)
    monkeypatch.delenv("LEARN_FEWSHOT_PRIMARY", raising=False)
    monkeypatch.setenv("LEARN_FEWSHOT_SHADOW", "1")
    # shadow computes/logs the block but must return "" (no injection)
    assert fs.primary_hint_block() == ""


def test_no_fields_returns_empty(monkeypatch):
    _fake_fields(monkeypatch, fields=[])
    monkeypatch.setenv("LEARN_FEWSHOT_PRIMARY", "1")
    assert fs.primary_hint_block() == ""


def test_never_raises_on_db_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(fs, "frequently_corrected_fields", boom)
    monkeypatch.setenv("LEARN_FEWSHOT_PRIMARY", "1")
    assert fs.primary_hint_block() == ""       # fail-safe, extraction never breaks
