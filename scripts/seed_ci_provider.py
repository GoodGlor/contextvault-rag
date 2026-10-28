"""Seed a placeholder verified Gemini key so CI can run the e2e suite (#214).

**This exists because a spec was silently depending on the developer's own
credentials.** ``get_embedder`` refuses with a 409 unless a *verified* Gemini key
is present — embeddings are global, so ingestion cannot start without one — and
``POST .../web-sources`` depends on it. Every developer machine that has ever
ingested anything has such a key, so ``sources.spec.ts`` passed locally for
years; on CI's clean database the POST 409'd, no row was created, and the spec
failed. The suite was right and the environment was incomplete.

The key is a placeholder and cannot call anything. It does not need to: the rows
these specs assert on are created *before* the background ingestion runs, and
that ingestion failing is exactly what already happens for an unreachable URL.
Nothing in the suite asserts an embedding was produced.

**CI only.** ``dev.sh`` does not call this — a developer configures a real key
through the UI, and overwriting it with a placeholder would break their machine's
ingestion for the sake of a test.
"""

import asyncio
import datetime

import sqlalchemy as sa

from contextvault.core.crypto import encrypt
from contextvault.db.session import SessionLocal
from contextvault.models import LLMProviderName, ProviderSetting


async def main() -> None:
    async with SessionLocal() as session, session.begin():
        existing = (
            await session.execute(
                sa.select(ProviderSetting).where(ProviderSetting.provider == LLMProviderName.GEMINI)
            )
        ).scalar_one_or_none()
        if existing is not None:
            print("  Gemini provider already configured; left alone.")
            return
        session.add(
            ProviderSetting(
                provider=LLMProviderName.GEMINI,
                api_key_encrypted=encrypt("ci-placeholder-key"),
                verified_at=datetime.datetime.now(datetime.UTC),
            )
        )
        print("  Seeded a placeholder verified Gemini key (CI only).")


if __name__ == "__main__":
    asyncio.run(main())
