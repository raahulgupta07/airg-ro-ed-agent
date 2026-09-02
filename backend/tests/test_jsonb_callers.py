"""`json.loads(row["x_json"])` breaks the moment the column becomes `jsonb`.

Migration 0007 converts eleven `*_json` columns from `text` to `jsonb`. psycopg3
returns `jsonb` already parsed — a dict or a list, never a string — so every call
site that does `json.loads(row["payload_json"])` receives an object and raises
`TypeError: the JSON object must be str, bytes or bytearray`.

The type change is right. This file is about the other half of it: a schema
migration that lands a breaking change on its readers is only safe if someone
enumerated the readers. Nothing else in the repo does.

THE RULE, and why `try/except` does not satisfy it: most of these sites are
already wrapped in a `try` that falls back to `{}`. That does not make them
correct — it makes them worse, and the scan labels them accordingly:

  LOUD    no absorbing handler. `json.loads(<dict>)` raises TypeError, the
          request 500s, someone files a bug within the hour. One site.
  SILENT  an `except (TypeError, …)` / `except Exception` catches it and
          substitutes `None`, `{}`, `continue` or `pass`. HTTP 200, payload
          gone, nothing logged. Seven sites, counting the hand-pinned one.

So the guarded sites are strictly more dangerous than the unguarded one, which
inverts the usual intuition — worth stating plainly for whoever picks up the fix.

ALL SITES ARE NOW FIXED (4 Aug 2026) and the strict xfail that tracked them has
been removed. This file is a live guard, not a to-do list: it now fails when a
NEW unguarded parse appears. The history is kept because the fix order was
chosen from per-site descriptions, and two of those descriptions were wrong:

  `routes/evidence.py` — was the correctly-diagnosed one, and the reason the
  reviewer evidence queue rendered `{"documents": [], "total_checks": 0}` with
  `/count` agreeing, removing the one inconsistency a UI might have shown.

  `routes/data.py` (GET /api/data/ai-tables) — SILENT as described. Its `except`
  absorbed the TypeError per ROW inside the aggregation loop, so once every row
  failed identically the endpoint answered 200 with an empty table list.

  `database.py` `get_page_extractions` — pinned as the worst SILENT site,
  "nothing raised anywhere". Wrong: `json.loads(row.pop(jf))` popped BEFORE
  parsing, so the TypeError left the key already gone and the handler's own
  `del row[jf]` raised KeyError, which its except tuple does not catch. It
  crashed rather than emptying.

Both were fixed with `jsonio.loads_maybe`, which the scan accepts because it
removes the `json.loads` call entirely; the `isinstance` form below is equally
valid. Either is correct before AND after the migration, so call sites never
had to be fixed in lockstep with 0007.

The only form that is correct both before and after the migration is an explicit
string check:

    json.loads(value) if isinstance(value, str) else value

which this repo already uses in three places (`database.py:1437`,
`routes/review.py:416`, `rover/store_pg.py:131`). That is what this file
requires.

WHAT THIS CANNOT CATCH:
  * A read that reaches the column through a variable named nothing like it —
    the scan matches on the converted column names appearing in the argument
    source text. A site that does `payload = row[key]` in a loop and parses
    `payload` later is invisible here. `database.py:2584` is caught only because
    the column names are literals in the loop header.
  * The other half of the same breaking change: `json.dumps()` of a row carrying
    a converted MONEY column. psycopg3 returns `numeric` as `decimal.Decimal`,
    which the stdlib encoder refuses. That is a runtime shape, not a syntactic
    one, and no static scan finds it.
  * Anything in the frontend or in SQL that used `->>` on a text column.
"""
import ast
import os
import re

import pytest

import db_engine

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_JSON_NAME_RE = re.compile(r"\b\w*_json\b")

