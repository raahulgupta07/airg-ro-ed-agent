"""Two files author DDL in this repo, and only one of them can change a type.

The defect this guards: a column type was edited in `database.py`'s `CREATE TABLE`
and it changed nothing at all in the live database. Alembic owns the schema, and
the self-heal block in `database.py` is all `IF NOT EXISTS` — `CREATE TABLE IF NOT
EXISTS` is a no-op on an existing table and `ADD COLUMN IF NOT EXISTS` is a no-op
on an existing column. Neither ever ALTERs. The edit looked correct, was
reviewed, and was inert. `importer_profiles.exchange_rate_{min,max,avg}` sat at
`real` for months for exactly this reason.

Three separate things have to be true, and each is checked here:

  * The DDL sources SAY the right thing        — `TestTheContractHoldsInTheSource`
  * The guard NOTICES when they do not         — `TestTheGuardCatchesWhatShipped`
  * The LIVE database matches what they say    — `TestTheLiveSchema` (needs a DB)

The third is the one that catches "the migration exists but was never applied",
which is not hypothetical: while this file was being written the tree contained
`0007_storage_types` and the running database was still stamped
`0006_decl_money_precision`.

The contract itself — which columns hold money, which `real` columns are
deliberate — lives in `db_engine.MONEY_COLUMNS` / `TOLERATED_REAL_COLUMNS`,
beside the boot-time check that uses it, so there is one list rather than two.
"""
import ast
import re

import pytest

import db_engine
from tests import _ddl_sources
from tests._ddl_sources import (alembic_types, alembic_types_detailed,
                                effective_types, migration_chain,
                                selfheal_forced_types, selfheal_types)


# ── the schema as it actually was, read off the running database ─────────────
# Captured from `information_schema.columns` while the database was stamped
# `0006_decl_money_precision` — i.e. the exact schema that shipped the truncated
# figures. Restricted to the columns the contract has an opinion about (every
# `real`, every `*_json`, and every money column), which is all the guard reads.
SCHEMA_AT_0006 = {
    ("activity_logs", "payload_json"): "text",
    ("declarations", "adjustment_value"): "double precision",
    ("declarations", "advance_income_tax_at"): "double precision",
    ("declarations", "commercial_tax_ct"): "double precision",
    ("declarations", "evidence_json"): "text",
    ("declarations", "exchange_rate"): "double precision",
    ("declarations", "exemption_reduction"): "double precision",
    ("declarations", "freight_value"): "double precision",
    ("declarations", "import_export_customs_duty"): "double precision",
    ("declarations", "insurance_value"): "double precision",
    ("declarations", "invoice_price"): "double precision",
    ("declarations", "invoice_price_fc"): "double precision",
    ("declarations", "invoice_price_mmk"): "double precision",
    ("declarations", "maccs_service_fee_mf"): "double precision",
    ("declarations", "sanity_flags_json"): "text",
    ("declarations", "security_fee_sf"): "double precision",
    ("declarations", "total_customs_value"): "double precision",
    ("importer_profiles", "exchange_rate_avg"): "real",
    ("importer_profiles", "exchange_rate_max"): "real",
    ("importer_profiles", "exchange_rate_min"): "real",
    ("importer_profiles", "fee_baseline_json"): "text",
    ("items", "cif_unit_price"): "text",
    ("items", "commercial_tax_percent"): "real",
    ("items", "customs_duty_rate"): "real",
    ("items", "customs_value_mmk"): "real",
    ("items", "exchange_rate"): "text",
    ("items", "hs_code"): "text",
    ("items", "invoice_unit_price"): "text",
    ("items", "quantity"): "text",
    ("jobs", "accuracy_percent"): "real",
    ("jobs", "cost_usd"): "real",
    ("jobs", "cross_validation_json"): "text",
    ("jobs", "field_bboxes_json"): "text",
    ("jobs", "processing_time_seconds"): "real",
    ("page_extractions", "amounts_json"): "text",
    ("page_extractions", "confidence"): "real",
    ("page_extractions", "entities_json"): "text",
    ("page_extractions", "fields_json"): "text",
    ("page_extractions", "items_json"): "text",
    ("pdf_metadata", "metadata_json"): "text",
    ("processing_logs", "duration_seconds"): "real",
}


