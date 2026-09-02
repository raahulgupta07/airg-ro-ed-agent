"""Field registry — the single source of truth for what a declaration/item field IS.

Today the definition of one field is written down in six places that are free to
disagree, and have:

  1. the extraction prompt        `rover/single_agent.py` `_PROMPT`/`_COLS`, `rover/products.py`
  2. the engine->DB bridge        `v11/workflow.py` `_run_rover`
  3. the save whitelist           `v11/workflow.py` `_save_to_db` `db_decl` / `db_items`
  4. the DB write                 `database.py` `save_declarations` / `save_items`
  5. two hand-written exporters   `routes/jobs.py` `download_job_excel`,
                                  `routes/data.py` bulk writers
  6. the migrations               `alembic/versions/*`

Nothing reconciles them, so a field can be extracted, stored, shown on screen and
still be absent from the customer's workbook; or keep its column, its type and its
tests while quietly changing what the number MEANS. Both have happened:

  * `invoice_price` silently moved from the invoice currency to MMK. A float column
    got a valid float, and the CIF gate reads `invoice_price_fc` first, so the
    arithmetic still closed. Score fell 54/60 -> 44/60 with nothing red anywhere.
  * `customs_value_mmk` on items was filled from the invoice-currency 'Item value'
    (~58x wrong on a THB document) and shipped to the customer.
  * `freight`/`insurance`/`adjustment` were extracted, stored, editable and on
    screen, yet missing from every export because two writer dicts were never
    updated.
  * `items.customs_value_mmk` carried `default=0.0`, making "could not read" look
    identical to "the form says zero"; the item-sum gate then reported a 100%
    shortfall on a document where nothing was missing.

So the registry carries the two things a column definition normally does not:
the **unit** and the **rule that an unread value is NULL**. `type` alone cannot
tell MMK from THB, and `NOT NULL DEFAULT 0` cannot tell blank from zero.

This module is DECLARATIVE and INERT. It imports nothing from the app, has no
side effects, and does not currently drive any behaviour. Consumers will be
migrated onto it incrementally — prompt generation, the bridge, the whitelist,
the writers and a migration check, in that order — so that adding a field becomes
one edit here instead of six edits that can silently disagree.

Nothing below describes what SHOULD be built. Every name, alias, header, order
and type was read out of the code listed above; where the sources contradict each
other the registry records the contradiction in `note` rather than picking a
winner, and `db_type_actual` records what Postgres really holds today next to the
`type` the field was meant to have.

Two key spaces, both live
-------------------------
Engines emit the RAW schema names (`customs_duty`, `commercial_tax`,
`advance_income_tax`, `security_fee`, `maccs_service_fee`, `exemption`,
`commercial_tax_pct`, `origin`, ...). The Phase-4 merge alias map in
`v11/workflow.py` rewrites them to the DB names (`import_export_customs_duty`,
`commercial_tax_ct`, ...). `reconcile()` runs on BOTH — pre-merge on the raw dict
inside `_call_typed`/`scribe.run`, and again post-merge in Phase 4.4 — so any gate
that knows only one spelling silently fails on every call in the other space.
That is exactly how the Presto fast-path once failed its own tax-completeness gate
on every run and fell back to full V7, paying for both.

Both spellings resolve here: the raw names live in `Field.engine_keys`, the DB
names in `Field.name`, and `resolve()` accepts either. `MERGE_ALIAS_MAP` is
derived from the registry and mirrors the map in `workflow.py`.

Usage
-----
    import fields
    fields.validate()                          # self-check, raises RegistryError
    f = fields.resolve("customs_duty")         # raw engine name -> the DB field
    f.name, f.unit, f.type                     # 'import_export_customs_duty', 'MMK', 'decimal'
    fields.export_headers("declarations")      # the customer's 23 columns, in order
"""
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

__all__ = [
    "Field", "RegistryError", "AmbiguousFieldName",
    "ALL", "DECLARATION_FIELDS", "ITEM_FIELDS", "ORPHANS",
    "TABLES", "UNITS", "TYPES", "ROLES", "SOURCES",
    "MERGE_ALIAS_MAP", "UNIT_NAME_CONFLICTS",
    "get", "resolve", "for_table", "data_fields", "money_fields",
    "export_columns", "export_headers", "unit_distribution",
    "coverage_gaps", "validate",
]

# ── Tables ───────────────────────────────────────────────────────────────────
DECLARATIONS = "declarations"
ITEMS = "items"
TABLES = (DECLARATIONS, ITEMS)

# ── Units ────────────────────────────────────────────────────────────────────
# The point of the whole exercise. `invoice_price` losing its unit was invisible
# to the type system, to the tests and to the one arithmetic gate that could have
# caught it.
UNIT_MMK = "MMK"                    # Myanmar kyat, the assessed/settlement currency
UNIT_FC = "invoice_currency"        # the declaration's own invoice currency (THB/USD/...)
UNIT_RATE = "rate"                  # invoice currency -> MMK, as printed on the form
UNIT_FRACTION = "fraction"          # 0.15 meaning 15%
UNIT_PERCENT = "percent"            # 15 meaning 15% — defined, not currently used
UNIT_COUNT = "count"                # a quantity of goods (may be fractional: "583.2 KG")
UNIT_DATE = "date"                  # calendar date, stored ISO yyyy-mm-dd
UNIT_TEXT = "text"                  # free text
UNIT_CCY = "currency_code"          # an ISO-ish currency code, not an amount
UNIT_ID = "id"                      # an identifier: never coerce to a number
UNITS = (UNIT_MMK, UNIT_FC, UNIT_RATE, UNIT_FRACTION, UNIT_PERCENT, UNIT_COUNT,
         UNIT_DATE, UNIT_TEXT, UNIT_CCY, UNIT_ID)

MONEY_UNITS = (UNIT_MMK, UNIT_FC)

# ── Storage types (INTENDED) ─────────────────────────────────────────────────
# Money is `decimal` — an exact decimal, not a float. `db_type_actual` records
# what is really there; migration 0006 widened the declaration money columns from
# `real` to `double precision` after `real` (about 7 significant digits) had been
# rounding 46,487,178.29 to 46,487,180.0 on every write. `double precision` buys
# headroom, it is still binary floating point.
TYPE_DECIMAL = "decimal"
TYPE_TEXT = "text"
TYPE_DATE = "date"
TYPE_INTEGER = "integer"
TYPE_TIMESTAMP = "timestamp"
TYPE_JSON = "json"
TYPES = (TYPE_DECIMAL, TYPE_TEXT, TYPE_DATE, TYPE_INTEGER, TYPE_TIMESTAMP, TYPE_JSON)

# ── Roles ────────────────────────────────────────────────────────────────────
ROLE_DATA = "data"          # read off the customs document
ROLE_IDENTITY = "identity"  # keys / joins
ROLE_META = "metadata"      # pipeline bookkeeping, not a reading
ROLES = (ROLE_DATA, ROLE_IDENTITY, ROLE_META)

# ── Sources: the six places (ten concrete sites) a field has to be written ───
SRC_ROVER_PROMPT = "rover_prompt"      # rover/single_agent.py _PROMPT+_COLS, rover/products.py
SRC_ROVER_SCHEMA = "rover_schema"      # rover/schema.py COLUMNS
SRC_PRESTO_SCHEMA = "presto_schema"    # v11/presto_schema.py (Presto/Scribe key space)
SRC_BRIDGE = "bridge"                  # v11/workflow.py _run_rover
SRC_WHITELIST = "whitelist"            # v11/workflow.py _save_to_db db_decl / db_items
SRC_DB_WRITE = "db_write"              # database.py save_declarations / save_items
SRC_MIGRATION = "migration"            # alembic/versions/*
SRC_SELF_HEAL = "self_heal"            # database.py init_database ALTER ... IF NOT EXISTS
SRC_EXPORT_JOB = "export_job"          # routes/jobs.py download_job_excel
SRC_EXPORT_BULK = "export_bulk"        # routes/data.py bulk writers
SOURCES = (SRC_ROVER_PROMPT, SRC_ROVER_SCHEMA, SRC_PRESTO_SCHEMA, SRC_BRIDGE,
           SRC_WHITELIST, SRC_DB_WRITE, SRC_MIGRATION, SRC_SELF_HEAL,
           SRC_EXPORT_JOB, SRC_EXPORT_BULK)

