"""Unit tests for v11.learn.evaluate — PURE functions only.

No live API, no real DB. Covers field_match, compare_record, aggregate, and the
promote decision (with save_score / best_score monkeypatched so nothing touches
the archive). These only run once the evaluate module exists — collection will
error until then, which is expected. Import path is provided by conftest.py.
"""
from v11.learn import evaluate as ev


# --------------------------------------------------------------------------- #
# field_match
# --------------------------------------------------------------------------- #
def test_field_match_numeric_within_tolerance():
    # 1000 vs 1005 = 0.5% < 1% default tolerance -> match
    assert ev.field_match(1000, 1005) is True


def test_field_match_numeric_outside_tolerance():
    # 1000 vs 1050 = 5% > 1% -> no match
    assert ev.field_match(1000, 1050) is False


def test_field_match_date_iso_prefix():
    # date compared on ISO prefix (ignores time component) — date logic is gated
    # on the field NAME, mirroring how compare_record calls it (field=f).
    assert ev.field_match("2026-07-16", "2026-07-16T00:00:00",
                          field="declaration_date") is True


def test_field_match_string_case_insensitive():
    assert ev.field_match("Acme Imports", "acme imports") is True


def test_field_match_both_empty():
    assert ev.field_match("", "") is True
    assert ev.field_match(None, None) is True


# --------------------------------------------------------------------------- #
# compare_record
# --------------------------------------------------------------------------- #
def test_compare_record_two_right_one_wrong():
    truth = {
        "declaration_no": "123456789012",
        "importer_name": "Acme Imports",
        "total_customs_value": 1000,
    }
    extracted = {
        "declaration_no": "123456789012",   # right
        "importer_name": "acme imports",    # right (case-insensitive)
        "total_customs_value": 9999,        # wrong
    }
    res = ev.compare_record(extracted, truth)
    assert res["scored"] == 3
    assert res["matched"] == 2
    assert abs(res["accuracy"] - (2 / 3)) < 1e-6


def test_compare_record_only_scores_fields_in_truth():
    # extracted has an extra field not present in truth -> not scored
    truth = {"declaration_no": "111111111111"}
    extracted = {"declaration_no": "111111111111", "importer_name": "Extra Co"}
    res = ev.compare_record(extracted, truth)
    assert res["scored"] == 1
    assert res["matched"] == 1
    assert abs(res["accuracy"] - 1.0) < 1e-6


def test_compare_record_item_recall_by_customs_value():
    truth = {
        "items": [
            {"customs_value_mmk": 1000},
            {"customs_value_mmk": 2000},
        ]
    }
    extracted = {
        "items": [
            {"customs_value_mmk": 1000},   # matches truth[0]
        ]
    }
    res = ev.compare_record(extracted, truth)
    assert res["n_items_truth"] == 2
    # 1 of 2 truth items recovered
    assert abs(res["item_recall"] - 0.5) < 1e-6


# --------------------------------------------------------------------------- #
# aggregate
# --------------------------------------------------------------------------- #
def test_aggregate_two_records_mean_and_per_field():
    # per_field values are plain bools — the shape compare_record emits.
    records = [
        {
            "accuracy": 1.0, "scored": 2, "matched": 2,
            "per_field": {"declaration_no": True, "importer_name": True},
        },
        {
            "accuracy": 0.5, "scored": 2, "matched": 1,
            "per_field": {"declaration_no": True, "importer_name": False},
        },
    ]
    agg = ev.aggregate(records)
    # mean field accuracy = (1.0 + 0.5) / 2
    assert abs(agg["field_accuracy"] - 0.75) < 1e-6
    pf = agg["per_field"]
    # declaration_no matched in both records
    assert pf["declaration_no"]["n"] == 2
    assert abs(pf["declaration_no"]["acc"] - 1.0) < 1e-6
    # importer_name matched in 1 of 2
    assert pf["importer_name"]["n"] == 2
    assert abs(pf["importer_name"]["acc"] - 0.5) < 1e-6


# --------------------------------------------------------------------------- #
# promote_if_better  (monkeypatch archive so no DB is touched)
# --------------------------------------------------------------------------- #
def test_promote_if_better_candidate_above_baseline(monkeypatch):
    saved = {}
    monkeypatch.setattr(ev, "save_score",
                        lambda label, metrics, by="system": saved.update(
                            label=label, metrics=metrics) or {"label": label})
    monkeypatch.setattr(ev, "best_score", lambda metric="field_accuracy": None)

    candidate = {"field_accuracy": 0.90}
    baseline = {"field_accuracy": 0.80}
    res = ev.promote_if_better("cand-1", candidate, baseline)
    assert res["promoted"] is True
    assert res["delta"] > 0
    assert abs(res["delta"] - 0.10) < 1e-6


def test_promote_if_better_candidate_below_baseline(monkeypatch):
    monkeypatch.setattr(ev, "save_score",
                        lambda label, metrics, by="system": {"label": label})
    monkeypatch.setattr(ev, "best_score", lambda metric="field_accuracy": None)

    candidate = {"field_accuracy": 0.70}
    baseline = {"field_accuracy": 0.80}
    res = ev.promote_if_better("cand-2", candidate, baseline)
    assert res["promoted"] is False
    assert res["delta"] <= 0