def _findings_for(types):
    return {(f["table"], f["column"]): f for f in db_engine.find_schema_drift(types)}


class _FakeConn:
    """Serves a `{(table, column): type}` map through the cursor API the guard uses.

    Lets the connection-taking entry points be exercised without a database, so
    the difference between "logs" and "raises" is pinned on every machine rather
    than only where Postgres happens to be up.
    """

    def __init__(self, types):
        self._rows = [(t, c, ty) for (t, c), ty in types.items()]

    def cursor(self):
        return self

    def execute(self, _sql, _params=None):
        return self

    def fetchall(self):
        return self._rows

    def close(self):
        pass


# ── the DDL sources ──────────────────────────────────────────────────────────

class TestTheMigrationChainIsWellFormed:
    """An unorderable history makes every other claim here meaningless."""

    def test_the_chain_is_linear_and_complete(self):
        # `migration_chain()` raises on a fork, a cycle, more than one root, or a
        # file unreachable from the root.
        chain = migration_chain()
        assert len(chain) >= 6

    def test_apply_order_matches_filename_order(self):
        # Not cosmetic: everyone reads the directory listing to work out what
        # runs when. A migration whose `down_revision` disagrees with its
        # numbering is a trap for the next person to add one.
        by_chain = [fn for fn, _src, _r, _d in migration_chain()]
        assert by_chain == sorted(by_chain)


class TestBothDDLSourcesAreFullyUnderstood:
    """A statement the parser skips is a hole in every check below it."""

    def test_no_migration_statement_was_skipped(self):
        _types, unparsed = alembic_types()
        assert unparsed == []

    def test_no_self_heal_statement_was_skipped(self):
        _types, unparsed = selfheal_types()
        assert unparsed == []


class TestTheContractHoldsInTheSource:
    """What a fresh boot would create, checked against the contract."""

    def setup_method(self):
        self.types, _unparsed = effective_types()

    @pytest.mark.parametrize("table,column", sorted(db_engine.MONEY_COLUMNS))
    def test_money_column_is_numeric_or_double_precision(self, table, column):
        declared = self.types.get((table, column))
        assert declared in db_engine._ACCEPTABLE_MONEY_TYPES, (
            f"{table}.{column} would be created as {declared} — "
            f"{db_engine.MONEY_COLUMNS[(table, column)]}")

    def test_every_json_column_is_jsonb(self):
        offenders = sorted(
            k for k, v in self.types.items()
            if k[1].endswith("_json") and v == "text"
            and k not in db_engine.TOLERATED_TEXT_JSON_COLUMNS)
        assert offenders == [], (
            "these would be created as text: %s. A JSON document in a text "
            "column is unvalidated and unqueryable — a malformed payload is only "
            "discovered when a reader tries to parse it." % offenders)

    def test_no_unclassified_real_column_is_introduced(self):
        offenders = sorted(
            k for k, v in self.types.items()
            if v == "real"
            and k not in db_engine.MONEY_COLUMNS
            and k not in db_engine.TOLERATED_REAL_COLUMNS)
        assert offenders == [], (
            "new `real` column(s) %s. If one holds money or a rate, widen it in "
            "a migration; if it is telemetry, add it to "
            "db_engine.TOLERATED_REAL_COLUMNS with the reason." % offenders)

    def test_the_tolerated_lists_do_not_rot(self):
        # An entry that no longer matches anything is a stale excuse. It costs
        # nothing to keep, but it hides the fact that the column was fixed.
        for key in db_engine.TOLERATED_REAL_COLUMNS:
            assert self.types.get(key) == "real", (
                f"{key} is listed as a tolerated `real` column but is now "
                f"{self.types.get(key)} — drop the entry")