# A value that reaches the DB must pass through all three of these. Anything a
# `coverage_gaps()` caller sees missing here is a field that cannot be persisted
# no matter how well it extracts.
CORE_CHAIN = (SRC_WHITELIST, SRC_DB_WRITE)

# An unread value is NULL. Never 0.0, never "". `0` is a reading — Commercial Tax
# is genuinely zero on many declarations — and a default erases the difference
# between "the form says zero" and "we could not read it". Enforced by validate().
DEFAULT_ON_UNREAD = None


class RegistryError(Exception):
    """The registry contradicts itself. Carries every problem, not just the first."""


class AmbiguousFieldName(KeyError):
    """A bare name that exists on both tables was looked up without a table."""


@dataclass(frozen=True)
class Field:
    name: str                       # the DB column
    table: str                      # declarations | items
    meaning: str                    # one line, precise enough to generate prompt text
    unit: str                       # see UNITS — the reason this registry exists
    type: str                       # INTENDED storage type
    nullable: bool = True
    export_header: Optional[str] = None   # exact title in the customer's workbook
    export_order: Optional[int] = None    # 1-based position within that sheet
    aliases: Tuple[str, ...] = ()         # other spellings already in the codebase
    engine_keys: Tuple[str, ...] = ()     # RAW engine names (the pre-merge key space)
    present_in: Tuple[str, ...] = ()      # which of SOURCES actually carries it today
    role: str = ROLE_DATA
    db_type_actual: str = ""              # what Postgres holds now, if it differs
    default_on_unread: Optional[object] = None
    note: str = ""

    @property
    def key(self) -> str:
        return f"{self.table}.{self.name}"

    @property
    def is_money(self) -> bool:
        return self.unit in MONEY_UNITS

    @property
    def is_exported(self) -> bool:
        return self.export_header is not None

    def all_keys(self) -> Tuple[str, ...]:
        return (self.name,) + tuple(self.aliases) + tuple(self.engine_keys)

    def missing_sources(self, *required: str) -> Tuple[str, ...]:
        return tuple(s for s in (required or SOURCES) if s not in self.present_in)


# ─────────────────────────────────────────────────────────────────────────────
# Names that do not carry their own unit.
#
# This is the landmine list, enumerated rather than left silent. `validate()`
# requires every data field whose name does not disambiguate its unit to appear
# here, so a NEW ambiguous field cannot be added without a deliberate edit to
# this set. It is not permission — it is the backlog of renames.
# ─────────────────────────────────────────────────────────────────────────────
UNIT_NAME_CONFLICTS = frozenset({
    # declarations — invoice currency, named as though the unit were obvious
    "declarations.invoice_price",          # THE regression: read as MMK for one release
    "declarations.freight_value",
    "declarations.insurance_value",
    "declarations.adjustment_value",
    # declarations — MMK, named as though the unit were obvious
    "declarations.total_customs_value",
    "declarations.import_export_customs_duty",
    "declarations.commercial_tax_ct",
    "declarations.advance_income_tax_at",
    "declarations.security_fee_sf",
    "declarations.maccs_service_fee_mf",
    "declarations.exemption_reduction",
    # items
    "items.invoice_unit_price",
    "items.cif_unit_price",
    "items.customs_duty_rate",             # says "rate", holds a fraction (0.15)
    "items.commercial_tax_percent",        # says "percent", holds a fraction (0.05)
})


