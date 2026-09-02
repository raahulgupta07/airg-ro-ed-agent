"""Read the column types each DDL author *claims*, without a database.

This repo has two authors of DDL and they disagree:

  * `alembic/versions/` — owns the schema in practice.
  * `database.init_database()` — `CREATE TABLE IF NOT EXISTS` plus a long list of
    `ALTER TABLE … ADD COLUMN IF NOT EXISTS`.

The second one cannot change a TYPE. `CREATE TABLE IF NOT EXISTS` is a no-op on
an existing table and `ADD COLUMN IF NOT EXISTS` is a no-op on an existing
column — neither ever ALTERs. So `database.py:552` has claimed
`items.customs_value_mmk DOUBLE PRECISION` for a long time while the live column
stayed `real`, because migration 0001 created it REAL. That edit looked correct,
reviewed clean, and was inert.

This module replays each source statically and reports:

  * `alembic_types()`   — what the migration chain leaves behind (authoritative).
  * `selfheal_types()`  — what `database.py` claims (advisory, often ignored).

Both return `(types, unparsed)`. `unparsed` matters as much as `types`: a parser
that silently skips a statement it does not recognise reports "no drift" for the
wrong reason, which is the exact failure mode this whole exercise exists to
prevent. Tests assert it is empty, so a migration written in a shape this module
cannot read breaks loudly here instead of quietly widening the blind spot.

Only `upgrade()` is replayed. Walking a migration file wholesale would apply its
`downgrade()` too and derive the schema you get from installing and then
uninstalling every migration — which is not the schema anybody runs.

WHAT THIS DOES NOT PROVE: that the live database matches. A migration can exist
and never have been applied, and older databases carry columns no migration
mentions. `test_schema_contract.py` closes that gap by diffing these derived
types against `information_schema` whenever a database is reachable — and skips
when it is not, so this file's claims have to stand alone on a machine without
one.
"""
from __future__ import annotations

import ast
import functools
import os
import re

_BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSIONS_DIR = os.path.join(_BACKEND, "alembic", "versions")
DATABASE_PY = os.path.join(_BACKEND, "database.py")


# ── type normalisation ───────────────────────────────────────────────────────
# Normalised to the spellings `information_schema.columns.data_type` uses, so a
# derived type and a live type compare directly.

_TYPE_ALIASES = {
    "REAL": "real",
    "FLOAT4": "real",
    "DOUBLE PRECISION": "double precision",
    "FLOAT8": "double precision",
    "FLOAT": "double precision",
    "NUMERIC": "numeric",
    "DECIMAL": "numeric",
    "TEXT": "text",
    "VARCHAR": "character varying",
    "CHARACTER VARYING": "character varying",
    "INTEGER": "integer",
    "INT": "integer",
    "SERIAL": "integer",
    "SMALLINT": "smallint",
    "BIGINT": "bigint",
    "BIGSERIAL": "bigint",
    "BOOLEAN": "boolean",
    "BOOL": "boolean",
    "JSONB": "jsonb",
    "JSON": "json",
    "DATE": "date",
    "BYTEA": "bytea",
    "TSVECTOR": "tsvector",
    "TIMESTAMPTZ": "timestamp with time zone",
    "TIMESTAMP WITHOUT TIME ZONE": "timestamp without time zone",
    "TIMESTAMP WITH TIME ZONE": "timestamp with time zone",
    "TIMESTAMP": "timestamp without time zone",
}

# SQLAlchemy type classes, for the one migration that uses `op.add_column`.
_SA_TYPE_ALIASES = {
    "JSONB": "jsonb",
    "JSON": "json",
    "Text": "text",
    "UnicodeText": "text",
    "String": "character varying",
    "Unicode": "character varying",
    "Integer": "integer",
    "SmallInteger": "smallint",
    "BigInteger": "bigint",
    "Boolean": "boolean",
    "Float": "double precision",
    "Numeric": "numeric",
    "Date": "date",
    "DateTime": "timestamp without time zone",
    "LargeBinary": "bytea",
}

# Longest-first so "DOUBLE PRECISION" beats "DOUBLE", "BIGSERIAL" beats "BIGINT".
_TYPE_RE = re.compile(
    r"^\s*(" + "|".join(re.escape(t) for t in
                        sorted(_TYPE_ALIASES, key=len, reverse=True)) + r")\b"
    r"(\s*\([^)]*\))?",
    re.IGNORECASE,
)

