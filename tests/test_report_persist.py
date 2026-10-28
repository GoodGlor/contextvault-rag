import datetime as dt
import json
import uuid
from decimal import Decimal

from contextvault.services.report_execution import QueryResult
from contextvault.services.report_persist import (
    decode_rows,
    decode_value,
    dump_sections,
    encode_rows,
    encode_value,
    load_sections,
)
from contextvault.services.report_types import SectionResult, SectionSourceRef


def test_decimal_survives_the_round_trip_as_a_decimal() -> None:
    # Not merely equal — the type is what report_format branches on.
    value = Decimal("365.817")
    restored = decode_value(encode_value(value))
    assert restored == value
    assert isinstance(restored, Decimal)


def test_dates_and_datetimes_keep_their_types() -> None:
    day = dt.date(2025, 9, 27)
    moment = dt.datetime(2025, 9, 27, 14, 30, tzinfo=dt.UTC)
    assert decode_value(encode_value(day)) == day
    assert isinstance(decode_value(encode_value(day)), dt.date)
    assert decode_value(encode_value(moment)) == moment


def test_plain_json_values_pass_through_untouched() -> None:
    for value in [1, 2.5, "текст", True, False, None]:
        assert decode_value(encode_value(value)) == value
        assert type(decode_value(encode_value(value))) is type(value)


def test_rows_round_trip_as_tuples() -> None:
    rows = [(Decimal("1.5"), dt.date(2024, 1, 31), None), (2, "б", True)]
    assert decode_rows(encode_rows(rows)) == rows
    assert all(isinstance(row, tuple) for row in decode_rows(encode_rows(rows)))


def test_plain_dict_with_no_tag_passes_through_untouched() -> None:
    # Customer data can legitimately contain dicts
    value = {"name": "John", "age": 30}
    restored = decode_value(value)
    assert restored == value
    assert isinstance(restored, dict)


def test_dict_with_unknown_tag_passes_through_untouched() -> None:
    # A legitimate customer field called __t with an unknown tag value
    value = {"__t": "something-else", "other": "field"}
    restored = decode_value(value)
    assert restored == value
    assert isinstance(restored, dict)


def test_dict_with_tag_but_no_value_key_passes_through_untouched() -> None:
    # A dict with __t but missing the v key — not one of our tagged wrappers
    value = {"__t": "decimal", "other": "field"}
    restored = decode_value(value)
    assert restored == value
    assert isinstance(restored, dict)


def test_containers_are_encoded_element_by_element() -> None:
    # asyncpg hands back a numeric[] column as list[Decimal] and a jsonb column as
    # a dict — neither is JSON-safe until every element inside it is encoded.
    value = {"items": [Decimal("1.5"), dt.date(2024, 1, 31)], "total": Decimal("2.5")}
    encoded = encode_value(value)
    json.dumps(encoded)  # the whole point: this must not raise
    restored = decode_value(encoded)
    assert restored == value
    assert isinstance(restored["total"], Decimal)
    assert isinstance(restored["items"][0], Decimal)
    assert isinstance(restored["items"][1], dt.date)


def test_nested_lists_round_trip_their_elements() -> None:
    value = [[Decimal("1.5")], [dt.date(2024, 1, 31)]]
    encoded = encode_value(value)
    json.dumps(encoded)
    assert decode_value(encoded) == value


def test_types_json_cannot_hold_degrade_to_text_instead_of_raising() -> None:
    # Every one of these is an ordinary asyncpg return type: uuid, bytea, time,
    # interval. ``report_format`` already treats each as text (``_as_decimal``
    # accepts only Decimal/int/float), so ``str`` loses nothing at render time —
    # while raising here would fail the entire report.
    identifier = uuid.UUID("2f1c2f8a-7c9e-4d5b-9a3e-0c1d2e3f4a5b")
    row = (identifier, b"\x00\xff", dt.time(9, 30), dt.timedelta(hours=3))
    encoded = encode_rows([row])
    json.dumps(encoded)
    assert encoded[0] == [str(identifier), str(b"\x00\xff"), "09:30:00", str(dt.timedelta(hours=3))]


def test_non_finite_floats_become_null() -> None:
    # ``json.dumps`` happily emits bare NaN/Infinity, which are not JSON — Postgres
    # rejects them at COMMIT, poisoning the transaction the report is written in.
    encoded = encode_rows([(float("nan"), float("inf"), float("-inf"))])
    assert encoded[0] == [None, None, None]
    json.dumps(encoded, allow_nan=False)  # what Postgres's jsonb parser accepts


def _data_section() -> SectionResult:
    return SectionResult(
        title="Виторг Apple",
        source="database",
        sub_question="Як змінювався виторг?",
        kind="data",
        result=QueryResult(columns=["fy", "revenue"], rows=[(2024, Decimal("391.0"))]),
        chart_png=b"ignored-on-purpose",
        generated_sql="SELECT 1",
        chart_spec={"chart_type": "bar", "x_column": "fy", "y_column": "revenue"},
        column_labels={"revenue": "Виторг"},
    )


