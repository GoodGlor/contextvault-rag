"""The ordered block list a report's document is built from.

``report_style`` owns the tokens a template is made of and ``report_design``
owns which template a report wears; this module owns the *structure* — which
blocks appear, in what order, and how each presents the section it binds to.

**Writes reject, reads tolerate**, inherited from ``report_design``. ``kind`` is
typed ``str`` rather than a ``Literal`` precisely so an unknown kind survives the
load and is skipped by the renderer: a kind dropped from the code in a later step
must not make old reports unrenderable. ``validate_blocks`` is the write gate,
and it runs where the caller is present to be told what is wrong.

**The version is validated**, which departs from ``report_design``'s stated
position. That module objected to #134's ``PAYLOAD_VERSION`` — written on every
store, never read — and the objection was correct. The answer is not to avoid
versions but to avoid unvalidated ones: every value this field can take has a
defined behaviour, and an unrecognised one discards the block list so the report
renders as if unedited. The distinction matters more here than for
``{"template": …}``: an unknown *kind* degrades visibly because the renderer
knows it does not know it, but a changed *meaning* of an existing key degrades
silently, and no amount of read-tolerance detects that.
"""

import uuid
from collections.abc import Collection, Sequence
from dataclasses import replace
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from contextvault.services.report_execution import QueryResult
from contextvault.services.report_format import is_period_column
from contextvault.services.report_icons import ICON_NAMES
from contextvault.services.report_llm import ChartSpec
from contextvault.services.report_types import SectionResult

__all__ = [
    "BLOCKS_VERSION",
    "CHART_TYPES",
    "KNOWN_KINDS",
    "MAX_BLOCK_TEXT",
    "TEXT_KINDS",
    "Block",
    "BlockIn",
    "allowed_chart_types",
    "chart_type_overrides",
    "default_blocks",
    "default_blocks_from_storage",
    "document_title",
    "draws_chart",
    "load_blocks",
    "validate_blocks",
]

BLOCKS_VERSION = 1

KNOWN_KINDS = frozenset(
    {
        "cover",
        "heading",
        "viz",
        "stats",
        "prose",
        "sources",
        "error",
        "callout",
        "pagebreak",
        # 5f. ``toc`` is furniture derived from the heading blocks; ``image``
        # carries an uploaded asset. Neither is bound to a section.
        "toc",
        "image",
    }
)

_IMAGE_MIN_WIDTH = 25
_IMAGE_MAX_WIDTH = 100
_ALIGNMENTS = frozenset({"left", "center", "right"})


class Block(BaseModel):
    """One entry in a report's block list.

    Every field beyond ``kind`` is optional because the kinds carry different
    payloads and a flat model keeps the JSON legible. ``section`` is an index into
    ``sections_data.sections``; for a finished report that list is immutable, so
    an index is stable and the persistence codec needs no addition.

    ``text`` is the block's own words. Step 5a gave it to ``callout`` alone,
    precisely so author text stayed out of the payload and could not break the
    citation round-trip; step 5e generalises it to the kinds that render words a
    reader can disagree with (``TEXT_KINDS``), where it *overrides* what the
    bound section says rather than replacing it. Everything computed from the
    stored result stays the database's to answer — see ``validate_blocks``.
    """

    model_config = ConfigDict(extra="ignore")

    kind: str
    section: int | None = None
    variant: str | None = None
    chart_type: str | None = None
    text: str | None = None
    # Hidden is stored, not a client-side toggle, because the point of hiding a
    # block is that the *printed* artifact stops carrying it. It differs from
    # deleting only in that the block keeps its place in the list and can be put
    # back without the editor having to remember what was there.
    hidden: bool = False
    # 5f — an ``image`` block's payload. ``asset_id`` references report_assets by
    # id rather than inlining bytes, so a 300 ms debounced design write stays
    # small. ``caption`` is the reader's own words, not an override of generated
    # ones, which is why it is not ``text``.
    asset_id: uuid.UUID | None = None
    caption: str | None = None
    width: int | None = None
    align: str | None = None
    # 6d — a drawn mark from ``report_icons.ICON_NAMES``, on a heading or a
    # callout. **The default must stay ``None``**: designs are persisted with
    # ``exclude_defaults=True``, so any other default would appear in every
    # payload the next time one was written — a silent rewrite of ~470 stored
    # documents for a field none of them uses.
    icon: str | None = None