# ═════════════════════════════════════════════════════════════════════════════
# DECLARATIONS
# ═════════════════════════════════════════════════════════════════════════════
DECLARATION_FIELDS: List[Field] = [
    Field(
        name="job_id", table=DECLARATIONS, role=ROLE_IDENTITY,
        meaning="Extraction job this declaration belongs to.",
        unit=UNIT_ID, type=TYPE_TEXT, nullable=False,
        export_header="Job", export_order=1,
        db_type_actual="text",
        present_in=(SRC_DB_WRITE, SRC_MIGRATION, SRC_EXPORT_JOB, SRC_EXPORT_BULK),
    ),
    Field(
        name="declaration_no", table=DECLARATIONS,
        meaning="Customs declaration number — the top-of-form 'Declaration No.' ONLY. 'First approval declaration No.' is a different, earlier declaration.",
        unit=UNIT_ID, type=TYPE_TEXT,
        export_header="Declaration No", export_order=2,
        aliases=("Declaration No",),
        db_type_actual="text",
        present_in=(SRC_ROVER_PROMPT, SRC_ROVER_SCHEMA, SRC_PRESTO_SCHEMA, SRC_BRIDGE,
                    SRC_WHITELIST, SRC_DB_WRITE, SRC_MIGRATION,
                    SRC_EXPORT_JOB, SRC_EXPORT_BULK),
        note="Holds a slash on MA-series docs, so it must never go through a numeric "
             "coercion that strips non-digits.",
    ),
    Field(
        name="declaration_date", table=DECLARATIONS,
        meaning="The form's 'Declaration date' — when the declaration was registered.",
        unit=UNIT_DATE, type=TYPE_DATE,
        export_header="Declaration Date", export_order=3,
        aliases=("Declaration Date",),
        db_type_actual="text",
        present_in=(SRC_ROVER_PROMPT, SRC_ROVER_SCHEMA, SRC_PRESTO_SCHEMA, SRC_BRIDGE,
                    SRC_WHITELIST, SRC_DB_WRITE, SRC_MIGRATION,
                    SRC_EXPORT_JOB, SRC_EXPORT_BULK),
        note="Stored as TEXT and normalised to ISO by dates.normalise at save time; the "
             "column held '2025-06-25', '2024/04/01' and '12/10/2025' at once before that.",
    ),
    Field(
        name="arrival_date", table=DECLARATIONS,
        meaning="Ship arrival date from the page-1 header block, near B/L and Conveyance.",
        unit=UNIT_DATE, type=TYPE_DATE,
        export_header=None,
        aliases=("Arrival Date",),
        db_type_actual="text",
        present_in=(SRC_ROVER_PROMPT, SRC_ROVER_SCHEMA, SRC_BRIDGE,
                    SRC_WHITELIST, SRC_DB_WRITE, SRC_SELF_HEAL),
        note="Deliberately not exported (team's sheet has no column for it). Exists only via "
             "the self-heal ALTER in database.py — no alembic migration creates it, so a "
             "database built by `alembic upgrade head` alone does not have this column.",
    ),
    Field(
        name="release_order_date", table=DECLARATIONS,
        meaning="'Release order' date from the customs-decision block on the last form page. "
                "This is the date the team's own ledger keys on ('RO/ID Date').",
        unit=UNIT_DATE, type=TYPE_DATE,
        export_header=None,
        aliases=("Release Order Date",),
        db_type_actual="text",
        present_in=(SRC_ROVER_PROMPT, SRC_ROVER_SCHEMA, SRC_BRIDGE,
                    SRC_WHITELIST, SRC_DB_WRITE, SRC_SELF_HEAL),
        note="Not the page title 'Release order notification'. Not exported, even though it "
             "is the date the customer reconciles against. Self-heal only, no migration.",
    ),
    Field(
        name="completion_date", table=DECLARATIONS,
        meaning="'Declaration completion' date from the customs-decision block.",
        unit=UNIT_DATE, type=TYPE_DATE,
        export_header=None,
        aliases=("Completion Date",),
        db_type_actual="text",
        present_in=(SRC_ROVER_PROMPT, SRC_ROVER_SCHEMA, SRC_BRIDGE,
                    SRC_WHITELIST, SRC_DB_WRITE, SRC_SELF_HEAL),
        note="Self-heal only, no migration.",
    ),
    Field(
        name="importer_name", table=DECLARATIONS,
        meaning="The Importer line on the header block.",
        unit=UNIT_TEXT, type=TYPE_TEXT,
        export_header="Importer (Name)", export_order=4,
        aliases=("Importer (Name)",),
        db_type_actual="text",
        present_in=(SRC_ROVER_PROMPT, SRC_ROVER_SCHEMA, SRC_PRESTO_SCHEMA, SRC_BRIDGE,
                    SRC_WHITELIST, SRC_DB_WRITE, SRC_MIGRATION,
                    SRC_EXPORT_JOB, SRC_EXPORT_BULK),
    ),
    Field(
        name="consignor_name", table=DECLARATIONS,
        meaning="The 'Consignor' — the overseas sender/exporter. Not the importer and not "
                "the customs agency.",
        unit=UNIT_TEXT, type=TYPE_TEXT,
        export_header="Consignor (Name)", export_order=5,
        aliases=("Consignor (Name)",),
        db_type_actual="text",
        present_in=(SRC_ROVER_PROMPT, SRC_ROVER_SCHEMA, SRC_PRESTO_SCHEMA, SRC_BRIDGE,
                    SRC_WHITELIST, SRC_DB_WRITE, SRC_MIGRATION,
                    SRC_EXPORT_JOB, SRC_EXPORT_BULK),
    ),
    Field(
        name="invoice_number", table=DECLARATIONS,
        meaning="The form's 'Invoice' value, stored bare — the bridge strips a leading "
                "'A-'/'INV-' prefix because the team's ledger books the core number.",
        unit=UNIT_ID, type=TYPE_TEXT,
        export_header="Invoice Number", export_order=6,
        aliases=("Invoice Number", "invoice_no"),
        db_type_actual="text",
        present_in=(SRC_ROVER_PROMPT, SRC_ROVER_SCHEMA, SRC_PRESTO_SCHEMA, SRC_BRIDGE,
                    SRC_WHITELIST, SRC_DB_WRITE, SRC_MIGRATION,
                    SRC_EXPORT_JOB, SRC_EXPORT_BULK),
        note="`invoice_no` is a wrong spelling that has bitten before (issues.py) — it is "
             "recorded as an alias so a lookup resolves rather than silently missing. Values "
             "like 'A- 9518633846' must never reach a loose numeric parser.",
    ),
    Field(
        name="invoice_number_customs_declaration", table=DECLARATIONS,
        meaning="The invoice reference as printed on the customs declaration (the 'A-'/'AM-' form).",
        unit=UNIT_ID, type=TYPE_TEXT,
        export_header="Invoice Number (Customs Declaration)", export_order=7,
        aliases=("Invoice Number (Customs Declaration)",),
        engine_keys=("invoice_number_customs",),
        db_type_actual="text",
        present_in=(SRC_PRESTO_SCHEMA, SRC_WHITELIST, SRC_DB_WRITE,
                    SRC_MIGRATION, SRC_SELF_HEAL, SRC_EXPORT_JOB, SRC_EXPORT_BULK),
        note="An exported column that ROVER/ROSETTA never produce — no prompt asks for it and "
             "the bridge does not map it, so it is NULL on every rover-engine job.",
    ),
    Field(
        name="invoice_number_commercial_invoice", table=DECLARATIONS,
        meaning="The exporter's own commercial invoice number (e.g. 'EX25003MM', 'PD001').",
        unit=UNIT_ID, type=TYPE_TEXT,
        export_header="Invoice Number (Commercial Invoice)", export_order=8,
        aliases=("Invoice Number (Commercial Invoice)",),
        engine_keys=("invoice_number_commercial",),
        db_type_actual="text",
        present_in=(SRC_PRESTO_SCHEMA, SRC_WHITELIST, SRC_DB_WRITE,
                    SRC_MIGRATION, SRC_SELF_HEAL, SRC_EXPORT_JOB, SRC_EXPORT_BULK),
        note="Same gap as the customs-declaration reference: exported, never extracted by the "
             "rover engines. The UAT found the ledger keys on THIS number while the model "
             "returns the form's 'A-' reference.",
    ),
    Field(
        name="invoice_price", table=DECLARATIONS,
        meaning="Total invoice amount IN THE INVOICE CURRENCY (the team's ledger column, both "
                "Excel writers, and the signed Beta v3 requirement form all read it this way).",
        unit=UNIT_FC, type=TYPE_DECIMAL,
        export_header="Invoice Price", export_order=9,
        aliases=("Invoice Price",),
        db_type_actual="double precision",
        present_in=(SRC_PRESTO_SCHEMA, SRC_BRIDGE, SRC_WHITELIST, SRC_DB_WRITE,
                    SRC_MIGRATION, SRC_EXPORT_JOB, SRC_EXPORT_BULK),
        note="THE unit regression. Bridged from `invoice_price_mmk` for one release; nothing "
             "failed because a float column got a valid float and the CIF gate reads "
             "`invoice_price_fc` first. The bridge now derives it via "
             "`workflow.invoice_price_fields`: fc when present, else mmk — which means on a "
             "document printing only an MMK figure this column IS in MMK. The unit is "
             "data-dependent; that is the remaining hazard, and the reason "
             "`invoice_price_fc`/`invoice_price_mmk` exist. `database.save_declarations` "
             "writes 0.0 here when unread, violating DEFAULT_ON_UNREAD.",
    ),
    Field(
        name="invoice_price_fc", table=DECLARATIONS,
        meaning="'Invoice price' as printed on the foreign-currency line (e.g. 'THB 1,118,431.80').",
        unit=UNIT_FC, type=TYPE_DECIMAL,
        export_header=None,
        aliases=("Invoice Price (FC)",),
        db_type_actual="double precision",
        present_in=(SRC_ROVER_PROMPT, SRC_ROVER_SCHEMA, SRC_BRIDGE, SRC_WHITELIST,
                    SRC_DB_WRITE, SRC_SELF_HEAL),
        note="The CIF gate's FIRST basis choice, so a wrong value here disables the one check "
             "that could catch a wrong invoice. Not exported by request; self-heal only, no "
             "migration; Presto/Scribe do not emit it.",
    ),
    Field(
        name="invoice_price_mmk", table=DECLARATIONS,
        meaning="'Invoice price' as printed on the (MMK) line — the accountant's 'Total Value'.",
        unit=UNIT_MMK, type=TYPE_DECIMAL,
        export_header=None,
        aliases=("Invoice Price (MMK)", "INVOICE PRICE (MMK)"),
        db_type_actual="double precision",
        present_in=(SRC_ROVER_PROMPT, SRC_ROVER_SCHEMA, SRC_BRIDGE, SRC_WHITELIST,
                    SRC_DB_WRITE, SRC_SELF_HEAL),
        note="Added so the kyat figure stops displacing `invoice_price`. 'INVOICE PRICE (MMK)' "
             "is the header `rover/mapping.py` writes into the standalone accountant sheet — a "
             "seventh key space that books the MMK figure where the workbook books FC.",
    ),
    Field(
        name="currency", table=DECLARATIONS,
        meaning="The INVOICE currency (USD/THB/...) taken from 'Invoice price' / "
                "'Exchange Rate (n) <CCY>'. Not the consignor's country.",
        unit=UNIT_CCY, type=TYPE_TEXT,
        export_header="Currency", export_order=13,
        aliases=("Currency",),
        db_type_actual="text",
        present_in=(SRC_ROVER_PROMPT, SRC_ROVER_SCHEMA, SRC_PRESTO_SCHEMA, SRC_BRIDGE,
                    SRC_WHITELIST, SRC_DB_WRITE, SRC_MIGRATION,
                    SRC_EXPORT_JOB, SRC_EXPORT_BULK),
        note="Selects the per-currency exchange-rate plausibility band in reconcile.py, so a "
             "wrong code disables the primary rate check.",
    ),
    Field(
        name="currency_2", table=DECLARATIONS,
        meaning="Second currency column on the V7 declaration form.",
        unit=UNIT_CCY, type=TYPE_TEXT,
        export_header="Currency 2", export_order=15,
        aliases=("Currency 2",),
        db_type_actual="text",
        present_in=(SRC_PRESTO_SCHEMA, SRC_WHITELIST, SRC_DB_WRITE, SRC_MIGRATION,
                    SRC_EXPORT_JOB, SRC_EXPORT_BULK),
        note="No prompt asks for it. `save_declarations` defaults it to `currency`, so the "
             "exported column is a copy of column 13 on every rover-engine job.",
    ),
    Field(
        name="exchange_rate", table=DECLARATIONS,
        meaning="The PRINTED 'Exchange Rate (1)' — invoice currency to MMK, read verbatim, "
                "never computed.",
        unit=UNIT_RATE, type=TYPE_DECIMAL,
        export_header="Exchange Rate", export_order=14,
        aliases=("Exchange Rate",),
        db_type_actual="double precision",
        present_in=(SRC_ROVER_PROMPT, SRC_ROVER_SCHEMA, SRC_PRESTO_SCHEMA, SRC_BRIDGE,
                    SRC_WHITELIST, SRC_DB_WRITE, SRC_MIGRATION,
                    SRC_EXPORT_JOB, SRC_EXPORT_BULK),
        note="`real` rounded 67.2133333 to 67.21333 until migration 0006. "
             "`save_declarations` writes 0.0 when unread, violating DEFAULT_ON_UNREAD — and a "
             "rate of 0 is not a rate.",
    ),
    Field(
        name="freight_value", table=DECLARATIONS,
        meaning="'Freight' from the OGA / valuation block — part of the CIF build-up.",
        unit=UNIT_FC, type=TYPE_DECIMAL,
        export_header="Freight", export_order=10,
        aliases=("Freight",),
        db_type_actual="double precision",
        present_in=(SRC_ROVER_PROMPT, SRC_ROVER_SCHEMA, SRC_PRESTO_SCHEMA, SRC_BRIDGE,
                    SRC_WHITELIST, SRC_DB_WRITE, SRC_MIGRATION, SRC_SELF_HEAL,
                    SRC_EXPORT_JOB, SRC_EXPORT_BULK),
        note="Presence (even at 0.0) tightens the CIF tolerance from 15% to 4%, so a "
             "hallucinated value here does not just store a wrong number — it changes how "
             "strictly every other number is judged.",
    ),
    Field(
        name="insurance_value", table=DECLARATIONS,
        meaning="'Insurance' from the same valuation block — part of the CIF build-up.",
        unit=UNIT_FC, type=TYPE_DECIMAL,
        export_header="Insurance", export_order=11,
        aliases=("Insurance",),
        db_type_actual="double precision",
        present_in=(SRC_ROVER_PROMPT, SRC_ROVER_SCHEMA, SRC_PRESTO_SCHEMA, SRC_BRIDGE,
                    SRC_WHITELIST, SRC_DB_WRITE, SRC_MIGRATION, SRC_SELF_HEAL,
                    SRC_EXPORT_JOB, SRC_EXPORT_BULK),
    ),
    Field(
        name="adjustment_value", table=DECLARATIONS,
        meaning="The 'Adjustment value' MONEY amount ('Adjustment value AD - <CCY> - <n>'), "
                "signed. Not the small 'Adjustment' code integer printed beside it.",
        unit=UNIT_FC, type=TYPE_DECIMAL,
        export_header="Adjustment", export_order=12,
        aliases=("Adjustment",),
        db_type_actual="double precision",
        present_in=(SRC_ROVER_PROMPT, SRC_ROVER_SCHEMA, SRC_PRESTO_SCHEMA, SRC_BRIDGE,
                    SRC_WHITELIST, SRC_DB_WRITE, SRC_MIGRATION, SRC_SELF_HEAL,
                    SRC_EXPORT_JOB, SRC_EXPORT_BULK),
        note="The alias 'Adjustment' is genuinely dangerous: on the release order that word "
             "labels the CODE integer (2). `_save_to_db` therefore consults it only when the "
             "real key is absent, never when it is present-and-null. A fabricated value here "
             "also closes the CIF identity — the P10 silent-ship.",
    ),
    Field(
        name="total_customs_value", table=DECLARATIONS,
        meaning="The form's 'Total customs value' in MMK — the assessed value, which may carry "
                "an uplift over the invoice. Returned as printed, never reconciled at read time.",
        unit=UNIT_MMK, type=TYPE_DECIMAL,
        export_header="Total Customs Value", export_order=16,
        aliases=("Total Customs Value",),
        db_type_actual="double precision",
        present_in=(SRC_ROVER_PROMPT, SRC_ROVER_SCHEMA, SRC_PRESTO_SCHEMA, SRC_BRIDGE,
                    SRC_WHITELIST, SRC_DB_WRITE, SRC_MIGRATION,
                    SRC_EXPORT_JOB, SRC_EXPORT_BULK),
        note="The reconcile anchor: total == sum of item customs values. `save_declarations` "
             "writes 0.0 when unread, and reconcile treats 0 as 'no anchor' — so an unread "
             "total silently disables the gate rather than failing it.",
    ),
    Field(
        name="import_export_customs_duty", table=DECLARATIONS,
        meaning="Customs Duty (CD) from its own row in 'Taxes and fees', MMK.",
        unit=UNIT_MMK, type=TYPE_DECIMAL,
        export_header="Import/Export Customs Duty", export_order=17,
        aliases=("Import/Export Customs Duty",),
        engine_keys=("customs_duty",),
        db_type_actual="double precision",
        present_in=(SRC_ROVER_PROMPT, SRC_ROVER_SCHEMA, SRC_PRESTO_SCHEMA, SRC_BRIDGE,
                    SRC_WHITELIST, SRC_DB_WRITE, SRC_MIGRATION,
                    SRC_EXPORT_JOB, SRC_EXPORT_BULK),
        note="Core tax: its absence (with all of CT/AT) trips the tax-completeness gate. "
             "`save_declarations` writes 0.0 when unread — which reads as 'the form says zero' "
             "and can mask a dropped tax block from the very gate built to catch it.",
    ),
    Field(
        name="commercial_tax_ct", table=DECLARATIONS,
        meaning="Commercial Tax (CT) from its own row in 'Taxes and fees', MMK.",
        unit=UNIT_MMK, type=TYPE_DECIMAL,
        export_header="Commercial Tax (CT)", export_order=18,
        aliases=("Commercial Tax (CT)",),
        engine_keys=("commercial_tax",),
        db_type_actual="double precision",
        present_in=(SRC_ROVER_PROMPT, SRC_ROVER_SCHEMA, SRC_PRESTO_SCHEMA, SRC_BRIDGE,
                    SRC_WHITELIST, SRC_DB_WRITE, SRC_MIGRATION,
                    SRC_EXPORT_JOB, SRC_EXPORT_BULK),
        note="Genuinely 0 on many declarations — the field that proved `a or b` cannot be used "
             "to pick a money value.",
    ),
    Field(
        name="advance_income_tax_at", table=DECLARATIONS,
        meaning="Advance Income Tax (AT) from its own row in 'Taxes and fees', MMK.",
        unit=UNIT_MMK, type=TYPE_DECIMAL,
        export_header="Advance Income Tax (AT)", export_order=19,
        aliases=("Advance Income Tax (AT)",),
        engine_keys=("advance_income_tax",),
        db_type_actual="double precision",
        present_in=(SRC_ROVER_PROMPT, SRC_ROVER_SCHEMA, SRC_PRESTO_SCHEMA, SRC_BRIDGE,
                    SRC_WHITELIST, SRC_DB_WRITE, SRC_MIGRATION,
                    SRC_EXPORT_JOB, SRC_EXPORT_BULK),
        note="UAT: read as 0 on P15/P16 where the ledger holds exactly 2% of the total.",
    ),
    Field(
        name="security_fee_sf", table=DECLARATIONS,
        meaning="The 'Security' fee (SF), MMK. A flat fee, typically 20,000.",
        unit=UNIT_MMK, type=TYPE_DECIMAL,
        export_header="Security Fee (SF)", export_order=20,
        aliases=("Security Fee (SF)",),
        engine_keys=("security_fee",),
        db_type_actual="double precision",
        present_in=(SRC_ROVER_PROMPT, SRC_ROVER_SCHEMA, SRC_PRESTO_SCHEMA, SRC_BRIDGE,
                    SRC_WHITELIST, SRC_DB_WRITE, SRC_MIGRATION,
                    SRC_EXPORT_JOB, SRC_EXPORT_BULK),
        note="Must stay separate from MACCS. The team's ledger line 'Security Fees' is "
             "security + MACCS SUMMED, which is why a naive comparison against it fails. "
             "Not a core tax for the completeness gate: being flat and near-always present, "
             "it used to satisfy an any()-over-all-taxes check while every real tax was NULL.",
    ),
    Field(
        name="maccs_service_fee_mf", table=DECLARATIONS,
        meaning="The 'MACCS SERVICE FEE' (MF), MMK. A flat fee, typically 30,000.",
        unit=UNIT_MMK, type=TYPE_DECIMAL,
        export_header="MACCS Service Fee (MF)", export_order=21,
        aliases=("MACCS Service Fee (MF)",),
        engine_keys=("maccs_service_fee",),
        db_type_actual="double precision",
        present_in=(SRC_ROVER_PROMPT, SRC_ROVER_SCHEMA, SRC_PRESTO_SCHEMA, SRC_BRIDGE,
                    SRC_WHITELIST, SRC_DB_WRITE, SRC_MIGRATION,
                    SRC_EXPORT_JOB, SRC_EXPORT_BULK),
        note="Never collapse with security_fee_sf.",
    ),
    Field(
        name="exemption_reduction", table=DECLARATIONS,
        meaning="The 'Exemption/Reduction' value, MMK. Not the 'Taxes and fees' total.",
        unit=UNIT_MMK, type=TYPE_DECIMAL,
        export_header="Exemption/Reduction", export_order=22,
        aliases=("Exemption/Reduction",),
        engine_keys=("exemption",),
        db_type_actual="double precision",
        present_in=(SRC_ROVER_PROMPT, SRC_ROVER_SCHEMA, SRC_PRESTO_SCHEMA, SRC_BRIDGE,
                    SRC_WHITELIST, SRC_DB_WRITE, SRC_MIGRATION,
                    SRC_EXPORT_JOB, SRC_EXPORT_BULK),
        note="The whitelist in `_save_to_db` keys on the RAW name 'exemption' and does not "
             "consult 'exemption_reduction' at all. An engine that emits only the DB spelling "
             "(which is what rover/schema.COLUMNS names it) would be dropped here; the bridge "
             "only survives because it writes BOTH keys.",
    ),
    # ── metadata ────────────────────────────────────────────────────────────
    Field(
        name="document_format", table=DECLARATIONS, role=ROLE_META,
        meaning="Self-reported form family: MACCS / CUSDEC / CUSDEC1.",
        unit=UNIT_TEXT, type=TYPE_TEXT,
        db_type_actual="text",
        present_in=(SRC_WHITELIST, SRC_DB_WRITE, SRC_MIGRATION, SRC_SELF_HEAL),
    ),
    Field(
        name="sanity_flags_json", table=DECLARATIONS, role=ROLE_META,
        meaning="JSON list of the suspect fields the math supervisor flagged.",
        unit=UNIT_TEXT, type=TYPE_JSON,
        db_type_actual="text",
        present_in=(SRC_WHITELIST, SRC_DB_WRITE, SRC_MIGRATION, SRC_SELF_HEAL),
        note="Written under the underscore key `_sanity_flags_json`, not the column name.",
    ),
    Field(
        name="cross_val_passed", table=DECLARATIONS, role=ROLE_META,
        meaning="1 when the reconcile gate balanced, 0 when it did not.",
        unit=UNIT_COUNT, type=TYPE_INTEGER,
        db_type_actual="integer",
        present_in=(SRC_WHITELIST, SRC_DB_WRITE, SRC_MIGRATION, SRC_SELF_HEAL),
        note="Written under the underscore key `_cross_val_passed`.",
    ),
    Field(
        name="verified", table=DECLARATIONS, role=ROLE_META,
        meaning="1 when a human confirmed the declaration.",
        unit=UNIT_COUNT, type=TYPE_INTEGER,
        db_type_actual="integer",
        present_in=(SRC_DB_WRITE, SRC_MIGRATION, SRC_SELF_HEAL),
    ),
    Field(
        name="evidence_json", table=DECLARATIONS, role=ROLE_META,
        meaning="Per-field ROVER Cell record: value, source text, confidence, model, page/bbox.",
        unit=UNIT_TEXT, type=TYPE_JSON,
        db_type_actual="text",
        present_in=(SRC_WHITELIST, SRC_DB_WRITE, SRC_MIGRATION, SRC_SELF_HEAL),
        note="Written under the underscore key `_evidence_json`. Computed on every rover run "
             "and dropped at the whitelist until 2026-07; the DB never saw it.",
    ),
    Field(
        name="is_valid", table=DECLARATIONS, role=ROLE_META,
        meaning="Legacy validity flag, defaults to 1.",
        unit=UNIT_COUNT, type=TYPE_INTEGER,
        db_type_actual="integer",
        present_in=(SRC_MIGRATION,),
    ),
    Field(
        name="created_at", table=DECLARATIONS, role=ROLE_META,
        meaning="When the row was written.",
        unit=UNIT_DATE, type=TYPE_TIMESTAMP,
        export_header="Processed", export_order=23,
        db_type_actual="timestamp",
        present_in=(SRC_MIGRATION, SRC_EXPORT_JOB, SRC_EXPORT_BULK),
        note="The per-job writer reads `created_at`; the bulk writer reads `job_created_at` "
             "(the JOB's timestamp) for the same 'Processed' column. Two different clocks "
             "under one header.",
    ),
]