#: Sites the scan structurally cannot see, kept here so they are not lost.
#:
#: `database.py:2584` is `json.loads(row.pop(jf))` inside
#: `for jf in ('fields_json', 'items_json', 'amounts_json', 'entities_json')`.
#: The argument source is `row.pop(jf)` — no column name, no `_json` token — so
#: no scan that reads the call site alone can match it. It is also the worst of
#: the sites: its `except (JSONDecodeError, TypeError, ValueError)` sets the
#: parsed value to `{}` AND deletes the original key, so after 0007 all four
#: page-extraction payloads become empty with nothing raised anywhere.
#:
#: `(relative path, the loop header that makes it a hit)`
KNOWN_INVISIBLE_SITES = [
    ("database.py",
     "for jf in ('fields_json', 'items_json', 'amounts_json', 'entities_json')"),
]

# The columns migration 0007 converts. Taken from the guard's own contract rather
# than retyped, so the two cannot drift apart.
CONVERTED_JSON_COLUMNS = sorted({
    "sanity_flags_json", "evidence_json", "cross_validation_json",
    "field_bboxes_json", "payload_json", "metadata_json", "fields_json",
    "items_json", "amounts_json", "entities_json", "fee_baseline_json",
})

SKIP_DIRS = {".venv", "__pycache__", "node_modules", "alembic", "tests",
             "frontend-build", "data", "storage"}


def _python_files():
    for root, dirs, files in os.walk(BACKEND):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in sorted(files):
            if name.endswith(".py"):
                yield os.path.join(root, name)


#: Exception names whose handler would absorb the `TypeError` that
#: `json.loads(<dict>)` raises — turning a crash into missing data.
_ABSORBING = {"TypeError", "Exception", "BaseException", "ValueError"}


def _swallows_the_type_error(node, ancestors):
    """True when a `try` around this call would catch `json.loads(<dict>)`'s TypeError.

    Counter-intuitively this makes a site MORE dangerous, not less. An unguarded
    call raises and someone files a bug; a caught one substitutes `None`, `{}`,
    `continue` or `pass` and returns 200 with the payload missing. That is the
    same class of quiet-wrong-data defect as the `real` truncation — which is the
    whole reason this suite exists.
    """
    for parent in ancestors:
        if not isinstance(parent, ast.Try):
            continue
        for handler in parent.handlers:
            if handler.type is None:            # bare `except:`
                return True
            names = {n.id for n in ast.walk(handler.type)
                     if isinstance(n, ast.Name)}
            if names & _ABSORBING:
                return True
    return False


def _is_string_guarded(node, ancestors):
    """True when a `json.loads` call sits under an `isinstance(..., str)` test.

    Covers both shapes the repo already uses: the conditional expression
    (`json.loads(v) if isinstance(v, str) else v`) and the statement form
    (`if isinstance(v, str): ... json.loads(v)`).
    """
    for parent in ancestors:
        test = None
        if isinstance(parent, ast.IfExp):
            test = parent.test
        elif isinstance(parent, ast.If):
            test = parent.test
        if test is None:
            continue
        for sub in ast.walk(test):
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                    and sub.func.id == "isinstance"
                    and len(sub.args) == 2
                    and "str" in ast.dump(sub.args[1])):
                return True
    return False


def _unguarded_sites():
    """`[(relpath, lineno, 'LOUD'|'SILENT', source)]` for every unguarded parse.

    The classification is the triage order. LOUD raises and is found in minutes;
    SILENT returns 200 with the data gone and may never be found at all.
    """
    hits = []
    for path in _python_files():
        with open(path, encoding="utf-8") as fh:
            source = fh.read()
        # Cheap substring gate before the AST parse. Parsing all ~100 backend
        # modules costs ~0.6s, which is a third of this suite's whole runtime;
        # a file with no `json.loads` in its text cannot contain a hit.
        if "json.loads" not in source:
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:                     # not ours to fix, not ours to read
            continue

        parents = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[id(child)] = parent

        def ancestry(node):
            chain, cur = [], parents.get(id(node))
            while cur is not None:
                chain.append(cur)
                cur = parents.get(id(cur))
            return chain

        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "loads"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "json"
                    and node.args):
                continue
            arg_src = ast.get_source_segment(source, node.args[0]) or ""
            # Match a converted column by name, and also any local named after
            # one (`cv_json` holds `cross_validation_json`). Deliberately broad:
            # a false positive costs one `isinstance` check, a false negative
            # costs a silently empty payload in production.
            if not (any(c in arg_src for c in CONVERTED_JSON_COLUMNS)
                    or _JSON_NAME_RE.search(arg_src)):
                continue
            chain = ancestry(node)
            if _is_string_guarded(node, chain):
                continue
            kind = "SILENT" if _swallows_the_type_error(node, chain) else "LOUD"
            hits.append((os.path.relpath(path, BACKEND), node.lineno, kind,
                         (ast.get_source_segment(source, node) or "").strip()))
    return sorted(hits)