class TestTheDeclaredNumericWidthIsWideEnough:
    """`data_type = 'numeric'` is not the same claim as "wide enough".

    A bare `numeric` is arbitrary precision and always safe. A DECLARED width
    rounds anything wider — `numeric(20,4)` on an exchange rate keeps four of the
    ten decimals a real ledger rate carries (61.95007144978846), which is the
    same defect as `real` wearing a different type name. A check that stopped at
    the base type would pass on exactly that column.
    """

    def test_every_declared_width_matches_the_contract(self):
        # Feeding the guard the widths as if read off a live database.
        types = {k: f"numeric({p},{s})"
                 for k, (p, s) in db_engine.MONEY_COLUMN_WIDTHS.items()}
        types.update({k: "double precision" for k in db_engine.MONEY_COLUMNS
                      if k not in db_engine.MONEY_COLUMN_WIDTHS})
        assert db_engine.find_schema_drift(types) == []

    def test_a_rate_column_narrowed_to_the_money_default_is_caught(self):
        # The realistic mistake: applying `numeric(20,4)` uniformly. It is right
        # for a kyat amount and wrong for a rate.
        types = {k: f"numeric({p},{s})"
                 for k, (p, s) in db_engine.MONEY_COLUMN_WIDTHS.items()}
        types.update({k: "double precision" for k in db_engine.MONEY_COLUMNS
                      if k not in db_engine.MONEY_COLUMN_WIDTHS})
        types[("items", "exchange_rate")] = "numeric(20,4)"
        f = _findings_for(types)[("items", "exchange_rate")]
        assert f["severity"] == "error"
        assert "4 decimal place(s)" in f["detail"]
        assert "needs 10" in f["detail"]

    def test_a_width_free_numeric_is_accepted_not_guessed_at(self):
        # `numeric` with no typmod is unbounded, so there is nothing to check and
        # nothing to report. Absent is not the same as wrong.
        types = {k: "numeric" for k in db_engine.MONEY_COLUMNS}
        assert db_engine.find_schema_drift(types) == []

    def test_the_scale_matters_not_just_the_precision(self):
        types = {k: "numeric" for k in db_engine.MONEY_COLUMNS}
        types[("items", "customs_value_mmk")] = "numeric(20,2)"
        assert ("items", "customs_value_mmk") in _findings_for(types)

    @pytest.mark.parametrize("table,column", sorted(db_engine.MONEY_COLUMN_WIDTHS))
    def test_the_width_the_migration_asks_for_is_the_width_in_the_contract(
            self, table, column):
        """Pin the widths against the migration's own source text.

        The contract is written by hand in `db_engine`; the widths are chosen in
        `0007_storage_types`. Two hand-maintained lists of the same thing drift,
        which is the defect this whole file is about — so read the migration.
        """
        precision, scale = db_engine.MONEY_COLUMN_WIDTHS[(table, column)]
        sources = "\n".join(src for _fn, src, _r, _d in migration_chain())
        assert f'"{column}", "numeric({precision},{scale})"' in sources, (
            f"db_engine says {table}.{column} is numeric({precision},{scale}), "
            f"but no migration asks for that width")


class TestNoBootTimeAlterCanNarrowAMigration:
    """`database.py:588` re-types 14 columns on EVERY container start.

    Not `IF NOT EXISTS` — an unconditional
    `ALTER TABLE declarations ALTER COLUMN <c> TYPE double precision`, run inside
    `init_database()` at every boot. It is correct today: 0006 put those columns
    at `double precision`, and 0007 deliberately leaves `declarations` alone.

    It stops being correct the moment anyone converts `declarations` to `numeric`
    the way 0007 converted `items`. The migration would widen the columns, the
    deploy would look clean, and the next container restart would silently narrow
    every one of them back — with no migration to blame and nothing in the logs.

    This is the test that catches that, and it is the reason `numeric` and
    `double precision` are BOTH acceptable in the contract: pinning one of them
    would make this pass for the wrong reason.
    """

    def test_a_forced_alter_agrees_with_what_the_migrations_establish(self):
        forced = selfheal_forced_types()
        alembic, _u = alembic_types()
        conflicts = sorted(
            (k, forced[k], alembic[k]) for k in forced
            if k in alembic and forced[k] != alembic[k])
        assert conflicts == [], "\n".join(
            f"  {t}.{c}: migrations make it {a}, but database.py re-ALTERs it "
            f"to {fv} on every boot — the restart wins"
            for (t, c), fv, a in conflicts)

    def test_the_forced_columns_are_still_only_the_declarations_money_set(self):
        # Pinned so an unconditional boot-time ALTER cannot be added to another
        # table without someone reading the docstring above.
        forced = selfheal_forced_types()
        assert {t for t, _c in forced} == {"declarations"}
        assert set(forced) <= set(db_engine.MONEY_COLUMNS)

    def test_and_every_forced_type_is_one_the_contract_accepts(self):
        for key, sql_type in selfheal_forced_types().items():
            assert sql_type in db_engine._ACCEPTABLE_MONEY_TYPES, (
                f"database.py forces {key} to {sql_type} at every boot")