class BlockIn(Block):
    """The write side of ``Block``: same shape, no tolerance for a typo.

    ``Block`` carries ``extra="ignore"`` because the read path needs it — a
    stored payload with unknown keys must not raise. But ``Block`` is also used
    as the request schema, so a misspelled key (``chartType`` instead of
    ``chart_type``) used to pass validation and store a block with no override,
    silently. The request path has the caller present to be told, so it should
    reject rather than swallow the typo. Request handlers should validate
    against this subclass instead of ``Block`` directly; ``Block`` itself stays
    tolerant for everything reading a stored design back.
    """

    model_config = ConfigDict(extra="forbid")


def load_blocks(raw: dict[str, Any] | None) -> list[Block] | None:
    """A stored design's block list, or ``None`` meaning "unedited".

    Never raises. ``None`` is the caller's signal to derive the default list, so
    every failure mode here — absent, mis-versioned, malformed — lands on the
    same safe behaviour: the report renders the way it was generated.
    """
    if not raw:
        return None
    blocks = raw.get("blocks")
    if blocks is None:
        return None
    if raw.get("blocks_version") != BLOCKS_VERSION:
        return None
    if not isinstance(blocks, list):
        return None
    try:
        return [Block.model_validate(block) for block in blocks]
    except ValidationError:
        return None


def default_blocks(sections: list[SectionResult]) -> list[Block]:
    """The block list that reproduces the document a report renders unedited.

    **Order matters and so does when this runs.** ``chart_svg`` is populated by
    ``reports._with_chart`` immediately before the document is built, so this must
    be called from inside the builder. Derive the default any earlier and every
    report silently loses its figures — the chart block is the one entry decided
    by a field that does not exist yet at generation time.
    """
    blocks = [Block(kind="cover")]
    for index, section in enumerate(sections):
        blocks.append(Block(kind="heading", section=index))
        if section.kind == "error":
            blocks.append(Block(kind="error", section=index))
        elif section.kind == "text":
            blocks.append(Block(kind="prose", section=index))
            if section.sources:
                blocks.append(Block(kind="sources", section=index))
        else:
            # An empty result renders one note and nothing else today; the table
            # block carries it, so neither a chart nor a statistics line is derived.
            if section.result is not None and section.result.rows:
                if section.chart_svg is not None:
                    blocks.append(Block(kind="viz", section=index, variant="chart"))
                blocks.append(Block(kind="stats", section=index))
            blocks.append(Block(kind="viz", section=index, variant="table"))
    return blocks


def draws_chart(section: SectionResult) -> bool:
    """Whether this section's stored result and spec would produce a figure.

    ``default_blocks`` answers the same question with ``chart_svg is not None``,
    which is correct *inside the builder* — ``reports._with_chart`` has just run.
    It is unanswerable anywhere else: ``chart_svg`` is never persisted (both
    encodings are redrawn from the stored result and spec, so ``report_persist``
    carries no image bytes at all), and anything reading a report back from
    storage sees ``None`` on every section.

    Drawing the chart to find out is the honest answer and the wrong one: 5c
    measured chart drawing at ~198 ms per section on the dyno, and the editor
    asks this question for a whole report at once. This probe costs a
    ``prepare_plot`` instead.

    **It is exact, not an approximation, and only for one reason.**
    ``render_chart_images`` returns ``None`` if and only if ``prepare_plot``
    does: ``build_figure`` has a single ``return None`` — the one immediately
    after that call — and the save path is a ``try/finally`` with no ``except``,
    so it either returns bytes or raises. ``test_draws_chart_agrees_with_the
    _renderer`` pins that equivalence against the real renderer, and a second
    ``return None`` appearing in ``build_figure`` breaks it loudly.

    Note the stored spec is used **as stored**, unlike ``allowed_chart_types``,
    which substitutes each candidate. The questions are different: this one asks
    what the report *does* draw, that one what it *could*.
    """
    from contextvault.services.report_chart import prepare_plot

    if section.kind != "data" or section.result is None or not section.chart_spec:
        return False
    try:
        spec = ChartSpec(**section.chart_spec)
    except ValidationError:
        return False
    return prepare_plot(section.result, spec, column_labels=section.column_labels) is not None


