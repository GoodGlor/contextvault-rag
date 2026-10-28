"""Shared FastAPI dependencies for authentication and authorization."""

import uuid
from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.core.config import get_settings
from contextvault.core.tokens import InvalidToken, decode_access_token
from contextvault.db.session import SessionLocal, get_session
from contextvault.embeddings.base import EmbeddingProvider
from contextvault.embeddings.gemini import GeminiEmbeddingProvider
from contextvault.llm import LLMProvider, get_llm_provider
from contextvault.models import LLMProviderName, Repository, Role, User
from contextvault.services import grants as grant_service
from contextvault.services import providers as provider_service
from contextvault.services import users as user_service
from contextvault.services.ingestion import SessionFactory
from contextvault.services.report_design import is_known_template
from contextvault.services.report_style import TEMPLATES

_bearer = HTTPBearer(auto_error=False)

_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_authenticated_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> User:
    """Resolve the bearer token to the current user, or raise 401.

    Authentication only — it does *not* enforce the forced-password-change bounce,
    so the change-password endpoint (the escape hatch) can depend on it while a
    user is flagged. Normal endpoints use :func:`get_current_user` instead.
    """
    if credentials is None:
        raise _UNAUTHENTICATED
    try:
        claims = decode_access_token(credentials.credentials)
        user_id = uuid.UUID(claims.subject)
    except (InvalidToken, ValueError) as exc:
        raise _UNAUTHENTICATED from exc

    user = await user_service.get_user_by_id(session, user_id)
    if user is None:
        raise _UNAUTHENTICATED
    return user


async def get_current_user(user: User = Depends(get_authenticated_user)) -> User:
    """The authenticated user, bounced if they owe a forced password change.

    A user with ``must_change_password`` set is stopped here — at the single auth
    chokepoint every protected endpoint funnels through — so admin-issued
    recovery forces the change before any normal request succeeds. The change-password
    endpoint depends on :func:`get_authenticated_user` instead, so it stays usable.
    """
    if user.must_change_password:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Password change required before continuing",
        )
    return user


def require_role(*allowed: Role) -> Callable[[User], Awaitable[User]]:
    """Build a dependency that passes only users whose role is in ``allowed``."""

    async def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient privileges",
            )
        return user

    return dependency


require_admin = require_role(Role.ADMIN)


async def get_repo_or_404(session: AsyncSession, repository_id: uuid.UUID) -> Repository:
    repo = await session.get(Repository, repository_id)
    if repo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repository not found")
    return repo


async def guard_repo_access(session: AsyncSession, user: User, repository_id: uuid.UUID) -> None:
    """The query-endpoint guard: repo must exist (404); non-admins need an active
    grant (403). Admins bypass the grant check."""
    await get_repo_or_404(session, repository_id)
    if user.role != Role.ADMIN and not await grant_service.has_active_grant(
        session, user.id, repository_id
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="No access to this repository"
        )


async def get_embedder(session: AsyncSession = Depends(get_session)) -> EmbeddingProvider:
    """Build the Gemini embedder from the global provider key, or 409.

    Embeddings are global (one vector space for every repository), so the deployment
    must have a verified Gemini key — even for repositories that chat through another
    provider. Without it, ingestion and querying cannot embed, so fail fast with a
    clear, actionable message rather than deep in the pipeline.
    """
    settings = get_settings()
    key = await provider_service.get_provider_key(session, LLMProviderName.GEMINI)
    if key is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Configure a verified Gemini API key to enable embeddings: embeddings "
                "for every repository are generated by Gemini."
            ),
        )
    return GeminiEmbeddingProvider(
        api_key=key, model_name=settings.embedding_model, dimension=settings.embedding_dim
    )


def get_ingestion_session_factory() -> SessionFactory:
    """Dependency yielding the session factory background ingestion opens.

    Defaults to ``SessionLocal`` so the background task gets its own session; tests
    override it to run ingestion inside their transaction.
    """
    return SessionLocal


RepoLLMBuilder = Callable[[AsyncSession, Repository], Awaitable[LLMProvider]]


async def build_repo_llm(session: AsyncSession, repo: Repository) -> LLMProvider:
    """Build the LLM provider a repository is configured to generate with.

    A repository picks a provider + model; the API *key* is shared from the global
    provider settings (``ProviderSetting``). Routing looks up that provider's
    credentials via ``get_call_credentials`` (decrypting the key in memory when one
    is stored) and constructs the matching provider so the query loop generates
    through the repo's chosen LLM. The query endpoint refuses repositories that
    aren't answerable (409) before reaching here, so provider and model are always
    present and the provider setting is always verified — though the key itself may
    be absent for a keyless ``custom`` (OpenAI-compatible) endpoint, which resolves
    to a placeholder credential instead.
    """
    assert repo.llm_provider is not None
    api_key, base_url = await provider_service.get_call_credentials(session, repo.llm_provider)
    return get_llm_provider(
        repo.llm_provider.value, api_key=api_key, model=repo.llm_model, base_url=base_url
    )


def get_llm_builder() -> RepoLLMBuilder:
    """Dependency yielding the per-repo provider builder (overridable in tests).

    The query endpoint resolves each request's provider from the
    target repository's chosen provider/model plus the global provider key through
    this seam; tests override it to route to a recording fake instead of constructing
    a real vendor client.
    """
    return build_repo_llm


def ensure_known_template(slug: str | None) -> None:
    """Reject a slug that names no shipped template. ``None`` is not a slug.

    The *write* half of ``report_design``'s asymmetry: reads tolerate an unknown
    template and fall back, because a template dropped in a later step must not
    make old reports unrenderable. A write has the caller present to be told.

    Lives here rather than in ``report_design`` so the service layer stays free
    of HTTP concerns, and in one place rather than two so the two routers that
    call it cannot drift on the message.

    ``None`` passes through: it means "no choice", which is a legitimate state
    and the only way back out of one. Before this, the request schemas rejected
    null and a repository default was a one-way door with no way to clear it.
    """
    if slug is None:
        return
    if not is_known_template(slug):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown report template. Choose one of: {', '.join(sorted(TEMPLATES))}.",
        )
