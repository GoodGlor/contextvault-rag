"""Admin per-repository LLM configuration.

Each repository chooses its own LLM: a provider, a model, and an API key. The key
is encrypted at rest (``core/crypto.py``) and only ever returned masked
(``sk-…•••4f2a``) — never in full after entry. A repository has no system
default; until an admin configures it here, the query endpoint refuses to answer.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.api.deps import ensure_known_template, get_current_user, require_admin
from contextvault.db.session import get_session
from contextvault.llm.models import ModelListError, list_models
from contextvault.models import LLMProviderName, Repository, User
from contextvault.services import grants as grant_service
from contextvault.services import providers as provider_service
from contextvault.services.report_design import design_payload, load_design

router = APIRouter(tags=["repositories"])


class LLMConfigRequest(BaseModel):
    """Admin's choice of which model a repository answers with.

    Only a provider + model — the API key is not per-repository. Keys live once per
    provider (Providers settings); the chosen provider must already have a verified
    key (enforced in the handler), and every repo using it shares that one key.
    """

    provider: LLMProviderName
    model: str = Field(min_length=1)


class ReportTemplateRequest(BaseModel):
    """The repository's default report template, by slug.

    ``None`` clears the choice, returning new reports to the shipped default.
    Before this the field was ``str`` with ``min_length=1``, so a default once set
    could never be unset through the API — a one-way door that needed direct SQL
    to undo.
    """

    template: str | None = None


class ReportTemplateResponse(BaseModel):
    """The saved slug, or ``None`` when the repository has made no choice and
    its new reports therefore get the default template."""

    template: str | None


class ListModelsRequest(BaseModel):
    """Ask a provider for its available models, using that provider's global key.

    The provider must have a verified key in the Providers settings; there is no
    per-request key — the model list is fetched with the stored, shared credential.
    """

    provider: LLMProviderName


class ListModelsResponse(BaseModel):
    """The model ids a provider currently offers, for the admin dropdown."""

    models: list[str]


class RepositoryCreateRequest(BaseModel):
    """Admin-supplied details for a new repository."""

    name: str = Field(min_length=1, max_length=255)
    description: str | None = None


class RepositoryUpdateRequest(BaseModel):
    """Partial update for a repository. Omitted fields are left
    unchanged; an explicit ``description: null`` clears the description."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None


class RepositoryDeleteRequest(BaseModel):
    """Confirmation payload: the client must echo the repo's name to delete it."""

    confirm_name: str


class RepositoryResponse(BaseModel):
    """A repository as seen by a user choosing where to ask (their repo picker)."""

    id: uuid.UUID
    name: str
    description: str | None

    model_config = {"from_attributes": True}


class AdminRepositoryResponse(BaseModel):
    """A repository as the admin manages it: identity plus whether it can answer.

    ``configured`` is the same predicate the query endpoint gates on — a model is
    picked *and* its provider has a verified key — so the admin list can flag repos
    that can't yet answer. No key is ever included; keys live in Providers settings.
    """

    id: uuid.UUID
    name: str
    description: str | None
    configured: bool


def _admin_response(repo: Repository, verified: set[LLMProviderName]) -> AdminRepositoryResponse:
    """Serialize a repo for the admin list. ``verified`` is the set of providers with a
    working key, so ``configured`` (answerable) is decided without a per-repo query."""
    answerable = repo.llm_selected and repo.llm_provider in verified
    return AdminRepositoryResponse(
        id=repo.id,
        name=repo.name,
        description=repo.description,
        configured=answerable,
    )


class LLMConfigResponse(BaseModel):
    """A repository's chosen provider/model and whether it can answer.

    No key is included — keys are global (Providers settings). ``configured`` means
    answerable: a model is picked and its provider has a verified key.
    """

    provider: LLMProviderName | None
    model: str | None
    configured: bool


def _config_response(repo: Repository, *, answerable: bool) -> LLMConfigResponse:
    """Serialize a repo's model choice plus its answerability (computed by the caller,
    which can see the global provider keys)."""
    return LLMConfigResponse(
        provider=repo.llm_provider,
        model=repo.llm_model,
        configured=answerable,
    )


async def _get_repo(session: AsyncSession, repository_id: uuid.UUID) -> Repository:
    repo = await session.get(Repository, repository_id)
    if repo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repository not found")
    return repo


