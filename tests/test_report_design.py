"""The stored design payload: what it reads as, and what it refuses.

The asymmetry under test is deliberate. ``is_known_template`` is the *write*
gate and rejects; ``load_design``/``style_for`` are the *read* path and never
raise, because a report in the wrong template is recoverable and one that fails
to render is not.
"""

import uuid
from typing import Any

import pytest

from contextvault.services.report_design import (
    ReportDesign,
    cover_brand_for,
    design_payload,
    is_known_template,
    load_design,
    style_for,
)
from contextvault.services.report_html import CoverBrand
from contextvault.services.report_style import TEMPLATES, resolve_style


def test_load_design_reads_the_stored_template() -> None:
    assert load_design({"template": "prezydia"}).template == "prezydia"


@pytest.mark.parametrize("raw", [None, {}])
def test_an_absent_payload_chooses_nothing(raw: dict[str, Any] | None) -> None:
    # Null means "no choice recorded", which is what every pre-3b row says.
    assert load_design(raw).template is None


def test_unknown_keys_survive_being_read() -> None:
    # Step 4 adds ``blocks`` and ``overrides`` to this payload. If reading one
    # raised, a 3b-era code path touching a step-4 row would 500.
    assert load_design({"template": "minimum", "blocks": [{"kind": "cover"}]}).template == "minimum"


def test_a_malformed_payload_reads_as_no_choice() -> None:
    # Not a crash: a corrupted column must degrade to the default template, the
    # same way ``resolve_style`` degrades on an unknown name.
    assert load_design({"template": 5}).template is None


def test_design_payload_round_trips_through_load() -> None:
    assert load_design(design_payload("chasopys")).template == "chasopys"


def test_style_for_resolves_the_stored_template() -> None:
    assert style_for({"template": "chasopys"}) is TEMPLATES["chasopys"]


def test_style_for_an_unknown_template_falls_back_rather_than_raising() -> None:
    # The read side tolerates. A template deleted from the code in a later step
    # must not turn every report that wore it into a 500.
    assert style_for({"template": "no-such-template"}) is resolve_style(None)


def test_style_for_nothing_is_the_default() -> None:
    assert style_for(None) is resolve_style(None)


@pytest.mark.parametrize("slug", sorted(TEMPLATES))
def test_every_shipped_template_is_known(slug: str) -> None:
    assert is_known_template(slug)


def test_an_unknown_slug_is_not_known() -> None:
    # The write side rejects. This is the half that makes a typo a 400 instead
    # of a silently-default-styled report.
    assert not is_known_template("no-such-template")


def test_report_design_defaults_to_nothing_chosen() -> None:
    assert ReportDesign().template is None


# --- 5f: typography, page and brand groups ------------------------------------


def test_a_design_with_no_tokens_resolves_to_the_bare_template() -> None:
    """The identity property, at the persistence boundary."""
    for slug in TEMPLATES:
        assert style_for({"template": slug}) == resolve_style(slug)


def test_style_for_applies_stored_typography_and_page_tokens() -> None:
    style = style_for(
        {
            "template": "kvarta",
            "typography": {"scale": 1.2, "density": "airy"},
            "page": {"section_rules": True},
        }
    )
    plain = resolve_style("kvarta")
    assert style.body_pt == pytest.approx(plain.body_pt * 1.2)
    assert style.line_height > plain.line_height
    assert style.section_rules is True


def test_a_stored_accent_reaches_the_style() -> None:
    style = style_for({"template": "kvarta", "brand": {"accent": "#c2544d"}})
    assert style.accent == "#c2544d"
    assert style.chart.series[0] == "#c2544d"
    assert style.chart.series[1:] == resolve_style("kvarta").chart.series[1:]


def test_a_malformed_token_group_does_not_break_the_style() -> None:
    """The landmine this module exists to disarm.

    ``StyleTokens`` tolerates unknown *keys* but raises ``ValidationError`` on a
    malformed *value* — ``scale: "big"`` is not a float. A stored payload must
    never be able to make an existing report unrenderable, which is exactly what
    ``load_design`` already promises for the template key.
    """
    plain = resolve_style("kvarta")
    for broken in (
        {"typography": "nonsense"},
        {"typography": {"scale": "big"}},
        {"typography": {"density": 5}},
        {"page": {"margin_mm": "wide"}},
        {"page": ["not", "a", "mapping"]},
        {"brand": {"accent": 5}},
        {"brand": "nonsense"},
    ):
        assert style_for({"template": "kvarta", **broken}) == plain, broken


def test_cover_brand_is_read_from_the_stored_design() -> None:
    brand = cover_brand_for(
        {"brand": {"cover": "band", "approval": True, "logo_asset_id": str(_ASSET_ID)}}
    )
    assert brand.variant == "band"
    assert brand.approval is True
    assert brand.logo_asset_id == _ASSET_ID


def test_a_malformed_brand_group_yields_the_default_cover() -> None:
    for broken in ({"brand": "nonsense"}, {"brand": {"logo_asset_id": "not-a-uuid"}}, {}):
        assert cover_brand_for(broken) == CoverBrand(), broken


_ASSET_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