class TestColumnsNoMigrationOwns:
    """Columns that exist only because `database.py` adds them at startup.

    These are a live trap, not a tidiness complaint. Migration 0006 originally
    ALTERed `invoice_price_fc` unconditionally; on a virgin database the column
    does not exist yet (the self-heal runs after alembic), the ALTER aborts the
    transactional upgrade, and every migration rolls back leaving no schema at
    all. 0006 and 0007 both now guard with a `_present_cols()` check for exactly
    this reason.

    Pinned so the set cannot grow quietly. Adding a column here means adding one
    that no migration will ever be able to re-type.
    """

    EXPECTED = {
        ("declarations", "arrival_date"),
        ("declarations", "completion_date"),
        ("declarations", "invoice_price_fc"),
        ("declarations", "invoice_price_mmk"),
        ("declarations", "release_order_date"),
        ("jobs", "doc_class"),
        ("users", "must_change_password"),
    }

    def test_the_set_of_migration_less_columns_is_unchanged(self):
        alembic, _u = alembic_types()
        selfheal, _u2 = selfheal_types()
        orphans = {k for k in selfheal if k not in alembic}
        assert orphans == self.EXPECTED

    def test_a_migration_only_alters_such_a_column_conditionally(self):
        # If a migration names one of these in an ALTER, it must be guarded — an
        # unconditional ALTER on a column the migration cannot guarantee exists
        # is what took the whole upgrade down.
        _types, _unparsed, alter_orphans = alembic_types_detailed()
        assert alter_orphans <= self.EXPECTED
        for table, column in sorted(alter_orphans):
            for fn, src, _r, _d in migration_chain():
                if column in src:
                    assert "_present_cols" in src, (
                        f"{fn} ALTERs {table}.{column}, which no migration "
                        f"creates, without checking that it exists")