@router.get("/repositories")
async def list_repositories(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[RepositoryResponse]:
    """List the repositories the caller can actively reach (their granted, non-expired
    repos) — the picker for "which vault do I ask?". A user never
    sees repositories they haven't been granted."""
    repos = await grant_service.list_accessible_repositories(session, user.id)
    return [RepositoryResponse.model_validate(r) for r in repos]


@router.post("/repositories", status_code=status.HTTP_201_CREATED)
async def create_repository(
    payload: RepositoryCreateRequest,
    admin: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> AdminRepositoryResponse:
    """Create a repository (admin-only). It starts
    unconfigured — an admin must set its LLM provider/model/key before it can answer.
    The creating admin is granted access immediately, so they don't have to grant
    themselves before they can use the repo they just made."""
    repo = Repository(name=payload.name, description=payload.description)
    session.add(repo)
    await session.flush()  # populate repo.id (UUID default is applied on flush) before granting
    await grant_service.grant_access(
        session, user_id=admin.id, repository_id=repo.id, expires_at=None
    )
    await session.commit()
    await session.refresh(repo)
    verified = await provider_service.verified_provider_names(session)
    return _admin_response(repo, verified)


@router.get("/admin/repositories")
async def list_all_repositories(
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> list[AdminRepositoryResponse]:
    """List *every* repository with its config state (admin-only). Distinct
    from ``GET /repositories``, which is scoped to the caller's granted repos."""
    result = await session.execute(select(Repository).order_by(Repository.created_at))
    verified = await provider_service.verified_provider_names(session)
    return [_admin_response(r, verified) for r in result.scalars().all()]


@router.patch("/repositories/{repository_id}")
async def update_repository(
    repository_id: uuid.UUID,
    payload: RepositoryUpdateRequest,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> AdminRepositoryResponse:
    """Update a repository's name and/or description (admin-only).

    Only the fields present in the request are applied; an explicit ``description:
    null`` clears it, while omitting a field leaves it unchanged."""
    repo = await _get_repo(session, repository_id)
    data = payload.model_dump(exclude_unset=True)
    if data.get("name") is not None:
        repo.name = data["name"]
    if "description" in data:
        repo.description = data["description"]
    await session.commit()
    await session.refresh(repo)
    verified = await provider_service.verified_provider_names(session)
    return _admin_response(repo, verified)


@router.delete("/repositories/{repository_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_repository(
    repository_id: uuid.UUID,
    payload: RepositoryDeleteRequest,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete a repository, confirmation-gated by echoing its name (admin-only, card
    #89). Its sources, chunks, and grants cascade away with it (FK ``ON DELETE
    CASCADE``)."""
    repo = await _get_repo(session, repository_id)
    if payload.confirm_name != repo.name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Name confirmation does not match",
        )
    await session.delete(repo)
    await session.commit()


@router.put("/repositories/{repository_id}/llm-config")
async def set_llm_config(
    repository_id: uuid.UUID,
    payload: LLMConfigRequest,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> LLMConfigResponse:
    """Pick the provider + model a repository answers with.

    The API key is not set here — it is shared from the global provider settings. The
    chosen provider must already have a verified key (400 otherwise); the repo then
    answers through that provider using the shared credential.
    """
    repo = await _get_repo(session, repository_id)
    if payload.provider not in await provider_service.verified_provider_names(session):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This provider has no verified API key; add one in Providers settings first.",
        )
    repo.llm_provider = payload.provider
    repo.llm_model = payload.model
    await session.commit()
    await session.refresh(repo)
    answerable = await provider_service.repo_is_answerable(session, repo)
    return _config_response(repo, answerable=answerable)


@router.get("/repositories/{repository_id}/llm-config")
async def get_llm_config(
    repository_id: uuid.UUID,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> LLMConfigResponse:
    """Read a repository's chosen provider/model and whether it can answer."""
    repo = await _get_repo(session, repository_id)
    answerable = await provider_service.repo_is_answerable(session, repo)
    return _config_response(repo, answerable=answerable)


@router.put("/repositories/{repository_id}/report-template")
async def set_report_template(
    repository_id: uuid.UUID,
    payload: ReportTemplateRequest,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ReportTemplateResponse:
    """Choose the template new reports in this repository inherit.

    Existing reports are untouched, by design: each one snapshotted its template
    when it was generated, so a change here can never restyle a finished report.
    """
    repo = await _get_repo(session, repository_id)
    ensure_known_template(payload.template)
    repo.report_template = None if payload.template is None else design_payload(payload.template)
    await session.commit()
    return ReportTemplateResponse(template=payload.template)


@router.get("/repositories/{repository_id}/report-template")
async def get_report_template(
    repository_id: uuid.UUID,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ReportTemplateResponse:
    """The repository's default template, or ``None`` if none was chosen."""
    repo = await _get_repo(session, repository_id)
    return ReportTemplateResponse(template=load_design(repo.report_template).template)


@router.post("/repositories/{repository_id}/llm-models")
async def list_llm_models(
    repository_id: uuid.UUID,
    payload: ListModelsRequest,
    _: User = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> ListModelsResponse:
    """Fetch ``payload.provider``'s available models, for the admin model dropdown.

    Uses the provider's global (verified) key from the Providers settings; if that
    provider has no key, returns 400. A provider failure (network) is a 400 too.
    """
    await _get_repo(session, repository_id)  # 404 if the repo is unknown
    if payload.provider not in await provider_service.verified_provider_names(session):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This provider has no verified API key; add one in Providers settings first.",
        )
    key, base_url = await provider_service.get_call_credentials(session, payload.provider)
    try:
        models = await list_models(payload.provider.value, key, base_url=base_url)
    except ModelListError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return ListModelsResponse(models=models)
