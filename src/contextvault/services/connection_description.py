"""Writing a connection's description without being asked (#212).

#202 gave a connection the *ability* to describe itself and left the trigger to
an admin pressing a button; the owner's requirement is that it simply happens,
and happens again whenever the allow-list changes. This module is that trigger's
worker.

**It deliberately does not overrule #202's objection — it satisfies it.** That
card refused to generate inside ``PATCH .../schema`` because doing so makes every
allow-list edit wait on a provider round-trip and fail when one is down. Running
here, after the save has committed and answered, keeps the save exactly as fast
and exactly as independent of any provider as it was.

**Nothing in here may raise.** It runs detached from the request that queued it,
so a failure has no caller to report to and no response to fail; a connection
keeps its previous description, its allow-list and its usability, which is #202's
rule unchanged — *nothing about querying a database depends on it having been
described*. The explicit ``POST .../describe`` remains how an admin sees the
provider's actual reason.
"""

import logging
import uuid

from contextvault.llm.textgen import TextGenError
from contextvault.models import DatabaseConnection, Repository
from contextvault.services import providers as provider_service
from contextvault.services.ingestion import SessionFactory
from contextvault.services.report_llm import (
    DescriptionUnavailable,
    generate_connection_description,
    schema_fingerprint,
)

logger = logging.getLogger(__name__)


def description_is_current(connection: DatabaseConnection) -> bool:
    """Whether this connection already has a description of *this* allow-list.

    The one condition both the dispatcher and the worker ask, so they cannot
    disagree about what "needs describing" means. False covers both cases the
    owner named — a connection nobody has described, and one whose tables
    changed — because ``description_schema_hash`` is empty in the first and
    stale in the second.
    """
    return bool(connection.description) and connection.description_schema_hash == (
        schema_fingerprint(connection.exposed_schema)
    )


async def run_connection_description(
    connection_id: uuid.UUID,
    language: str | None = None,
    *,
    session_factory: SessionFactory = None,  # type: ignore[assignment]
) -> None:
    """BackgroundTasks entrypoint: fresh session, load, describe, never raise.

    ``language`` is the interface language of the save that triggered this
    (#215), carried rather than stored: every trigger today is a request, and a
    request knows what the admin is reading.
    """
    if session_factory is None:  # imported lazily so tests can pass their own
        from contextvault.db.session import SessionLocal

        session_factory = SessionLocal
    async with session_factory() as session:
        connection = await session.get(DatabaseConnection, connection_id)
        if connection is None:
            return  # deleted between the save and this task
        # Re-checked here, not just at dispatch: two saves in quick succession
        # queue two tasks, and the second has nothing left to do.
        if description_is_current(connection) or not connection.exposed_schema:
            return
        repo = await session.get(Repository, connection.repository_id)
        if repo is None or not await provider_service.repo_is_answerable(session, repo):
            return
        assert repo.llm_provider is not None  # repo_is_answerable proved it
        try:
            api_key, base_url = await provider_service.get_call_credentials(
                session, repo.llm_provider
            )
            description = await generate_connection_description(
                repo.llm_provider.value,
                api_key,
                repo.llm_model or "",
                base_url=base_url,
                exposed_schema=connection.exposed_schema,
                db_type=connection.db_type,
                language=language,
            )
        except (TextGenError, DescriptionUnavailable, ValueError) as exc:
            # Logged, not raised, and nothing on the row is touched.
            logger.warning("Could not describe connection %s: %s", connection_id, exc)
            return
        connection.description = description
        connection.description_schema_hash = schema_fingerprint(connection.exposed_schema)
        await session.commit()
