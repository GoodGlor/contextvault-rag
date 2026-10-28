"""The report asset library — upload, list, fetch, delete.

Scoped to a repository in the path *and* in every query. An asset id alone is
not authority: the row must belong to the repository named in the URL, or the
answer is 404. Grants in this app are per repository, so a lookup by id alone
would let one repository's report print another's bytes.
"""

import uuid
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from contextvault.api.deps import get_current_user, guard_repo_access
from contextvault.db.session import get_session
from contextvault.models import ReportAsset, User
from contextvault.services.report_assets import validate_upload

router = APIRouter(tags=["report-assets"])


class ReportAssetResponse(BaseModel):
    """An asset's facts. Deliberately never its bytes.

    The same reasoning that made ``_list_statement`` use ``load_only`` so a
    4 MiB ``sections_data`` is not fetched for a row the UI is merely listing.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    filename: str
    mime: str
    width: int
    height: int
    byte_size: int


async def _own_asset_or_404(
    session: AsyncSession, repository_id: uuid.UUID, asset_id: uuid.UUID
) -> ReportAsset:
    asset = await session.scalar(
        sa.select(ReportAsset).where(
            ReportAsset.id == asset_id, ReportAsset.repository_id == repository_id
        )
    )
    if asset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found.")
    return asset


@router.post(
    "/repositories/{repository_id}/report-assets",
    status_code=status.HTTP_201_CREATED,
    response_model=ReportAssetResponse,
)
async def upload_report_asset(
    repository_id: uuid.UUID,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ReportAsset:
    """Store an uploaded image for this repository's reports to print."""
    await guard_repo_access(session, user, repository_id)
    raw = await file.read()
    try:
        image = validate_upload(raw, filename=file.filename or "upload")
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    asset = ReportAsset(
        repository_id=repository_id,
        mime=image.mime,
        filename=file.filename or "upload",
        width=image.width,
        height=image.height,
        byte_size=len(image.data),
        data=image.data,
        created_by=user.id,
    )
    session.add(asset)
    await session.commit()
    return asset


_LIST_COLUMNS = (
    ReportAsset.id,
    ReportAsset.filename,
    ReportAsset.mime,
    ReportAsset.width,
    ReportAsset.height,
    ReportAsset.byte_size,
)


def _list_statement(repository_id: uuid.UUID) -> Select[Any]:
    """Every asset in a repository, newest first, without ``data``.

    ``load_only`` keeps the potentially multi-megabyte ``data`` column off the
    wire for a call that only ever renders facts about each asset — the same
    reasoning behind ``_list_statement`` in ``api/reports.py``. Split out as its
    own function (rather than inlined in the route) so the projected columns
    can be asserted on directly, the same way ``api/reports.py`` does.
    """
    return (
        sa.select(ReportAsset)
        .options(load_only(*_LIST_COLUMNS))
        .where(ReportAsset.repository_id == repository_id)
        .order_by(ReportAsset.created_at.desc())
    )


@router.get("/repositories/{repository_id}/report-assets", response_model=list[ReportAssetResponse])
async def list_report_assets(
    repository_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ReportAsset]:
    """The library, newest first. Never the bytes."""
    await guard_repo_access(session, user, repository_id)
    rows = await session.scalars(_list_statement(repository_id))
    return list(rows)


@router.get("/repositories/{repository_id}/report-assets/{asset_id}")
async def get_report_asset_bytes(
    repository_id: uuid.UUID,
    asset_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """The bytes, with the content-type they were stored under."""
    await guard_repo_access(session, user, repository_id)
    asset = await _own_asset_or_404(session, repository_id, asset_id)
    return Response(content=asset.data, media_type=asset.mime)


@router.delete(
    "/repositories/{repository_id}/report-assets/{asset_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_report_asset(
    repository_id: uuid.UUID,
    asset_id: uuid.UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    """Explicit removal. A design still naming it renders without the image."""
    await guard_repo_access(session, user, repository_id)
    asset = await _own_asset_or_404(session, repository_id, asset_id)
    await session.delete(asset)
    await session.commit()