class TestTheAlembicFailureIsSwallowedSilently:
    """The root cause, pinned at source — and the reason the guard only logs.

    Everything downstream of a failed migration follows from four lines in
    `database.py`. This is the durable assertion: it holds regardless of what any
    database currently looks like, so unlike a schema-state check it cannot go
    quiet after a restart.

    It is also load-bearing for a decision in `db_engine`. `_log_schema_drift`
    logs at ERROR rather than raising, and the justification written there is
    that a raise out of `run_alembic_upgrade()` would be swallowed here — a line
    nobody reads, on a level nobody enables. If this swallow is ever removed,
    that trade-off is worth revisiting, and this test is what surfaces the change.
    """

    def _init_database_node(self):
        with open(_ddl_sources.DATABASE_PY, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        return next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "init_database")

    def _alembic_try(self):
        for node in ast.walk(self._init_database_node()):
            if not isinstance(node, ast.Try):
                continue
            if "run_alembic_upgrade" in ast.dump(ast.Module(body=node.body,
                                                            type_ignores=[])):
                return node
        return None

    def test_the_migration_call_is_wrapped_in_a_try(self):
        assert self._alembic_try() is not None, (
            "database.init_database() no longer wraps run_alembic_upgrade() in a "
            "try — if a failed migration is now fatal, revisit the log-don't-raise "
            "decision documented in db_engine._log_schema_drift")

    def test_it_catches_every_exception_not_a_specific_one(self):
        handlers = self._alembic_try().handlers
        assert len(handlers) == 1
        caught = handlers[0].type
        assert isinstance(caught, ast.Name) and caught.id == "Exception"

    def test_and_reports_it_at_debug_level(self):
        # The whole problem in one assertion. A failed migration is indexed at
        # the same level as routine chatter, so in any normal deployment there is
        # no signal at all that the app is running on a schema it did not expect.
        handler_src = ast.dump(ast.Module(body=self._alembic_try().handlers[0].body,
                                          type_ignores=[]))
        assert "attr='debug'" in handler_src
        for louder in ("error", "warning", "critical", "exception"):
            assert f"attr='{louder}'" not in handler_src

    def test_boot_continues_past_the_failure(self):
        # There is no re-raise and no early return in the handler, so execution
        # falls through into the CREATE TABLE block below.
        body = self._alembic_try().handlers[0].body
        assert not any(isinstance(n, (ast.Raise, ast.Return))
                       for stmt in body for n in ast.walk(stmt))

    def test_every_create_table_in_the_initial_migration_is_idempotent(self):
        """This is what makes the wrong schema temporary rather than permanent.

        If any `CREATE TABLE` in 0001 were unconditional it would raise against
        the tables `database.py` already made, alembic would fail on every
        subsequent boot too, and the degraded schema would be permanent instead
        of lasting one restart.
        """
        src = next(s for fn, s, _r, _d in migration_chain()
                   if fn.startswith("0001"))
        total = len(re.findall(r"CREATE\s+TABLE", src, re.I))
        guarded = len(re.findall(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS", src, re.I))
        assert total == guarded == 20


@pytest.mark.xfail(
    strict=True,
    reason="KNOWN AND OPEN: 22 columns where database.py's self-heal claims an "
           "older type than the migrations. Harmless on a normal boot, because "
           "alembic runs first. The cost lands when a migration FAILS — see the "
           "docstring: the wrong schema is temporary, the data written onto it "
           "is not. Fixing it means editing database.py, which this guard does "
           "not own. When it is fixed this test starts PASSING, which fails the "
           "strict xfail and prompts removing this marker.")
class TestTheTwoDDLOwnersAgree:
    """`database.py` still describes the pre-0007 schema. Here is what that costs.

    `database.init_database()` calls `run_alembic_upgrade()` inside:

        try:
            db_engine.run_alembic_upgrade()
        except Exception as exc:            # pragma: no cover — non-fatal
            logger.debug("alembic upgrade skipped: %s", exc)

    A failed migration is swallowed at DEBUG level and boot continues into the
    `CREATE TABLE IF NOT EXISTS` block below it, which is a full parallel copy of
    the schema carrying the pre-0007 types inline — `cost_usd REAL`,
    `customs_value_mmk` as whatever `database.py` says, eleven `*_json` columns
    as `text`. The app then starts and serves.

    THE SCHEMA RECOVERS; THE DATA DOES NOT. Every `CREATE TABLE` in
    `0001_initial_schema` is `IF NOT EXISTS` (20 of 20, asserted in
    `TestTheAlembicFailureIsSwallowedSilently`), so the next boot where alembic
    succeeds no-ops over those tables, applies 0002-0007 in order, and 0007's
    `_present_cols()` converts the columns. The schema is self-healing.

    What is not self-healing is everything written during the window. Rows
    inserted while the app was serving on the `database.py` schema were
    float32-truncated on write, and widening the column afterwards cannot restore
    digits that were never stored — the same point 0007's own docstring makes
    about historical rows. Nothing in the data marks which rows came from the
    degraded window.

    So the accurate shape is: a transient wrong schema, permanent silent data
    loss for its duration, and no signal above debug level that it happened. The
    two sources agreeing is what removes the second of those.

    NOTE ON WHAT THIS TEST ASSERTS: source text, not live schema state. That is
    deliberate — an assertion about the resulting schema would start passing the
    moment someone restarted the container, which is exactly the wrong time to go
    quiet.
    """

    def test_the_self_heal_claims_the_same_type_as_the_migrations(self):
        alembic, _u = alembic_types()
        selfheal, _u2 = selfheal_types()
        disagreements = sorted(
            (k, selfheal[k], alembic[k]) for k in selfheal
            if k in alembic and selfheal[k] != alembic[k])
        assert disagreements == [], "\n".join(
            f"  {t}.{c}: database.py says {s}, migrations say {a}"
            for (t, c), s, a in disagreements)


# ── the guard itself ─────────────────────────────────────────────────────────

class TestTheGuardCatchesWhatShipped:
    """Feed the guard the real schema that shipped the wrong numbers."""

    def setup_method(self):
        self.found = _findings_for(SCHEMA_AT_0006)

    def test_it_flags_the_column_that_truncated_the_customer_figures(self):
        f = self.found[("items", "customs_value_mmk")]
        assert f["severity"] == "error"
        assert f["actual"] == "real"
        assert "19,363,898" in f["detail"]

    @pytest.mark.parametrize("column",
                             ["exchange_rate_min", "exchange_rate_max",
                              "exchange_rate_avg"])
    def test_it_flags_the_rate_columns_the_inert_edit_never_fixed(self, column):
        assert self.found[("importer_profiles", column)]["severity"] == "error"

    def test_it_flags_the_items_columns_that_were_holding_numbers_as_text(self):
        for column in ("quantity", "invoice_unit_price", "cif_unit_price",
                       "exchange_rate"):
            f = self.found[("items", column)]
            assert f["severity"] == "error"
            assert f["actual"] == "text"

    def test_it_flags_every_json_column_still_typed_text(self):
        json_findings = [k for k, f in self.found.items()
                         if f["expected"] == "jsonb"]
        assert len(json_findings) == 11

    def test_it_leaves_the_money_columns_0006_already_fixed_alone(self):
        # A guard that also shouted about the parts that were fixed would train
        # people to ignore it. The two `declarations` *_json columns ARE still
        # flagged, correctly — 0006 only touched the money columns.
        money_on_declarations = [k for k in self.found
                                 if k[0] == "declarations"
                                 and k in db_engine.MONEY_COLUMNS]
        assert money_on_declarations == []

    def test_it_does_not_shout_about_deliberate_real_columns(self):
        for key in db_engine.TOLERATED_REAL_COLUMNS:
            assert key not in self.found

    def test_a_hard_error_is_distinguishable_from_a_hint(self):
        severities = {f["severity"] for f in self.found.values()}
        assert severities == {"error"}


class TestTheGuardIsQuietOnACorrectSchema:
    def test_the_post_0007_schema_produces_no_findings(self):
        fixed = dict(SCHEMA_AT_0006)
        for key, value in list(fixed.items()):
            if key in db_engine.MONEY_COLUMNS:
                fixed[key] = "numeric"
            elif key[1].endswith("_json") and value == "text":
                fixed[key] = "jsonb"
        assert db_engine.find_schema_drift(fixed) == []

    def test_double_precision_is_accepted_as_well_as_numeric(self):
        fixed = {k: "double precision" for k in db_engine.MONEY_COLUMNS}
        assert db_engine.find_schema_drift(fixed) == []


class TestTheGuardGeneralisesBeyondItsOwnList:
    """The next instance of this defect will be in a column nobody listed."""

    def test_a_brand_new_json_column_left_as_text_is_caught(self):
        types = {("some_new_table", "id"): "integer",
                 ("some_new_table", "payload_json"): "text"}
        findings = _findings_for(types)
        assert findings[("some_new_table", "payload_json")]["severity"] == "error"

    def test_a_brand_new_real_column_is_reported_as_unclassified(self):
        types = {("some_new_table", "amount_paid"): "real"}
        f = _findings_for(types)[("some_new_table", "amount_paid")]
        # A warning, not an error: the guard cannot know whether it is money.
        # What it can do is refuse to let it pass unremarked.
        assert f["severity"] == "warning"

    def test_a_money_column_that_disappears_is_reported(self):
        # A rename that misses a call site looks exactly like this.
        types = {k: "numeric" for k in db_engine.MONEY_COLUMNS}
        del types[("items", "customs_value_mmk")]
        f = _findings_for(types)[("items", "customs_value_mmk")]
        assert f["severity"] == "error"
        assert f["actual"] is None

    def test_a_table_that_does_not_exist_yet_is_not_drift(self):
        # A young database that has never created `importer_profiles` is not
        # drifted, and a guard that cried about it would be ignored.
        types = {k: "numeric" for k in db_engine.MONEY_COLUMNS
                 if k[0] != "importer_profiles"}
        assert db_engine.find_schema_drift(types) == []


class TestTheGuardCannotBreakBoot:
    """It runs on the boot path, so failing to check must not fail the boot."""

    def test_a_broken_connection_yields_no_findings_and_no_exception(self):
        class Exploding:
            def cursor(self):
                raise RuntimeError("connection is gone")

        assert db_engine.check_schema_drift(Exploding()) == []

    def test_and_the_status_says_unknown_rather_than_ok(self):
        class Exploding:
            def cursor(self):
                raise RuntimeError("connection is gone")

        db_engine.check_schema_drift(Exploding())
        status = db_engine.schema_drift_status()
        # "unknown" is the whole point. A check that could not run reporting
        # "ok" is worse than no check at all.
        assert status["status"] == "unknown"
        assert "connection is gone" in status["error"]

    def test_logging_a_drift_report_does_not_raise(self):
        db_engine._log_schema_drift(db_engine.find_schema_drift(SCHEMA_AT_0006))

    def test_the_strict_variant_does_raise_because_it_is_for_ci(self):
        with pytest.raises(db_engine.SchemaDriftError) as exc:
            db_engine.assert_no_schema_drift(_FakeConn(SCHEMA_AT_0006))
        assert "items.customs_value_mmk" in str(exc.value)

    def test_the_strict_variant_is_silent_on_a_correct_schema(self):
        fixed = {k: "numeric" for k in db_engine.MONEY_COLUMNS}
        db_engine.assert_no_schema_drift(_FakeConn(fixed))

    def test_the_strict_variant_raises_when_it_could_not_look(self):
        # Same principle as the boot path: not being able to check is not a pass.
        class Exploding:
            def cursor(self):
                raise RuntimeError("connection is gone")

        with pytest.raises(db_engine.SchemaDriftError):
            db_engine.assert_no_schema_drift(Exploding())


class TestTheHealthPayloadIsUsable:
    def test_it_counts_errors_and_warnings_separately(self):
        db_engine._drift_findings = db_engine.find_schema_drift(SCHEMA_AT_0006)
        db_engine._drift_error = None
        status = db_engine.schema_drift_status()
        assert status["errors"] == len(SCHEMA_AT_0006_ERROR_KEYS)
        assert status["warnings"] == 0

    def test_a_clean_check_reports_ok(self):
        db_engine._drift_findings = []
        db_engine._drift_error = None
        assert db_engine.schema_drift_status()["status"] == "ok"


SCHEMA_AT_0006_ERROR_KEYS = [
    f for f in db_engine.find_schema_drift(SCHEMA_AT_0006)
    if f["severity"] == "error"
]


# ── the live database ────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def live_conn():
    from tests.test_numeric_precision_roundtrip import _live_conn
    conn = _live_conn()
    if conn is None:
        pytest.skip("no database reachable (set RO_ED_TEST_DB or DATABASE_URL)")
    yield conn
    try:
        conn.rollback()
        conn.close()
    except Exception:
        pass


class TestTheLiveSchema:
    """The only layer that can tell a written migration from an applied one."""

    def test_the_live_schema_satisfies_the_contract(self, live_conn):
        db_engine.assert_no_schema_drift(live_conn)

    def test_the_live_schema_matches_what_the_ddl_sources_say(self, live_conn):
        """Catches an un-applied migration, and a column no source accounts for.

        Limited to the columns the contract cares about: a long-lived database
        legitimately carries columns from before this schema was written down,
        and diffing all ~290 of them would report history rather than drift.
        """
        live = db_engine._live_column_types(live_conn)
        declared, _unparsed = effective_types()
        interesting = (set(db_engine.MONEY_COLUMNS)
                       | {k for k in declared if k[1].endswith("_json")})
        # Compare BASE types on both sides. `_live_column_types` reads
        # `format_type`, so it carries the typmod (`numeric(24,10)`), while the
        # DDL parser normalises every width away to `numeric` — so comparing the
        # spellings directly reported all eleven widened columns as drift on a
        # correctly-migrated database, every run. The width has its own live
        # guard (`test_the_declared_width_survives_in_the_database`, driven by
        # MONEY_COLUMN_WIDTHS); this one asks whether the migration was applied
        # at all.
        mismatches = sorted(
            (k, db_engine._base_type(declared.get(k)),
             db_engine._base_type(live.get(k)))
            for k in interesting
            if k in live
            and db_engine._base_type(declared.get(k))
            != db_engine._base_type(live.get(k)))
        assert mismatches == [], "\n".join(
            f"  {t}.{c}: the DDL says {d}, the database has {l}"
            for (t, c), d, l in mismatches)