def _text_section() -> SectionResult:
    return SectionResult(
        title="Ризики",
        source="documents",
        sub_question="Які ризики?",
        kind="text",
        prose="Концентрація виторгу[1].",
        sources=[SectionSourceRef(number=1, title="Form 10-K", source_id=uuid.uuid4())],
    )


def test_payload_round_trips_every_section_kind() -> None:
    original = [_data_section(), _text_section()]
    title, language, restored = load_sections(
        dump_sections(title="Звіт", language="uk", sections=original)
    )
    assert (title, language) == ("Звіт", "uk")
    assert [s.title for s in restored] == ["Виторг Apple", "Ризики"]
    assert restored[0].result is not None
    assert restored[0].result.rows == [(2024, Decimal("391.0"))]
    # Verify the cell type is actually Decimal, not float or str (not just equality).
    assert isinstance(restored[0].result.rows[0][1], Decimal)
    assert restored[0].column_labels == {"revenue": "Виторг"}
    # ``chart_spec`` decides whether the re-render draws a chart at all, and
    # ``generated_sql`` is the audit trail; neither is reconstructible from the
    # rest, so both have to survive the trip.
    assert restored[0].chart_spec == original[0].chart_spec
    assert restored[0].generated_sql == original[0].generated_sql
    assert restored[1].prose == "Концентрація виторгу[1]."
    assert restored[1].sources[0].source_id == original[1].sources[0].source_id


def test_error_sections_keep_their_message() -> None:
    failed = SectionResult(
        title="Впало", source="database", sub_question="?", kind="error", error="timeout"
    )
    _, _, restored = load_sections(dump_sections(title="З", language="uk", sections=[failed]))
    assert restored[0].kind == "error"
    assert restored[0].error == "timeout"


def test_chart_png_is_not_stored() -> None:
    payload = dump_sections(title="З", language="uk", sections=[_data_section()])
    assert "chart_png" not in payload["sections"][0]
    assert b"ignored-on-purpose" not in repr(payload).encode()


def test_the_overlap_warning_survives_a_round_trip() -> None:
    """The canvas, the re-render and every stored report rebuild through
    load_sections. Dumping without loading loses the caveat silently — the PDF
    stays right while the canvas shows a chart under no warning."""
    section = SectionResult(
        title="Дохід",
        source="database",
        sub_question="дохід",
        kind="data",
        result=QueryResult(columns=["fiscal_year"], rows=[(2017,)]),
        overlap_warning="apple_financials",
    )
    payload = dump_sections(title="Звіт", language="uk", sections=[section])
    _, _, restored = load_sections(payload)
    assert restored[0].overlap_warning == "apple_financials"


def test_a_payload_written_before_this_field_still_loads() -> None:
    """~470 stored reports have no such key; PAYLOAD_VERSION stays 1."""
    payload = dump_sections(
        title="Звіт",
        language="uk",
        sections=[
            SectionResult(
                title="Дохід",
                source="database",
                sub_question="д",
                kind="data",
                result=QueryResult(columns=["a"], rows=[(1,)]),
            )
        ],
    )
    del payload["sections"][0]["overlap_warning"]
    _, _, restored = load_sections(payload)
    assert restored[0].overlap_warning is None


def test_merge_notes_survive_a_round_trip() -> None:
    """#204: the merge does not re-run on a re-render — the sections are rebuilt
    from this payload. A note that did not persist would mean a re-rendered
    report silently stopped disclosing the database that contributed nothing,
    which is the whole point of the note."""
    section = SectionResult(
        title="Дохід",
        source="database",
        sub_question="q",
        kind="data",
        result=QueryResult(columns=["a"], rows=[(1,)]),
        notes=["Немає даних із: Microsoft DB."],
    )
    _, _, loaded = load_sections(dump_sections(title="Звіт", language="uk", sections=[section]))
    assert loaded[0].notes == ["Немає даних із: Microsoft DB."]


def test_the_databases_a_section_read_survive_a_round_trip() -> None:
    """#205: a re-render rebuilds sections from this payload and does not re-run
    the merge, so a name that did not persist would mean a re-rendered report
    quietly stopped attributing its own figures."""
    section = SectionResult(
        title="Дохід",
        source="database",
        sub_question="q",
        kind="data",
        result=QueryResult(columns=["a"], rows=[(1,)]),
        databases=["Apple DB", "Microsoft DB"],
    )
    _, _, loaded = load_sections(dump_sections(title="Звіт", language="uk", sections=[section]))
    assert loaded[0].databases == ["Apple DB", "Microsoft DB"]
