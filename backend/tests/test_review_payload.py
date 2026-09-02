"""The review payload is a hand-written dict, and that makes it a schema.

`GET /api/review/{job_id}` builds its `job` object field by field. Anything the
UI reads that is not listed there arrives as `undefined`, silently, with no error
anywhere — the same defect shape as the `_save_to_db` whitelist that left
freight, insurance and adjustment NULL in the database for every job ever run.

`field_bboxes` was the live instance: computed on every job since V11, stored in
`jobs.field_bboxes_json`, read by `ReviewSplitView` as `job.field_bboxes`, and
absent from this dict. The viewer fell back to page 1 for every field, which on a
bundled release order whose declaration sits on page 10 was wrong on every row
and looked deliberate.

Read rather than imported: `routes.review` pulls in the auth stack.
"""
from __future__ import annotations

import os
import re

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REVIEW = os.path.join(BACKEND, "routes", "review.py")
SPLITVIEW = os.path.join(
    os.path.dirname(BACKEND), "frontend", "src", "lib", "components",
    "ReviewSplitView.svelte")


def _payload_keys() -> set:
    """Keys of the `job` dict returned by `get_review_job`."""
    with open(REVIEW, encoding="utf-8") as fh:
        src = fh.read()
    body = src.split('"job": {', 1)[1]
    depth, out = 1, []
    for ch in body:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        out.append(ch)
    return set(re.findall(r'"([a-z_]+)"\s*:', "".join(out)))


class TestTheUIGetsWhatItReads:
    """A named list, not a sweep.

    A first version of this cross-referenced every `job.x` in the Svelte
    component against the payload and reported sixteen missing. Eleven were
    false: `ReviewSplitView` is mounted by three different parents, and the
    review page assembles its prop as `{...res.job, declaration, items,
    item_review_flags}` while the agent and history pages pass objects of their
    own. A sweep across all of them fails on properties the API is not the
    source of, so it would be turned off the first week.

    These are the ones the API alone can supply, each with the symptom its
    absence produced.
    """

    # property -> what the reviewer saw while it was missing
    REQUIRED = {
        "field_bboxes": "every field showed page 1, including declarations on page 10",
        "cost_usd": "header read $0.000",
        "tokens_in": "header read TOK:0.0k/0.0k",
        "tokens_out": "header read TOK:0.0k/0.0k",
        "processing_time_seconds": "header read TIME:—",
        "model_used": "engine label blank",
        "review_status": "approve/reject state unknown",
        "total_pages": "page strip could not be built",
    }

    def test_the_payload_serves_every_property_only_it_can_supply(self):
        served = _payload_keys()
        missing = {k: why for k, why in self.REQUIRED.items() if k not in served}
        assert missing == {}, "\n".join(
            f"{k} — absent from routes/review.py; symptom: {why}"
            for k, why in missing.items())

    def test_the_component_still_reads_the_bboxes_under_this_name(self):
        """A rename on either side breaks the link silently.

        The property arrives as `undefined`, the viewer falls back to page 1,
        and nothing logs anything. That is how it went unnoticed since V11.
        """
        if not os.path.exists(SPLITVIEW):
            import pytest
            pytest.skip("frontend not present")
        with open(SPLITVIEW, encoding="utf-8") as fh:
            ui = fh.read()
        assert "field_bboxes" in ui