class TestTheScanItselfWorks:
    """A scan that found nothing because it was broken would read as a pass."""

    def test_it_recognises_the_guarded_pattern(self):
        src = 'import json\ndef f(v):\n    return json.loads(v_json) if isinstance(v_json, str) else v_json\n'
        assert "isinstance" in src              # the shape under test
        tree = ast.parse(src)
        call = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "loads")
        parents = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[id(child)] = parent
        chain, cur = [], parents.get(id(call))
        while cur is not None:
            chain.append(cur)
            cur = parents.get(id(cur))
        assert _is_string_guarded(call, chain) is True

    def test_it_does_not_recognise_a_bare_try_as_a_guard(self):
        src = ('import json\ndef f(row):\n    try:\n'
               '        return json.loads(row["payload_json"])\n'
               '    except Exception:\n        return {}\n')
        tree = ast.parse(src)
        call = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "loads")
        assert _is_string_guarded(call, []) is False

    def test_the_column_list_matches_what_the_guard_expects_to_be_jsonb(self):
        # Both sides derive from migration 0007; if they diverge the scan is
        # looking for columns that no longer change shape.
        assert len(CONVERTED_JSON_COLUMNS) == 11
        assert "TOLERATED_TEXT_JSON_COLUMNS" in dir(db_engine)
        assert db_engine.TOLERATED_TEXT_JSON_COLUMNS == set(), (
            "a column exempted from jsonb should be dropped from this scan too")

    @pytest.mark.parametrize("handler,expected", [
        ("except (json.JSONDecodeError, TypeError):", True),
        ("except Exception:", True),
        ("except ValueError:", True),
        ("except:", True),
        ("except KeyError:", False),          # would not absorb a TypeError
        ("except json.JSONDecodeError:", False),
    ])
    def test_it_tells_an_absorbing_handler_from_a_narrow_one(self, handler, expected):
        src = ('import json\ndef f(row):\n    try:\n'
               '        return json.loads(row["payload_json"])\n'
               f'    {handler}\n        return None\n')
        tree = ast.parse(src)
        parents = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[id(child)] = parent
        call = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "loads")
        chain, cur = [], parents.get(id(call))
        while cur is not None:
            chain.append(cur)
            cur = parents.get(id(cur))
        assert _swallows_the_type_error(call, chain) is expected

    def test_a_site_with_no_try_at_all_is_loud(self):
        assert _swallows_the_type_error(ast.parse("1").body[0], []) is False

    def test_the_scan_classifies_every_site_it_reports(self):
        for path, line, kind, _src in _unguarded_sites():
            assert kind in ("LOUD", "SILENT"), f"{path}:{line}"

    def test_it_actually_walks_the_backend(self):
        files = list(_python_files())
        assert len(files) > 50
        assert any(f.endswith("/database.py") for f in files)