# ═════════════════════════════════════════════════════════════════════════════
# ITEMS
# ═════════════════════════════════════════════════════════════════════════════
ITEM_FIELDS: List[Field] = [
    Field(
        name="job_id", table=ITEMS, role=ROLE_IDENTITY,
        meaning="Extraction job this line item belongs to.",
        unit=UNIT_ID, type=TYPE_TEXT, nullable=False,
        export_header="Job", export_order=1,
        db_type_actual="text",
        present_in=(SRC_DB_WRITE, SRC_MIGRATION, SRC_EXPORT_JOB, SRC_EXPORT_BULK),
    ),
    Field(
        name="item_name", table=ITEMS,
        meaning="The goods description for this row ('Item name' on the CUSDEC item block).",
        unit=UNIT_TEXT, type=TYPE_TEXT,
        export_header="Item Name", export_order=2,
        aliases=("Item name", "Item Name"),
        engine_keys=("description",),
        db_type_actual="text",
        present_in=(SRC_ROVER_PROMPT, SRC_PRESTO_SCHEMA, SRC_BRIDGE, SRC_WHITELIST,
                    SRC_DB_WRITE, SRC_MIGRATION, SRC_EXPORT_JOB, SRC_EXPORT_BULK),
        note="Part of the dedup key; exact normalised match, never substring.",
    ),
    Field(
        name="customs_duty_rate", table=ITEMS,
        meaning="Duty rate applied to this row, as a FRACTION (0.15 means 15%).",
        unit=UNIT_FRACTION, type=TYPE_DECIMAL,
        export_header="Customs Duty Rate", export_order=3,
        aliases=("Customs duty rate", "Customs Duty Rate"),
        db_type_actual="real",
        present_in=(SRC_PRESTO_SCHEMA, SRC_BRIDGE, SRC_WHITELIST, SRC_DB_WRITE,
                    SRC_MIGRATION, SRC_EXPORT_JOB, SRC_EXPORT_BULK),
        note="Name says 'rate', value is a fraction — see UNIT_NAME_CONFLICTS. The merge phase "
             "flags a MISSING rate (None) for review because 0 is genuine under Form D/FTA, "
             "yet `save_items` writes 0.0 when it is unread, erasing exactly that distinction. "
             "No rover prompt asks for it, so it is None on every rover-engine job — which "
             "also disables the duty closure check.",
    ),
    Field(
        name="quantity", table=ITEMS,
        meaning="Quantity of goods on this row, from the Quantity column — not the value column.",
        unit=UNIT_COUNT, type=TYPE_DECIMAL,
        export_header="Quantity (1)", export_order=4,
        aliases=("Quantity (1)",),
        db_type_actual="text",
        present_in=(SRC_ROVER_PROMPT, SRC_PRESTO_SCHEMA, SRC_BRIDGE, SRC_WHITELIST,
                    SRC_DB_WRITE, SRC_MIGRATION, SRC_EXPORT_JOB, SRC_EXPORT_BULK),
        note="Column is TEXT: Presto emits '108 KG' (unit kept inline) while ROVER emits a "
             "float. The unit of measure has no column of its own, so it survives only when "
             "it happens to be glued to the number.",
    ),
    Field(
        name="invoice_unit_price", table=ITEMS,
        meaning="'Invoice unit price' for this row, in the INVOICE currency.",
        unit=UNIT_FC, type=TYPE_DECIMAL,
        export_header="Invoice Unit Price", export_order=5,
        aliases=("Invoice unit price", "Invoice Unit Price"),
        engine_keys=("unit_price",),
        db_type_actual="text",
        present_in=(SRC_ROVER_PROMPT, SRC_PRESTO_SCHEMA, SRC_BRIDGE, SRC_WHITELIST,
                    SRC_DB_WRITE, SRC_MIGRATION, SRC_EXPORT_JOB, SRC_EXPORT_BULK),
        note="Stored in a TEXT column while the declaration money columns are double "
             "precision. The bridge derives it as row value / quantity when the form prints "
             "no unit price — a derived figure indistinguishable from a read one.",
    ),
    Field(
        name="cif_unit_price", table=ITEMS,
        meaning="CIF unit price for this row, in the INVOICE currency (invoice unit price plus "
                "the freight/insurance/adjustment share).",
        unit=UNIT_FC, type=TYPE_DECIMAL,
        export_header="CIF Unit Price", export_order=6,
        aliases=("CIF unit price", "CIF Unit Price"),
        db_type_actual="text",
        present_in=(SRC_PRESTO_SCHEMA, SRC_WHITELIST, SRC_DB_WRITE, SRC_MIGRATION,
                    SRC_SELF_HEAL, SRC_EXPORT_JOB, SRC_EXPORT_BULK),
        note="Exported but never produced by the rover engines: no prompt asks for it and the "
             "bridge does not map it. The per-row gate prefers it over invoice_unit_price, so "
             "on rover jobs that gate silently falls back.",
    ),
    Field(
        name="currency", table=ITEMS,
        meaning="Invoice currency for this row; inherited from the declaration when the row "
                "does not print one.",
        unit=UNIT_CCY, type=TYPE_TEXT,
        export_header="Currency", export_order=7,
        aliases=("Currency",),
        db_type_actual="text",
        present_in=(SRC_PRESTO_SCHEMA, SRC_BRIDGE, SRC_WHITELIST, SRC_DB_WRITE,
                    SRC_MIGRATION, SRC_EXPORT_JOB, SRC_EXPORT_BULK),
        note="`items` has no currency column in the 0001 DDL — the writers read it, the per-job "
             "export falls back to the declaration's currency, and the bulk export builds a "
             "per-job currency map instead of reading the row.",
    ),
    Field(
        name="commercial_tax_percent", table=ITEMS,
        meaning="Commercial tax applied to this row, as a FRACTION (0.05 means 5%).",
        unit=UNIT_FRACTION, type=TYPE_DECIMAL,
        export_header="Commercial Tax %", export_order=8,
        aliases=("Commercial tax %", "Commercial Tax %"),
        engine_keys=("commercial_tax_pct",),
        db_type_actual="real",
        present_in=(SRC_PRESTO_SCHEMA, SRC_WHITELIST, SRC_DB_WRITE, SRC_MIGRATION,
                    SRC_EXPORT_JOB, SRC_EXPORT_BULK),
        note="Column and header both say percent; Presto's schema comment says fraction. "
             "`save_items` writes 0.0 when unread. Not produced by the rover engines.",
    ),
    Field(
        name="exchange_rate", table=ITEMS,
        meaning="Exchange rate applied to this row — invoice currency to MMK. The bridge fills "
                "it from the declaration so the row is self-contained.",
        unit=UNIT_RATE, type=TYPE_DECIMAL,
        export_header="Exchange Rate (1)", export_order=9,
        aliases=("Exchange Rate (1)",),
        db_type_actual="text",
        present_in=(SRC_PRESTO_SCHEMA, SRC_BRIDGE, SRC_WHITELIST, SRC_DB_WRITE,
                    SRC_MIGRATION, SRC_EXPORT_JOB, SRC_EXPORT_BULK),
        note="TEXT column holding a rate the per-row gate multiplies by.",
    ),
    Field(
        name="hs_code", table=ITEMS,
        meaning="Harmonised System tariff code for this row, e.g. '8471.30.10'.",
        unit=UNIT_ID, type=TYPE_TEXT,
        export_header="HS Code", export_order=10,
        aliases=("HS Code",),
        db_type_actual="text",
        present_in=(SRC_ROVER_PROMPT, SRC_PRESTO_SCHEMA, SRC_BRIDGE, SRC_WHITELIST,
                    SRC_DB_WRITE, SRC_MIGRATION, SRC_SELF_HEAL,
                    SRC_EXPORT_JOB, SRC_EXPORT_BULK),
        note="Dotted, so it must never reach a numeric parser. Part of the dedup key.",
    ),
    Field(
        name="origin_country", table=ITEMS,
        meaning="Country of origin for this row (ISO-2, e.g. 'IT').",
        unit=UNIT_TEXT, type=TYPE_TEXT,
        export_header="Origin Country", export_order=11,
        aliases=("Origin Country",),
        engine_keys=("origin", "country_origin"),
        db_type_actual="text",
        present_in=(SRC_PRESTO_SCHEMA, SRC_BRIDGE, SRC_WHITELIST, SRC_DB_WRITE,
                    SRC_MIGRATION, SRC_SELF_HEAL, SRC_EXPORT_JOB, SRC_EXPORT_BULK),
        note="`country_origin` -> `origin_country` sits in the DECLARATION alias map in "
             "workflow.py Phase 4, but `origin_country` is a column on `items` and the "
             "declaration whitelist has no key for it — so that alias resolves to nothing. "
             "No rover prompt asks for origin, so the bridge always maps None.",
    ),
    Field(
        name="customs_value_mmk", table=ITEMS,
        meaning="The row's ASSESSED customs value in MMK ('Customs value' on the CUSDEC item "
                "block). A different unit from 'Item value', and usually far larger.",
        unit=UNIT_MMK, type=TYPE_DECIMAL,
        export_header="Customs Value (MMK)", export_order=12,
        aliases=("Customs Value (MMK)",),
        engine_keys=("customs_value",),
        db_type_actual="real",
        present_in=(SRC_ROVER_PROMPT, SRC_PRESTO_SCHEMA, SRC_BRIDGE, SRC_WHITELIST,
                    SRC_DB_WRITE, SRC_MIGRATION, SRC_SELF_HEAL,
                    SRC_EXPORT_JOB, SRC_EXPORT_BULK),
        note="Filled from the invoice-currency 'Item value' for a while (~58x wrong on a THB "
             "doc) and shipped. NEVER derive it as value x rate: the assessed value may carry "
             "an uplift. NULL when the row prints none — the `default=0.0` that used to sit "
             "here made an unread value look like a declared zero and the item-sum gate then "
             "reported a 100% shortfall. Still `real` (about 7 significant digits) on any "
             "database built from 0001: migration 0006 widened the DECLARATION money columns "
             "only, and the self-heal is an ADD COLUMN IF NOT EXISTS, which is a no-op on an "
             "existing `real` column. An 8-digit MMK item value is rounded on write.",
    ),
    # ── metadata ────────────────────────────────────────────────────────────
    Field(
        name="is_valid", table=ITEMS, role=ROLE_META,
        meaning="Legacy validity flag, defaults to 1.",
        unit=UNIT_COUNT, type=TYPE_INTEGER,
        db_type_actual="integer",
        present_in=(SRC_MIGRATION,),
    ),
    Field(
        name="is_deleted", table=ITEMS, role=ROLE_META,
        meaning="Soft-delete flag set when a reviewer removes a row.",
        unit=UNIT_COUNT, type=TYPE_INTEGER,
        db_type_actual="integer",
        present_in=(SRC_MIGRATION, SRC_SELF_HEAL),
    ),
    Field(
        name="display_order", table=ITEMS, role=ROLE_META,
        meaning="Reviewer-controlled row order; set sequentially at save time.",
        unit=UNIT_COUNT, type=TYPE_INTEGER,
        db_type_actual="integer",
        present_in=(SRC_DB_WRITE, SRC_MIGRATION, SRC_SELF_HEAL),
    ),
    Field(
        name="created_at", table=ITEMS, role=ROLE_META,
        meaning="When the row was written.",
        unit=UNIT_DATE, type=TYPE_TIMESTAMP,
        export_header="Processed", export_order=13,
        db_type_actual="timestamp",
        present_in=(SRC_MIGRATION, SRC_EXPORT_JOB, SRC_EXPORT_BULK),
        note="Per-job writer reads `created_at`, bulk writer reads `job_created_at`.",
    ),
]

