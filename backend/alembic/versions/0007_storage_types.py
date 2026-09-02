"""Fix the three storage-type defects left outside `declarations`.

Migration 0006 widened the money columns on `declarations` and stopped there. The
same three defects are still live everywhere else, and all three have already
corrupted customer data.

1. MONEY STORED AS `real`
   Postgres `real` is single precision: ~7 significant decimal digits. Verified by
   round-tripping the review form against the column: `19,363,898.77` is read back
   as `19,363,898.0`, `9,580,894.17` as `9,580,894.0`, `486,007.83` as
   `486,007.84`. Those exact wrong figures are in the customer's Excel export.
   `items.customs_value_mmk` shows the damage in the raw column dump — a line worth
   2,133,479.8 kyat is stored as `2.1334798e+06` and cannot hold the last two
   digits at all. Eleven `real` columns remain across `items`, `jobs`,
   `importer_profiles`, `processing_logs`, `page_extractions`.

2. NUMBERS STORED AS `text` ON `items`
   `quantity`, `invoice_unit_price`, `cif_unit_price` and `exchange_rate` are
   `text`. Anything that sums, averages or compares them is doing string maths:
   `'9' > '10'` is true, and `SUM()` does not even typecheck. The per-row gate
   (value ~= qty x price x rate) reads all four.

3. `*_json` COLUMNS ARE `text`
   Eleven of them. A JSON document stored as a string is unqueryable (no `->>`, no
   containment, no index) and unvalidated — nothing has ever rejected a truncated
   or malformed payload on write, so a bad one is only discovered when a reader
   tries to `json.loads` it.

`real` -> `numeric` is a widening cast, so this is safe and needs no data
migration. It does NOT restore digits already lost: those values were truncated on
write and the information is gone. `19,363,898.0` stays `19,363,898.0` — it just
stops getting worse. Re-run affected documents to recover exact figures.

One thing widening does NOT get for free: the bare `real::numeric` cast is lossy
in its own right (Postgres renders float4 with `%.6g` first), so writing the
obvious `ALTER ... TYPE numeric` would have shaved another digit off every row on
the way in — 36574.65 lands as 36574.6. The conversion goes via `::text` instead;
see the comment on the loop for the measured comparison.

Type choices, per column, by use rather than by name:
  * numeric(20,4)  — money. Kyat amounts run to 10-11 digits before the decimal;
                     4 dp is more than a currency subunit needs.
  * numeric(24,10) — rate-like. A real ledger exchange rate is
                     61.95007144978846; `real` kept 61.95007. 10 dp is ~12
                     significant digits here, vs 7 before. Unit prices sit in the
                     same family (33.765, 111488.4288) and are multiplied BY a
                     rate, so they get the same width.
  * numeric(24,6)  — quantity. Fractional packs (236.16, 2937.6) are normal.
  * numeric(9,4)   — percentage rates that feed money arithmetic.
  * numeric(20,8)  — `jobs.cost_usd`. This one deviates from the money default on
                     purpose: a per-job LLM spend is $0.0151 and per-call costs go
                     below $0.0001, so 4 dp would quantise the entire cost
                     dashboard to a hundredth of a cent.

Left as `real` on purpose — telemetry, never multiplied into a money figure, and
7 digits is more than the value is meaningful to:
  * `jobs.accuracy_percent`, `jobs.processing_time_seconds`
  * `processing_logs.duration_seconds`
  * `page_extractions.confidence`   (a 0..1 model score)
By contrast `items.customs_duty_rate` and `items.commercial_tax_percent` ARE
converted: they are percentages too, but they are inputs to a monetary product
(tax = customs value x rate), so a float32 artefact in the rate lands in a kyat
figure.

`declarations` is deliberately untouched: 0006 already moved it to
`double precision` (15-17 significant digits), which is sufficient, and re-typing
it here would collide with work in flight on that table.

DOWNSTREAM: THIS MIGRATION IS NOT SAFE TO APPLY ON ITS OWN.

psycopg3 returns `numeric` as `decimal.Decimal`, not `float`, and `jsonb` as an
already-parsed dict/list, not `str`. So `json.loads(row["payload_json"])` stops
being a parse and starts being a type error. Eight call sites read a column this
migration converts, and they do NOT fail the same way:

  LOUD — raises TypeError, visible as a 500:
    database.py:1294   metadata_json          (no try/except at all)

  SILENT — `except (JSONDecodeError, TypeError)` swallows it and substitutes an
  empty value, so the endpoint returns 200 with the data missing:
    database.py:1299   cross_validation_json  -> None
    database.py:1308   field_bboxes_json      -> {}
    routes/data.py:294 cross_validation_json  (local `cv_json`)
    routes/evidence.py:188, :218  evidence_json
    v11/learn/priors.py:488       fee_baseline_json
    database.py:2584   fields/items/amounts/entities_json -> {} AND `del row[jf]`

Two of those escalate past "one empty field", and both were confirmed by reading
the handler rather than the call:

  * `routes/evidence.py:188` — the handler is `except Exception: continue`, and
    the `continue` is inside the loop that BUILDS the response list. Every row
    fails identically, so the reviewer's evidence listing comes back as an empty
    document set with `total = 0`. Not a document missing its evidence — the
    whole listing, reading as "nothing to review".
    Worse, it is undetectable rather than merely quiet: `/count` (line 218, "just
    the badge number for the nav tab") parses the SAME column under
    `except Exception: pass` and returns `{"count": 0}`. So the nav badge agrees
    with the empty table. A badge reading 12 over an empty list is the kind of
    inconsistency someone notices; a 0 badge over an empty list reads as a
    cleared queue. Two independent endpoints failing consistently removes the
    one accidental signal that would have raised the alarm — which is why this
    site, not the one that loses more data, is the first to fix.
  * `database.py:2584` — a loop over all four `page_extractions` payloads whose
    handler blanks the value AND does `del row[jf]`, so the original string is
    gone from the row too and nothing downstream can recover it. Nothing logs.

In both cases every request still returns 200.

A `try/except` is NOT the fix here — it is what converts the crash into silent
data loss. The correct guard is a type check, and the repo already has it in three
places (`database.py:1437`, `routes/review.py:416`, `rover/store_pg.py:131`):

    return json.loads(val) if isinstance(val, str) else val

That form works before and after this migration, so the call sites can be fixed
ahead of it. Also: a bare `json.dumps()` of a row carrying a converted money
column raises on the Decimal unless a default encoder is set.

Apply order — fix the eight call sites first, then run this migration. Applying
this alone trades a precision bug for an emptied-payload bug, which is the same
class of quiet-wrong-data defect and harder to notice. The exact columns changing
shape are `_REAL_TO_NUMERIC`, `_TEXT_TO_NUMERIC` and `_TEXT_TO_JSONB` below.
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_storage_types"
down_revision = "0006_decl_money_precision"
branch_labels = None
depends_on = None


# (table, column, new type, type to restore on downgrade)
_REAL_TO_NUMERIC = [
    ("items", "customs_value_mmk", "numeric(20,4)", "real"),
    ("items", "customs_duty_rate", "numeric(9,4)", "real"),
    ("items", "commercial_tax_percent", "numeric(9,4)", "real"),
    ("importer_profiles", "exchange_rate_min", "numeric(24,10)", "real"),
    ("importer_profiles", "exchange_rate_max", "numeric(24,10)", "real"),
    ("importer_profiles", "exchange_rate_avg", "numeric(24,10)", "real"),
    ("jobs", "cost_usd", "numeric(20,8)", "real"),
]

_TEXT_TO_NUMERIC = [
    ("items", "quantity", "numeric(24,6)", "text"),
    ("items", "invoice_unit_price", "numeric(24,10)", "text"),
    ("items", "cif_unit_price", "numeric(24,10)", "text"),
    ("items", "exchange_rate", "numeric(24,10)", "text"),
]

# `items.hs_code` stays text on purpose — an HS code is an identifier with
# significant leading zeros, not a number. So do `item_name`, `origin_country`
# and every date column (see `dates.py`: they hold three different formats).
_TEXT_TO_JSONB = [
    ("activity_logs", "payload_json"),
    ("declarations", "evidence_json"),
    ("declarations", "sanity_flags_json"),
    ("importer_profiles", "fee_baseline_json"),
    ("jobs", "cross_validation_json"),
    ("jobs", "field_bboxes_json"),
    ("page_extractions", "amounts_json"),
    ("page_extractions", "entities_json"),
    ("page_extractions", "fields_json"),
    ("page_extractions", "items_json"),
    ("pdf_metadata", "metadata_json"),
]


def _present_cols():
    """Every (table, column) that actually exists, as one query.

    0006 crashed the entire first-time deploy by altering `invoice_price_fc`, a
    column no migration creates — it is added by the self-heal `ALTER ADD COLUMN
    IF NOT EXISTS` block in `database.py`, which runs at app startup, i.e. AFTER
    alembic. On a virgin database the column is absent, the unconditional ALTER
    aborts the transactional upgrade, every migration rolls back, and the deploy
    is left with no schema at all.

    This migration touches 22 columns across 8 tables, so the same failure mode is
    22 times as likely. Rather than probe per column, pull the whole schema once
    and intersect. Filtered to `current_schema()` — the same schema the unqualified
    ALTERs below resolve against, so the guard cannot disagree with the statement
    it is guarding.
    """
    rows = op.get_bind().execute(
        sa.text(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema()"
        )
    )
    return {(r[0], r[1]) for r in rows}


def _warn_unconvertible(have, specs, fn):
    """Report values that the tolerant cast will turn into NULL.

    The casts below never raise — that is the point, one malformed row must not
    take the upgrade down with it. But "never raises" and "loses nothing" are not
    the same thing, and a silent NULL is exactly the kind of quiet data loss this
    migration exists to stop. Count them first and say so.
    """
    bind = op.get_bind()
    for table, column, *_ in specs:
        if (table, column) not in have:
            continue
        n = bind.execute(
            sa.text(
                f"SELECT count(*) FROM {table} "
                f"WHERE {column} IS NOT NULL AND btrim({column}::text) <> '' "
                f"AND {fn}({column}) IS NULL"
            )
        ).scalar()
        if n:
            print(
                f"  [0007] WARNING: {table}.{column}: {n} value(s) could not be "
                f"parsed and will become NULL. Re-run those documents to recover."
            )


def upgrade():
    have = _present_cols()

    # Tolerant casts as plpgsql functions rather than inline CASE expressions: a
    # plpgsql EXCEPTION block runs on an internal savepoint, so a value that no
    # regex anticipated (`'12.3.4'`, a truncated JSON payload) returns NULL
    # instead of aborting the transaction. Dropped again at the end of upgrade().
    op.execute(
        """
        CREATE FUNCTION _m0007_to_numeric(v text) RETURNS numeric AS $fn$
        DECLARE
            s text;
        BEGIN
            IF v IS NULL THEN
                RETURN NULL;
            END IF;
            -- These columns were written by ten near-copies of `_num()` over the
            -- years, so they hold whatever the form printed: thousands
            -- separators, a currency code beside the figure ("THB 652,279.7184"),
            -- a trailing unit, non-breaking spaces from a PDF text layer.
            s := btrim(v);
            s := replace(s, ',', '');
            s := replace(s, chr(160), '');
            s := replace(s, ' ', '');
            -- chr(37) is the percent sign, spelled this way on purpose: a
            -- literal one in the statement text can be eaten as a paramstyle
            -- placeholder by some DBAPI paths before Postgres ever sees it.
            s := replace(s, chr(37), '');
            s := regexp_replace(s, '^[A-Za-z$]+', '');   -- leading currency
            s := regexp_replace(s, '[A-Za-z]+$', '');    -- trailing unit
            IF s = '' THEN
                RETURN NULL;
            END IF;
            RETURN s::numeric;
        EXCEPTION WHEN others THEN
            RETURN NULL;
        END;
        $fn$ LANGUAGE plpgsql IMMUTABLE;
        """
    )
    op.execute(
        """
        CREATE FUNCTION _m0007_to_jsonb(v text) RETURNS jsonb AS $fn$
        BEGIN
            IF v IS NULL OR btrim(v) = '' THEN
                RETURN NULL;
            END IF;
            RETURN btrim(v)::jsonb;
        EXCEPTION WHEN others THEN
            RETURN NULL;
        END;
        $fn$ LANGUAGE plpgsql IMMUTABLE;
        """
    )

    _warn_unconvertible(have, _TEXT_TO_NUMERIC, "_m0007_to_numeric")
    _warn_unconvertible(have, _TEXT_TO_JSONB, "_m0007_to_jsonb")

    # 1. Money / rate columns: `real` -> `numeric`, VIA TEXT. Not decoration —
    #    the bare `real::numeric` cast is itself lossy. Postgres implements it as
    #    `snprintf("%.*g", FLT_DIG=6, val)`, i.e. it throws away digits the float32
    #    genuinely carried. Measured on the live `items` rows:
    #
    #        stored real   col::numeric   col::float8::numeric   col::text::numeric
    #          36574.65      36574.6000        36574.6484            36574.6500
    #          7973.9424      7973.9400         7973.9424             7973.9424
    #          164247.1     164247.0000       164247.0938           164247.1000
    #
    #    The direct cast drops a significant digit. Going via float8 keeps the
    #    exact binary value but surfaces its noise (36574.6484). `::text` uses the
    #    shortest representation that round-trips the float, which recovers the
    #    number the application actually wrote. `SET LOCAL extra_float_digits`
    #    pins that behaviour: a pooler or session that has set it to 0 puts
    #    float4 output back on the %.6g path and silently reintroduces exactly
    #    the truncation this migration is here to end.
    op.execute("SET LOCAL extra_float_digits = 3")
    for table, column, new_type, _old in _REAL_TO_NUMERIC:
        if (table, column) in have:
            op.execute(
                f"ALTER TABLE {table} ALTER COLUMN {column} TYPE {new_type} "
                f"USING {column}::text::numeric"
            )

    # 2. `items` numeric-ish text -> numeric, through the tolerant cast.
    for table, column, new_type, _old in _TEXT_TO_NUMERIC:
        if (table, column) in have:
            op.execute(
                f"ALTER TABLE {table} ALTER COLUMN {column} TYPE {new_type} "
                f"USING _m0007_to_numeric({column})"
            )

    # 3. `*_json` text -> jsonb, through the tolerant cast.
    for table, column in _TEXT_TO_JSONB:
        if (table, column) in have:
            op.execute(
                f"ALTER TABLE {table} ALTER COLUMN {column} TYPE jsonb "
                f"USING _m0007_to_jsonb({column})"
            )

    # 4. Verbatim engine output, so a mapping bug can be re-projected without
    #    paying for the model again. `_save_to_db` is a hand-written whitelist —
    #    any field an engine extracts but the dict does not map is dropped before
    #    the DB, and until now the only copy of the dropped value was in a log
    #    line. The column is created here; the code that fills it is separate.
    if ("jobs", "job_id") in have and ("jobs", "raw_extraction") not in have:
        op.add_column(
            "jobs", sa.Column("raw_extraction", postgresql.JSONB(), nullable=True)
        )

    op.execute("DROP FUNCTION IF EXISTS _m0007_to_numeric(text)")
    op.execute("DROP FUNCTION IF EXISTS _m0007_to_jsonb(text)")


def downgrade():
    # Narrowing `numeric` back to `real` re-rounds every value to ~7 significant
    # digits — the same loss this migration was written to stop, applied again to
    # the rows that were re-extracted since. jsonb -> text and numeric -> text are
    # lossless in value but not in form: JSON comes back normalised (whitespace
    # gone, object keys reordered) and numbers come back canonicalised, so the
    # exact byte string that was originally stored is not restored. Kept for
    # completeness; running it is a data event, not a no-op.
    have = _present_cols()

    if ("jobs", "raw_extraction") in have:
        op.drop_column("jobs", "raw_extraction")

    for table, column in _TEXT_TO_JSONB:
        if (table, column) in have:
            op.execute(
                f"ALTER TABLE {table} ALTER COLUMN {column} TYPE text "
                f"USING {column}::text"
            )

    for table, column, _new, old_type in _TEXT_TO_NUMERIC:
        if (table, column) in have:
            op.execute(
                f"ALTER TABLE {table} ALTER COLUMN {column} TYPE {old_type} "
                f"USING {column}::text"
            )

    for table, column, _new, old_type in _REAL_TO_NUMERIC:
        if (table, column) in have:
            op.execute(
                f"ALTER TABLE {table} ALTER COLUMN {column} TYPE {old_type} "
                f"USING {column}::{old_type}"
            )