class TestTheSitesTheScanCannotSee:
    """Pinned by hand, because a blind spot nobody wrote down is just a bug.

    These fail the same way as the scanned sites and need the same fix; they are
    simply not reachable by reading the call expression. Asserting the source
    text still contains them means a future refactor either keeps them findable
    or trips this test.
    """

    @pytest.mark.parametrize("relpath,marker", KNOWN_INVISIBLE_SITES)
    def test_the_known_blind_spot_is_still_where_it_was(self, relpath, marker):
        with open(os.path.join(BACKEND, relpath), encoding="utf-8") as fh:
            source = fh.read()
        assert marker in source, (
            f"{relpath} no longer contains {marker!r} — if it was fixed, drop "
            f"the entry from KNOWN_INVISIBLE_SITES; if it moved, re-check that "
            f"the new form parses a jsonb column")

    def test_that_blind_spot_now_goes_through_loads_maybe(self):
        """FIXED 4 Aug 2026 — and it was never the bug it was pinned as.

        The old form was `json.loads(row.pop(jf))` inside a `try`. The pop ran
        BEFORE the parse, so once the column became jsonb the TypeError left the
        key already removed, and the handler's own `del row[jf]` raised KeyError
        — which `except (JSONDecodeError, TypeError, ValueError)` does not
        catch. So it did not quietly empty the four payloads as the pin claimed;
        it propagated out of `get_page_extractions`.

        This asserts the mechanism, not the absence of a string. The first
        version of this test looked for "TypeError" and "= {}" in a window of
        source after the loop header, which the replacement's own EXPLANATORY
        COMMENT satisfied — it passed while asserting nothing.
        """
        with open(os.path.join(BACKEND, "database.py"), encoding="utf-8") as fh:
            source = fh.read()
        tree = ast.parse(source)

        loop = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.For)
             and (ast.get_source_segment(source, n) or "").lstrip().startswith(
                 KNOWN_INVISIBLE_SITES[0][1])),
            None)
        assert loop is not None, "the page-extraction loop moved"

        body = ast.dump(loop)
        assert "'loads_maybe'" in body, "the loop no longer parses via loads_maybe"
        assert "'loads'" not in body.replace("'loads_maybe'", ""), (
            "a raw json.loads is back in the page-extraction loop")
        assert not [n for n in ast.walk(loop) if isinstance(n, ast.Try)], (
            "the try is back — loads_maybe does not raise, so a handler here can "
            "only hide a different bug")
        assert not [n for n in ast.walk(loop) if isinstance(n, ast.Delete)], (
            "`del row[jf]` is back; pop already removed the key and this is what "
            "raised the uncaught KeyError")


class TestNoCallerParsesAColumnThatIsAboutToBecomeJsonb:
    """CLOSED 4 Aug 2026. Was `xfail(strict=True)` over 8 open sites.

    The marker did its job: it failed the suite the moment the last site was
    fixed, which is what prompted its removal. Kept as a live guard so the next
    `json.loads(row["something_json"])` fails here instead of in production.

    The last two, and what they actually did — neither matched its own pin:

      `routes/data.py:294` (GET /api/data/ai-tables) — SILENT, as described.
      Its `except` absorbed the TypeError and `pass`ed, per ROW, inside the
      aggregation loop. Post-0007 every row failed identically, so the endpoint
      answered 200 with `{"tables": [], "total_jobs": 50}`: not one job missing
      its tables, all of them, reading as "no tables were ever discovered".

      `database.py` `get_page_extractions` — pinned as the worst SILENT site,
      "all four payloads become empty with nothing raised anywhere". It was not
      silent. `json.loads(row.pop(jf))` popped before parsing, so the TypeError
      left the key gone and the handler's `del row[jf]` raised KeyError, which
      that except tuple does not catch. It crashed.

    Worth stating because the fix order was chosen from those descriptions. A
    pinned bug's description ages like any other comment; the scan is the part
    that stayed true.
    """


    def test_every_parse_of_a_converted_column_checks_for_a_string_first(self):
        sites = _unguarded_sites()
        assert sites == [], "\n".join(
            [""] + [f"  [{kind:6s}] {path}:{line}  {src}"
                    for path, line, kind, src in sites]
            + ["", "  SILENT sites are the urgent ones: their except clause "
               "absorbs the TypeError and returns 200 with the payload gone.",
               "  Fix shape (already used at database.py:1437, "
               "routes/review.py:416, rover/store_pg.py:131):",
               "      json.loads(v) if isinstance(v, str) else v",
               "  That form is correct BEFORE and AFTER the migration, so the "
               "call sites can be fixed ahead of 0007 rather than in lockstep."])
