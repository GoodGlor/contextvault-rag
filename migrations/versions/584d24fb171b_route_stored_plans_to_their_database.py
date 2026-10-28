"""route stored plans to their database

Every plan stored before #203 has database sections with no ``databases`` key:
the report read *the* repository's single connection, because that is all a
repository could have. The report row still records which one in
``connection_id``, so the routing is recoverable exactly — this is a backfill of
information the rows already hold, not a guess.

Without it a report still AWAITING_APPROVAL from before the deploy cannot be
approved: ``finalize_draft`` refuses an unrouted database section, and the plan
editor offers no control to route one (the plan chooses). Its reader
would have to spend a re-plan round to recover, for a plan that was already
complete.

Reports whose ``connection_id`` is NULL are left alone: with nothing to route to,
there is nothing to recover. Their sections are documents sections in practice —
a database section could not have run without a connection.

Revision ID: 584d24fb171b
Revises: c7d3f81a24b9
Create Date: 2026-08-14 14:05:11.402118
"""

from collections.abc import Sequence

from alembic import op


revision: str = "584d24fb171b"
down_revision: str | None = "c7d3f81a24b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Rebuilds `plan->'sections'` with `databases` added to every database section
# that lacks one. jsonb_agg over WITH ORDINALITY preserves section order, which
# is load-bearing: a plan's sections are identified positionally (merge_draft),
# so reordering them here would silently re-attribute every section.
_UPGRADE = """
UPDATE generated_reports AS r
SET plan = jsonb_set(
    r.plan,
    '{sections}',
    (
        SELECT jsonb_agg(
            CASE
                WHEN s.value->>'source' = 'database'
                     AND NOT (s.value ? 'databases')
                THEN s.value || jsonb_build_object(
                    'databases', jsonb_build_array(r.connection_id::text)
                )
                ELSE s.value
            END
            ORDER BY s.ordinality
        )
        FROM jsonb_array_elements(r.plan->'sections') WITH ORDINALITY AS s(value, ordinality)
    )
)
WHERE r.connection_id IS NOT NULL
  AND r.plan IS NOT NULL
  AND jsonb_typeof(r.plan->'sections') = 'array'
  AND EXISTS (
      SELECT 1
      FROM jsonb_array_elements(r.plan->'sections') AS s(value)
      WHERE s.value->>'source' = 'database' AND NOT (s.value ? 'databases')
  )
"""

# Strips the key back off, restoring the pre-#203 shape. Only from sections whose
# routing is the single connection this backfill would have written: a plan
# routed by the app after the upgrade may name a *different* database, and
# discarding that would destroy information rather than reverse this migration.
_DOWNGRADE = """
UPDATE generated_reports AS r
SET plan = jsonb_set(
    r.plan,
    '{sections}',
    (
        SELECT jsonb_agg(
            CASE
                WHEN s.value->>'source' = 'database'
                     AND s.value->'databases'
                         = jsonb_build_array(r.connection_id::text)
                THEN s.value - 'databases'
                ELSE s.value
            END
            ORDER BY s.ordinality
        )
        FROM jsonb_array_elements(r.plan->'sections') WITH ORDINALITY AS s(value, ordinality)
    )
)
WHERE r.connection_id IS NOT NULL
  AND r.plan IS NOT NULL
  AND jsonb_typeof(r.plan->'sections') = 'array'
"""


def upgrade() -> None:
    op.execute(_UPGRADE)


def downgrade() -> None:
    op.execute(_DOWNGRADE)
