"""Convert executed section results to and from a JSON-safe payload.

Opens no database session, performs no I/O, and imports no renderer (no matplotlib,
no fpdf). Therefore the codec runs and is tested without a database or rendering
backend. The module does import section dataclasses from ``report_execution`` and
``report_types``, which live in modules that import SQLAlchemy — that transitive
import is accepted deliberately, because the codec's entire job is converting
those types to and from JSON-safe form.

Every value carries its type where JSON has none. ``Decimal`` is the reason: money
arrives from asyncpg as ``Decimal``, and ``report_format`` decides both formatting
and whether a column may be summed by ``isinstance`` checks. A money column that
came back as ``str`` would render unformatted and silently vanish from every total.
"""

import datetime as dt
import math
import uuid
from decimal import Decimal
from typing import Any

from contextvault.services.report_execution import QueryResult
from contextvault.services.report_types import SectionResult, SectionSourceRef

_TAG = "__t"
_VAL = "v"


def encode_value(value: Any) -> Any:
    """One cell as something ``json.dumps`` can hold, whatever the driver returned.

    Total by construction: containers are walked, the types that carry meaning are
    tagged, and everything else degrades to ``str``. Totality is the requirement,
    not a nicety — the caller serializes inside the generation try block, so a
    value this function cannot handle would mark the whole report ``FAILED``. A
    query as ordinary as ``SELECT id, created_at, total FROM orders`` on a
    UUID-keyed table is enough to hit it.

    Degrading to ``str`` is type-neutral for rendering: ``report_format`` decides
    formatting by ``isinstance`` against ``Decimal``/``int``/``float``, so a
    ``UUID``, ``bytes``, ``time`` or ``timedelta`` was already rendered as text.
    """
    if isinstance(value, (str, bool, int)) or value is None:
        return value  # bool/int before float: json holds them exactly
    if isinstance(value, float):
        # json.dumps emits bare NaN/Infinity, which are not JSON: Postgres rejects
        # them when the payload reaches jsonb, aborting the whole transaction.
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return {_TAG: "decimal", _VAL: str(value)}
    if isinstance(value, dt.datetime):
        return {_TAG: "datetime", _VAL: value.isoformat()}
    if isinstance(value, dt.date):
        return {_TAG: "date", _VAL: value.isoformat()}
    if isinstance(value, dict):
        # A jsonb or hstore cell: JSON keys are strings, and any value inside can
        # be a Decimal just as it can at the top level.
        return {
            (key if isinstance(key, str) else str(key)): encode_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [encode_value(item) for item in value]  # numeric[] is list[Decimal]
    return str(value)


def decode_value(value: Any) -> Any:
    if isinstance(value, list):
        return [decode_value(item) for item in value]
    if not isinstance(value, dict):
        return value
    if _TAG not in value or _VAL not in value:
        # A plain dict from a jsonb column — its values still need decoding.
        return {key: decode_value(item) for key, item in value.items()}
    kind = value[_TAG]
    try:
        if kind == "decimal":
            return Decimal(value[_VAL])
        if kind == "datetime":
            return dt.datetime.fromisoformat(value[_VAL])
        if kind == "date":
            return dt.date.fromisoformat(value[_VAL])
    except (ValueError, ArithmeticError, TypeError):
        # Customer data that merely looks like one of our wrappers. Handing back
        # the dict is wrong-ish; raising would fail a re-render outright.
        return value
    return value


def encode_rows(rows: list[tuple[Any, ...]]) -> list[list[Any]]:
    return [[encode_value(cell) for cell in row] for row in rows]


def decode_rows(rows: list[list[Any]]) -> list[tuple[Any, ...]]:
    return [tuple(decode_value(cell) for cell in row) for row in rows]


PAYLOAD_VERSION = 1


def dump_sections(*, title: str, language: str, sections: list[SectionResult]) -> dict[str, Any]:
    """The JSON-safe payload for one report's executed sections.

    ``title`` and ``language`` travel in the envelope because a frozen-schedule
    report has no plan to carry them, and a re-render must not switch a Ukrainian
    report to English.
    """
    return {
        "version": PAYLOAD_VERSION,
        "title": title,
        "language": language,
        "sections": [_dump_section(section) for section in sections],
    }


def _dump_section(section: SectionResult) -> dict[str, Any]:
    return {
        "title": section.title,
        "source": section.source,
        "sub_question": section.sub_question,
        "kind": section.kind,
        "columns": list(section.result.columns) if section.result else None,
        "rows": encode_rows(section.result.rows) if section.result else None,
        "prose": section.prose,
        "sources": [
            {"number": ref.number, "title": ref.title, "source_id": str(ref.source_id)}
            for ref in section.sources
        ],
        "generated_sql": section.generated_sql,
        "chart_spec": section.chart_spec,
        "column_labels": section.column_labels,
        "error": section.error,
        "overlap_warning": section.overlap_warning,
        # Persisted, unlike ``empty``: the merge does not re-run on a re-render,
        # so a note that lived only in memory would mean a re-rendered report
        # quietly stopped naming the database that contributed nothing (#204).
        "notes": section.notes,
        "databases": section.databases,
    }


def load_sections(payload: dict[str, Any]) -> tuple[str, str, list[SectionResult]]:
    sections = [_load_section(raw) for raw in payload.get("sections", [])]
    return payload.get("title", ""), payload.get("language", ""), sections


def _load_section(raw: dict[str, Any]) -> SectionResult:
    result = None
    if raw.get("columns") is not None:
        result = QueryResult(columns=list(raw["columns"]), rows=decode_rows(raw["rows"] or []))
    return SectionResult(
        title=raw["title"],
        source=raw["source"],
        sub_question=raw["sub_question"],
        kind=raw["kind"],
        result=result,
        prose=raw.get("prose"),
        sources=[
            SectionSourceRef(
                number=ref["number"], title=ref["title"], source_id=uuid.UUID(ref["source_id"])
            )
            for ref in raw.get("sources", [])
        ],
        generated_sql=raw.get("generated_sql"),
        chart_spec=raw.get("chart_spec"),
        column_labels=raw.get("column_labels") or {},
        error=raw.get("error"),
        overlap_warning=raw.get("overlap_warning"),
        # ``or []`` rather than a default: every payload written before #204
        # lacks the key, and a re-render of one must not raise.
        notes=raw.get("notes") or [],
        databases=raw.get("databases") or [],
    )