def default_blocks_from_storage(sections: list[SectionResult]) -> list[Block]:
    """``default_blocks`` for sections that came back from ``load_sections``.

    The derived default is what an unedited report's editor has to show, and it
    must be the list that *actually renders* — otherwise the tree omits every
    figure the reader can see on the sheet. Handing storage's sections to
    ``default_blocks`` directly does exactly that, because their ``chart_svg`` is
    ``None`` by construction.

    So the sections are given the one field the derivation reads, from a probe
    rather than a draw. The value is a marker, never rendered: only
    ``chart_svg is not None`` is ever asked of it here.
    """
    return default_blocks(
        [
            replace(section, chart_svg=_CHART_MARKER if draws_chart(section) else None)
            for section in sections
        ]
    )


# Not image bytes and never rendered — ``default_blocks`` asks only whether this
# is None. Empty rather than a fake SVG so that anything which did try to draw it
# fails immediately instead of producing a plausible blank figure.
_CHART_MARKER = b""


def document_title(title: str, blocks: list[Block] | None) -> str:
    """What this report calls itself: the cover's override, or the stored title.

    The running header needs no part in this. ``stylesheet`` sets
    ``string-set: doctitle content()`` on the cover's ``h1`` and ``_page_css``
    prints ``string(doctitle)`` in ``@top-left``, so the header already names
    whatever the cover says. This exists so the ``<title>`` — and the deck's title
    slide, which has no cover element to publish from — give the same answer, and
    the document does not carry two different names for itself.

    The first non-hidden cover wins, matching ``chart_type_overrides``: a second
    one is a document that prints two covers, and it renders its own words.
    """
    if blocks is None:
        return title
    for block in blocks:
        if block.kind == "cover" and not block.hidden and block.text is not None:
            return block.text
    return title


def chart_type_overrides(blocks: list[Block] | None) -> dict[int, str]:
    """Section index → the chart type its first charting block asks for.

    A section draws one figure, so two chart blocks bound to the same section
    cannot each carry their own type; the first wins and the second renders the
    same picture. Accepted for 5a and worth knowing: it is the one place the
    block list is richer than the renderer behind it.
    """
    overrides: dict[int, str] = {}
    if not blocks:
        return overrides
    for block in blocks:
        if (
            not block.hidden
            and block.kind == "viz"
            and block.variant == "chart"
            and block.chart_type
            and block.section is not None
        ):
            overrides.setdefault(block.section, block.chart_type)
    return overrides


CHART_TYPES = ("bar", "line", "pie")


def _is_period_axis(result: QueryResult, x_column: str | None) -> bool:
    """Whether the x axis names a reporting period, whatever type it arrived as.

    ``x_kind == "date"`` is not enough on its own, and the gap is the common case
    rather than an exotic one. ``report_chart._coerce_x`` classifies a bare
    ``2024`` as a *number* deliberately — "a column of years belongs on a numeric
    axis" — so a ``fiscal_year`` column reaches the pie guard looking like an
    ordinary measure, and a pie of revenue across 2021/2022/2023 would be offered.
    That is precisely the chart #131 exists to prevent, and financial reports
    group by fiscal year constantly.

    ``is_period_column`` is the codebase's existing answer to this exact question
    — it already decides whether a total across rows is meaningless — and it
    recognises dates, years and ``to_char`` text periods (``2024-Q1``) alike.
    Reusing it keeps one definition of "this axis is time" rather than growing a
    second one free to drift from it.
    """
    if x_column is None or x_column not in result.columns:
        return False
    index = result.columns.index(x_column)
    return is_period_column(x_column, [row[index] for row in result.rows])