ALL: List[Field] = DECLARATION_FIELDS + ITEM_FIELDS


# ═════════════════════════════════════════════════════════════════════════════
# Fields an engine produces that NO column holds. Not part of ALL — they cannot
# be looked up as columns because they are not columns. Listed so the drop is
# recorded rather than rediscovered.
# ═════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Orphan:
    key: str
    produced_by: str
    meaning: str
    unit: str
    lost_at: str


ORPHANS: List[Orphan] = [
    Orphan(
        key="declaration_no_official", produced_by="rover prompt + schema + bridge",
        meaning="The doc's own 'Declaration No.' when a separate First-approval number exists.",
        unit=UNIT_ID,
        lost_at="`_save_to_db` db_decl has no key for it and `declarations` has no column. "
                "The bridge maps it, the evidence panel labels it, two rover pages render it "
                "— and it is discarded on the way to the DB.",
    ),
    Orphan(
        key="importer_code", produced_by="rover single_agent prompt + _COLS",
        meaning="The importer registration code printed before the name on the 'Importer' line, "
                "e.g. 'C162371223-000'.",
        unit=UNIT_ID,
        lost_at="Prompt and _COLS only. Absent from rover/schema.COLUMNS, the bridge, the "
                "whitelist, the DB and both exports — the newest field to be extracted with "
                "nowhere to land. It is the natural join key to an importer master, so it is "
                "worth a column rather than a rename.",
    ),
    Orphan(
        key="customs_value_usd", produced_by="rover single_agent prompt + _COLS + supervisor",
        meaning="'Total customs value' on the (USD) line, used by the math supervisor as a "
                "cross-check on the MMK total.",
        unit="USD",
        lost_at="Absent from rover/schema.COLUMNS (so blank_record() never seeds it), absent "
                "from the bridge, and no column exists. Extracted and used in-memory only.",
    ),
    Orphan(
        key="items[].value", produced_by="rover prompt ('Item value') + products.py",
        meaning="The row's line total in the INVOICE currency.",
        unit=UNIT_FC,
        lost_at="The bridge consumes it only to derive `invoice_unit_price` (value / quantity) "
                "and then drops it. No column. The per-row gate has to recompute it.",
    ),
    Orphan(
        key="items[].unit", produced_by="rover prompt + products.py",
        meaning="Unit of measure for the quantity (KG, PCS, ...).",
        unit=UNIT_TEXT,
        lost_at="No column. Survives only when the engine glues it into the TEXT quantity "
                "('108 KG'), which ROVER does not do.",
    ),
    Orphan(
        key="items[].no", produced_by="rover prompt + products.py dedupe renumber",
        meaning="The row number printed on the form ('No. 001').",
        unit=UNIT_COUNT,
        lost_at="No column; `display_order` records save order, which is not the same claim.",
    ),
]


