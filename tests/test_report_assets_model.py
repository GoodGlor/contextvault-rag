"""The first table in this app that stores bytes a user uploaded.

Every other binary column (``pdf_data``, ``pptx_data``) holds a *generated*
artifact, and uploaded source files are discarded after OCR — see
``ingestion.py``, which sets ``source.content = parsed.text``.
"""

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from contextvault.models import ReportAsset, Repository


async def _repo(session: AsyncSession) -> Repository:
    repo = Repository(name="Бухгалтерія", description="test")
    session.add(repo)
    await session.flush()
    return repo


@pytest.mark.asyncio
async def test_an_asset_round_trips_its_bytes(db_session: AsyncSession) -> None:
    repo = await _repo(db_session)
    asset = ReportAsset(
        repository_id=repo.id,
        mime="image/png",
        filename="logo.png",
        width=320,
        height=64,
        byte_size=4,
        data=b"\x89PNG",
    )
    db_session.add(asset)
    await db_session.flush()

    found = await db_session.get(ReportAsset, asset.id)
    assert found is not None
    assert found.data == b"\x89PNG"
    assert found.mime == "image/png"
    assert found.width == 320


@pytest.mark.asyncio
async def test_deleting_the_repository_takes_its_assets(db_session: AsyncSession) -> None:
    """Repository delete cascades; report delete does not (assets are shared)."""
    repo = await _repo(db_session)
    db_session.add(
        ReportAsset(
            repository_id=repo.id,
            mime="image/png",
            filename="logo.png",
            width=1,
            height=1,
            byte_size=1,
            data=b"x",
        )
    )
    await db_session.flush()

    await db_session.delete(repo)
    await db_session.flush()

    remaining = await db_session.scalar(sa.select(sa.func.count()).select_from(ReportAsset))
    assert remaining == 0