def allowed_chart_types(section: SectionResult) -> frozenset[str]:
    """Which chart types this section's stored result can actually support.

    Parent design §3: the variant list is derived from the result, not fixed, so
    the editor cannot reintroduce the defects #131 removed.

    **The probe substitutes the candidate rather than reading the stored type.**
    ``prepare_plot`` returns ``None`` immediately when ``chart_type == "none"``,
    so probing with the stored spec would refuse a chart to every section the
    model declined to chart — including sections whose ``x_column`` and
    ``y_column`` are perfectly usable. Substituting each candidate asks the only
    question that matters: could this picture be drawn from this data?

    The pie rule is enforcement added here, not inherited. Neither
    ``prepare_plot`` nor ``build_figure`` gates a pie today; both will prepare and
    draw one over a date axis, and only the prompt discouraged it.

    A time axis is **not** the same question as ``x_kind == "date"``; see
    ``_is_period_axis``.
    """
    # Imported here rather than at module scope: this pulls matplotlib in, and the
    # rest of this module is deliberately importable without a rendering backend.
    from contextvault.services.report_chart import prepare_plot

    if section.kind != "data" or section.result is None or not section.chart_spec:
        return frozenset()
    try:
        spec = ChartSpec(**section.chart_spec)
    except ValidationError:
        return frozenset()
    allowed: set[str] = set()
    for candidate in CHART_TYPES:
        plot = prepare_plot(section.result, spec.model_copy(update={"chart_type": candidate}))
        if plot is None:
            continue
        if candidate == "pie":
            # A pie states "these are the parts of one whole at one moment". A time
            # axis contradicts the second half, and more than one series the first.
            if plot.x_kind == "date" or _is_period_axis(section.result, spec.x_column):
                continue
            if any(len(panel.series) > 1 for panel in plot.panels):
                continue
        allowed.add(candidate)
    return frozenset(allowed)


VARIANTS = frozenset({"chart", "table"})

# These kinds render without reading any section: a cover draws the document's
# own title, a page break draws nothing, and a callout carries its own text.
# Every other kind reads its words or data from the section it binds to, so a
# missing ``section`` there is not a lesser document — it is a block that will
# render as "" and vanish.
_SECTIONLESS_KINDS = frozenset({"cover", "pagebreak", "callout", "toc", "image"})

# The kinds whose ``text`` is honoured: each renders words, and each has a
# generated original to fall back to when the override is dropped — except
# ``callout``, which has carried its own text since 5a and has no source to read.
# Anything absent from this set draws from the stored result instead, and reader
# text there would let the document disagree with the database (parent design,
# non-goals). That is why the refusal lives here and not only in the editor: this
# endpoint takes a block list the client composes.
TEXT_KINDS = frozenset({"cover", "heading", "prose", "sources", "callout"})

# An override is unbounded reader input landing in a JSONB column that is read on
# every render of this report. Generous enough that no honest edit meets it — a
# section's prose runs to a few hundred characters — and small enough that a
# paste of a whole document is refused while the caller is present to be told.
MAX_BLOCK_TEXT = 20_000

# 6d. The mockup offers a mark beside a heading and inside a callout and nowhere
# else, and that restraint is the point: everything else on the page is computed
# from the stored result, and a mark there would be the document having an
# opinion about it.
_ICON_KINDS = frozenset({"heading", "callout"})


