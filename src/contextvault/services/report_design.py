"""The design stored on a report row, and the repository default it inherits.

``report_style`` owns the *tokens* a template is made of; this module owns the
*document* that says which template a report wears. Keeping them apart is what
stops the token module from growing a persistence concern, and gives step 4 an
obvious place to put block lists and style overrides.

**Writes reject, reads tolerate**, and the asymmetry is on purpose.
``is_known_template`` gates the endpoints: at write time the caller is present
and can be told they typed the slug wrong. ``load_design`` and ``style_for``
never raise: a template removed from the code in some later step must not turn
every report that wore it into a 500, and a report in the wrong template is
recoverable while one that fails to render is not.

There is **no schema version** on the template key, and that stays true: keys
here are added, never repurposed, so an absent key means "not chosen".

The block list step 5a adds *does* carry one — ``report_blocks.BLOCKS_VERSION``
— and validates it on load. That is not a reversal of the reasoning below but
its conclusion: the objection was to #134's ``PAYLOAD_VERSION``, written on
every store and never read. A version with a defined behaviour for every value
it can take is a different object from a version nobody checks.
"""

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from contextvault.services.report_html import CoverBrand
from contextvault.services.report_style import TEMPLATES, ReportStyle, resolve_style
from contextvault.services.report_tokens import StyleTokens, apply_tokens

__all__ = [
    "ReportDesign",
    "cover_brand_for",
    "design_payload",
    "is_known_template",
    "load_design",
    "style_for",
]


class ReportDesign(BaseModel):
    """What a stored ``design`` payload says. Every field is optional.

    ``extra="ignore"`` so a payload written by a later step — which will carry a
    block list and overrides — is readable here instead of being an error.
    """

    model_config = ConfigDict(extra="ignore")

    template: str | None = None
    # 5f. Kept as raw payloads rather than typed sub-models on purpose: a
    # malformed *value* (``scale: "big"``) raises ``ValidationError`` where a
    # malformed *key* does not, and this model's whole promise is that a stored
    # payload can never make a report unrenderable. Each group is parsed
    # separately, behind its own try/except, by the readers below.
    typography: Any = None
    page: Any = None
    brand: Any = None


def load_design(raw: dict[str, Any] | None) -> ReportDesign:
    """A stored payload as a design. Never raises; see the module docstring."""
    if not raw:
        return ReportDesign()
    try:
        return ReportDesign.model_validate(raw)
    except ValidationError:
        return ReportDesign()


def design_payload(template: str) -> dict[str, Any]:
    """The JSONB a chosen template is stored as."""
    return {"template": template}


def is_known_template(slug: str) -> bool:
    """Whether a slug names a shipped template. The write gate."""
    return slug in TEMPLATES


def _mapping(value: Any) -> dict[str, Any]:
    """A stored group as a mapping, or empty when it is anything else.

    A group written as a list, a string or ``null`` degrades to "nothing was
    chosen" rather than raising — the same tolerance ``load_design`` gives the
    template key.
    """
    return value if isinstance(value, dict) else {}


def style_tokens_for(raw: dict[str, Any] | None) -> StyleTokens:
    """The reader's tokens, gathered from the three stored groups.

    The groups exist for the editor's benefit — Типографіка, Сторінка, Брендкіт
    are three panels — while the renderer wants one flat set, so they are merged
    here rather than three times downstream. Only the accent is taken from
    ``brand``: the rest of that group is cover furniture, not type.

    Never raises. ``StyleTokens`` tolerates unknown keys but **not** malformed
    values, so the construction is guarded: one bad stored value must not make
    an existing report unrenderable.
    """
    design = load_design(raw)
    merged = {
        **_mapping(design.typography),
        **_mapping(design.page),
    }
    accent = _mapping(design.brand).get("accent")
    if accent is not None:
        merged["accent"] = accent
    try:
        return StyleTokens.model_validate(merged)
    except ValidationError:
        return StyleTokens()


def cover_brand_for(raw: dict[str, Any] | None) -> CoverBrand:
    """What the cover wears, from the stored ``brand`` group. Never raises."""
    brand = _mapping(load_design(raw).brand)

    logo: uuid.UUID | None = None
    raw_logo = brand.get("logo_asset_id")
    if raw_logo is not None:
        try:
            logo = uuid.UUID(str(raw_logo))
        except (ValueError, AttributeError, TypeError):
            logo = None

    variant = brand.get("cover")
    approval = brand.get("approval")
    return CoverBrand(
        logo_asset_id=logo,
        variant=variant if isinstance(variant, str) else "full",
        approval=approval if isinstance(approval, bool) else False,
    )


def style_for(raw: dict[str, Any] | None) -> ReportStyle:
    """The style a stored design resolves to, tokens and all. Tolerant throughout.

    With no tokens stored this returns the template unchanged — the identity
    property ``apply_tokens`` guarantees, which is what keeps every report
    written before 5f rendering exactly as it did.
    """
    design = load_design(raw)
    return apply_tokens(resolve_style(design.template), style_tokens_for(raw))