_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_UNBOUND = "\x00"          # marks an f-string hole we could not resolve


def _normalise_type(text: str):
    """Leading SQL type of `text`, normalised — or None if it is not a type."""
    m = _TYPE_RE.match(text)
    return _TYPE_ALIASES[m.group(1).upper()] if m else None


# ── statically replaying a module's DDL statements ───────────────────────────

def _module_literals(tree) -> dict:
    """Module-level `NAME = <literal>` bindings, e.g. 0001's `DDL` list."""
    out = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        try:
            value = ast.literal_eval(node.value)
        except Exception:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                out[target.id] = value
    return out


def _identifier_lists(literals: dict) -> list:
    """Module-level lists of bare identifiers — column-name lists, in practice.

    Used only when a loop iterates over a CALL rather than a name. Migration 0006
    does `for c in _present_cols():`, where `_present_cols()` returns `_COLS`
    filtered to what exists. Statically we cannot run the filter, so we replay the
    unfiltered list: the derived type is what the migration intends for every
    column it names, which is exactly what a contract test should compare against.
    """
    return [list(v) for v in literals.values()
            if isinstance(v, (list, tuple)) and v
            and all(isinstance(e, str) and re.fullmatch(_IDENT, e) for e in v)]


def _docstring_nodes(tree) -> set:
    """ids of docstring Constant nodes, so prose is never read as SQL."""
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
            continue
        body = getattr(node, "body", None)
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            out.add(id(body[0].value))
    return out


def _bind(target, element, env: dict) -> bool:
    if isinstance(target, ast.Name):
        env[target.id] = element
        return True
    if isinstance(target, ast.Tuple):
        names = [t.id for t in target.elts if isinstance(t, ast.Name)]
        if len(names) != len(target.elts) or not isinstance(element, (list, tuple)):
            return False
        if len(names) != len(element):
            return False
        env.update(dict(zip(names, element)))
        return True
    return False


def _render_fstring(node: ast.JoinedStr, env: dict) -> str:
    parts = []
    for v in node.values:
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            parts.append(v.value)
        elif isinstance(v, ast.FormattedValue) and isinstance(v.value, ast.Name):
            val = env.get(v.value.id, _UNBOUND)
            parts.append(str(val) if isinstance(val, str) else _UNBOUND)
        else:
            parts.append(_UNBOUND)
    return "".join(parts)


