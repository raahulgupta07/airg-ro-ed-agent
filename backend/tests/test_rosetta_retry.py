"""ROSETTA's determinism guard.

Measured on 100306922661 — 28 pages, 8 scanned, two declarations inside. Three
runs of the SAME file through the SAME model:

    run 1   $0.3303   declaration no WRONG, no money, 0 items
    run 2   $0.1731   everything correct,             7 items
    run 3   $0.2050   header correct,                 0 items

Header 2/3, items 1/3. Nothing in the pipeline noticed, because a missing item
list is not an arithmetic error — there is no sum to fail when there are no
rows. The guard turns "0 items under a declared total" into a re-read.

ROSETTA the ENGINE has since been withdrawn — ATLAS V14 is the only selectable
one. `_looks_incomplete` / `_run_with_retry` remain in `v11/workflow.py` and are
still covered below, because the property they encode (a retry must never make
the outcome worse than not retrying) outlives the engine that first used them.
`TestTheEngineIsRetired` at the bottom now asserts the withdrawal instead of the
registration.
"""

import pytest

from v11.workflow import _looks_incomplete, _run_with_retry


class _Cell:
    def __init__(self, value):
        self.value = value


def _result(total=None, items=None):
    return {"values": {"total_customs_value": _Cell(total)}, "items": items or []}


class TestWhatCountsAsIncomplete:
    def test_a_total_with_no_products_is_incomplete(self):
        # The exact run-3 outcome: header read fine, item list empty.
        assert _looks_incomplete(_result(total=45791072.89, items=[]))

    def test_products_with_no_total_is_incomplete(self):
        assert _looks_incomplete(_result(total=None, items=[{"value": 1}]))

    def test_a_complete_read_is_not_flagged(self):
        assert not _looks_incomplete(_result(total=45791072.89, items=[{"value": 1}]))

    def test_a_document_with_neither_is_left_alone(self):
        # A doc with no items also has no total. Retrying would spend money to
        # get the same empty answer twice.
        assert not _looks_incomplete(_result(total=None, items=[]))

    def test_a_zero_total_is_not_treated_as_present(self):
        # 0 is what the old code wrote when it read nothing; it is not a
        # declared value, so an empty item list alongside it is not a new fact.
        assert not _looks_incomplete(_result(total=0, items=[]))


class _FakePipeline:
    """Returns each queued result in turn, counting calls."""

    def __init__(self, *results):
        self._results = list(results)
        self.calls = 0

    def run(self, pdf_path, on_log=None):
        self.calls += 1
        return self._results[min(self.calls - 1, len(self._results) - 1)]


def _noop(*a, **k):
    pass


class TestTheRetry:
    def test_a_complete_first_read_is_not_repeated(self):
        p = _FakePipeline(_result(45791072.89, [{"v": 1}]))
        _run_with_retry(p, "x.pdf", _noop, retry=True)
        assert p.calls == 1

    def test_an_incomplete_read_is_retried_once(self):
        good = _result(45791072.89, [{"v": 1}])
        p = _FakePipeline(_result(45791072.89, []), good)
        out = _run_with_retry(p, "x.pdf", _noop, retry=True)
        assert p.calls == 2
        assert out["items"] == good["items"]
        assert any("re-read" in n for n in out.get("notes", []))

    def test_it_retries_only_once_never_loops(self):
        p = _FakePipeline(_result(45791072.89, []))     # always incomplete
        out = _run_with_retry(p, "x.pdf", _noop, retry=True)
        assert p.calls == 2
        assert out["needs_review"] is True
        assert any("still" in n for n in out.get("notes", []))

    def test_a_crashing_retry_keeps_the_first_read(self):
        class _Boom(_FakePipeline):
            def run(self, pdf_path, on_log=None):
                self.calls += 1
                if self.calls == 1:
                    return _result(45791072.89, [])
                raise RuntimeError("upstream 502")

        p = _Boom()
        out = _run_with_retry(p, "x.pdf", _noop, retry=True)
        # A retry must never be able to make the outcome worse than no retry.
        assert p.calls == 2
        assert out["values"]["total_customs_value"].value == 45791072.89

    def test_rover_is_unaffected_when_the_guard_is_off(self):
        # ROVER PRO keeps its existing behaviour exactly; only ROSETTA opts in.
        p = _FakePipeline(_result(45791072.89, []))
        _run_with_retry(p, "x.pdf", _noop, retry=False)
        assert p.calls == 1


class TestTheEngineIsRetired:
    """ROSETTA was withdrawn — ATLAS V14 is the only selectable engine now.

    This class used to assert the opposite. It is kept, inverted, rather than
    deleted: the failure it guards against is someone reinstating a second
    engine, which is not a code-only change. `settings.engines_enabled` in the
    DB overrides `_DEFAULT_ENABLED`, so a stale row can put a retired engine
    back in the UI no matter what the source says — `_RETIRED_ENGINES` is the
    filter that stops it, and this pins that filter in place.
    """

    def _settings_src(self):
        # Read the source rather than import it: routes/settings pulls in auth,
        # which needs a reachable database, so importing here would fail on a
        # dev box for a reason unrelated to the engine list.
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "routes/settings.py")) as fh:
            return fh.read()

    def test_atlas_is_the_only_engine_on_by_default(self):
        assert '_DEFAULT_ENABLED = ["atlas"]' in self._settings_src()

    def test_every_withdrawn_engine_is_filtered_out(self):
        src = self._settings_src()
        for engine in ("rosetta", "rover", "presto", "classic", "auto"):
            assert f'"{engine}"' in src.split("_RETIRED_ENGINES")[1].split("\n")[0], \
                f"{engine} must stay in _RETIRED_ENGINES or a stored row can revive it"

    def test_a_stored_row_naming_a_retired_engine_is_not_honoured(self):
        # The DB row is the real risk, not the constant: `engines_enabled` held
        # ["rover","atlas"] for weeks and that is what the UI rendered.
        src = self._settings_src()
        assert "e for e in enabled if e not in _RETIRED_ENGINES" in src
        assert "if default in _RETIRED_ENGINES" in src