# ═════════════════════════════════════════════════════════════════════════════
# Indexes + lookups
# ═════════════════════════════════════════════════════════════════════════════
_BY_KEY: Dict[str, Field] = {f.key: f for f in ALL}
_BY_TABLE: Dict[str, List[Field]] = {t: [f for f in ALL if f.table == t] for t in TABLES}

# (table, any spelling) -> Field
_LOOKUP: Dict[Tuple[str, str], Field] = {}
for _f in ALL:
    for _k in _f.all_keys():
        _LOOKUP[(_f.table, _k)] = _LOOKUP.get((_f.table, _k), _f)

# Raw engine name -> DB column name, per table. Mirrors the Phase-4 merge alias
# map in v11/workflow.py. Both spellings resolve, which is the whole point:
# reconcile() runs pre-merge on the raw dict and post-merge on the mapped one.
MERGE_ALIAS_MAP: Dict[str, Dict[str, str]] = {
    t: {k: f.name for f in _BY_TABLE[t] for k in f.engine_keys} for t in TABLES
}


def get(name: str, table: Optional[str] = None) -> Optional[Field]:
    """The field with this exact DB column name.

    `table` is optional but not decorative: `currency`, `exchange_rate`,
    `job_id`, `is_valid` and `created_at` exist on BOTH tables and mean
    different things. Omitting it on one of those raises rather than guessing.
    """
    if table is not None:
        return _BY_KEY.get(f"{table}.{name}")
    hits = [f for f in ALL if f.name == name]
    if len(hits) > 1:
        raise AmbiguousFieldName(
            f"{name!r} exists on {[f.table for f in hits]} — pass table=")
    return hits[0] if hits else None


