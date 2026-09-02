"""The three lifecycle dates are read off the page, not asked of a model.

`arrival_date`, `release_order_date` and `completion_date` are printed on the
form at fixed labels, so a text-layer reader gets them for free and exactly.
Asking the primary engines for them as well bought nothing and cost something:
on a scanned page — where no reader can run and there is nothing to check an
answer against — the model fills the blank row by echoing a neighbouring date.
`rover/supervisor.flag_echoed_dates` documents two real documents that carried
an arrival date and a release-order date printed nowhere in the file, both
equal to that document's declaration date. A date has no arithmetic to fail,
so no gate can catch it; it just lands in the ledger looking like a reading.

So the engines stopped being asked, and the readers stayed:

    v11/textlayer_header.py   coordinate-anchored, $0
    v11/formread.py           labelled-date specs with decoy exclusion, $0

On a scanned declaration the value is now NULL rather than guessed. Blank is a
worse answer than a correct one and a much better answer than a wrong one — a
reviewer can see a blank; they cannot see an echo.

The columns, the review screen, the date filter and `DECLARATION_FIELD_MAP` are
all untouched. This file pins the prompts only.

`arrival_date` in `v11/tools/vision_rescue.py` is deliberately EXEMPT and
asserted below. That call reads the declaration page of a scanned bundle, and
it is the only thing that can overrule an arrival date the typed lane scraped
off a waybill attachment — which is why `workflow.py` lists the field as
authoritative from that source. Removing it there would reintroduce a fixed bug
to save a handful of tokens on a path that only fires on scans.
"""
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]

LIFECYCLE_DATES = ("arrival_date", "release_order_date", "completion_date")

# The primary extraction prompts. Both engines see every page they are given, so
# a field named here is a field paid for on every document.
PRIMARY_PROMPTS = (
    ("v11/presto.py", "PROMPT"),
    ("v13/scribe.py", "PROMPT"),
)


def _prompt_text(rel: str, attr: str) -> str:
    """Read the prompt as SOURCE, not by importing.

    Importing pulls in the OpenRouter client and the config module, which want
    credentials this test has no business needing. The prompt is a module-level
    string literal, so reading the file answers the question the test is asking.
    """
    src = (BACKEND / rel).read_text(encoding="utf-8")
    assert f"{attr} = " in src, f"{rel} no longer defines {attr}"
    return src


@pytest.mark.parametrize("rel,attr", PRIMARY_PROMPTS)
@pytest.mark.parametrize("field", LIFECYCLE_DATES)
def test_primary_prompts_do_not_ask_for_a_lifecycle_date(rel, attr, field):
    src = _prompt_text(rel, attr)
    assert field not in src, (
        f"{rel} asks the model for `{field}`. It is read for free off the text "
        f"layer by textlayer_header/formread, and on a scanned page the model "
        f"echoes a neighbouring date instead of admitting the row is blank. "
        f"Remove it from the prompt; the column stays."
    )


def test_the_columns_and_their_mapping_still_exist():
    """Not asked for is not the same as removed.

    Dropping the columns would take the team's ledger key with them — their
    sheet keys on the Release-Order date, not the declaration date.
    """
    import database
    for field in LIFECYCLE_DATES:
        assert field in database.DECLARATION_FIELD_MAP, field


def test_the_free_readers_are_still_wired():
    """The whole trade depends on these still running."""
    textlayer = (BACKEND / "v11/textlayer_header.py").read_text(encoding="utf-8")
    formread = (BACKEND / "v11/formread.py").read_text(encoding="utf-8")
    for field in LIFECYCLE_DATES:
        assert field in textlayer, f"textlayer_header lost `{field}`"
        assert field in formread, f"formread lost `{field}`"


def test_vision_rescue_keeps_arrival_date():
    """The exemption, asserted so it is not "tidied up" later."""
    src = (BACKEND / "v11/tools/vision_rescue.py").read_text(encoding="utf-8")
    assert "arrival_date" in src, (
        "vision_rescue is the only reader that can overrule an arrival date "
        "the typed lane took off a waybill attachment on a scanned bundle. "
        "workflow.py treats it as authoritative for that reason."
    )