def validate_blocks(
    blocks: Sequence[Block],
    sections: list[SectionResult],
    *,
    asset_ids: Collection[uuid.UUID],
) -> None:
    """The write gate. Raises ``ValueError`` naming the first problem found.

    Rejecting here and tolerating on read is the asymmetry ``report_design``
    established: at write time the caller is present and can be told they got it
    wrong, while a stored payload must never be able to make a report
    unrenderable.

    ``asset_ids`` is the set of assets belonging to this report's repository,
    and it is a **required** keyword: an image block naming an asset outside it
    would otherwise read another repository's bytes through the render path, and
    a default value is exactly how a caller forgets.
    """
    for position, block in enumerate(blocks):
        where = f"block {position} ({block.kind})"
        if block.kind not in KNOWN_KINDS:
            raise ValueError(
                f"{where}: unknown kind. Choose one of: {', '.join(sorted(KNOWN_KINDS))}."
            )
        if block.section is None:
            if block.kind not in _SECTIONLESS_KINDS:
                raise ValueError(f"{where}: this kind needs a section to bind to.")
        elif not 0 <= block.section < len(sections):
            raise ValueError(
                f"{where}: section {block.section} is out of range — "
                f"this report has {len(sections)} sections."
            )
        if block.text is not None:
            # ``is not None``, not truthiness: "" is a reader who emptied this
            # block, which is a different request from dropping the key — that one
            # restores the generated words.
            if block.kind not in TEXT_KINDS:
                raise ValueError(
                    f"{where}: this kind draws its content from the stored result, "
                    "so it carries no text of its own. Editable kinds: "
                    f"{', '.join(sorted(TEXT_KINDS))}."
                )
            if len(block.text) > MAX_BLOCK_TEXT:
                raise ValueError(
                    f"{where}: text is too long — {len(block.text)} characters, "
                    f"and the limit is {MAX_BLOCK_TEXT}."
                )
        if block.kind == "viz":
            if block.variant not in VARIANTS:
                raise ValueError(
                    f"{where}: unknown variant {block.variant!r}. "
                    f"Choose one of: {', '.join(sorted(VARIANTS))}."
                )
            if block.chart_type is not None:
                # viz is not in _SECTIONLESS_KINDS, so the branch above already
                # rejected a missing section by this point.
                assert block.section is not None
                allowed = allowed_chart_types(sections[block.section])
                if block.chart_type not in allowed:
                    offer = ", ".join(sorted(allowed)) if allowed else "none"
                    raise ValueError(
                        f"{where}: this result cannot be drawn as {block.chart_type!r}. "
                        f"Available: {offer}."
                    )
        if block.icon is not None:
            # The two kinds the mockup marks. A mark on a table or a figure would
            # be the document editorialising a computed value, which is the same
            # rule that keeps `text` off those kinds.
            if block.kind not in _ICON_KINDS:
                raise ValueError(
                    f"{where}: this kind carries no icon. Markable kinds: "
                    f"{', '.join(sorted(_ICON_KINDS))}."
                )
            if block.icon not in ICON_NAMES:
                raise ValueError(
                    f"{where}: unknown icon {block.icon!r}. "
                    f"Choose one of: {', '.join(sorted(ICON_NAMES))}."
                )
        if block.kind == "image":
            if block.asset_id is not None and block.asset_id not in asset_ids:
                raise ValueError(
                    f"{where}: asset {block.asset_id} does not belong to this repository."
                )
            if block.width is not None and not (
                _IMAGE_MIN_WIDTH <= block.width <= _IMAGE_MAX_WIDTH
            ):
                raise ValueError(
                    f"{where}: width must be between {_IMAGE_MIN_WIDTH} and "
                    f"{_IMAGE_MAX_WIDTH} percent."
                )
            if block.align is not None and block.align not in _ALIGNMENTS:
                raise ValueError(
                    f"{where}: align must be one of: {', '.join(sorted(_ALIGNMENTS))}."
                )