def _str_arg(node, env: dict):
    """The string this argument evaluates to, or None if we cannot tell.

    A literal, or a name bound by the enclosing loop. 0008 adds its three
    currency columns as `for c in _COLS: op.add_column("declarations",
    sa.Column(c, sa.Text(), ...))` — the column name is only ever a loop
    variable, so a literal-only reader records the whole statement as unparsed
    and the columns then look as though no migration creates them.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        value = env.get(node.id)
        return value if isinstance(value, str) else None
    return None


def _add_column_call(node: ast.Call, env: dict = None):
    """`op.add_column("t", sa.Column("c", <Type>(), ...))` -> a synthetic ALTER.

    Table and column name may each be a literal or a loop variable bound in
    `env`. Returns the statement, or None when the call is not this shape (the
    caller then records it as unparsed rather than ignoring it).
    """
    env = env or {}
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "add_column"):
        return None
    if len(node.args) < 2:
        return None
    table_name = _str_arg(node.args[0], env)
    col = node.args[1]
    if table_name is None:
        return None
    if not (isinstance(col, ast.Call) and col.args):
        return None
    col_name = _str_arg(col.args[0], env)
    if col_name is None:
        return None
    if len(col.args) < 2:
        return None
    type_node = col.args[1]
    if isinstance(type_node, ast.Call):
        type_node = type_node.func
    name = (type_node.attr if isinstance(type_node, ast.Attribute)
            else type_node.id if isinstance(type_node, ast.Name) else None)
    sql_type = _SA_TYPE_ALIASES.get(name)
    if sql_type is None:
        return None
    return (f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS "
            f"{col_name} {sql_type}")


def _collect(node, env, literals, ident_lists, skip_ids, out, unhandled):
    """Depth-first walk emitting the SQL strings this node would execute."""
    if isinstance(node, ast.For):
        iterable = None
        if isinstance(node.iter, (ast.List, ast.Tuple, ast.Set)):
            try:
                iterable = list(ast.literal_eval(node.iter))
            except Exception:
                iterable = None
        elif isinstance(node.iter, ast.Name):
            value = literals.get(node.iter.id)
            if isinstance(value, (list, tuple)):
                iterable = list(value)
        elif isinstance(node.iter, ast.Call):
            # Unknown callee — fall back to every module-level identifier list.
            iterable = [e for lst in ident_lists for e in lst]
        if iterable is not None:
            for element in iterable:
                inner = dict(env)
                if not _bind(node.target, element, inner):
                    continue
                for stmt in node.body:
                    _collect(stmt, inner, literals, ident_lists, skip_ids,
                             out, unhandled)
            for stmt in node.orelse:
                _collect(stmt, env, literals, ident_lists, skip_ids, out, unhandled)
            return

    if isinstance(node, ast.JoinedStr):
        out.append(_render_fstring(node, env))
        return

    if isinstance(node, ast.Constant):
        if isinstance(node.value, str) and id(node) not in skip_ids:
            out.append(node.value)
        return

    if isinstance(node, ast.Name):
        # A loop variable used bare, e.g. 0001's `for stmt in DDL: op.execute(stmt)`.
        value = env.get(node.id)
        if isinstance(value, str):
            out.append(value)
        return

    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in (
                "add_column", "alter_column", "create_table", "drop_column"):
            stmt = _add_column_call(node, env)
            if stmt is not None:
                out.append(stmt)
            else:
                unhandled.append(f"op.{func.attr}(...) in a shape this parser "
                                 f"cannot read — extend _ddl_sources or use raw SQL")
            return

    for child in ast.iter_child_nodes(node):
        _collect(child, env, literals, ident_lists, skip_ids, out, unhandled)


def _statements(source: str, entrypoint=None):
    """SQL strings a module would execute, plus calls we could not interpret."""
    tree = ast.parse(source)
    literals = _module_literals(tree)
    ident_lists = _identifier_lists(literals)
    skip_ids = _docstring_nodes(tree)

    roots = [tree]
    if entrypoint:
        roots = [n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == entrypoint]

    out, unhandled = [], []
    for root in roots:
        _collect(root, {}, literals, ident_lists, skip_ids, out, unhandled)
    return out, unhandled


# ── folding statements into a type map ───────────────────────────────────────

_CREATE_RE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(" + _IDENT + r")\s*\((.*)\)",
    re.IGNORECASE | re.DOTALL,
)
_ADD_COL_RE = re.compile(
    r"ALTER\s+TABLE\s+(" + _IDENT + r")\s+ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(" + _IDENT + r")\s+(.+)",
    re.IGNORECASE | re.DOTALL,
)
_ALTER_TYPE_RE = re.compile(
    r"ALTER\s+TABLE\s+(" + _IDENT + r")\s+ALTER\s+COLUMN\s+(" + _IDENT + r")\s+"
    r"(?:SET\s+DATA\s+)?TYPE\s+(.+)",
    re.IGNORECASE | re.DOTALL,
)
_DROP_COL_RE = re.compile(
    r"ALTER\s+TABLE\s+(" + _IDENT + r")\s+DROP\s+COLUMN\s+(?:IF\s+EXISTS\s+)?"
    r"(" + _IDENT + r")",
    re.IGNORECASE,
)
_NOT_A_COLUMN = re.compile(
    r"^\s*(PRIMARY|FOREIGN|UNIQUE|CHECK|CONSTRAINT|EXCLUDE)\b", re.IGNORECASE)


def _split_columns(body: str) -> list:
    """Split a CREATE TABLE body on top-level commas (parens nest)."""
    parts, depth, cur = [], 0, []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        parts.append("".join(cur))
    return parts


def _apply(statements, types: dict, unparsed: list, origin: str,
           alter_orphans: set = None) -> None:
    for stmt in statements:
        s = " ".join(stmt.split())          # collapse whitespace, keep one line
        upper = s.upper()
        if "ALTER TABLE" not in upper and "CREATE TABLE" not in upper:
            continue
        if _UNBOUND in s:
            unparsed.append(f"{origin}: unresolved f-string hole: "
                            f"{s.replace(_UNBOUND, '{?}')[:110]}")
            continue

        if "CREATE TABLE" in upper:
            m = _CREATE_RE.search(s)
            if not m:
                unparsed.append(f"{origin}: {s[:110]}")
                continue
            table = m.group(1)
            for line in _split_columns(m.group(2)):
                line = line.strip()
                if not line or _NOT_A_COLUMN.match(line):
                    continue
                name_m = re.match(r"^(" + _IDENT + r")\s+(.*)$", line, re.DOTALL)
                if not name_m:
                    continue
                col_type = _normalise_type(name_m.group(2))
                if col_type is None:
                    unparsed.append(f"{origin}: {table}: {line[:100]}")
                    continue
                # CREATE TABLE IF NOT EXISTS never rewrites an existing table.
                types.setdefault((table, name_m.group(1)), col_type)
            continue

        m = _ALTER_TYPE_RE.search(s)
        if m:
            col_type = _normalise_type(m.group(3))
            key = (m.group(1), m.group(2))
            if col_type is None:
                unparsed.append(f"{origin}: {s[:110]}")
            elif key in types:
                types[key] = col_type            # ALTER, unlike ADD, replaces
            elif alter_orphans is not None:
                # A migration ALTERs a column no migration CREATES. Both 0006 and
                # 0007 do this deliberately and guard with a `_present_cols()`
                # check, because `invoice_price_fc` and friends are created by the
                # self-heal in `database.py` — which runs AFTER alembic, so on a
                # virgin database the column is simply absent and the ALTER is
                # skipped. Recording the column here would claim a type the
                # migration chain does not actually establish.
                alter_orphans.add(key)
            continue

        m = _ADD_COL_RE.search(s)
        if m:
            col_type = _normalise_type(m.group(3))
            if col_type is None:
                unparsed.append(f"{origin}: {s[:110]}")
            else:
                # ADD COLUMN IF NOT EXISTS never replaces an existing column —
                # `setdefault`, not assignment. Modelling it as an overwrite is
                # exactly the mistake that made the database.py edit look right.
                types.setdefault((m.group(1), m.group(2)), col_type)
            continue

        m = _DROP_COL_RE.search(s)
        if m:
            types.pop((m.group(1), m.group(2)), None)
            continue

        unparsed.append(f"{origin}: {s[:110]}")


# ── migration chain ──────────────────────────────────────────────────────────

def _migration_modules() -> list:
    out = []
    for fn in sorted(os.listdir(VERSIONS_DIR)):
        if not fn.endswith(".py") or fn.startswith("__"):
            continue
        with open(os.path.join(VERSIONS_DIR, fn), encoding="utf-8") as fh:
            src = fh.read()
        literals = _module_literals(ast.parse(src))
        out.append((fn, src, literals.get("revision"), literals.get("down_revision")))
    return out


@functools.lru_cache(maxsize=1)
def migration_chain() -> tuple:
    """`((filename, source, revision, down_revision), …)` in apply order.

    Raises on a broken chain (missing parent, fork, cycle, or an unreachable
    file) — an unorderable migration history is itself a defect worth failing on.
    """
    mods = _migration_modules()
    by_rev = {rev: m for m in mods for rev in [m[2]]}
    roots = [m for m in mods if m[3] is None]
    if len(roots) != 1:
        raise ValueError(f"expected exactly one root migration, found {len(roots)}")

    children = {}
    for _fn, _src, rev, down in mods:
        if down is not None:
            children.setdefault(down, []).append(rev)

    ordered, seen, cur = [], set(), roots[0]
    while cur is not None:
        if cur[2] in seen:
            raise ValueError(f"cycle in the migration chain at {cur[2]}")
        seen.add(cur[2])
        ordered.append(cur)
        nxt = children.get(cur[2], [])
        if len(nxt) > 1:
            raise ValueError(f"the migration chain forks at {cur[2]}: {nxt}")
        cur = by_rev[nxt[0]] if nxt else None

    if len(ordered) != len(mods):
        raise ValueError("migrations unreachable from the root: %s"
                         % [m[0] for m in mods if m[2] not in seen])
    return tuple(ordered)


# ── public API ───────────────────────────────────────────────────────────────

# Parsing every migration plus the ~3000-line `database.py` takes ~40 ms, which
# is nothing once and a great deal across the ~60 parametrised cases that ask for
# it. Cached because the sources cannot change mid-run; every accessor hands back
# a fresh copy so a caller that mutates its result cannot poison the next one.
@functools.lru_cache(maxsize=1)
def _alembic_replay():
    types, unparsed, orphans = {}, [], set()
    for fn, src, _rev, _down in migration_chain():
        statements, unhandled = _statements(src, entrypoint="upgrade")
        unparsed.extend(f"{fn}: {u}" for u in unhandled)
        _apply(statements, types, unparsed, fn, orphans)
    return types, unparsed, orphans


def alembic_types_detailed():
    """`(types, unparsed, alter_orphans)` after replaying every `upgrade()`.

    `alter_orphans` are `(table, column)` pairs some migration ALTERs but no
    migration CREATES — columns whose existence depends on the `database.py`
    self-heal, and whose type therefore is NOT established by the migration chain.
    """
    types, unparsed, orphans = _alembic_replay()
    return dict(types), list(unparsed), set(orphans)


def alembic_types():
    """`({(table, column): type}, [unparsed])` after replaying every `upgrade()`."""
    types, unparsed, _orphans = alembic_types_detailed()
    return types, unparsed


def effective_types():
    """The types a fresh boot actually ends up with, from both sources.

    `database.init_database()` runs alembic FIRST and only then its own
    `CREATE TABLE IF NOT EXISTS` / `ADD COLUMN IF NOT EXISTS` block. So on a
    virgin database alembic wins every column it creates, and the self-heal fills
    in only what no migration mentions — `declarations.invoice_price_fc`,
    `invoice_price_mmk`, the three lifecycle date columns, `jobs.doc_class`,
    `users.must_change_password`.

    That ordering is why the 20-odd places where `database.py` claims a different
    type than the migrations are latent rather than active: they are all
    `IF NOT EXISTS` on a column alembic already made. They are not harmless,
    though — see `test_schema_contract.py`, which spells out the path where they
    become the live schema.
    """
    alembic, unparsed_a = alembic_types()
    selfheal, unparsed_s = selfheal_types()
    effective = dict(selfheal)
    effective.update(alembic)               # alembic runs first, so alembic wins
    return effective, unparsed_a + unparsed_s


def selfheal_types():
    """`({(table, column): type}, [unparsed])` claimed by `database.py`.

    Advisory only. Everything here is `IF NOT EXISTS`, so on any database that
    already has the table or the column it does precisely nothing.
    """
    types, unparsed = _selfheal_replay()
    return dict(types), list(unparsed)


@functools.lru_cache(maxsize=1)
def selfheal_forced_types():
    """`{(table, column): type}` that `database.py` ALTERs on EVERY boot.

    Distinct from `selfheal_types()`, which is dominated by `IF NOT EXISTS`
    statements that do nothing on an existing schema. These are unconditional
    `ALTER COLUMN … TYPE` statements — `database.py:588` re-asserts
    `double precision` across the fourteen `declarations` money columns at every
    container start.

    That is a one-way ratchet nobody has written down. It is correct today,
    because 0006 put those columns at `double precision` and 0007 deliberately
    leaves the table alone. It stops being correct the moment someone converts
    `declarations` to `numeric` the way 0007 converted `items`: the migration
    would widen the columns and the next restart would silently narrow them back.
    """
    with open(DATABASE_PY, encoding="utf-8") as fh:
        src = fh.read()
    statements, _unhandled = _statements(src, entrypoint="init_database")
    forced = {}
    for stmt in statements:
        s = " ".join(stmt.split())
        if _UNBOUND in s:
            continue
        m = _ALTER_TYPE_RE.search(s)
        if m and "ALTER TABLE" in s.upper():
            col_type = _normalise_type(m.group(3))
            if col_type:
                forced[(m.group(1), m.group(2))] = col_type
    return forced


@functools.lru_cache(maxsize=1)
def _selfheal_replay():
    with open(DATABASE_PY, encoding="utf-8") as fh:
        src = fh.read()
    statements, unhandled = _statements(src, entrypoint="init_database")
    types, unparsed = {}, [f"database.py: {u}" for u in unhandled]
    _apply(statements, types, unparsed, "database.py")
    return types, unparsed