def resolve(key: str, table: Optional[str] = None) -> Optional[Field]:
    """The field for a DB name, a legacy alias, or a RAW ENGINE name.

    This is the function a gate should call. `resolve('customs_duty')` and
    `resolve('import_export_customs_duty')` return the same field, so a check
    written once works on both sides of the merge.
    """
    if table is not None:
        return _LOOKUP.get((table, key))
    hits = []
    for t in TABLES:
        f = _LOOKUP.get((t, key))
        if f is not None and f not in hits:
            hits.append(f)
    if len(hits) > 1:
        raise AmbiguousFieldName(
            f"{key!r} resolves on {[f.table for f in hits]} — pass table=")
    return hits[0] if hits else None


def for_table(table: str) -> List[Field]:
    """Every field on a table, declaration order."""
    if table not in TABLES:
        raise KeyError(f"unknown table {table!r}")
    return list(_BY_TABLE[table])


def data_fields(table: Optional[str] = None) -> List[Field]:
    """Fields read off the customs document (excludes identity + bookkeeping)."""
    src = ALL if table is None else _BY_TABLE[table]
    return [f for f in src if f.role == ROLE_DATA]


def money_fields(table: Optional[str] = None) -> List[Field]:
    """Fields carrying an amount — MMK or invoice currency."""
    src = ALL if table is None else _BY_TABLE[table]
    return [f for f in src if f.is_money]


def export_columns(table: str) -> List[Field]:
    """The customer's sheet for this table, in their column order."""
    cols = [f for f in for_table(table) if f.is_exported]
    return sorted(cols, key=lambda f: f.export_order)


def export_headers(table: str) -> List[str]:
    """The exact column titles, in order. Declarations 23, Product Items 13."""
    return [f.export_header for f in export_columns(table)]


