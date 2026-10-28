"""AST-level validation of LLM-generated report SQL.

Layer 2–4 of the guardrail stack: parse (never regex), require exactly one
SELECT, forbid write/DDL nodes and dangerous functions, enforce the admin's
table/column allow-list, and clamp/inject a row LIMIT. DB-level read-only
roles (layer 1) and the audit trail (layer 5) live elsewhere.
"""

from typing import Any

import sqlglot
from sqlglot import exp

from contextvault.models import DatabaseType

DIALECTS = {DatabaseType.POSTGRES: "postgres", DatabaseType.MYSQL: "mysql"}

_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Merge,
    exp.TruncateTable,
    exp.Command,
    exp.Grant,
)

_FORBIDDEN_FUNCTIONS = {
    "pg_sleep",
    "pg_read_file",
    "pg_ls_dir",
    "pg_terminate_backend",
    "dblink",
    "copy",
    "lo_import",
    "lo_export",
    "sleep",
    "benchmark",
    "load_file",
}

# Whole families of Postgres system/large-object/dblink functions are
# dangerous (file access, session control, cross-database queries). Rather
# than enumerate every sibling (pg_read_binary_file, pg_stat_file,
# pg_ls_waldir, lo_get, ...), reject anything with these prefixes. Legitimate
# report/aggregation functions (SUM, COUNT, AVG, DATE_TRUNC, ROUND, ...) never
# use these prefixes, so this doesn't affect real report queries.
_FORBIDDEN_PREFIXES = ("pg_", "lo_", "dblink")


class SQLValidationError(Exception):
    """The generated SQL violated the guardrails; the message is LLM-repair feedback."""


def _function_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.this).lower()
    return node.sql_name().lower()


def validate_sql(
    sql: str,
    *,
    db_type: DatabaseType,
    exposed_schema: list[dict[str, Any]],
    max_rows: int = 10_000,
) -> str:
    """Validate ``sql`` against the allow-list; return it normalized with a LIMIT.

    Raises :class:`SQLValidationError` with a reason usable as self-repair
    feedback for the LLM. CTE names count as allowed tables (their inner selects
    are themselves validated); SELECT-list aliases count as allowed columns so
    ``ORDER BY alias`` passes.
    """
    dialect = DIALECTS[db_type]
    try:
        statements = sqlglot.parse(sql, read=dialect)
    except sqlglot.errors.ParseError as exc:
        raise SQLValidationError(f"SQL does not parse: {exc}") from exc
    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise SQLValidationError("Exactly one SQL statement is required.")
    tree = statements[0]
    if not isinstance(tree, exp.Select):
        raise SQLValidationError("Only a single SELECT statement is allowed.")
    if tree.args.get("into") is not None:
        raise SQLValidationError("SELECT INTO is not allowed.")

    for node in tree.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise SQLValidationError(f"Forbidden operation: {node.key.upper()}.")
        if isinstance(node, exp.Func):
            fname = _function_name(node)
            if fname in _FORBIDDEN_FUNCTIONS or fname.startswith(_FORBIDDEN_PREFIXES):
                raise SQLValidationError(f"Forbidden function: {fname}.")
        if isinstance(node, exp.Star) and not isinstance(node.parent, exp.Func):
            raise SQLValidationError("SELECT * is not allowed; list columns explicitly.")

    allowed_tables = {t["table"].lower() for t in exposed_schema}
    allowed_columns = {c["name"].lower() for t in exposed_schema for c in t["columns"]}
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    aliases = {a.alias.lower() for a in tree.find_all(exp.Alias) if a.alias}

    for table in tree.find_all(exp.Table):
        if table.db or table.catalog:
            raise SQLValidationError(f"Schema-qualified table not allowed: {table.sql()}.")
        if table.name.lower() not in allowed_tables | cte_names:
            raise SQLValidationError(f"Table not allowed: {table.name}.")
    for column in tree.find_all(exp.Column):
        if column.name.lower() not in allowed_columns | aliases:
            raise SQLValidationError(f"Column not allowed: {column.name}.")

    limit = tree.args.get("limit")
    current = None
    if limit is not None and isinstance(limit.expression, exp.Literal):
        try:
            current = int(limit.expression.this)
        except ValueError:
            current = None
    if current is None or current > max_rows:
        tree = tree.limit(max_rows)
    return tree.sql(dialect=dialect)
