"""Self-improvement phases P4 (approved prompt rules) + P6 (golden corpus).

DB is stubbed so no Postgres is needed — the store logic, rendering, gating, and
fail-safe behaviour are what these pin.
"""
from v11.learn import rules, golden, fewshot


# ---- P4: approved prompt rules -----------------------------------------------

class _FakeSettings:
    """In-memory stand-in for the database settings kv + _connect (for _now)."""
    def __init__(self):
        self.kv = {}

    def get_setting(self, key):
        return self.kv.get(key)

    def set_setting(self, key, value, updated_by="system"):
        self.kv[key] = value

    def _connect(self):
        raise RuntimeError("no db")  # forces _now() -> "" (fine)


def test_rules_approve_render_dedup_remove(monkeypatch):
    fake = _FakeSettings()
    monkeypatch.setattr(rules, "database", fake)

    assert rules.list_approved() == []
    assert rules.approved_rules_block() == ""

    rules.approve_rule("Always emit origin as ISO-2 country code.", field="origin")
    rules.approve_rule("Exchange rate must satisfy CIF build-up.")
    rules.approve_rule("Always emit origin as ISO-2 country code.")  # dup → no-op

    approved = rules.list_approved()
    assert len(approved) == 2, "dup must not create a second rule"

    block = rules.approved_rules_block()
    assert "ISO-2" in block and "CIF build-up" in block
    assert "admin-approved" in block.lower()

    assert rules.remove_rule("Exchange rate must satisfy CIF build-up.") is True
    assert len(rules.list_approved()) == 1
    assert rules.remove_rule("nonexistent") is False


def test_rules_empty_text_rejected(monkeypatch):
    monkeypatch.setattr(rules, "database", _FakeSettings())
    import pytest
    with pytest.raises(ValueError):
        rules.approve_rule("   ")


def test_prompt_rules_flag_injects_into_primary(monkeypatch):
    fake = _FakeSettings()
    monkeypatch.setattr(rules, "database", fake)
    rules.approve_rule("Double-check the declaration date is the actual one.")
    # no few-shot fields — isolate the rules path
    monkeypatch.setattr(fewshot, "frequently_corrected_fields", lambda *a, **k: [])
    monkeypatch.setenv("LEARN_PROMPT_RULES", "1")
    monkeypatch.delenv("LEARN_FEWSHOT_PRIMARY", raising=False)
    monkeypatch.delenv("LEARN_FEWSHOT_SHADOW", raising=False)
    block = fewshot.primary_hint_block()
    assert "actual one" in block, "approved rule not injected into primary hint"


# ---- P6: golden corpus from approved jobs ------------------------------------

def test_golden_build_shapes_approved_jobs(monkeypatch):
    fake_jobs = [
        {"job_id": "j1", "pdf_name": "a.pdf", "pdf_hash": "h1",
         "declaration": {"importer_name": "ACME", "exchange_rate": 57.4,
                         "total_customs_value": 1000, "id": 9, "job_id": "j1",
                         "junk_field": None, "empty": ""},
         "items": [{"item_name": "WIDGET", "customs_value_mmk": 1000}]},
    ]
    monkeypatch.setattr(golden.database, "get_approved_jobs_full",
                        lambda limit=None: fake_jobs)
    corpus = golden.build_golden()
    assert corpus["count"] == 1
    rec = corpus["records"][0]
    assert rec["pdf_hash"] == "h1"
    d = rec["declaration"]
    assert d["importer_name"] == "ACME" and d["exchange_rate"] == 57.4
    assert "empty" not in d and "junk_field" not in d  # empty/None dropped
    assert rec["items"][0]["item_name"] == "WIDGET"


def test_golden_empty_on_no_approved(monkeypatch):
    monkeypatch.setattr(golden.database, "get_approved_jobs_full",
                        lambda limit=None: [])
    assert golden.build_golden() == {"count": 0, "records": []}