def unit_distribution(table: Optional[str] = None) -> Dict[str, int]:
    """How many fields carry each unit — the shape of the registry at a glance."""
    src = ALL if table is None else _BY_TABLE[table]
    out: Dict[str, int] = {}
    for f in src:
        out[f.unit] = out.get(f.unit, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


def coverage_gaps(required: Tuple[str, ...] = CORE_CHAIN) -> Dict[str, Tuple[str, ...]]:
    """Data fields missing a step of the chain a value must pass to be persisted.

    Default is the save chain. Pass e.g. `(SRC_EXPORT_JOB, SRC_EXPORT_BULK)` to
    list what is stored but never exported, or `(SRC_ROVER_PROMPT,)` to list what
    is exported but nothing asks the model for.
    """
    out = {}
    for f in data_fields():
        missing = f.missing_sources(*required)
        if missing:
            out[f.key] = missing
    return out


# ═════════════════════════════════════════════════════════════════════════════
# Self-check
# ═════════════════════════════════════════════════════════════════════════════
_UNIT_TYPES = {
    UNIT_MMK: {TYPE_DECIMAL},
    UNIT_FC: {TYPE_DECIMAL},
    UNIT_RATE: {TYPE_DECIMAL},
    UNIT_FRACTION: {TYPE_DECIMAL},
    UNIT_PERCENT: {TYPE_DECIMAL},
    UNIT_COUNT: {TYPE_DECIMAL, TYPE_INTEGER},
    UNIT_DATE: {TYPE_DATE, TYPE_TIMESTAMP},
    UNIT_TEXT: {TYPE_TEXT, TYPE_JSON},
    UNIT_CCY: {TYPE_TEXT},
    UNIT_ID: {TYPE_TEXT},
}

# Sheet sizes verified against a real workbook the customer supplied.
EXPORT_SHAPE = {DECLARATIONS: 23, ITEMS: 13}


def _name_states_unit(f: Field) -> bool:
    """Can a reader tell this field's unit from its name alone?"""
    n = f.name.lower()
    if f.unit == UNIT_MMK:
        return n.endswith("_mmk")
    if f.unit == UNIT_FC:
        return n.endswith("_fc")
    if f.unit == UNIT_RATE:
        return "rate" in n
    if f.unit == UNIT_FRACTION:
        return "fraction" in n
    if f.unit == UNIT_PERCENT:
        return "percent" in n or "_pct" in n
    if f.unit == UNIT_DATE:
        return n.endswith("_date")
    return True   # text / id / currency code / count carry no unit hazard


def validate() -> bool:
    """Check the registry against itself. Raises RegistryError listing every problem.

    This is what a test calls. It cannot tell you the registry matches the
    codebase — only that the registry does not contradict itself and still
    describes the customer's workbook.
    """
    problems: List[str] = []

    # 1. structural
    for f in ALL:
        if f.table not in TABLES:
            problems.append(f"{f.key}: unknown table {f.table!r}")
        if f.unit not in UNITS:
            problems.append(f"{f.key}: unknown unit {f.unit!r}")
        if f.type not in TYPES:
            problems.append(f"{f.key}: unknown type {f.type!r}")
        if f.role not in ROLES:
            problems.append(f"{f.key}: unknown role {f.role!r}")
        if not f.meaning.strip():
            problems.append(f"{f.key}: no meaning")
        for s in f.present_in:
            if s not in SOURCES:
                problems.append(f"{f.key}: unknown source {s!r}")

    # 2. no duplicate names, and no alias/engine key colliding within a table
    for t in TABLES:
        seen: Dict[str, str] = {}
        for f in _BY_TABLE[t]:
            if f.name in seen:
                problems.append(f"{t}: duplicate field name {f.name!r}")
            seen[f.name] = f.name
        spellings: Dict[str, str] = {}
        for f in _BY_TABLE[t]:
            for k in f.all_keys():
                if k != f.name and k in {g.name for g in _BY_TABLE[t]}:
                    problems.append(
                        f"{t}: {f.name!r} claims alias {k!r}, which is another field's name")
                if k in spellings and spellings[k] != f.name:
                    problems.append(
                        f"{t}: spelling {k!r} claimed by both {spellings[k]!r} and {f.name!r}")
                spellings[k] = f.name
            if len(set(f.all_keys())) != len(f.all_keys()):
                problems.append(f"{f.key}: repeats one of its own spellings")

    # 3. money is exact decimal, and unit/type agree everywhere
    for f in ALL:
        if f.is_money and f.type != TYPE_DECIMAL:
            problems.append(
                f"{f.key}: unit {f.unit} must be stored as {TYPE_DECIMAL}, not {f.type!r}")
        allowed = _UNIT_TYPES.get(f.unit, set())
        if allowed and f.type not in allowed:
            problems.append(
                f"{f.key}: unit {f.unit!r} is incompatible with type {f.type!r}")

    # 4. a name that hides its unit must be on the tracked list — the invoice_price
    #    regression is invisible without this
    for f in data_fields():
        if not _name_states_unit(f) and f.key not in UNIT_NAME_CONFLICTS:
            problems.append(
                f"{f.key}: name does not state its unit ({f.unit}) and is not listed in "
                f"UNIT_NAME_CONFLICTS — rename it or add it deliberately")
    known = {f.key for f in ALL}
    for k in sorted(UNIT_NAME_CONFLICTS):
        if k not in known:
            problems.append(f"UNIT_NAME_CONFLICTS lists {k!r}, which is not a field")
        else:
            f = _BY_KEY[k]
            if _name_states_unit(f):
                problems.append(
                    f"UNIT_NAME_CONFLICTS lists {k!r}, but its name does state its unit — "
                    f"drop it from the list")

    # 5. exports: one header per column, contiguous order, headers unique per sheet
    for t in TABLES:
        cols = [f for f in _BY_TABLE[t] if f.is_exported]
        orders = [f.export_order for f in cols]
        if any(o is None for o in orders):
            problems.append(f"{t}: an exported field has no export_order")
        else:
            if len(set(orders)) != len(orders):
                problems.append(f"{t}: duplicate export_order values")
            if sorted(orders) != list(range(1, len(orders) + 1)):
                problems.append(
                    f"{t}: export_order must be 1..{len(orders)} with no gaps, got "
                    f"{sorted(orders)}")
        headers = [f.export_header for f in cols]
        for h in set(headers):
            if headers.count(h) > 1:
                problems.append(f"{t}: export header {h!r} used {headers.count(h)} times")
        if len(cols) != EXPORT_SHAPE[t]:
            problems.append(
                f"{t}: customer workbook has {EXPORT_SHAPE[t]} columns, registry has "
                f"{len(cols)}")
    for f in ALL:
        if (f.export_order is not None) != f.is_exported:
            problems.append(f"{f.key}: export_header and export_order must be set together")

    # 6. an unread value is NULL — never 0.0, never ""
    for f in ALL:
        if f.default_on_unread is not DEFAULT_ON_UNREAD:
            problems.append(
                f"{f.key}: default_on_unread must be None — a default makes 'could not read' "
                f"indistinguishable from a value the form actually prints")
        if f.role == ROLE_DATA and not f.nullable:
            problems.append(
                f"{f.key}: a data field must be nullable; an unread reading has no value")
        if f.role == ROLE_IDENTITY and f.nullable:
            problems.append(f"{f.key}: an identity field must not be nullable")

    # 7. both key spaces resolve
    for t, m in MERGE_ALIAS_MAP.items():
        for raw, dbname in m.items():
            if resolve(raw, table=t) is None:
                problems.append(f"{t}: raw engine key {raw!r} does not resolve")
            if get(dbname, table=t) is None:
                problems.append(f"{t}: alias target {dbname!r} is not a field")

    if problems:
        raise RegistryError(
            "%d problem(s):\n  - %s" % (len(problems), "\n  - ".join(problems)))
    return True


if __name__ == "__main__":
    validate()
    print(f"{len(ALL)} fields OK "
          f"({len(_BY_TABLE[DECLARATIONS])} declarations, {len(_BY_TABLE[ITEMS])} items)")
    print(f"units: {unit_distribution()}")
    print(f"exports: declarations={len(export_columns(DECLARATIONS))}, "
          f"items={len(export_columns(ITEMS))}")
    gaps = coverage_gaps()
    print(f"save-chain gaps: {gaps if gaps else 'none'}")
