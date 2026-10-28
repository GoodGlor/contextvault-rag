# ContextVault — Architecture & reference

Detailed reference for ContextVault's backend and frontend, split out of the
[README](../README.md) to keep the front page lean. It documents each subsystem's
behavior and API. The live,
interactive API reference is at `/docs` (Swagger) when the server is running.

## Contents

- [Authentication](#authentication)
- [First-admin bootstrap](#first-admin-bootstrap)
- [Invitations (onboarding)](#invitations-onboarding)
- [Provider API-key encryption](#provider-api-key-encryption)
- [Embeddings](#embeddings)
- [Document parsing](#document-parsing)
- [Chunking](#chunking)
- [Ingestion pipeline](#ingestion-pipeline)
- [Access grants (admin)](#access-grants-admin)
- [Retrieval (access-filtered vector search)](#retrieval-access-filtered-vector-search)
- [Generation (LLM provider interface)](#generation-llm-provider-interface)
- [Source API (admin)](#source-api-admin)
- [Repository management (admin)](#repository-management-admin)
- [Repository LLM configuration (admin)](#repository-llm-configuration-admin)
- [Query API (the full RAG loop)](#query-api-the-full-rag-loop)
- [Reports (blended: database + documents)](#reports-blended-database--documents)
- [Frontend (React SPA)](#frontend-react-spa)

## Authentication

`POST /auth/login` takes `{"username", "password"}` and returns a JWT:

```json
{ "access_token": "…", "token_type": "bearer", "must_change_password": false }
```

Send it as `Authorization: Bearer <token>` on protected routes. Two auth
dependencies resolve the token to a user (401 otherwise): `get_authenticated_user`
does authentication only, while `get_current_user` adds the forced-password-change
bounce (below) and is what every normal endpoint uses. Build role guards with
`require_role(...)` / `require_admin` (they chain off `get_current_user`, so they
enforce the bounce too; 403 when the role is insufficient). Tune
`ACCESS_TOKEN_EXPIRE_MINUTES` and set a strong `SECRET_KEY` (≥ 32 bytes) in
production.

### Listing users

- `GET /users` (**admin-only**) lists every account — `id`, `username`, `role`,
  `must_change_password`, `created_at` — oldest first. It never includes the
  password hash. This is the enumeration the admin user-management UI drives
  (reset / delete, and picking grant recipients).

### Password recovery & forced change

Account recovery is admin-issued:

- `POST /users/{id}/reset-password` (**admin-only**) issues a **random temporary
  password**, returned **once** in plaintext (only its hash is stored), and sets
  the user's `must_change_password` flag. The admin hands the temp password over
  and never learns the user's eventual real password.

  ```json
  { "temporary_password": "…", "must_change_password": true }
  ```

- **Enforcement:** while `must_change_password` is set, `get_current_user` bounces
  the user with `403 Password change required before continuing` — so *every*
  normal endpoint is blocked at the single auth chokepoint until the password is
  changed. `login` (unauthenticated) and `change-password` are the only reachable
  routes.

- `POST /auth/change-password` (**authenticated**, the escape hatch) takes
  `{"current_password", "new_password"}` (new password ≥ 8 chars), verifies the
  current password, sets the new one, and **clears the flag**. It depends on
  `get_authenticated_user`, so a bounced user can still reach it; it returns a
  fresh token whose `must_change_password` is now false.

An optional expiry on the temporary password is out of scope for this card.

### Deleting a user

Removing a user is destructive, so it is **confirmation-gated** and preserves
analytics signal by anonymizing rather than erasing:

- `DELETE /users/{id}` (**admin-only**) permanently deletes a user. The request
  body must **echo the target's username** — `{"confirm_username": "<name>"}` — or
  the call is a `400` no-op. Success returns `204 No Content`.

- **Cascade vs. detach.** The user's access grants are **cascade-deleted** (their
  access vanishes with the account), while their contributions are **detached**:
  admin-authored sources keep existing with `created_by = NULL` ("by a deleted
  user") instead of being deleted, so curation/analytics signal survives. This is
  enforced at the database (`grants.user_id ON DELETE CASCADE`,
  `sources.created_by ON DELETE SET NULL`).

- **Last-admin guard.** Deleting the **last remaining admin** is refused with
  `409 Conflict`, so the system can never be locked out of its bootstrap invariant.

> The user's **past questions are anonymized too** : the `query_logs.user_id` FK is `ON DELETE SET NULL`, so this same
> delete detaches their logged queries ("asked by a deleted user") instead of
> erasing them — the analytics signal survives the account (see *Query logging*).

## First-admin bootstrap

Onboarding is invite-based, but the first admin has no one to invite them. Seed one
(idempotent — a no-op once any admin exists):

```bash
# credentials via flags…
uv run python -m contextvault.cli create-admin --username admin --password '<strong>'

# …or via the environment (INITIAL_ADMIN_USERNAME / INITIAL_ADMIN_PASSWORD)
uv run python -m contextvault.cli create-admin
```

## Invitations (onboarding)

New accounts are created by invite, so an admin never sees or handles a user's
password. The flow is two endpoints:

- `POST /invitations` (**admin-only**) issues a single-use, expiring invite for a
  new `username` (optional `role`, default `user`; optional `expires_in_hours`,
  default `INVITE_EXPIRY_HOURS` = 72). It returns the raw token **once** — for the
  invite link — plus the username, role, and `expires_at`. A username that already
  has an account is rejected (409).

  ```json
  { "token": "…", "username": "alice", "role": "user", "expires_at": "…" }
  ```

- `POST /invitations/accept` (**public**) redeems a token: `{"token", "password"}`
  (password ≥ 8 chars). It creates the account with the user's own chosen password
  (`must_change_password` stays false — distinct from the temp-password recovery
  flow) and marks the invite spent. Returns the new `{id, username, role}` (201).

The token is high-entropy and stored **only as a SHA-256 hash**
(`core/invite_tokens.py`), so a leaked database never yields a usable invite — the
raw token lives solely in the link handed out once (unlike provider keys, which are
reversibly encrypted because they must be recovered; an invite is only ever
compared). An invite is valid while unexpired **and** unaccepted; reused, expired,
and unknown tokens all return one uniform `400 Invalid or expired invitation`, so a
caller cannot probe which tokens exist.

## Provider API-key encryption

Provider API keys are live credentials, so they are **encrypted at rest** and only
ever stored as ciphertext. `core/crypto.py` wraps Fernet (AES-128-CBC + HMAC,
authenticated) behind three helpers:

- `encrypt(plaintext)` / `decrypt(token)` — round-trip a secret to a URL-safe
  ciphertext token and back. Encryption is non-deterministic (random IV per
  message), and a tampered or wrong-key token raises `EncryptionError` rather than
  returning garbage. Keys are decrypted into memory only at call time.
- `mask_key(key)` — a display-safe preview (`sk-…•••4f2a`) for surfacing a stored
  key in the UI/API without ever re-showing it in full.

One provider is the exception to "every key is mandatory": the `custom`
(OpenAI-compatible) provider's key is **optional** (keyless local servers, e.g.
Ollama, are supported) — its `provider_settings` row allows a `NULL`
`api_key_encrypted`. Its `base_url`, unlike the key, is not a secret and is
stored in plaintext alongside the (optional) encrypted key; see *Custom
(OpenAI-compatible)*, below.

The master key comes from `ENCRYPTION_KEY` (a Fernet key) in the environment or
secrets — never in the database or the code. It is unset by default: encryption
fails loudly instead of silently storing plaintext, so it must be set before any
provider key is persisted. Generate one with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Wiring these helpers into per-repository provider configuration lands with the
repo-config card; this card provides the primitive.

## Embeddings

Text is embedded through a pluggable `EmbeddingProvider`. The shipped implementation,
`GeminiEmbeddingProvider`, calls Google's Gemini embedding API
(`gemini-embedding-001`, multilingual, 1024-dim via `output_dimensionality`) instead of
running a model locally — document and query text is sent to the configured Gemini
provider (consistent with how image OCR already sends content to the provider). It
uses Gemini's asymmetric task types (`RETRIEVAL_DOCUMENT` for ingested text,
`RETRIEVAL_QUERY` for questions) and L2-normalizes the returned vectors so retrieval's
cosine similarity is a plain dot product. Embeddings are global — one vector space for
every repository — so a verified global Gemini provider key is required; ingestion and
query both fail fast (409) without one.

### Quota handling

Gemini answers `429 RESOURCE_EXHAUSTED` once a per-minute quota is spent. The
binding limit is a **request count, not a token count** — the free tier reports:

```
quotaId:    EmbedContentRequestsPerMinutePerUserPerProjectPerModel-FreeTier
quotaValue: 100          # requests per minute
retryDelay: 59s          # i.e. the remainder of the current minute
```

A large document reaches that easily: at `chunk_size` 1000 / `chunk_overlap` 150 a
multi-megabyte legal code becomes thousands of windows, batched 100 chunks per
request — dozens of requests. Two defences, plus a retry:

- **Process-wide pacing.** The quota is metered *per project*, so every concurrent
  ingestion and every query-time embedding draws on one budget. `_RequestPacer` in
  `embeddings.gemini` is therefore a module-level singleton holding a lock across
  its sleep: `embedding_requests_per_minute` (default 50) is a **total** rate for
  the process, not a per-upload one. Pacing held per provider instance is *not*
  enough — each upload builds its own provider from the request dependency, so N
  simultaneous uploads sent N times the intended rate and tripped the ceiling
  anyway. Set the value to 0 to disable pacing on a paid tier.
- **Retry with backoff.** A 429 retries up to `embedding_max_retries` (default 6)
  times, waiting `embedding_retry_base_delay` (2s) doubled per attempt and capped at
  `embedding_retry_max_delay` (60s), jittered so concurrent ingests do not retry in
  lockstep. Gemini's own `retryDelay` wins over the computed value when present.
- **Scope.** Only the failing batch is retried; batches already embedded are kept,
  so a 429 late in a long document costs one request rather than a full re-embed.

Non-quota failures (invalid key, malformed request) never retry — they cannot
succeed on a second attempt and propagate immediately as `EmbeddingError`.

Two consequences worth knowing. The pacer serializes embedding requests, so a query
embedding can queue behind a large ingestion for up to one interval. And retrying is
a safety net, not a quota increase: raising `embedding_requests_per_minute` above the
tier's real ceiling just converts pacing into 429s. Limits per tier are visible in
[AI Studio](https://aistudio.google.com/rate-limit).

The pacer is per *process*. A deployment running more than one web dyno or uvicorn
worker would need a shared (e.g. Redis-backed) limiter instead; today Heroku runs a
single dyno.

**This coupling does not loosen when chat goes local.** A repository configured
with the `custom` (OpenAI-compatible) LLM provider (see *Custom
(OpenAI-compatible)*, below) still embeds through Gemini at ingestion time —
embeddings are deployment-wide, not per-repository — so document text still
leaves the network at index time even for a repository whose questions and
answers never do. Decoupling embeddings from Gemini (a local embedding provider
plus per-dimension vector storage) is a planned next phase,
not built here.

Swapping the model is a config change via `EMBEDDING_MODEL` and `EMBEDDING_DIM` —
the two must match the model's requested output width and the pgvector column.

## Document parsing

Uploads are turned into text by `parse_document(filename, data)`, the first stage
of the ingestion pipeline. It supports **PDF, DOCX, TXT, and images** and returns a
`ParsedDocument` — the full text plus positioned `TextBlock`s that tile it exactly.
Each block records its character span and, for PDFs, its 1-based page, so a citation
can later map a character offset back to its source passage.

```python
from contextvault.ingestion import parse_document

doc = parse_document("report.pdf", data)   # data: bytes
doc.text                                    # full extracted text
doc.blocks[0].page, doc.blocks[0].start     # position info for citations
```

Unsupported types raise `UnsupportedDocumentError`; corrupt or invalid files of a
supported type raise `DocumentParseError`.

### Images (local OCR)

Files with an image suffix — `.png .jpg .jpeg .webp .tiff .bmp` (`IMAGE_SUFFIXES`)
— are routed to the image parser instead of a document parser. It decodes the file
with Pillow, then reads text out of it with **local** OCR
([`rapidocr-onnxruntime`](https://github.com/RapidAI/RapidOCR), lazily loaded once
and cached) via `ocr_image` — no third-party OCR service ever sees the image. OCR
is **text-only**: it produces one page-less block of recognized text, with no
layout/position info beyond that. An unreadable image file raises
`DocumentParseError("Could not read image file.")`; an image with no recognizable
text raises `DocumentParseError("No text found in image.")`, which the ingestion
pipeline (below) turns into a `FAILED` source with that message in `ingest_error`
rather than storing an empty source.

## Chunking

The next stage, `chunk_document(parsed)`, slices a `ParsedDocument` into overlapping
`TextChunk`s sized for retrieval. Chunking is character-based, so every chunk keeps
the exact span it came from — `parsed.text[chunk.char_start:chunk.char_end] ==
chunk.text` — which is what later lets a citation jump to the highlighted passage. It
also carries the distinct source `pages` that span touches (empty for page-less
sources), derived from the parsed blocks.

**`chunk_size` is a quota lever as much as a retrieval one.** Gemini meters embedding
per *request*, and requests are `ceil(chunks / 100)`, so the chunk count drives quota
spend directly. The defaults (3000 / 300, from 1000 / 150) cut a multi-megabyte legal
code to roughly a third of the requests, and 3000 chars of Cyrillic — about 1000–1500
tokens — still sits well inside `gemini-embedding-001`'s 2048-token input limit. The
cost is coarser retrieval: a citation points at a larger passage. Note this affects
only newly ingested sources; existing chunks keep their old boundaries until
re-uploaded, so a vault can be mixed-granularity.

```python
from contextvault.ingestion import chunk_document

chunks = chunk_document(doc)                 # or size=/overlap= to override
chunks[0].char_start, chunks[0].char_end     # span into doc.text, for citations
chunks[0].pages                              # e.g. (1, 2) for a chunk crossing a page break
```

Windows advance by `size - overlap` and the final window ends exactly at the end of
the text, so chunks tile it with no redundant tail. Size and overlap default to the
`chunk_size` / `chunk_overlap` settings (1000 / 150 characters) and must satisfy
`0 <= overlap < size`.

## Ingestion pipeline

`ingest_source(session, source, filename=…, data=…, embedder=…)` orchestrates the
full pipeline — **parse → chunk → embed → store** — for one source. It parses the
uploaded bytes, chunks the text, embeds every chunk with the given
[`EmbeddingProvider`](#embeddings), and stores the results as `Chunk` rows (each with
its char offsets and vector). It is **idempotent**: a re-ingest deletes the source's
prior chunks before writing the new ones.

Each `Source` tracks its own ingestion `status` — `pending → processing → done` on
success, or `failed` on error, with the message captured in `ingest_error`. Failures
are recorded on the source rather than raised, so they are never silent; a caller
inspects `source.status`.

```python
from contextvault.services.ingestion import ingest_source, run_ingestion

await ingest_source(session, source, filename="report.pdf", data=data, embedder=embedder)
source.status        # SourceStatus.DONE (or .FAILED, with source.ingest_error set)
```

Because ingestion is slower than a request should block on, `run_ingestion(source_id,
…)` is the seam a handler schedules via FastAPI `BackgroundTasks`: it opens its own
session (the request's is already closed by the time it runs) and delegates to
`ingest_source`.

**Both slow stages run off the event loop**, via `asyncio.to_thread`: parsing *and*
embedding. Background tasks share the dyno's single event loop, so a synchronous
stage there blocks every other request — extracting text from a multi-megabyte PDF
froze the API for as long as it took, including the admin UI's own status polls.

### Ingestion progress

A source carries `chunks_total` / `chunks_done` alongside its status, so the admin UI
can draw a real percentage instead of an indeterminate spinner while a large document
ingests. `store_parsed` writes `chunks_total` *before* any embedding starts — the
first poll already has a denominator — then embeds in slices of
`EMBED_PROGRESS_SLICE` (100, matching the provider's request batch).

**Each slice's `Chunk` rows are written as that slice lands**, not accumulated for a
single flush at the end. Holding every vector until the end cost roughly 200 MB of
Python floats for a 7,000-chunk document, which a 512 MB dyno cannot afford alongside
the parsed text. Committing between slices also releases the pooled DB connection
across the slow call, the same care `_ocr_image` takes.

### Resuming a failed ingest

Chunks written before a failure are **kept**, and a re-ingest resumes from them.
Embedding is the expensive, rate-limited step, and under a daily quota a retry that
re-embeds from zero re-spends the whole document — which is how an allowance
disappears. So `_reusable_prefix` compares stored chunk text against the freshly
computed chunks and returns how many leading chunks still match; `store_parsed`
deletes only from that point on and embeds only the tail.

Reuse is a **prefix match on content**, not a count. If the document changed or the
chunking settings changed, the first mismatch ends the prefix and everything from
there is re-embedded — which keeps resuming correct without tracking a content hash,
and makes a `chunk_size` change safely invalidate the old chunks.

This is what requires retrieval to filter on source status: rows exist for sources
that never finished, so `search_chunks` joins `sources` and takes only `DONE` ones.
Without that, a part-embedded source would answer questions from a fragment of
itself.

Failures are also **logged**, not only stored on the source: a failure that exists
only in a database row is invisible in `heroku logs`, which makes production
diagnosis guesswork.

Slicing does not reintroduce the request bursts that [quota
handling](#quota-handling) exists to prevent: `GeminiEmbeddingProvider` paces per
instance rather than per call, so its spacing spans the slices of one document.

A failed ingest keeps the counters it reached — "stopped at 1900/4042" is diagnostic
— and re-ingesting overwrites them. Both are nullable: sources ingested before the
counters existed report no progress, and the UI falls back to the status badge alone.

`ingest_source` and `run_web_ingestion` (below) share a single writer,
`store_parsed(session, source, parsed, embedder)` — chunk → embed → replace-chunks →
mark `DONE` — so document/image ingestion and web ingestion converge on identical
storage behavior once they have a `ParsedDocument`.

### Web-link sources

`run_web_ingestion(source_id, url=…, embedder=…)` is the background-task seam for a
web-link source (mirrors `run_ingestion`): it marks the source `PROCESSING`, fetches
the page and extracts its main text off the event loop, then hands the result to
`store_parsed`. A page with no extractable text fails the source with `"No readable
text found at URL."`; on success, a title recovered from the page's metadata
replaces the source's title (the URL).

The fetch itself, `web_source.fetch_html(url)`, guards against fetching an
untrusted or hostile URL:

- **Scheme:** only `http`/`https` are accepted.
- **SSRF:** every hostname — including on each redirect hop — is resolved and
  rejected if any resolved address is private, loopback, link-local, reserved,
  multicast, or unspecified, so a URL can't be used to reach internal
  infrastructure.
- **Redirects:** followed manually (not via the HTTP client) up to 5 hops, each
  re-validated the same way; more hops raise `"Too many redirects."`.
- **Content type:** non-HTML/text responses are rejected.
- **Size cap:** the body is streamed and capped at 5 MiB; exceeding it raises
  `"Response exceeds the size cap."`.
- **Timeout:** a 15-second request timeout.

`web_source.extract_web_text(html)` then pulls the main article text (and title)
out of the fetched HTML with [trafilatura](https://trafilatura.readthedocs.io/),
discarding boilerplate (nav, ads, footers).

## Access grants (admin)

Access is a per-user, per-repository **grant** (many-to-many), optionally
time-boxed with an `expires_at`. An admin manages grants; every
retrieval is hard-filtered to the caller's **active** (non-expired) grants.

| Endpoint | Behavior |
|---|---|
| `POST /repositories/{id}/grants` | **Admin-only.** Grant a user access: body `{"user_id": "…", "expires_at": "<ISO-8601>"\|null}`. **Idempotent** — re-granting the same pair updates the expiry (never a duplicate). `404` if the repo or user is unknown. |
| `DELETE /repositories/{id}/grants/{user_id}` | **Admin-only.** Revoke a grant. `204` on success, `404` if no such grant. |
| `GET /repositories/{id}/grants` | **Admin-only.** List every grant on a repository (including expired ones — the audit view). |
| `GET /repositories` | **Any authenticated user.** The caller's repo picker: the repositories they hold an **active** grant on — never others', never expired ones. |

A grant with `expires_at` in the past grants nothing: the repository disappears
from `GET /repositories` and retrieval/query deny access with `403`. The
active-grant rule (`expires_at IS NULL OR expires_at > now()`) is the same one the
retrieval query and the query endpoint enforce, so management and enforcement never
drift.

## Retrieval (access-filtered vector search)

`search_chunks(session, user_id=…, repository_id=…, query_embedding=…)` is the
RAG loop's core access boundary. It runs the similarity search and the permission
check as a **single SQL query**: `chunks` is joined to `grants`, so a user only
ever retrieves from a repository they hold an **active (non-expired)** grant on —
the boundary lives in the query, not in app code layered on top.

```python
from contextvault.retrieval import search_chunks

hits = search_chunks(
    session, user_id=user.id, repository_id=repo.id, query_embedding=vector, k=5
)
hits[0].content, hits[0].score            # passage text + cosine similarity (higher = closer)
hits[0].char_start, hits[0].char_end      # source offsets, for citation highlighting
```

Results are the top-k chunks ordered by cosine similarity (`k` defaults to the
`retrieval_top_k` setting). Chunks without an embedding are skipped, and so are
chunks of any source that is not `DONE` — see *[Resuming a failed
ingest](#resuming-a-failed-ingest)*, which leaves rows behind for sources that never
finished. Similarity is
cosine, backed by an **HNSW ANN index** on `chunks.embedding` with
`vector_cosine_ops` (migration `b6be69ab221b`). This is the raw query layer.

> **The HNSW index is approximate, and an index scan can return nothing.** A scan
> visits only `hnsw.ef_search` candidates (40 by default) and the
> `repository_id` / grant / `status` predicates are applied *afterwards*, so a
> candidate budget spent on rows the filter then discards yields zero hits even
> though matching rows exist. Measured on pgvector 0.8.4: with a graph holding
> many entries at identical distance, a forced index scan returned **0** rows
> where a sequential scan returned the 1 live row; raising `hnsw.ef_search`, or
> `VACUUM`ing away the dead entries, restored it. `hnsw.iterative_scan`
> (`strict_order`) also fixes it.
>
> In practice today's queries do **not** take that path: `repository_id = ?` is a
> selective equality with its own btree index, so the planner bitmap-scans that
> and sorts — verified both with statistics and on a never-analyzed table. The
> exposure appears once a single repository grows large enough that
> `repository_id` stops being selective and the planner switches to the ANN
> index. Tracked as a known issue rather than fixed, because nothing reaches it
> yet.

### Retrieval service (question → relevant chunks)

`retrieve(session, question=…, repository_id=…, user_id=…, embedder=…)` is the
service the RAG loop calls. It embeds the question with the system-wide provider,
runs `search_chunks`, then keeps only hits whose cosine similarity clears a
**relevance threshold** (`min_score`, defaulting to the `retrieval_min_score`
setting). Filtering weak matches is what makes the honest "not in this vault"
answer and the knowledge-gap dashboard possible.

The embedder is resolved per-request by the `get_embedder` FastAPI dependency, which
reads the global Gemini key from the database and raises `409` if none is configured
(there is no standalone factory to call outside a request):

```python
from fastapi import Depends
from contextvault.api.deps import get_embedder
from contextvault.embeddings import EmbeddingProvider
from contextvault.retrieval import retrieve

async def ask(
    ...,
    embedder: EmbeddingProvider = Depends(get_embedder),
):
    result = await retrieve(
        session,
        question="How do I rotate the signing key?",
        repository_id=repo.id,
        user_id=user.id,
        embedder=embedder,
    )
    result.chunks          # relevant hits, ranked closest first (empty → "not in this vault")
    result.has_results     # True when at least one chunk cleared the threshold
    result.top_score       # best similarity among *retrievable* chunks, or None
```

`top_score` distinguishes a **knowledge gap** (chunks exist but none relevant
enough — `top_score` set, `chunks` empty) from an **empty or inaccessible vault**
(`top_score` is `None`). The query endpoint builds on top of this.

## Generation (LLM provider interface)

Answers are generated through a pluggable `LLMProvider`, so any vendor —
Anthropic, OpenAI/OpenRouter, Google — sits behind one contract and the RAG loop
never depends on a vendor SDK. This is the interface only;
concrete providers arrive in later cards.

```python
from contextvault.llm import Answer, Citation, LLMProvider

# every provider implements:
async def answer(self, question: str, chunks: Sequence[RetrievedChunk]) -> Answer: ...
```

An `Answer` is the answer `text` plus a list of `Citation`s. Because only Claude
has native citations, the scheme is **provider-agnostic**: the retrieved chunks
are numbered `[1..n]`, the model is told to cite those numbers, and each
`Citation` maps a marker back to its exact source span:

```python
Citation(number=1, chunk_id=…, source_id=…, char_start=120, char_end=170)
```

`char_start`/`char_end` are `None` when the cited chunk had no positional offsets.
An `Answer` also carries a **`not_in_vault`** flag — the first-class honest "not in
this vault" signal. It is `True` when the answer grounds nothing in the repository:
either retrieval surfaced no relevant chunks (the provider short-circuits) or the
model, given chunks, cited none of them. Downstream — the query endpoint and the
knowledge-gap dashboard — reads this flag rather than inferring the refusal from an
empty citation list, so a curated vault states it doesn't cover the question
instead of answering from the model's own training data.

### Providers and default selection

`get_llm_provider()` builds a provider behind the shared contract, so callers
never name a vendor SDK. Called bare it returns the system-default provider chosen
by the `LLM_PROVIDER` setting (default **`gemini`**); called with a name — and
optionally an `api_key` and `model` — it builds that specific provider, which is
how per-repo routing constructs each repository's own LLM (see *Repository LLM
configuration*). Omitted `api_key`/`model` fall back to the settings defaults:

```python
from contextvault.llm import get_llm_provider

provider = get_llm_provider()               # honours LLM_PROVIDER (default: gemini)
answer = await provider.answer(question, chunks)

# per-repo routing: a specific provider built from stored config
repo_provider = get_llm_provider("openai", api_key=key, model="gpt-4o")
```

All providers share the same behaviour: they lay the retrieved chunks out under
`[1..n]` markers, instruct the model to answer **only** from them, and parse the
`[n]` markers in the reply back into `Citation`s — no vendor-native citation
feature is used, so the citation experience is identical across providers. Empty
`chunks` short-circuit to the honest "not in this vault" answer (`not_in_vault=True`)
without an API call, and an answer that cites none of its sources is flagged the
same way. That numbered-chunk prompt/parse/map machinery lives in one shared module,
[`contextvault.llm.citations`](#numbered-chunk-citation-scheme), which every
provider imports. `get_llm_provider()` wires all five providers — **Gemini**,
**OpenAI**, **OpenRouter**, **Anthropic**, and **Custom** (any self-hosted
OpenAI-compatible server) — selectable by name (or, for the system default, via
`LLM_PROVIDER`).

#### Google (Gemini) — default

`GeminiLLMProvider` (via the Google GenAI SDK) is the current default.
Configuration (`.env` / settings): `GEMINI_API_KEY` authenticates the SDK
(falling back to the SDK's own `GEMINI_API_KEY` / `GOOGLE_API_KEY` resolution),
`GEMINI_MODEL` selects the model (default `gemini-2.5-flash`), and
`LLM_MAX_TOKENS` caps the answer length.

#### OpenAI (ChatGPT)

`OpenAILLMProvider` (via the OpenAI SDK's Chat Completions API) is selectable with
`LLM_PROVIDER=openai`. Configuration: `OPENAI_API_KEY` authenticates the SDK
(falling back to the SDK's own `OPENAI_API_KEY` resolution), `OPENAI_MODEL`
selects the model (default `gpt-4o`), and `LLM_MAX_TOKENS` caps the answer length.

#### OpenRouter (OpenAI-compatible gateway)

`OpenRouterLLMProvider` reaches [OpenRouter](https://openrouter.ai) — a single
gateway to hundreds of models — over the OpenAI-compatible wire format, so it
**subclasses `OpenAILLMProvider`** and reuses its request shape and citation
machinery unchanged; only the client is re-aimed at OpenRouter's base URL.
Selectable with `LLM_PROVIDER=openrouter`. Configuration: `OPENROUTER_API_KEY`
authenticates the SDK, `OPENROUTER_MODEL` selects the model — ids are
vendor-namespaced, e.g. `openai/gpt-4o` (the default) or
`anthropic/claude-3.5-sonnet` — `OPENROUTER_BASE_URL` overrides the gateway
endpoint (default `https://openrouter.ai/api/v1`), and `LLM_MAX_TOKENS` caps the
answer length.

#### Anthropic (Claude)

`AnthropicLLMProvider` (via the official Anthropic SDK) is selectable through
`get_llm_provider("anthropic")` — the factory wire-up that lets a repository
configured for Anthropic route to it. Configuration: `ANTHROPIC_API_KEY`
authenticates the SDK, `ANTHROPIC_MODEL` selects the Claude model (default
`claude-opus-4-8`), and `LLM_MAX_TOKENS` caps the answer length.

#### Custom (OpenAI-compatible) — self-hosted / local

**"Custom (OpenAI-compatible)"** lets a deployment point chat (and report
generation, and image OCR) at an LLM it runs itself — Ollama, vLLM, LM Studio,
TGI, LocalAI, or any server that speaks the OpenAI `/v1` API — instead of a
cloud vendor.

Unlike the four cloud providers, `custom` has no vendor SDK of its own: selecting
it constructs `OpenAILLMProvider` directly, aimed at an admin-supplied
`base_url` instead of OpenAI's endpoint — the same trick `OpenRouterLLMProvider`
uses (by subclassing it) to reach OpenRouter's gateway. It is also **one global
endpoint, not a per-vendor key**: an admin sets a `base_url` (stored on the
`custom` row's `provider_settings.base_url`) plus an **optional** API key —
`api_key_encrypted` is nullable for this row, so keyless local servers (the
common Ollama case) are supported, unlike every cloud provider. OpenAI-compatible
servers still require *some* non-empty `Authorization` header even when they
ignore it, so when no key is stored a placeholder (`"sk-noauth"`) is sent
instead — it is constructed only at call time and never persisted.

One resolution seam feeds every call site that needs credentials for a provider:

```python
from contextvault.services.providers import get_call_credentials

api_key, base_url = await get_call_credentials(session, provider)
# custom, keyless: (NOAUTH_PLACEHOLDER, "http://localhost:11434/v1")
# custom, keyed:   (decrypted key,      "http://localhost:11434/v1")
# cloud providers: (decrypted key,      None)
```

`deps.build_repo_llm` (chat), `services/reports.py` (NL→SQL report generation),
`services/ingestion.py` (image OCR), and the repository model-list endpoint all
resolve a provider's key/endpoint through this one function, so there is a
single place a base URL or key could go stale rather than four independent
copies of the same lookup.

The model is chosen **per repository**, the same mechanic as every other
provider (`PUT`/`GET .../llm-config`): the model `<select>` loads from the
endpoint's own `GET /v1/models` when the server exposes it, falling back to
**free-text entry** for servers that don't (or for typing an arbitrary local
model id directly).

**The load-bearing caveat: this is not air-gapped.** Selecting `custom` moves
only the *chat/report/OCR* model onto a self-hosted server — ingestion still
calls **Gemini** to embed documents (see *Embeddings*, above), so document text
still leaves the network at index time. Do not describe this provider as
air-gapped or as keeping data off the network; removing the Gemini coupling is
Phase 2 (local embeddings + per-dimension vector tables), not built here.

#### Numbered-chunk citation scheme

The one place the citation machinery lives — every provider imports it, so
citations behave identically no matter which vendor generated the answer:

```python
from contextvault.llm.citations import (
    SYSTEM_PROMPT,        # grounding contract: answer only from the numbered sources
    NOT_IN_VAULT,         # the honest refusal text
    not_in_vault_answer,  # the flagged (not_in_vault=True) refusal Answer, no API call
    build_user_message,   # lays chunks out as [1..n] sources + the question
    parse_citations,      # maps the [n] markers in the reply back to source spans
)
```

`build_user_message` numbers the retrieved chunks `[1..n]` in rank order;
`parse_citations` reads the `[n]` markers back out of the model's answer and
resolves each to its exact source span — taking markers in first-appearance
order, collapsing repeats, and dropping any out-of-range (fabricated) marker so a
`Citation` always points at a real retrieved passage. The Anthropic, Gemini,
OpenAI, OpenRouter, and Custom providers only wire this scheme to their vendor
SDK (Custom being the OpenAI provider class itself, aimed at a different
endpoint, so it inherits the same wiring unchanged).

## Source API (admin)

Admin-only endpoints manage a repository's sources and expose ingestion status. All
require an admin bearer token; non-admins get `403`.

| Method & path | Purpose |
|---|---|
| `POST /repositories/{id}/sources` | Upload a file (multipart `file`). Creates the source `pending` and schedules background ingestion; returns `201` with the source. This one endpoint handles both documents and images — a filename whose suffix is one of `IMAGE_SUFFIXES` is tagged `kind: "image"` and routed to OCR (see *Document parsing*) instead of the document parsers; there is no separate image-upload route. |
| `POST /repositories/{id}/web-sources` | Add a single web page as a source: body `{"url": "https://..."}`. Creates a `kind: "web"` source `pending` (`title` and `source_url` set to the URL, `title` later replaced by the page's own title if one is found) and schedules a background fetch + extract; returns `201` with the source. `404` if the repository is unknown; `422` if `url` is not a well-formed `http`/`https` URL. |
| `POST /repositories/{id}/admin-notes` | Write an **Admin Note** (JSON `{"title": "...", "content": "..."}`). Creates an `admin_note` source attributed to the author and schedules ingestion; returns `201`. |
| `GET /repositories/{id}/sources` | List a repository's sources (oldest first). |
| `GET /sources/{id}` | Fetch one source, including `status` and `ingest_error`. |
| `DELETE /sources/{id}` | Delete a source; its chunks cascade away. |

`SourceResponse` (the shape returned by the endpoints above) carries: `id`,
`repository_id`, `kind` (`document` / `admin_note` / `image` / `web`), `title`,
`original_filename` (the uploaded filename; `None` for admin notes and web
sources), `source_url` (the fetched URL for a `web` source; `None` otherwise),
`status`, `ingest_error`, and `created_at`.

One endpoint here is **not** admin-only — it is the user-facing counterpart that lets a
reader open a cited passage:

| Method & path | Purpose |
|---|---|
| `GET /repositories/{id}/sources/{source_id}` | **Any authenticated user.** Return a source's `title`, `kind`, and `content` (its passage text). Gated by an **active grant** on the repository (`403` without — the same rule retrieval enforces); `404` if the source is not in that repository. |

Upload returns immediately with `status: "pending"` — ingestion runs in the
background, so poll `GET /sources/{id}` to watch it move to `done` (or `failed`, with
`ingest_error` set). The embedding provider is injected via a dependency
(`get_embedder`), defaulting to the local model.

### Admin Notes (the curation flywheel)

An **Admin Note** is an admin-authored answer that becomes a first-class source
. It closes the knowledge-gap loop: the admin reads a gap (see
*Knowledge-gap dashboard*), writes the answer — typically titling the note with the
gap's question — and the note is ingested (chunk → embed → store) through the **same
pipeline as uploads** (its body handled as plain text). From then on the next user
who asks that question gets the answer automatically.

A cited Admin Note is marked in the query response's `sources`: `kind` is
`admin_note`, `verified` is `true` (the *Verified* badge), and `author` is the
admin's nickname it is cited to (uploaded documents are `verified: false`,
`author: null`). If the authoring admin is later deleted, the note survives with
`author: null` — `created_by` is `ON DELETE SET NULL` (see *Deleting a user*).

## Repository management (admin)

Admins create repositories and see all of them; regular users only ever see the
repositories they've been granted. These endpoints are
admin-only (`403` for non-admins):

| Method & path | Purpose |
|---|---|
| `POST /repositories` | Create a repository. Body `{"name": "...", "description": "..."?}`; returns the repo with a `configured` flag (always `false` at creation). |
| `GET /admin/repositories` | List **every** repository with its `configured` state — distinct from `GET /repositories`, which is scoped to the caller's granted repos. |
| `PATCH /repositories/{id}` | Update `name` and/or `description`. Only the fields present are applied; an explicit `description: null` clears it, an omitted field is left unchanged; a blank `name` is `422`. |
| `DELETE /repositories/{id}` | Delete a repository, **confirmation-gated** by echoing its name (`{"confirm_name": "…"}`, else `400`). Its sources, chunks, and grants cascade away with it (`ON DELETE CASCADE`). `204` on success. |

A freshly created repository is unconfigured and cannot answer until an admin sets
its LLM (below). `GET /admin/repositories` never includes key material — the key
lives only behind the per-repo `llm-config` route, always masked.

## Repository LLM configuration (admin)

Each repository chooses its own LLM — there is no system default, so a repository
must be configured before it can answer. Admin-only endpoints
set and read that config; non-admins get `403`.

| Method & path | Purpose |
|---|---|
| `PUT /repositories/{id}/llm-config` | Set (or replace) the repo's `provider`, `model`, and `api_key`. Returns the config with the key **masked**. |
| `GET /repositories/{id}/llm-config` | Read the repo's config (`configured` flag; key masked; `null` fields when unconfigured). |

`provider` is one of `gemini` / `openai` / `openrouter` / `anthropic`. The
`api_key` is **encrypted at rest** on write (Fernet, see *Provider API-key
encryption*) and **never returned in full** — responses carry only
`api_key_masked` (`sk-…•••4f2a`), produced by decrypting in memory just long
enough to keep the prefix/suffix:

```json
{"provider": "openai", "model": "gpt-4o", "api_key_masked": "sk-…•••4f2a", "configured": true}
```

Setting the config requires `ENCRYPTION_KEY` to be present (encryption fails
loudly rather than storing plaintext). At query time generation **routes to this
stored config**: the endpoint decrypts the key in memory and builds the
repository's own provider/model through `get_llm_provider(...)`, so each
repository answers with the LLM it was configured for — never a shared default.

## Query API (the full RAG loop)

One endpoint runs the whole loop end-to-end — authenticate, enforce access,
retrieve, generate, cite:

| Method & path | Purpose |
|---|---|
| `POST /repositories/{id}/query` | Ask a question against one repository. Body `{"question": "..."}`; returns the grounded answer with citations and their source documents. |

Any authenticated user may call it, but access is enforced up front: the
repository must exist (`404` otherwise) and the caller must hold an **active
grant** on it (`403` otherwise — the same grant predicate the retrieval query
enforces at the SQL level, surfaced here as an explicit denial rather than an
empty result). An expired grant is treated as no grant. The repository must also
have its **LLM configured** (`409` otherwise, with a message to configure a
provider, model, and key — see *Repository LLM configuration*).

Past the gate the loop is: embed the question → access-filtered, thresholded
retrieval → generate through the repository's **own configured provider** (built
per request from its stored provider/model/decrypted key via the `get_llm_builder`
dependency) → resolve the `[n]` markers to source spans. The response is:

```json
{
  "answer": "…grounded prose with [1] markers…",
  "not_in_vault": false,
  "citations": [
    {"number": 1, "chunk_id": "…", "source_id": "…", "char_start": 0, "char_end": 42}
  ],
  "sources": [
    {"id": "…", "title": "policy.txt", "original_filename": "policy.txt", "kind": "document", "verified": false, "author": null}
  ]
}
```

`sources` lists the distinct documents the citations point at (first-cited
order), so the UI can label and link each `[n]`. When retrieval surfaces nothing
relevant, the honest "not in this vault" behaviour carries through the provider
untouched: `not_in_vault` is `true`, `answer` is the refusal text, and both
`citations` and `sources` are empty — the endpoint never special-cases it.

### Conversation memory (persisted, server-authoritative)

Conversation history is **persisted per (user, repository)** and **owned by the
server**, not the client: the request body carries only `{"question": "..."}` — a
client never sends a `history` field. `POST /repositories/{id}/query` loads this
user's saved thread for the repository, threads the most recent
`MAX_HISTORY_TURNS` (10) `(question, answer)` pairs into the prompt (and folds the
prior question into the retrieval query, so a follow-up like "what about X"
retrieves in context), then appends the new exchange — question, answer,
`not_in_vault`, and the exact `citations`/`sources` it was served with — as one
`ConversationTurn`. There is exactly one conversation per user per repository,
created lazily on first use.

| Method & path | Purpose |
|---|---|
| `GET /repositories/{id}/conversation` | This user's saved conversation for the repository, oldest turn first (empty `turns: []` if none yet). Each turn mirrors the query response shape exactly, so a page reload rebuilds the thread verbatim — including its citations and sources. |
| `DELETE /repositories/{id}/conversation` | Delete this user's saved conversation for the repository (the "Clear conversation" action). `204` on success. |

Both endpoints require the same **active grant** as `/query` (`404` unknown repo,
`403` no access). Conversations and their turns are per-user and cascade away with
the user or the repository (`ON DELETE CASCADE`).

### Query logging

Every answered query writes one `query_logs` row — the raw
material for the knowledge-gap dashboard and usage analytics. Each row records who
asked (`user_id`), the repository, the question text, the retrieval signal
(`top_score` — best similarity among retrievable chunks; `chunk_count` — how many
cleared the relevance threshold), whether the answer was grounded (`not_in_vault`),
and when (`created_at`). Only *answered* queries are logged: requests rejected at a
pre-generation gate (`404`/`403`/`409`) never reach the log.

`user_id` is `ON DELETE SET NULL`: deleting a user (see *Deleting a user*)
anonymizes their past questions to "asked by a deleted user" rather than erasing
them, so the analytics signal survives the account. `repository_id` cascades — a
repository's history dies with the repository.

### Knowledge-gap dashboard

The gaps a repository couldn't answer become the admin's curation to-do list
. A **gap** is a logged query whose answer was the honest "not in
this vault" (`not_in_vault = true` — retrieval was empty or too weak to ground).

| Endpoint | Behavior |
|---|---|
| `GET /repositories/{id}/knowledge-gaps` | **Admin-only.** Ranked, aggregated gaps for the repository. `?limit=` (1–200, default 50). `404` if the repo is unknown. |
| `POST /repositories/{id}/knowledge-gaps/reject` | **Admin-only.** Reject a gap question with a required `reason`: body `{"question": "...", "reason": "..."}` (both non-empty). Upserted on `(repository_id, normalized_question)`, so rejecting the same topic again just replaces the reason. Returns `201` with the rejection (`question`, `reason`, `rejected_by`, `rejected_at`). `404` if the repo is unknown. |
| `GET /repositories/{id}/knowledge-gaps/rejected` | **Admin-only.** Rejected gaps for the repository, newest first. |

Similar questions are aggregated **case- and whitespace-insensitively** (a v1
heuristic — lowercase, trim, collapse whitespace; not semantic clustering), so
re-asks of the same topic merge into one row. Each row carries a representative
question, `ask_count` (times asked), `user_count` (distinct known askers), and
`last_asked_at`, ranked most-asked then most-recent — "N users asked about X, no
source covers it." An admin closes a gap one of two ways: **answer** it by writing
an Admin Note (a source), after which the next user who asks gets the answer
automatically; or **reject** it — decide it's out of scope, with a required
written reason (`GapRejection`, keyed by the same normalized question the
aggregation uses). Either way the topic drops out of the active gap list — a
rejected question is excluded at the same SQL query that aggregates gaps — and
survives its authoring admin being deleted (`rejected_by` goes `null`, matching
Admin Notes).

### Query analytics

Usage insight for the admin, aggregated from the query log:

| Endpoint | Behavior |
|---|---|
| `GET /analytics` | **Admin-only.** One composite summary. `?top_limit=` (1–100, default 10) bounds the questions/users lists. |

The response carries everything the dashboard needs in a single call:

- `total_queries`, `answered`, `not_in_vault`, and `not_in_vault_rate` — the overall answered-vs-gap picture;
- `per_repository` — query + gap counts per repo, busiest first ("which repos are active");
- `top_questions` — most-asked questions (aggregated case/whitespace-insensitively, the same rule as the gap dashboard);
- `active_users` — the most active **known** users (anonymized/deleted askers are excluded — "who's using what");
- `by_day` — a daily time series of `total` vs `not_in_vault`, for the answered-vs-gap rate over time.

## Reports (blended: database + documents)

**Blended reports.** A report request (`POST /repositories/{id}/reports`) is
planned by the repository's LLM into at most 6 sections (`services/report_planner.py`),
each routed to one source. Planning **stops** for review (below); once approved,
`database` sections run the guardrailed NL→SQL path
(`report_llm` → `sql_guardrails` → `report_execution`, self-repair ≤3 attempts per
section), `documents` sections run the grant-filtered RAG path (`retrieval.retrieve`
under the requesting user's grants → the repo LLM's `answer` with `[n]` citations;
`services/report_documents.py`). Sections assemble with partial success — a failed
section renders as a note; the report fails only when every section fails — into
one PDF (`report_render.build_report_pdf`) with per-section source lists (citation
numbers are per-section).

**A section names WHICH database it reads (#203).** `source` is a *kind* of
source — `database` or `documents` — and once a repository could own several
connections (#201) that stopped being enough to execute anything.
`PlannedSection.databases` carries connection ids, and the planner picks from
`PlannerDatabase` rows built from the repository's own connections: name,
description (#202) and the whole allow-list, per connection. The description
makes routing cheap; the schema is what keeps a sub-question using the wording
the data actually uses. A connection **nobody has described** is still offered on
its name and schema alone — #202's rule was "absent is not out-of-date", and the
routing counterpart is *absent is not unusable*: gating on a description would
make curating a database the thing that silently removes it from every report.

*The field is a list, and #204 cashed that in.* `databases` was a list from the
start because the plan is persisted JSONB — changing a stored contract twice is
what that avoided. Raising `MAX_DATABASES_PER_SECTION` from 1 to 3 was therefore
the whole schema change #204 needed: **no migration, and no stored plan touched a
second time.** Running the first id and ignoring the rest was rejected in #203
and stayed rejected: it answers from half the data without erroring. The same
reasoning killed a "pick the first connection" stopgap for the crash below.

*Creation stopped choosing a database, which is also a bug fix.* `POST
.../reports` resolved "the repository's connection" with `scalar_one_or_none()`;
#201 made a second connection representable, so that call raised
`MultipleResultsFound` and **creating any report in a repository with two
databases was a 500** (shipped in v55/v56, reproduced by
`test_create_report_201_with_several_connections`). It is now an existence check.
`generated_reports.connection_id` keeps its meaning — *the database this report
read* — and is written by `generate_report` from the first database section in
plan order, which is the earliest point anything can know it.

*Validation is server-side and repository-scoped.* `check_sources_available` took
a `has_database` boolean and now takes `connection_ids`: with several connections
the two would have been separate facts that could disagree. The id check is also
the access rule — `/replan` and `/approve` carry an untrusted draft, and without
it a hand-crafted body would route a section at **another repository's** database
and run SQL with its stored credentials. Same lesson as #201's resolve-by-pair
routes. An *empty* `databases` is not rejected there: on a draft it is a blank the
next re-plan fills, exactly like a blank `source`, and `finalize_draft` is what
refuses a still-unrouted section at approval.

*Reading stored data is lenient; the trust boundary is strict.* `PlannedSection`
defaults `databases` to empty rather than requiring it, because the same model
parses plans back **out** of the `plan` JSONB column — including every plan
written before #203, none of which carry the key. Requiring it would have turned
"list my reports" into a 500 for every report that already existed. The rule
therefore lives in `parse_report_plan` (the LLM's output) and `finalize_draft` (a
caller's draft). Migration `584d24fb171b` backfills the key on stored plans from
each report's own `connection_id` — recovery of information the rows already
hold, not a guess — so a report left AWAITING_APPROVAL across the deploy is still
approvable instead of needing a re-plan round to route a plan that was complete.

*The routing is read-only in the plan editor*, like #186's source chip: the plan
chooses and the reader reads. The chip prints the database's **name**, which
required a reader-scoped `GET .../report-databases` — `GET .../databases` is
admin-only and the plan editor is not an admin screen. It returns a name and
description, never connection detail, and the editor prints nothing rather than a
raw id it cannot name. Since #204 it prints **every** name the section reads.

**One section merges several databases (#204).** #203 put Apple and Microsoft in
adjacent sections and left the reader to do the comparing; this is the card that
puts them in one table, one chart. Two databases cannot be joined by one SQL
query, so the join happens in the app.

*The planner defines the output contract; the executor enforces it.*
`PlannedSection.columns` is the SELECT list every one of that section's queries
must return, in order. The planner owns it because it is the only actor that sees
every database's schema at once — the alternative, letting the first result define
the shape and instructing the rest to match, serialises the databases, shifts the
contract under the retry loop, and founders on the fact that the first database's
natural naming may be ungeneratable in the second. This project owns the proof:
production's replica renamed `trade_date` → `date` and dropped the `us-gaap:` tag
prefix while the local one did not. Enforcement adds no new control flow — a
result whose columns are not the contract appends self-repair feedback and
re-asks, exactly like an empty result or a rejected SQL.

*The contract defaults, for #203's reason.* `columns` is required only of a
section naming more than one database, and it is never required by the model
itself, because `PlannedSection` still parses plans back out of the `plan` column.
An empty contract is already correct for every stored single-database section,
which is why this card ships **no migration**. Presence is checked at the trust
boundary (`parse_report_plan`, `finalize_draft`); shape rules — the column cap,
duplicates, and the reserved provenance name — live in `check_sources_available`
with every other routing rejection. An empty contract mid-edit is a *blank* the
next re-plan fills, exactly like an unrouted section: rejecting it in
`check_sources_available` would 400 the reader who added the second database and
asked the model to work out the shape.

*The guards stay per database because the merge is a layer above the executor.*
`_execute_planned_databases` runs the unchanged `_execute_database_section` once
per named database, so #176/#178's overlapping-period guard, the ambiguous-key
check and the empty-result hold all judge each result against its **own**
database's period notes and schema before anything is concatenated. That is
structural, not a check that could be forgotten — and it matters, because the
provenance column makes almost any merged row-set look distinctly keyed, so a
within-database duplicate would stop being visible the moment it was merged. One
database's overlap warning propagates to the merged section, which is what
suppresses the total and the chart downstream.

*Provenance is a real first column.* `report_merge.merge_section_results`
prepends `source_database`, holding each connection's **name**, so the merged
table can be read on its own; its heading is localised like every other
reader-facing string. A contract that claims that name is refused rather than
renamed, because a silent rename would leave the chart spec naming a column that
moved. Rows keep the order the plan named the databases in — sorting or
interleaving them would invent an order the data does not have. Columns are
projected **by name**, not by position: the executor already guarantees the
order, and this is belt-and-braces against the one bug class that leaves no trace
in the output, rows misaligned under headings that look right.

*Charts needed no new concept.* `ChartSpec.series_column` already splits rows into
one series per group and `report_chart` assigns colours from #131's validated
palette in first-appearance order, so a merged result is charted exactly like a
single one and nothing invents a colour. The spec and the display labels come
from the first database that returned rows — the contract guarantees the columns
are identical — and the chart is drawn **once, from the merged rows**, via the
same `_with_chart` the re-render path uses.

*Partial failure is delivered, not hidden.* A database that errored, returned
nothing, or returned a non-conforming result is named in `SectionResult.notes`
while the rest of the table is still built; only when no database produced a data
result does the section fail. The notes are a separate field from
`overlap_warning` on purpose — that one suppresses the chart, and a partial
merge's chart is still honest about the databases that did answer. They are
**persisted**, unlike `empty`, because the merge does not re-run on a re-render:
a note that lived only in memory would mean a re-rendered report quietly stopped
disclosing the gap.

*`_MAX_TABLE_ROWS` applies after the merge*, because the limit describes the
printed table and applying it per database would triple the table the number was
chosen for. The accepted cost: a head+tail window over a concatenation can drop a
middle database's rows out of the visible table. That follows the existing
rationale — the chart, not the table, carries the shape, and the chart draws every
row.

**A report says which database each figure came from (#205).** #204 gave the
merged *table* a provenance column and left the **chart** — the thing a comparison
report is actually read for — saying nothing, because `ChartSpec.series_column`
splits *entities*, not sources. A section built from several databases now
captions its figure with their names.

*Provenance rides inside each block that shows the figures, and that is what makes
it uneditable.* Not a new block kind: a separate `provenance` block would be
deletable, which is exactly the failure to avoid — a marker a reader can delete
separately is a citation a reader can delete separately. The table block carries
the `source_database` column, the chart block carries a `<figcaption>`, so
deleting either leaves the other still attributed. This is `_table_body`'s
existing rule applied again: the omitted-rows note and the overlap caveat already
ride with the table so a reader cannot delete the thing and keep the note about
it. **The residual case is recorded rather than solved:** a design that keeps the
`stats` line while deleting *both* the table and the chart shows Σ/x̄ with no
attribution. `default_blocks` never derives that arrangement, and a design that
keeps a summary while deleting what it summarises has discarded more context than
provenance.

*The chart legend and the sources line were both rejected.* The legend belongs to
`series_column`, which the planner uses to split the entities being compared;
commandeering it for databases would break that comparison or coincide with it
only in the Apple-versus-Microsoft case. The sources line is the **documents**
line: `default_blocks` derives it only for `text` sections, and its contents are
`[n]` markers the section's prose refers to by number. A database has no `[n]` and
no prose citing it, so listing one there would break the correspondence between
`[1]` in the text and `[1]` in the list — the property the citation discipline
exists to guarantee.

*`SectionResult.databases` carries the names.* Read from the section, never
reverse-engineered out of the result's provenance column: that would couple every
renderer to `PROVENANCE_COLUMN` and would give a chart-only design nothing at all.
It is set for **every** database section, single or merged — a field populated only
sometimes, for no reason visible in the code, is the more confusing artifact — and
**rendered only when there is more than one**, so no existing report gains
furniture and no golden moves. Persisted, like #204's `notes`, because a re-render
does not re-run the merge. The deck states the same thing as a text line under the
picture, since a disclosure the PDF makes and the PPTX does not is one a reader can
miss by choosing the other format.

**Two whole-report refusals sit at that seam, and they are different failures.** Every section
*failing* raises `ReportGenerationError` naming the first problem. Every section coming back
**empty** raises `EmptyReportError` — a subclass, so existing handlers still catch it, but the row
stores `f"{type(exc).__name__}: {exc}"` and the distinct name is what lets a reader tell "nothing
matched" from a provider outage. It is checked after execution and before rendering: the first
point where "did anything come back?" is answerable for the whole document, and the last before any
bytes exist. A report was delivered as a cover page, a heading and a sentence saying there was
nothing to say (#185); a full generation run spent on a PDF with no content in it.

Emptiness is **marked by the executor that knows**, never inferred: `SectionResult.empty` is set by
`report_documents`' found-nothing path, because a documents section that found nothing and a genuine
answer that cited nothing are indistinguishable downstream, and comparing prose against a translated
string is a coincidence rather than a test. A `data` section needs no flag — zero rows is zero rows —
and `produced_nothing` reads the two together. An `error` section is deliberately *not* empty in
this sense, so an all-errors report keeps its own message.

**One empty section among several still generates**, exactly as before: a three-section report with
one empty section is useful, and refusing it would throw away the two that worked. This overturned a
recorded decision — *"Sometimes nothing IS the answer"* — under which a single empty section was
delivered as a report; the section stays honest (`"done"`, not an error, which is what the retry
tests are about) while the *report* is refused. **The frozen-schedule path carries the same rule**
and needs it more, since a nightly run cannot retry: `_run_and_render` raises before any bytes
exist, and `execute_frozen` turns that into a **failed row carrying the reason** rather than a
silent night — the owner's recorded choice, because a night that writes nothing makes "did the
schedule fire?" unanswerable from the UI.

*Plan review loop.* Nothing executes until the user approves the plan — a wrong
plan otherwise costs a full generation run (up to 3 NL→SQL calls *per section*,
retrieval, charts, PDF and PPTX) before the user learns it was wrong. `POST
…/reports` creates the row at `planning` and dispatches `plan_report`, which
stores the plan on the row and stops at `awaiting_approval`. The approval card
the reader works through is four stages — request, plan, design, generate —
and only the middle two act on that row. **Plan**: `POST …/reports/{id}/replan`
sends the edited plan back for another LLM round, 409 unless the report is
`awaiting_approval`. **Design**: before anything expensive runs, the reader
picks a template and typography; `PUT …/reports/{id}/design` stores that
choice as an *intent*, nothing more, so it is allowed in any status, including
a report with no stored section data at all — only a `blocks` payload is
refused there (422), because a block list would reference sections that do not
exist yet (`test_blocks_on_a_report_with_no_section_data_is_rejected`).
**Generate**: `POST …/reports/{id}/approve` freezes the plan as it stands on
the caller's screen and starts generation at `pending`. `/approve` is the only
path into `PENDING`, so that gate lives in the router — `generate_report` does
not itself check that a plan was approved. The plan and generate transitions
are conditional `UPDATE … WHERE status = awaiting_approval` writes, so a
double-click cannot dispatch generation twice; the design write carries no such
gate; it never starts anything, so there is nothing for it to double-dispatch.

A design chosen in that step wins immediately, by the same resolution order
"Which template a report wears" describes below: `_bind_style` resolves a
design already on the row ahead of the repository default
(`test_a_design_already_on_the_row_beats_the_repository_default`), so the
first artifact a report ever produces already wears the template the reader
chose in step 2 — not the repository default they would otherwise have to
notice and correct afterwards. **Block editing stays a post-generation step**,
in `ReportDesignPanel`/the workbench described further below, not here:
`default_blocks` derives its list from fields that exist only once a report's
sections have actually executed — `chart_svg`, `draws_chart` — so before
generation there is nothing yet for a block editor to show
(`test_the_derived_default_carries_the_chart_block_storage_cannot_know_about`).

*The convergence lock.* The re-plan loop terminates because the user's edits are
enforced by code, not by the prompt. The client posts the whole plan with `""`
where it wants help; `merge_draft` (`report_planner.py`) overlays the model's
reply onto that draft **user-first** — every field the user supplied survives
byte-for-byte, and only a blank takes the model's text. `source`, `databases`
(#203 — a routing the draft already carries survives the round), `columns`
(#204 — likewise for the output contract) and section
count/order are always the user's; a reply with a different number of sections is
a contract violation fed back through the same ≤3-attempt repair loop. A draft
with no blanks is stored without a provider call at all. The submitted plan is
untrusted input, so both endpoints re-validate it through `check_sources_available`,
the same gate that guards the LLM's own output.

*Two new columns and two new statuses.* `generated_reports.plan` (JSONB, nullable)
holds the current working plan — written by planning and every re-plan, frozen at
approval, read by generation; `plan_rounds` counts the LLM rounds the report
actually paid for, and is not incremented for a blank-free re-plan. `ReportStatus`
gains `planning` and `awaiting_approval`; both are exposed on `ReportResponse`
alongside `plan`, so the UI can poll and render the plan editor. Frozen schedules
(`execute_frozen`) have no plan and skip both states entirely, as do reports
created before this change (`plan = NULL`). An *initial* planning failure is
terminal (`failed`), but a *re-plan* failure returns the report to
`awaiting_approval` with the error on the row: `failed` is a dead end, the edited
draft is never persisted, and the previous plan is still perfectly approvable.

*Presentation.* The plan carries a `title` and a `language`; the LLM decides
language, `services/report_format.py` decides numbers — asking a model to format
figures is what produced `3.6498000E+11` in a table cell. That module is pure (no
matplotlib or renderer imports) so the chart renderer, the PDF builder and any later
exporter share one implementation. It classifies each result column
(`infer_column_kind`) so a **year is never summed** — totalling every numeric
column previously yielded `Σ fiscal_year: 68,544` — scales money to localised
unit words (`364,98 млрд`) while leaving counts grouped and exact, formats
percentages to one decimal, and supplies the localised strings (no-documents
note, `Rows`, omitted-rows line). Charts set an explicit tick formatter, so a
dollar axis no longer collapses to matplotlib's `1e11` offset. Column headings
come from the SQL generator's `column_labels` map; the SQL's own aliases stay
ASCII, because a quoted non-ASCII identifier is a needless risk in a string that
must survive `validate_sql`.

*A wrong Σ has five separate causes, and they need separate rules.* Four are about
the column — the kind has no total at all (a percentage), the quantity is intensive
(a price), the quantity is a level and the rows are periods (assets per quarter), or
there is only one row, where the "total" is the value and the "mean" is the value
again. `sums_across_rows` and `aggregates_meaningfully` encode those. The fifth is
about the **rows**: *overlapping rather than disjoint observations.* A long-format
fact table stores quarterly, year-to-date **and** annual figures for one metric under
the same fiscal-period label — measured on `apple_financials`: 90 d (2 479 rows),
363 d (1 766), 272 d (1 146), 181 d (1 065), plus 4 897 instant facts — and the YTD
rows overlap the quarterly ones by construction, so `sec-01` reported Apple's revenue
as **Σ $1.2 trillion**. `dimensions_identify_rows` is the test: when the non-measure
columns do not pick out one row each, **neither Σ nor x̄ is printed**, and the
self-repair loop asks the model once more before delivering what it has. Deliberately
*not* "the first column repeats" — in `(date, company, revenue)` the date repeats
across companies and Σ revenue is sound. Because a column name can never reveal a
period's *length*, `services/report_period_hints.py` measures the duration histogram
once per report and inlines it into the prompt's schema block; the note is measured
rather than authored, since prose cannot track a renamed column, and a probe that
fails leaves the prompt byte-identical.

**The fifth cause has two shapes, and only one is visible in the result.** When the overlapping
rows survive into the result, `dimensions_identify_rows` sees the repeated key and suppresses Σ and
x̄ (#176). When the query aggregates them away — `SUM(value) … GROUP BY fiscal_year` — one row per
year comes back with genuinely distinct keys and every figure two to three times too large, so
nothing in the result reveals it. That shape is caught **before execution** from the SQL alone:
`report_period_hints.aggregates_without_duration` fires when a query aggregates a table the probe
found to hold mixed period lengths without referencing its duration columns anywhere. The verdict
rides on `SectionResult.overlap_warning`; the renderer prints a caveat above the table and no
chart, and the generation loop asks once more before delivering. **Charts have two doors** — a
direct `render_chart_images` call in `_execute_database_section` and `_with_chart`, which *redraws*
on every re-render — so both suppress, or a re-rendered report regains the chart the caveat says
was withheld.

The user's prompt is **not** printed in the PDF — it
used to be the heading *and* the paragraph beneath it — and remains on the report
row and in history. `language` falls back to a Cyrillic check on the prompt when
the model omits it.

*How a page is assembled.* `services/report_html.py` builds the whole report as
one self-contained HTML string — stylesheet included — and
`report_render.render_pdf` prints it with **WeasyPrint**. Pagination, repeating
table headers, page breaks, widow and orphan control and the running header are
CSS declarations rather than arithmetic; the ~200 lines of fpdf2 geometry that
measured columns, wrapped wide tables into blocks and fitted text with an
ellipsis are gone, because their only job was compensating for fpdf2 not
clipping. Charts are still drawn by matplotlib (`report_chart`), emitted **twice**
from one figure — SVG for the document, PNG for the deck, since python-pptx has
no SVG path at all.

Two things about this are load-bearing and easy to undo by accident. Fonts are
declared with `@font-face` pointing at the files inside matplotlib's own wheel
(DejaVu Sans, Serif and Mono), never by family name: fontconfig cannot see a font
in a wheel, and naming a family lets WeasyPrint substitute one **silently** — the
spike produced a perfectly legible Ukrainian report in Liberation Mono, with no
warning. The `FontConfiguration` must therefore reach both `CSS(...)` and
`write_pdf(...)`, and every family declares weight 400 *and* 700, because a
missing bold face is fabricated rather than refused. The document is built as a
*string* so tests can assert on markup: both renderers compress their content
streams, which once made a passing byte-level assertion meaningless.

*The style model.* `services/report_style.py` owns every colour, face and measure
a report wears, and five templates fill them — Кварта (Swiss financial, the
default), Президія (a dark deck), Магістраль (dense operational), Часопис (serif,
two-column) and Мінімум. One `ReportStyle` is resolved per report and handed to
all three renderers, so the page, the figure and the deck cannot disagree; before
this the paper lived in a stylesheet constant, the chart's surface in
`report_chart`'s constants, and the deck had no colour at all. A template supplies
paper, ink, rules, type and spacing — never a number, a label or a translation,
and **never the series palette**: those stay #131's validated categorical order.
Президія is the single exception, and not a repaint: the light steps measurably
fail against `#14161d`, so it uses the same eight hues at their dark-band
lightness. Both sets come from the dataviz validator, not from taste.

The document opens on a cover (eyebrow, title, rule, the row's creation date —
never the prompt, and never today's date on a re-render) and every interior sheet
carries a running header. A data section with eight or more columns claims a
named landscape page and breaks after it, so the next section returns to
portrait. Paper colour is set on `@page`: on `html` or `body` it paints only the
content box and frames every page in white.

**Three cover variants, and what makes them different documents.** `full` is the
cover just described. `minimal` drops the eyebrow's company of furniture and pads
60 mm down. `band` puts the eyebrow and title in a `.banner` that bleeds to all
three top edges of the sheet, and drops `full`'s 34 %-wide `.rule` — the accent
returns as the band's own bottom edge, at that rule's 2pt.

Until #173 the `band` variant **painted no band**: `.cover.band .banner` carried
padding and margin and nothing in `src` ever gave it a background, so the variant
differed from `full` by the missing rule and a 34 mm pull-up alone. Rasterised,
the left margin over the top 40 % of the page held exactly one colour — the paper
— on both Кварта and Президія.

**The fill is `style.band`, not `style.accent`, and that is what settles #146's
contrast worry** rather than deferring it a fourth time. The banner holds the
eyebrow, which is `style.accent` at 0.8× body size and so is small text needing
4.5:1. On an accent-strength fill the existing text colours measure **1.99–2.83:1
across the five templates** — a fail everywhere, which is what #146 guessed at and
nobody had measured. `style.band` is a tint of the paper (1.07–1.13:1 against it),
so text crossing onto it keeps the contrast it already had: the eyebrow lands at
**5.17:1 on Президія**, the near-black deck #146 named, and 6.1–9.0:1 elsewhere;
the title at 14.6–18.2:1. Moving text from paper to band costs at most 0.72 and
crosses no threshold. Being a tint is also why the fill alone would not read as a
band, and why the accent edge is not decoration.

The bleed is arithmetic rather than a constant: `.cover` pads 34 mm down and the
banner already pulls that back, so another `page_margin_mm` in each direction
reaches the paper's edges, with the same amounts added back as padding so the
type does not move. Магістраль's margins are 13/12 mm against Кварта's 18/16, so
a hardcoded bleed would leave one template short of the edge and hang another off
it — and an edge check cannot tell either apart from a correct one, since both
paint *something* at x=0. `_band_cover_css` is emitted from `_conditional_css`
**keyed on the variant**, so a stored design that chose any other cover renders
the document it always did, and no golden moves.

*Download formats.* Generation builds **both** artifacts — `build_report_pdf`
and `report_render_pptx.build_report_pptx` — into `pdf_data`/`pptx_data`, so
`GET …/download?format=pdf|pptx` is a byte read that cannot fail halfway and the
reader picks the format. `format` defaults to `pdf`, keeping existing links
valid; reports generated before PPTX existed have no deck and 404 for that
format rather than returning a PDF under a `.pptx` name, which is why the
response carries `has_pptx` and the UI only offers the button when the artifact
is really there. The deck reuses `report_format` unchanged but lays out
differently on purpose — a slide is not a page, so tables cap at 12 rows and 6
columns and a section's chart and table take separate slides instead of
reflowing.

**The statistics decision has exactly one implementation**, `report_format.measure_summary`, and that
is a repaired defect rather than tidiness. The deck used to derive it itself, and the copy
drifted twice in the same direction: #176 taught `keys_repeat` to the document alone and #178
taught it `overlap_warning` alone, so v42 shipped a deck printing `Σ` for precisely the rows
the PDF withheld — the worse artifact to get wrong, since a frozen schedule emits it nightly
unwatched. Both drifts survived review because mutating one path left the other suite green.
So the property to preserve is not "the code looks shared" but **one mutation to
`measure_summary` fails a test in `tests/test_report_html.py` and one in
`tests/test_report_render_pptx.py`**; the overlap guard and the `keys_repeat` rule are each
proved that way. It takes `columns`/`rows` rather than a `QueryResult`, because this module is
dependency-light by contract and `QueryResult` lives beside SQLAlchemy. What stays per-medium
is the cap — 4 for an A4 page (nineteen Σ/x̄ lines once pushed the content off it), 3 for a
slide (no room for more) — passed in as `max_columns`. Making it a parameter is what made
unifying the two values *possible*, so the deck's number is pinned by
*a deck shows three measure columns and a page shows four*, which asserts the two differ
rather than asserting 3 twice. Nothing guarded it before: raising the deck's cap to 4 passed
the entire suite. The lines name themselves in words — `Сума`/`Середнє`, `Total`/`Average` —
from `_LABELS`, never as literals at the call site. `Σ` and `x̄` are notation, and they sit beside
each other on one line, which is the single place telling a total from an average matters most; a
reader who has not met the symbols cannot. The words cost 16–24 characters on a deck's stats line
and do not change how many lines it wraps to at either the reported shape or the worst case, and the
document's line wraps within the page rather than overflowing it. Because the caption is composed as
`<word> <column heading>`, a column that is itself called `Сума` renders as `Сума Сума: …` — correct,
and visible in the deck goldens, whose fixture labels a column that way. The `generated_reports.sections` JSONB column is the
admin-visible audit (per-section SQL / cited sources); `connection_id` is nullable
— a docs-only repository can report. Creating a report requires a connected
database **or** at least one `DONE` source. Ingestion/embedding still requires a
verified Gemini key, so document sections error clearly if that key is removed.
Nightly schedules apply only to legacy single-SQL reports; new
(blended) reports are not schedulable yet — the UI hides "Repeat nightly" and the
schedule-create endpoint rejects reports without frozen SQL.

**A schedule's `run_at_time` is a local wall clock, and `SCHEDULE_TIMEZONE` says whose** (#164).
It is a naive time somebody typed meaning their own two o'clock; until that fix it was compared
against `datetime.now(UTC).time()`, so a Ukrainian schedule set for 02:00 fired at 05:00 in summer
and 04:00 in winter. The setting is app-wide rather than a per-schedule column **because that
preserves what every stored value already means** — no migration rewrites anybody's schedule — and
because one deployment in one country does not need the generality. A second region would make this
the default and add the column beside it.

The same mistake had a quieter half. "Has it already run today?" asked whether `last_run_at`'s
**UTC date** was earlier than today's UTC date, and between local midnight and 03:00 those disagree
— it is already the 23rd in Kyiv while UTC still says the 22nd — so yesterday-local's run read as
today's and the schedule was skipped. The guard now compares `last_run_at` against local midnight
as an **absolute instant**; the column is `timestamptz`, so that is exact and does not depend on the
Postgres session's own `TimeZone`, which nothing in this app sets.

The zone is **validated at startup**. `scheduler_loop` swallows per-tick failures by design so one
bad tick cannot kill it, which means a typo would raise once into a log nobody reads and leave a
schedule that silently never fires at the right hour.

*Re-rendering from stored sections.* `generated_reports.sections_data` (JSONB,
nullable) holds the executed sections themselves — columns, rows, prose,
citations, per-section SQL and chart spec — beside the short `sections` audit,
which stays a summary a human reads rather than megabytes of result rows.
`services/report_persist.py` is the codec: every cell that JSON has no type for
is tagged, because a money column returned as `str` would render unformatted and
vanish from every total (`report_format` branches on `isinstance`). It is total
by construction — containers are walked, non-finite floats become null, anything
else degrades to text — and the caller swallows a serialization failure, because
both write paths run inside the try block that decides whether the report failed:
losing the rebuild is acceptable, losing the report is not. `NULL` means the
report predates the column, or its payload exceeded the 4 MiB budget, which is
refused rather than truncated (a capped result would redraw a different chart).
`POST …/reports/{id}/rerender` rebuilds `pdf_data` and `pptx_data` from that
payload with no LLM call and no query against the reporting database, and is
synchronous — it reads storage and runs the renderers, and the caller wants the
artifact, not a status to poll. Title and language travel in the payload
envelope, not the plan, because a frozen-schedule report has no plan; charts are
redrawn from `chart_spec` rather than stored, `render_chart` being deterministic
given the result and the spec. Both write paths draw their charts through the
same helper the rebuild uses, so a chart title suppressed for echoing its heading
is suppressed identically in both. The endpoint answers 409 for a report with no
stored payload or one that is not `done` — section data is written before the
renderers run, so a report that died rendering can carry a payload it must never
be rebuilt from. `ReportResponse.can_rerender` is that same pair of conditions,
so the UI only offers a rebuild that will work; the list endpoint derives it in
SQL and never fetches the column.

*Which template a report wears.* `repositories.report_template` (JSONB) is the
default new reports inherit; `generated_reports.design` (JSONB) is what one
report actually wears. Generation **snapshots**: it resolves the row's design,
then the repository's default, then Кварта, and writes the resolved slug back
onto the row. `POST …/rerender` reads that row and never the repository, so
changing a repository's default restyles **new reports only** — a finished
report keeps its appearance forever. Restyling an existing one is an explicit
`PUT …/reports/{id}/design` followed by a re-render.

A scheduled run is a new report, so a schedule picks up the repository default
as it stands that night.

Both columns are `NULL` on every report generated before this change; `NULL`
resolves to the default template, which is what those reports already rendered
in. Unknown slugs are **rejected on write** and **tolerated on read**: a
template deleted from the code later must not make old reports unrenderable.

*The block list (step 5a).* `generated_reports.design` also carries `blocks` —
the ordered list `services/report_html.py` now builds the document from, in
place of a renderer that walked `sections` and had a section's shape (heading,
prose, chart, stats, table) hardcoded into it. `services/report_blocks.py` owns
the model: a `Block` names a `kind` (`cover`, `heading`, `viz`, `stats`, `prose`,
`sources`, `error`, `callout`, `pagebreak`), an optional `section` — an index
into the immutable `sections_data.sections` list, stable because a finished
report's sections never change — and, for `viz`, a `variant` (`chart` or
`table`) and an optional `chart_type` override. Absent `blocks` means
"unedited": nothing is derived or backfilled at write time, and `default_blocks`
reconstructs the list a report already renders the moment one is actually
needed, called from inside the PDF builder because `chart_svg` is populated
immediately before that call — deriving the default any earlier would silently
drop every figure from a report nobody asked to edit.

`blocks_version` is **validated** on load, departing from this module's own
stated objection to #134's `PAYLOAD_VERSION` — written on every store, never
read. The difference is that every value `blocks_version` can take has a
defined behaviour: an unrecognised version discards the stored block list, and
the report renders as if unedited — the same safe fallback as no list at all.
That distinction matters more here than for `{"template": …}`, because an
unknown *kind* inside the list degrades visibly (the renderer just skips a
block it does not recognise), while a silently changed *meaning* of an existing
key would not, and no amount of read-tolerance detects that.

*The grouping rule.* `_blocks_html` walks the list once: a `heading` block opens
a `<section>`, and every block after it nests inside until the next heading
closes it and opens the next. This is not incidental structure —
`numbered_sections`' CSS counter, the named landscape page a wide table claims,
and the inter-section margin (`section { margin-bottom: … }`) are all declared
against `section` itself, so a flat run of `<h2>`/`<p>`/`<table>` siblings would
break all three at once: numbering would count every heading-shaped element
instead of every section, a landscape table would turn the page for whatever
came after it too, and the gap between sections would collapse into the gap
between paragraphs.

*Variant gating.* `allowed_chart_types` derives which chart types an editor may
offer for a section from that section's **stored result**, not from a fixed
list, so a reader cannot reintroduce the defects #131 removed by picking a
variant the data cannot support. It probes by **substituting** each candidate
chart type into a copy of the stored spec, rather than reading the stored
`chart_type`, because `report_chart.prepare_plot` returns `None` immediately
when `chart_type == "none"` — probing with the stored spec unchanged would
refuse every section the model declined to chart, including ones whose axes are
perfectly plottable. A pie is refused whenever the x axis is a date **or** a
reporting period that merely looks numeric or textual (`fiscal_year`, `fy`,
`2024-Q1`), via `report_format.is_period_column`: `x_kind == "date"` alone
misses the common case, because `report_chart._coerce_x` classifies a bare year
as a number on purpose, so a pie of revenue across 2021–2023 would otherwise be
offered as ordinary data rather than time.

That period rule is a **name-and-range** test, and the name half distinguishes
spelled-out words from abbreviations. `year`/`yr` are recognised as a *component*
of a longer name, so `fiscal_year` and `period_fiscal_year` both qualify;
`fy` and `period` are recognised **only as the whole column name**. The
asymmetry is deliberate: `period_amount` is money, and a `$2,024.00` total is
integral and sits inside the 1900–2100 range guard, so a component match would
call it a year and strip its grouping. Missing the abbreviations entirely was
its own defect (#152) — a series on a column named `fy` was offered a pie across
2021/2022/2023, the exact chart #131 exists to prevent, and production escaped it
only because its reports happened to group by `period_end`, which the *date*
branch catches without consulting the name at all.

*Assets, tokens and the cover (step 5f).* Uploaded images live in
`report_assets`, one row per file, bytes in Postgres. That is the app's **first
persisted user upload**: an uploaded *source* file is OCR'd and its bytes
discarded (`ingestion.py` sets `source.content = parsed.text`), and `pdf_data` /
`pptx_data` hold generated artifacts. Bytes rather than an object store because
this app has no bucket and adding a network hop to the render path would buy a
second failure mode for a file capped at 2 MB. An asset belongs to a
**repository**, not a report, so the same logo is worn by many reports and
deleting a report never takes one away; `design` references it by id, which is
what keeps a 300 ms debounced design write small.

**Raster only** — PNG, JPEG, WebP, validated on the bytes with Pillow rather
than on the client's declared type. SVG is refused at upload with a reason,
because python-pptx places pictures and supports no SVG: accepting one would
render it in the document and leave a silent gap in the deck, the same shape as
the recorded WeasyPrint failure where an unparseable image is dropped and a
complete-looking, pictureless artifact ships.

**Access control is at the write gate, not in the picker.** `validate_blocks`
refuses an `image` block whose `asset_id` is outside the report's repository,
and `PUT …/design` applies the same rule to `brand.logo_asset_id`. Both
endpoints take a payload the client composes, so a UI that lists only your own
assets restricts nothing.

**A logo upload writes back through a functional update, on both surfaces that offer one.** The
upload resolves long after the click, and a style group travels *whole* — the client's pending-patch
merge is deliberately whole-key, because a deep merge would make "unset this token" inexpressible.
So reading `brand` when the upload starts and writing `{ ...brand, logo_asset_id }` when it lands
replaces, rather than merges with, anything the reader changed meanwhile: accent, cover layout,
the approval toggle, all of which `StyleInspector` applies synchronously. `StyleUpdate` (in
`pages/reports/styleGroups.ts`) therefore admits a function, resolved against a ref holding the
current groups at the moment it is applied; synchronous callers keep passing a plain object, for
which "now" and "when applied" are the same instant. `useDesignDraft.restyle` (the pre-generation
step) and `ReportDesignPage.applyStyle` (the workbench) both take it. The two paths are **not**
equivalent and were checked separately: `useReportPreview.request` keeps only the *last* waiting
patch rather than coalescing them, so on the workbench the accent's own request is dropped as well
and the logo's patch is the only one that will be sent. Each surface has its own test, and each was
proved by restoring that surface's pre-capture read and watching its test — and only its test —
fail.

*Style tokens.* `report_tokens.apply_tokens(style, tokens)` folds the reader's
choices into a `ReportStyle`: a font pairing, a type scale, a density, a page
margin, a section-rule toggle and a brand accent. Every control is a closed set
or a slider inside a fixed range — never free CSS. **With every token at its
default, `apply_tokens` is the exact identity**, which is what lets the eleven
byte-identity goldens stay green while this shipped and what keeps ~470 stored
reports from silently restyling. The accent reaches `style.accent` and chart
series 0 only; series 1..n stay the palette #131 validated.

Reading is total. `StyleTokens` tolerates unknown *keys* but raises on a
malformed *value*, so `report_design` parses each stored group behind its own
`try/except` — one bad payload must never make an existing report unrenderable.

*Fonts.* The repo ships Inter and Source Serif 4 (SIL OFL 1.1, static Regular
and Bold), reached through the `@font-face`-by-file-path mechanism `_font_css`
already used because fontconfig cannot see a font inside a Python wheel. They
are bound to **new** family names `SANS_ALT` / `SERIF_ALT`; `SANS`, `SERIF` and
`MONO` keep resolving to matplotlib's DejaVu, because those are the names the
five templates use and repointing one would change four templates' default
typeface.

**⚠️ The goldens cannot catch that.** They capture `build_report_html`, which
never resolves a font file — resolution happens only in the PDF printer, and
the PDF guards assert the `/BaseFont` *name*, not the backing file. Adding
`SANS` to the shipped map passed the entire suite until
`test_sans_serif_mono_stay_on_dejavu` was added. That test is the only thing
protecting it.

*Conditional markup.* Heading anchors and the `toc` / `image` / cover /
section-rule CSS are emitted **only when a block that needs them is present**.
The goldens capture the whole document including its `<style>`, so an
unconditional addition would move every one of them — a silent restyle wearing
the costume of "goldens to regenerate". The band cover's fill (#173) is keyed one
level finer, on `brand.variant` rather than on the brand being non-default,
because the cover CSS is already emitted for every branded report.

Note what a *rendered* check cannot do here: `.cover.band .banner` matches no
other variant's markup, so emitting the fill unconditionally would paint nothing
and every pixel assertion would still pass while all eleven goldens moved. The
guard that catches it has to read the CSS.

*What the deck omits.* Approval fields are document-only: a deck is not a filed
record. A `toc` becomes an agenda slide with no page numbers, because a deck has
no pages and inventing slide indexes would be a second numbering nobody asked
for. `validate_blocks` accepts both and the deck ignores them — the established
writes-reject/reads-tolerate split.

**Writes reject, reads tolerate**, inherited unchanged from the template design
above. `validate_blocks` is the gate `PUT …/design` runs while the caller is
present to be told a section index is out of range, a `kind` or `variant` is
unrecognised, or a `chart_type` is not one `allowed_chart_types` returns for
that section. `_block_html` runs the opposite policy: an unknown kind, or a
`section` index that no longer resolves, renders as nothing, so a payload valid
under one code version can never make a report unrenderable under a later one.

*The deck is block-driven, too.* Both artifacts are assembled from the same ordered block list
(step 5b). The deck honours it at **slide granularity** — which blocks exist, which section each
binds to, and where slides break — while the arrangement *within* a slide stays the
deck's own: the statistics line is the slide's caption, drawn under the title
wherever the list puts it, and a chart consumes the rest of its slide so what
follows starts a continuation. A deck built from an absent or default list is
byte-identical to the one this renderer produced before it read blocks at all,
which `tests/test_report_pptx_goldens.py` asserts. A `chart_type` override
reaches the deck's figure the same way it always has: `rerender_report` passes
the same `redrawn` list — already run through `_with_chart`, which applies the
override — to both `build_report_pdf` and `build_report_pptx`, and the latter
embeds `section.chart_png` per slide.

*Hiding a block, and deriving a default outside the builder* (step 5d). `Block.hidden` is
stored rather than held in the client, because the point of hiding a block is that the *printed*
artifact stops carrying it. Both renderers skip a hidden block **before** their grouping switch,
so a hidden `heading` opens no `<section>` and what followed it joins the previous group —
precisely the document deleting that heading produces. Hide and delete differ in reversibility,
not in what they render. `chart_type_overrides` skips hidden blocks for the same reason: a chart
type is a contribution, and an invisible block must not repaint the figure a visible one draws.

`default_blocks` decides a section's chart block from `chart_svg is not None`, which is correct
only *inside* the builder, where `reports._with_chart` has just run. `chart_svg` is never
persisted, so anything reading a report back from storage — the editor, above all — would derive a
document with no figures in it. `report_blocks.draws_chart` answers the same question with a
`prepare_plot` probe against the **stored** spec, and `default_blocks_from_storage` derives over
it. The probe is exact rather than approximate because `render_chart_images` returns `None` if and
only if `prepare_plot` does: `build_figure` has a single `return None`, and the save path is a
`try/finally` with no `except`. A test pins that equivalence against the real renderer, so a
second `return None` appearing there breaks loudly. Note the deliberate contrast with
`allowed_chart_types`, which substitutes each *candidate*: `draws_chart` asks what the report
**does** draw, `allowed_chart_types` what it **could**, and a section whose stored `chart_type` is
`none` answers `false` to the first while offering all three to the second.

*Editing the generated text* (step 5e). `Block.text` was the callout's private field through 5a–5d,
kept that way so author text stayed out of the payload and could not break the citation
round-trip. It now means **this block's own words** on `cover` (the document title), `heading`,
`prose` and `sources`, where it overrides what the bound section says; `callout` keeps its 5a
meaning, having no generated original to fall back to.

**An edit is an override in `design`, never a mutation of `sections_data`** — the same rule the
chart-type override already follows, and for the same reason: `sections_data` is the record of
what was executed and what the model wrote, while `design` is what the reader wants done with it.
Four things fall out of that. Dropping `text` restores the generated words, so no second copy of
them is kept anywhere. The audit keeps a copy of the report that actually ran. A 4 MiB payload is
not read-modify-written on a 300 ms debounce. And the write path is the one that already exists,
with its validated `blocks_version`.

On **re-generation** the edits deliberately do not travel: a regenerated report is a *new row*,
and `reports._resolve_style` writes only the template slug onto it, so the new model's words stand
beside the new data. On **re-render** — the same row, rebuilt from storage — every override
survives, because `rerender_report` reads `report.design`.

`validate_blocks` **rejects** `text` on `stats`, `viz`, `error` and `pagebreak`, which is where the
parent design's *no editing of computed values* is enforced. A UI that declines to draw the control
is not a restriction: the endpoint takes a block list the client composes. The guard tests
`is not None` rather than truthiness — `""` is a reader who emptied a block, a different request
from dropping the key — and caps length at `MAX_BLOCK_TEXT`, since an override is unbounded reader
input landing in a JSONB column read on every render.

The cover's override reaches the running header with no second mechanism: `stylesheet` sets
`string-set: doctitle content()` on the cover's `h1` and `_page_css` prints `string(doctitle)` in
`@top-left`, so the header names whatever the cover says. `report_blocks.document_title` gives the
`<title>` and the deck's title slide the same answer — the deck has no cover element to publish
from — and `_SlideGroup` carries the effective section title so a *continuation* slide, which has
no heading block of its own, cannot name one section two ways in one deck.

**The citation round-trip holds because nothing on the path parses the string.** `_text` is
`html.escape(clean_text(...))`; `escape` touches only `&<>"'`, so `[1]` passes through untouched,
and `clean_text` strips control characters only. An override does not touch `section.sources`, so
the numbers a marker refers to are the same before and after the edit. Hostile input is that same
property seen from the other side, and it is asserted on the HTML string — both renderers compress
their content streams, so a byte-level assertion on the PDF proves nothing.

**`sub_question` is not editable, and that is a finding rather than an omission**: it is rendered
by neither `report_html` nor `report_render_pptx`. It is a *plan* field, and the plan-review loop
(#129/#130) already edits it before generation, which is where changing it changes something.

*Chart reproducibility.* `report_chart` now pins
`matplotlib.rcParams["svg.hashsalt"]` to a fixed value and suppresses the SVG's
`Date` RDF metadata (`savefig(..., metadata={"Date": None})`) — both otherwise
non-deterministic content matplotlib writes into every SVG it emits, so the same
figure drawn from the same data used to come out byte-different on every render.
`build_report_pptx` pins its own source of drift the same way: it stamps every
zip entry's timestamp to a fixed epoch before returning the package, since
python-pptx otherwise writes each entry's timestamp from the wall clock. This is
what makes a `/rerender` genuinely **reproduce** a report's artifact rather than
merely resemble it, and what keeps the document's markup testable as a plain
string in the first place (see "How a page is assembled" above).

*That reproducibility holds across time, not across platforms.* A chart reaches
the document as an **SVG** — text, and deterministic everywhere — but reaches the
deck as a **PNG**, and raster bytes depend on the freetype build doing the glyph
hinting. A deck built on macOS therefore cannot byte-match one built on Linux,
while the document can. CI demonstrated exactly that split: of the twelve golden
decks captured on macOS, all seven containing a chart failed on Linux and all
five without one passed, which also confirms that everything *except* the raster
— the block-driven assembly and the pinned zip timestamps — is portable. So
`tests/test_report_pptx_goldens.py` feeds the charted builds fixed chart bytes
from `tests/fixtures/`, captured from a real `render_chart_images` call. The
battery then asserts what it is actually for — that deck *assembly* is unchanged
— on every platform, while chart rendering keeps its own coverage in
`tests/test_report_chart.py`. The goldens themselves were not regenerated for
this: the captured bytes are what the renderer already produced, so the decks
built from them stayed byte-identical to goldens that still have exactly one
commit behind them.

| Method & path | Purpose |
|---|---|
| `PUT /repositories/{id}/report-template` | Admin. Set the template new reports inherit (`{"template": "<slug>"}`). Unknown slug → 400. `{"template": null}` clears the choice, returning new reports to the shipped default. Existing reports are untouched. |
| `GET /repositories/{id}/report-template` | Admin. Read it back; `null` when no choice has been made. |
| `PUT /repositories/{id}/reports/{id}/design` | Owner-or-admin. **Partial update**: `template`, `blocks` and `blocks_version` are each optional — an omitted key leaves that part of the stored design alone, `null` clears it. The whole `blocks`/`blocks_version` pair is gated on `blocks` being present in the payload (`"blocks" in payload.model_fields_set`): a lone `blocks_version` with no `blocks` is ignored, and `blocks` arriving without a matching `blocks_version` is a 422. Storing `blocks` is also rejected (422) on a report with no stored section data, since a block list would reference sections that do not exist. Does **not** rebuild — call `/rerender` after. |
| `GET /repositories/{id}/reports/{id}/design` | Owner-or-admin. The editor's read model: the block list the report renders from (stored if there is one, **derived** otherwise, with `stored` saying which), plus one summary per section — `title`, `kind`, `allowed_chart_types`, `draws_chart`, and (5e) the `prose` and `sources` words a block on that section renders unedited, alongside the document's stored `title`, so the editor seeds its fields without reading them back out of the PDF. Its own endpoint rather than fields on `ReportResponse`, because `_list_statement` uses `load_only` precisely so `sections_data` (up to 4 MiB) is never fetched for a row the UI is merely listing, and this answer needs the full result rows plus a `prepare_plot` probe per candidate. A report with no stored section data answers with empty lists rather than an error — `can_rerender` already tells a client it cannot be rebuilt. |
| `GET /report-templates` | Any signed-in user. The five templates with `key`, `name` and paper/ink/accent hexes for a swatch. |

*Choosing a template in the SPA (`pages/reports/TemplateGallery.tsx`,
`ReportDesignPanel.tsx`, `RepositoryTemplateField.tsx`).* The gallery — five
swatch cards built from `GET /report-templates` — is one presentational
component, `TemplateGallery`, mounted twice: `ReportDesignPanel` uses it to
restyle one finished report, `RepositoryTemplateField` uses it to set the
repository default. Neither wraps the other; both hand it a selection, a
callback **and their own lede** and know nothing of each other. The lede is a
required prop for a reason: while the gallery owned that sentence itself, the
repository default told admins their choice would be "applied to this report
and rebuilt from stored data" — a promise `RepositoryTemplateField` never
keeps, and the only sentence it gives them. A default is not a report, so it
says so: the choice becomes the default for *new* reports and reports already
generated keep the template they were built with, which is the binding rule
below restated where an admin actually meets it. `ReportDesignPanel` is the only one
that rebuilds anything, and it consumes four endpoints in a fixed order:
`GET /report-templates` to fill the gallery, then on a choice `PUT …/design`
first (so the choice is on the row even if the rebuild that follows fails),
then `POST …/rerender`, then `GET …/download?format=pdf` — fetched as a blob
with the session's bearer token and shown in an `<iframe>` on a `blob:` URL,
never an `<iframe src>` pointed at the download URL directly, which would
arrive unauthenticated. The rebuild is gated on
`report.can_rerender`, which the SPA now declares (it did not before this
step, so nothing consumed it and no UI could rebuild a report at all): a
report that cannot be rebuilt from storage still accepts and stores a choice,
it just skips the last two calls and says so.

*Step 5c chose the printed PDF over an HTML preview, and recorded why: an HTML
preview would need a font endpoint (the report's faces are files inside a Python
wheel, reached by `file://` URLs a browser cannot load) and would hand-emulate
what lives in `@page` — the paper colour, the running header, the page footer,
the landscape page for wide tables — which a browser ignores outright. That is
"a second layout system, free to drift from the one that prints", and it is the
same thing the thumbnail swatch (five bars and three grey lines, never a
miniature of the report) was designed to avoid becoming.

*Step 5c measured that decision instead of restating it.* Producing the markup
costs under 6 ms; **printing** it costs 257 ms on the dyno for the report size
this system actually produces, and 2.25 s for a twelve-section stress case.

**Step 6b answered that objection rather than overruling it — see *The canvas*
below.** The measurement stands; what it concluded no longer follows, because
the canvas is the print renderer's *own output* rather than a second one. The
round-trip is still what `ReportDesignPanel` on the reports list does, and is
made affordable in `useReportPreview` (`pages/reports/`), the seam both the panel
and the workbench consume.

Two different rules make it affordable, and both are needed. **Debounce** collapses
a burst of edits into one render, so typing costs nothing until it stops.
**Coalescing** handles what debounce cannot — edits arriving while a render is
already in flight. A queue would run one render per edit and fall further behind
the longer someone works; at 257 ms–2.25 s each that backlog is unbounded. Instead
at most one render runs and at most one state waits behind it, a newer edit
replacing the waiting one rather than joining a line, so the reader converges on
their latest state having paid for two renders at most. The hook also reports
`pending` from the keystroke rather than from the request — the preview on screen
is stale the moment the design changes, which is what a loading indicator has to
say — and revokes each superseded object URL, including on unmount.

*The workbench* (`pages/ReportDesignPage.tsx`, step 6a). **The editor is a route,
`/reports/:reportId/design`, not a panel inside the reports list.** Its repository comes from the
app's current-repository context rather than the URL: a report id is what a person would paste, and
the selection persists in `localStorage`, so a reload survives. A deep link opened while a
*different* repository is selected gets a 404 from `GET …/reports/{id}` and is told so, rather than
rendering an editor with nothing in it. The row keeps its one-click template gallery — opening a
full-page workbench to swap one template is a bad trade — and gains a link to the route; reaching
it is still gated on `can_rerender`, because every action the workbench offers ends in a rebuild.

Three columns: the block tree at 272px, the canvas, and a 320px inspector of five collapsible
groups (Блок, Типографіка, Сторінка, Брендкіт, Звіт). The Звіт group reports blocks, sections and
language and deliberately **no page count**: one needs WeasyPrint's line breaking, so nothing in the
browser can compute it and a guess in a filed record's editor is a lie the reader cannot catch.

*The canvas* (`pages/reports/ReportCanvas.tsx` + `services/report_html.py`, step 6b). **The centre
column is `build_report_html`'s own output under `preview=True` — the print renderer, not a second
one.** That is what answers 5c's objection above rather than ignoring it: measured against the code,
exactly three things in that output are hostile to a browser, and **charts and images need no work
at all because both are already `data:` URIs**.

| Print | Canvas |
|---|---|
| `@page { size; margin; background }` | a `.cv-sheet` box: A4 width, that padding, that paper |
| `@page { @bottom-right counter(page); @top-left string(doctitle) }` | a static header and footer drawn once on the sheet |
| `target-counter(…, page)` in the contents | a literal `—` |
| *(no font rules at all — they are `report_render`'s separate stylesheet)* | `@font-face` over `GET /api/report-fonts/{slug}` |

So what can drift is roughly forty lines of page-box emulation (`_preview_css`), generated from the
same `ReportStyle` the printed document uses, and nothing else. **Every one of those is emitted only
when preview mode is asked for**, because the eleven byte-identity goldens capture the document's
whole `<style>`; the mutation making any of it unconditional fails all eleven, and that mutation is
run. Preview mode is otherwise purely **additive**: strip the `data-block-index` attributes, the
sheet wrapper and the overlay stylesheet and what is left is the printed document character for
character, which is the property `test_preview_is_additive_the_blocks_markup_is_identical` asserts.
The attribute goes **on each block's own outermost element rather than in a wrapper**: a wrapper is
structure, and a `<div>` inside `section.text-section { columns: 2 }` also breaks `column-span: all`
on the heading it would then nest.

**The canvas does not paginate, and says so.** A browser cannot reproduce WeasyPrint's line
breaking, so it is one continuous sheet with explicit breaks; contents page numbers render as `—`
and the footer carries no number. The topbar's `Аркуш`/`PDF` switch keeps the real artifact one
click away — 5c's objection answered in the interface. The running header is dropped when the
document opens on a cover, exactly as `@page :first` drops it, and for the same reason: otherwise
the title appears twice near the top of the sheet. That one was found by eye, not by a test.

**The document mounts into a shadow root, and that is load-bearing.** `stylesheet()` styles bare
element selectors — `html`, `body`, `p`, `table`, `h2`, `section` — so writing it into the
application's own DOM would be a global restyle of every page the router can reach rather than a
preview. The shadow root keeps everything the card wanted (same document, same DOM, directly
scriptable, no iframe) and adds the boundary. Its one cost is that `html { … }` and `body { … }`
match nothing inside a shadow tree, which is why `.cv-sheet` restates them from the same tokens.
Hover and click are **delegated** to the host and read `event.nativeEvent.composedPath()[0]`, never
`event.target`: a shadow root retargets an event on its way out, so `target` *is* the host by the
time React sees it and every click would read as "the reader clicked the background".

**The sheet scales, and where the scale is applied is the whole of it (#168).** The canvas offers
50 %–150 % plus fit-to-width, and sets `--cv-zoom` on the host; the property inherits across the
shadow boundary and `_preview_css` reads it as `zoom: var(--cv-zoom, 1)` on `.cv-sheet`. Zoom issues
**no request** — it is a custom property on a document already mounted, and a rebuild is ~300 ms of
chart redraw.

⚠️ **The scale's value lives in `Workbench`, not in the canvas — the control and the state are
split on purpose (#172).** The centre column renders `ReportCanvas` behind a ternary on
`preview.view`, so a trip to the PDF and back **unmounts it**. #168 kept the whole thing local,
reasoning that the scale is meaningless in `pdf` view and that fit-to-width needs a width only the
canvas has measured. The second half is still true, so `fit()` still measures there and only calls
up; the first half was the defect, because "meaningless in `pdf` view" meant *unmounted* and the
reader's scale was discarded on a switch they meant as a detour. **A value that must outlive an
element belongs to whatever outlives it** — and because the canvas is now a controlled component,
its own tests must drive it through a stateful harness or every zoom assertion passes without the
buttons doing anything.

The editor opens **fit-to-width**, measured from the host once it has a width — never on mount,
because `fit()` clamps an unmeasured `0` up to the minimum and would open every report at 50 %. It
fits **once per opened report**: the flag lives beside `zoom` on the page, because the Аркуш ⇄ PDF
switch unmounts the canvas and a flag kept there would re-fit over a scale the reader chose.

⚠️ **The scale must not sit on an ancestor of the floating toolbar.** `.cv-toolbar` is
`position: fixed` and anchored from a block's `getBoundingClientRect()`; a `transform`ed ancestor
becomes its containing block, and a zoomed ancestor rescales the coordinates those offsets resolve
in. Measured drift with the scale on `.cv-canvas`: 187px at 75 %, and 20px even at 100 % under
`transform`. On `.cv-sheet` it is zero at every scale, with no compensating arithmetic anywhere —
rects come back already scaled. #167 built a zoom, saw the drift, and recorded it as "Chrome reports
`getBoundingClientRect()` unzoomed"; **that diagnosis was wrong**, and the mechanism was cut for a
containing-block mistake.

⚠️ **`.cv-host` is a block container on purpose.** A shadow root's content are children of the host,
so under `display: flex` the sheet was a flex *item* and **shrank** to fit instead of overflowing —
794px asked for, 507px given at 125 %. Block layout plus the sheet's own `margin: 0 auto` centres it
while it fits and overflows it rightward when it does not, which is the direction `overflow-x` can
scroll. And **the toolbar's anchor is a frozen rect**, so the scale is one of its effect's
dependencies; without that it stays where the block used to be, and re-selecting cannot fix it
because selecting an already-selected block re-runs nothing.

**Selecting a block moves the page to it, on both columns** (#194). The sheet scrolls the block to
the **centre** — `scrollIntoView({ block: "center" })`. It used to be `"nearest"`, which is the
minimal-effort option: a block already partly on screen moved nothing at all and one off screen was
clipped to the edge, so choosing a block frequently looked as though it had done nothing. It is
deliberately **not** `behavior: "smooth"`: the toolbar anchor is a rect read immediately after this
call, nothing sets `scroll-behavior: smooth`, and animating the scroll would take that rect
mid-flight and strand the toolbar — the frozen-rect defect above, reintroduced by the back door.
Note that the **window** is the scroller, not the host or `.wb-canvas`: the host is not
height-constrained (measured `scrollHeight === clientHeight`), so it grows to fit the sheet, and a
test that measures the block against either box compares it with itself and passes however the sheet
sits.

On the other side, the «Блок» group opens for the selected block via `InspectorGroup`'s `revealKey`
— a *when*, never a *whether*, so `open` stays out of React's hands for the reason that component's
docstring records. **A reveal is not a preference:** setting `.open` fires `toggle` exactly like a
click, and the handler persists on every toggle, so without suppression an automatic open would
silently rewrite a group the reader had collapsed to `"1"` and it would never close again. The guard
for that lives in e2e and **must**: `.open = true` appears not to fire `toggle` in jsdom at all, so
the unit test of it passed with the suppression deleted. The e2e asserts it by **reloading** rather
than reading `localStorage`, since reading straight after the reveal races the queued write and
passes either way.

`GET …/reports/{id}/design/preview` returns that document. It runs the same `assemble_document` the
printer runs — extracted from `rerender_report` so the two cannot diverge — makes **no provider
call**, and **writes nothing**: a preview that stored artifacts would let merely opening the editor
change what a reader downloads. **Charts are redrawn, not read**: a section loaded from storage
carries no `chart_svg`, so skipping that would show a canvas with its figures missing beside a PDF
that plainly has them. The route serving the faces is deliberately **unauthenticated** — a `url()`
inside a stylesheet carries no bearer token, so an authenticated route would 401 every face and the
canvas would silently fall back to whatever the browser had — and its `slug` is a key into a closed
dict rather than anything joined to a path.

⚠️ **The saving is real but smaller than "6 ms versus 257 ms" suggests, and it was measured.** On a
four-section report with four charts: redrawing the charts 269 ms, assembling the markup 0.5 ms,
printing a further 331 ms. Chart redraw dominates *both* paths, so the sheet costs about **45%** of
the print path, not a fortieth. It is still worth having — and it is why the PDF is built **on
demand**, when the switch is thrown rather than on every edit, so a reader who stays on the sheet
never pays to print at all. Both views are staleness-tracked, not just the shown one: editing on the
sheet leaves the stored PDF describing a design nobody chose.

**Block-list state lives in `useBlockList`, not in a component.** The workbench draws the tree and
the per-block controls in *different columns* and they must agree about the selection; a component
owning that state could only share it by rendering both, which is the single-column arrangement 6a
exists to undo. `BlockTree` and `BlockInspector` are presentational readers of the hook.

**An inspector group sets `open` on the element, never as a React prop.** The `toggle` event is
dispatched asynchronously, so a controlled `open` loses a race: the click closes the DOM, a
re-render re-applies React's stale value, and the handler persists the state that was just undone.
jsdom dispatches `toggle` synchronously and cannot see it — an e2e run found it. Relatedly, a
*closed* `<details>` genuinely hides its body in a browser and not in jsdom, so a test must open a
group before clicking anything inside it.

The tree is a list of rows with drag-reorder, a per-row hide toggle and delete; the palette adds
the kinds 5a implements, minus `cover` (derived once, at the top) and `error` (produced by a
failed section, not chosen). A kind needing a section is not offered at all when the report has
none, so the editor cannot build a payload `validate_blocks` would reject. **The variant chips are
the backend's answer**: they render `section.allowed_chart_types` verbatim, so a pie is absent for
a time series because the server refused it, not because the SPA knows to leave it out — asserted
both in the unit suite and end to end. Deleting is reversible for six seconds with the remaining
time shown, restoring the block to its original index; a second deletion inside the window commits
the first, which is `setDeleted` replacing the state rather than a branch implementing the rule.

*Editing on the sheet* (step 6c). **The words are typed where they are read.** The inspector's
textarea is gone; a block whose kind is in `TEXT_KINDS` opens to a caret on the page itself,
dashed at rest and solid on focus. Nothing new became permitted — it is 5e's `text` override,
through 5e's debounced `PUT …/design`, refused for every other kind by the same `validate_blocks`.
A block that prints computed values still gets a note in place of a field, stating the rule the
backend enforces, and offers no caret on the sheet either. "Restore the generated words" survives
in the Блок group and still **deletes** the override rather than writing the generated text back
in: copying it would freeze today's words into the design.

**The editable *run* is marked, not the block.** `_editable_at` emits `data-block-text` and
`contenteditable` on the element whose content **is** the override — the cover's `<h1>`, the
heading, the paragraph, the callout's paragraph — because a cover is also an eyebrow, a date and
two signature lines, and reading the block's own `textContent` would fold all of them into the
stored text at the first keystroke. `sources` is the exception and the one place preview mode adds
an *element*: its `Джерела / Sources:` label shares the paragraph with the run, so the run gets an
inline `<span>`. That narrows 6b's "attributes, never elements" to what its reason was always
about — never a block box, because a `<div>` inside `columns: 2` breaks `column-span: all` — and
the additive-identity test unwraps the span before comparing bytes, so the property still holds.

**The redraw waits for the caret to leave.** An edit costs a ~300 ms debounce plus a ~300 ms
render and the document comes back *whole*, so applying it under a caret destroys the element being
typed into. Restoring the caret by offset afterwards was rejected: the server does not return the
same markup — a cover edit rewrites the running header, a heading edit rewrites the contents entry
— so an offset would land in a document that moved underneath it. Nothing is lost by waiting; the
keystrokes are already painted, and what changes *elsewhere* appears when the reader looks away.
The flush is deferred one turn past `focusout`, because focus moving between two editable runs
fires `focusout` before `focusin` and drawing at once would destroy the element being moved to.
Its cost, stated: **a stale document survives as long as the caret stays in it** — narrow, because
the only edits reaching beyond their own block are cover→header and heading→contents.

*Marks and actions* (step 6d). **`Block.icon` is a closed set of drawn glyphs**
(`services/report_icons.py`) a `heading` or a `callout` may wear — and no other kind, because every
other block prints a value computed from the stored result and a mark there would be the document
having an opinion about it. The write gate refuses an unknown name; the renderer *draws* nothing
for one, which is the same read-tolerance an unknown `kind` gets, so a design written by a later
version cannot make an existing report unrenderable.

**Paths, never emoji.** An emoji is a different typeface at a different weight in every renderer,
so a mark chosen for a filed report would look deliberate on one machine and like a defect on
another. The picker's copy of the table lives in `documentIcons.tsx` and **cannot drift**: a Python
test reads that file and compares every path string, because a comment claiming two constants agree
does not make them agree.

Two consequences worth stating. **`icon` defaults to absent and must stay so** — designs persist
with `exclude_defaults=True`, so any other default would appear in every payload the next time one
was written, silently rewriting ~470 stored documents for a field none of them uses. And
**`_ICON_CSS` is keyed off a mark actually being drawn**, not off the field being present, because
the goldens capture the whole `<style>`: emitting it unconditionally moves all eleven.

**The deck ignores the field, deliberately.** python-pptx cannot place inline SVG, so a mark would
have to be rasterised per occurrence — a rendering path, a font-colour question and a cache, for a
decoration. Asserted by digest in `tests/test_report_icons.py` rather than left to a comment, since
the field rides on the same block list both renderers read.

The mark takes `contenteditable="false"` in preview: 6c put a caret in the `<h2>` it shares, and
that attribute is what makes a browser treat the glyph as one atomic thing. A reader who deletes it
anyway loses nothing — it lives in `design`, not in the words, so the next redraw restores it.

**Duplicate** copies the block and selects the copy, for the same reason `add` does: a change the
reader cannot see is indistinguishable from nothing having happened. **Move up / down** shipped in
6a and gained end-to-end coverage here — they are the only reordering path a reader without a
pointer has, which a synthetic drag event cannot speak for.

**Chart height, grid, legend and value labels are explicitly not built.** Each is a new field, a new
path in *both* renderers and a golden regeneration, and none changes what the report says.
**Permanently dropped by the owner on 2026-08-10** when #159 was resolved — not deferred. Re-propose
only with a new reason.

**`_text()` is now what stands between a customer's database cell and script execution in the
SPA's own document.** It has escaped since step 2, but what it guarded was a printer; 6b made the
canvas write this document into the application's DOM with `innerHTML`, and 6c added a path where
the reader's own text goes in. `tests/test_report_preview.py` asserts it there.

**`tests/test_report_html_escaping.py` sweeps the funnel rather than sampling it** (#171). The
escaping tests written before it each lived beside the feature that introduced their path, so the
suite proved `_text` escapes *the paths somebody remembered* — it could not answer whether every
value reaching the markup goes through it. The sweep runs each value path twice, once printed and
**once with `preview=True`, the document the canvas actually mounts, which no test asserted for
escaping before**. Every case asserts the raw payload is absent **and** the escaped payload is
present: absence alone passes when a value is silently dropped, which would hide the very path the
case claims to cover.

**Four fields and the chart are safe by construction rather than by escaping, and the construction
is invisible.** `align` is allow-listed before it reaches `class="img-block {align}"`; `icon` is a
dict lookup in `report_icons`; `kind` and `variant` are only ever `==`-compared; and a chart is
**base64 in a data URI, never inlined markup**, so its bytes cannot become elements. Nothing would
have failed if any of them started being interpolated directly, so each is pinned by its own case.
A case must put the field on a block kind that actually reads it — `align` on a `heading` is
ignored, and a version of that test survived deleting the very allow-list it claimed to protect.

The working block list is local and seeded once. The reports page polls the row while a rebuild
runs, and re-reading `design.blocks` on every render would let a poll landing mid-drag replace the
reader's list with the last-saved one. Every change goes to `useReportPreview.request`, whose
argument widened from a template to the whole design patch so that a reorder and a template choice
converge on one debounced path — two paths would race, and the later rebuild would publish
whichever design its own `PUT` had stored. The rebuild indicator renders from that hook's
`pending`, beside the tree: `reports.design.applying` had existed since step 4 but rendered inside
the gallery, which closes before the rebuild finishes, so nothing ever showed it.

`.report-design` is `display: contents`, and the full-width basis lives on the parts that are
genuinely page-sized. The panel used to declare `flex: 1 1 100%` on *itself*: that gave the
expanded PDF a full line, but it also meant the collapsed panel — a button and a link —
claimed a whole line of the row, so a finished report's download buttons, design triggers and
Delete each ended up on a line of their own. Generating no box of its own puts the two
triggers on the action row beside the downloads, and `.report-design > .report-preview`,
`> .error` and `> .report-note` carry `flex: 1 1 100%` instead. They also carry `order: 1`,
which is the other half of the fix: a note is a sentence and wants its own line, but it sits
in the DOM *between* the triggers and Delete, so without an order it takes that line there and
pushes Delete below it. Sorting the sentences after `.report-delete` (default `order: 0`)
leaves one line of controls with any explanation beneath it. All three combinators are child
ones — `display: contents` removes the box, not the node, so `.report-item > .error` matches
nothing at all, while a descendant `.report-design .error` would also catch `TemplateGallery`'s
own message inside the dialog.

**No unit test can see any of this** — jsdom performs no layout and reports every box as zero —
so the guards are Playwright assertions, and they pull in opposite directions. In
`e2e/reports.spec.ts`, three cases measure the row: a report the panel says nothing about, one
it says a sentence about (`can_rerender: false`), and one whose template apply failed; each
asserts the controls' combined span is about one control tall, and the latter two that the
sentence sits below Delete rather than among the controls. In `e2e/report-design.spec.ts`, *the
workbench PDF preview keeps the canvas column's own height* pins the viewport at 900px and
asserts the preview is exactly 770px: `.wb-canvas` is a flex **column**, so a basis that
escaped the `.report-design` scope would override its `calc(100vh - 130px)` and shrink the
preview. Widen the scope and that one fails; drop it and the row ones do.

**Since #214 this suite runs in CI**, as a third job that brings up Postgres, migrations, a
seeded admin, uvicorn and the Vite dev server (the dev server specifically: only
`server.proxy` is configured, so `vite preview` would serve the SPA with no `/api` behind
it). Before that a red e2e suite merged green, and the price was measured rather than
guessed — the first run in five cards found six failing specs while every one of those
cards had passed CI. `workers: 1` stays, for the reason the config records: every spec signs
in as the same seeded admin, so parallel workers race one another's session.

*Turning it on immediately found a spec that depended on the developer's own credentials.*
`get_embedder` refuses with a 409 unless a **verified Gemini key** exists — embeddings are
global, so nothing can ingest without one — and `POST …/web-sources` depends on it. Every
machine that has ever ingested anything has such a key, so `sources.spec.ts` passed locally
for as long as it had existed; on CI's clean database the POST 409'd, no row was created, and
the spec failed. The suite was right and the environment was incomplete, so CI seeds a
placeholder through `scripts/seed_ci_provider.py`. It never calls out: the rows these specs
assert on are created *before* background ingestion runs, and that ingestion failing is
already the normal path for an unreachable URL. **A green local e2e run proves less than it
appears to** — it is running against whatever that machine happens to have configured.

**The same class of defect reached the database panel, and #211 fixed it there.**
`.database-connected`, `.database-summary` and `.database-description` had **no rules at all** —
the allow-list editor below them was styled when it was built and this block never was — so a
`<textarea>` and four buttons rendered as bare siblings on the first screen an admin sees after
connecting a database. The fix is a flex column whose `gap` spaces every child, including the
ones that appear conditionally (the stale warning, the save confirmation, each error line);
the buttons are grouped into `.database-actions` rows that wrap rather than collide, and
disconnecting is marked `.danger` so it is distinguishable from its neighbours by more than
position.

*Its guard had to be rewritten before it was worth anything.* The first version asserted the
controls did not **intersect** — and passed on the broken layout, because they never did
intersect: measured, `Describe`'s right edge and `Save description`'s left edge were both at
x=405.25, and each button's bottom was the next one's top. Zero gap reads as overlapping
without being overlap. `e2e/database-panel.spec.ts` therefore measures **separation** and
requires 6px, a threshold derived rather than picked: the broken block measures 0, 0, 0 and
3px, the fixed one 8, 9.6, 13.6 and 13.6px. Removing the CSS fails it with
`description sits 3px from Describe`.

**Running that suite also found five stale e2e stubs, and the fixture was wrong rather than
the product.** A server-returned section always carries `databases` (#203) and `columns`
(#204) because the response model defaults them; the stubs in `e2e/report-planning.spec.ts`
and `e2e/reports.spec.ts` predated both cards, so the plan editor read `databases.length` off
`undefined` and never mounted. Routing those fixtures then exposed the second half: a
`database` section with an **empty** `databases` is a blank the reader cannot approve, so
`Next →` stays disabled — correct behaviour, and the fixture had to name a database to reach
approval at all. Nothing shipped was broken; the suite simply had not been run since those
cards, because **Playwright is not in CI**.

Spacing between the request form and what follows it is its own rule, and its own guard.
`.report-request` had never appeared in `index.css`, and `.report-list` set `margin: 0`, so the
submit button's bottom edge *was* the top edge of the first report row — the same coordinate to
the sub-pixel at both widths measured, 1280px and 768px. That reads as the button sitting on the card rather than above it,
because `button[type="submit"]` paints a shadow 6px down and 18px blurred: roughly 16px of ink
below its own box, landing on the row. `.report-request` now carries `margin-bottom: 1.5rem`,
which clears that reach. The margin sits on the form rather than on the list because the form is
followed by the plan card as often as by the list, and because this page's blocks own the space
beneath them (`h1`, `h2` and `.report-note` all declare `margin: 0 0 x`). *The plan button does
not sit on the first report row* measures the gap at 1280px and 768px — both widths, since 768 is
where the action row wraps — and asserts it clears the shadow rather than merely being non-zero.
This was **not** a regression from the `display: contents` change above: the same measurement
against v43 (`f7d9c95`) reports the identical flush coordinates.

The page had two more edges of the same family, found while verifying that fix and given their
own card (#198). `.plan-card` — the wrapper the page mounts the steps rail and plan editor in —
had no rule at all, so an open plan's bottom edge was the report list's top edge; and
`.plan-editor` carries `box-shadow: var(--shadow)` (`0 8px 24px -12px`, about 20px of ink below
the card, across its full width), so the editor visibly sat *on* the first row. And `.report-list`
owned nothing below itself while the `h2` that follows it ("Nightly schedules") declares
`margin: 0 0 0.9rem` — space below only — so the heading started at the last row's final pixel and
read as that row's caption. Both now follow the same convention: `.plan-card` carries
`margin-bottom: 1.5rem`, clearing the editor card's shadow, and `.report-list` declares
`margin: 0 0 1.5rem`, granting the heading more than the 0.9rem it grants the content below
itself. Guarded by *the plan card does not sit on the first report row* and *the report list does
not sit on the nightly-schedules heading*, each of which asserts the element is visible before
measuring it — a missing card reports an all-zero rect, which would hand the assertion a
few-hundred-px "gap" exactly when the thing under test is gone.

*Frontend (`ReportsPage.tsx`, `pages/reports/PlanEditor.tsx`).* `isGenerating` in
`api/reports.ts` extends to `planning` alongside `pending`/`processing`, so a report waiting
on the LLM keeps polling; `isAwaitingApproval` is the one status that stops it, since nothing
is running and only the user can move it forward. The request button reads **Спланувати
звіт** / "Plan report", not «Створити звіт», because `POST …/reports` now plans and stops —
it no longer generates anything.

The plan editor renders *additively*, above the report list, rather than replacing the page:
the request form always stays on screen. An earlier design hid it behind a one-stage-at-a-time
flow gated on report status, but that gates on *any* row reaching `awaiting_approval` —
including a stale one from another session — which would remove the page's primary action
for a reader with no connection to that row. The editor is pinned to one report by id
(`activeId`), not re-derived as "the first row awaiting approval", so a second report
finishing its plan mid-edit cannot steal it out from under the reader.

Provenance badges (**закріплено** / **дописано LLM**) are derived entirely on the client by
`diffFilled(sentDraft, receivedPlan)` in `PlanEditor.tsx` — a value-diff between what was
posted and what came back, so the server needs no extra field. `/replan` is asynchronous: it
answers immediately with the row at `planning` and the *old* plan still attached, so that
response is no baseline to diff against. `ReportsPage` holds the sent draft in state and only
diffs once a later poll returns the row to `awaiting_approval` **with a plan that actually
changed** (compared by value, not by `plan_rounds`, which the backend does not increment for
a blank-free re-plan). That guard also covers a re-plan that fails inside the worker: the
backend rolls the row back to `awaiting_approval` with the *previous* plan and an error
attached, which without the value check would look like a successful round and misattribute
that old text to the model. The badge set is stored with the report id it was computed for
(`{ id, keys }`) and compared at render time rather than cleared by an effect on `activeId`:
a passive effect is not guaranteed to run before paint, so a reset would land *after* the
next report's editor has already mounted and flash the previous report's provenance over
round-one text nobody has reviewed.

While a round is in flight, `busy` disables **every** control in the editor — title,
language, section titles, sub-questions, move and remove — not only the two action
buttons. The chosen source is shown as a static chip, not a control, so it has no
disabled state to freeze. The plan deliberately stays on screen for the seconds the LLM
takes, and the landing round re-seeds the draft wholesale, so a still-editable field
would invite the reader to type into text that is about to be replaced with no warning.
The inputs also mirror the backend's `ReportPlanDraft` caps (`title` 300, `sub_question`
2000) as `maxLength`, so an over-long entry is refused by the browser instead of by a raw
pydantic message at replan/approve. A row at `planning` shows `planningLabel` ("the LLM
is composing the plan"), not `generatingLabel` — generation has not started and will not
until the reader approves.

The reader's source toggle was removed along with the positional CSS (`:nth-child`)
that coloured it: the chosen source is now shown as a static `.plan-source-chosen` chip,
tinted by a per-source modifier class (`--accent` for database, `--success` for documents)
selected on value rather than position.

## Frontend (React SPA)

The web UI lives in [`frontend/`](frontend/) — a Vite + React + TypeScript single-page
app that talks to this backend over the REST/JSON API using JWT bearer auth. It covers the foundation (routing, API client,
auth context, protected routes, base layout), the auth screens, the user query experience, and the
admin surface (repository management + LLM config).

```bash
cd frontend
npm install          # install dependencies
npm run dev          # dev server at http://localhost:5173 (proxies /api -> :8000)
```

Run the backend (`uvicorn …` above) alongside `npm run dev`: the dev server proxies
every `/api/*` request to `http://localhost:8000`, so the SPA and API share an origin
in development (no CORS).

**Architecture.**

- **API client** (`src/api/client.ts`) — a typed `fetch` wrapper that prefixes `/api`,
  attaches the bearer token, JSON-encodes bodies, and turns any non-2xx into a typed
  `ApiError` carrying the backend's `detail`. A `401` clears the session (expired token
  → back to login).
- **Auth** (`src/auth/`) — `AuthProvider` holds the session (token + claims decoded from
  the JWT + username), persists it to `localStorage`, and exposes `login` / `logout` /
  `changePassword` via the `useAuth()` hook. `RequireAuth` gates routes on a session
  (and optionally an admin role); it mirrors the backend's forced-password-change bounce
  by redirecting flagged users to `/change-password`.
- **Routing** (`src/App.tsx`) — public auth screens (`/login`, `/accept-invite`,
  `/change-password`) sit outside the protected `Layout` shell that wraps the app's
  authenticated pages. `Layout` renders a left **sidebar** (`src/components/Sidebar.tsx`)
  alongside the routed page content; on narrow screens the sidebar collapses behind a
  menu toggle. The sidebar groups navigation into *Workspace* (Ask, Reports, visible to
  everyone), *Manage this repo* (Data, Providers, Insights, admin-only), and *Admin*
  (Repositories, Users, admin-only).
- **Current repository** (`src/repository/`) — `RepositoryProvider` owns the single
  "current repository" for the whole app and persists the selection to `localStorage`.
  The sidebar's **repository switcher**, at the top above the nav groups, is the only
  place that changes it; Ask, Reports, Data, and Insights all read it via the shared
  `useCurrentRepository()` hook rather than each holding their own repo picker. The
  switcher's *list*, however, is route-scoped: workspace surfaces (Ask, Reports) show
  only the repositories the user is **granted**, matching the backend's grant model
  (which subjects even admins to grants for querying), while admin management surfaces
  (`/admin/*`, e.g. Data and Insights) show **all** repositories, since managing one
  doesn't require a query grant. Members only ever see their granted list either way.

**Auth screens** (`src/pages/`):

- **Login** (`/login`) — username + password → `POST /auth/login`; a
  `must_change_password` response routes the user to the change-password screen.
- **Accept invite** (`/accept-invite?token=…`) — a new user chooses a password
  (`POST /invitations/accept`) and is signed straight in with it, landing authenticated.
- **Change password** (`/change-password`) — reachable voluntarily or via the forced-change
  bounce; both new-password screens require a matching confirmation.

**Token handling.** There is no refresh endpoint — a JWT simply expires. The client
drops an expired token on load (no dead session mounts the app shell) and clears the
session on any `401`, bouncing the user back to `/login`.

**Query experience** (the app's home page `/`):

- **Repository** — comes from the shared sidebar switcher (see Routing above), not a
  picker on this page; a user with no granted repositories is told so instead of shown
  an empty box.
- **Ask + cited answer** — `POST /repositories/{id}/query` returns the answer, its
  `[n]` citations, and the cited sources. The answer's inline `[n]` markers render as
  clickable chips (`parseAnswer` splits them from text) that highlight and scroll to the
  matching entry in the **Sources** panel. Each source shows its title, a **Verified**
  badge + author for Admin Notes, and the citation's character span.
- **View passage** — each source has a "View passage" button that loads its raw text on
  demand from the user-scoped content endpoint (`GET /repositories/{id}/sources/{source_id}`) and shows it inline. Access is gated by the caller's active grant (403 otherwise).
- **Not in this vault** — when the response's `not_in_vault` is set, the turn shows an
  explicit callout rather than dressing up the refusal as an answer.
- **Persisted conversation** — on selecting a repository the page restores the saved
  thread from the server (`GET …/conversation`) instead of starting blank, so a reload
  never loses history; a **Clear conversation** button deletes it (`DELETE …/conversation`).
  The client never sends history in the query request — the server is authoritative.

**Admin surface** (#40, admin-only pages, reached via the *Manage this repo*
and *Admin* groups in the sidebar):

- **Repositories** (`/admin/repositories`) — lists **all** repositories
  (`GET /admin/repositories`) with a Configured / Not configured badge; a form creates
  a new one (`POST /repositories`). Each repo expands to an **LLM config** editor that
  reads the current config (`GET …/llm-config`, key shown only masked) and sets a new
  `provider` / `model` / `api_key` (`PUT …/llm-config`). The key field is write-only —
  it is never pre-filled, and the saved key comes back masked. Each repo can also be
  **renamed** (`PATCH /repositories/{id}`) or **deleted** (`DELETE /repositories/{id}`,
  confirmation-gated by echoing its name).
- **Data** (`/admin/data`) — one surface for the current repository (from the sidebar
  switcher), split into two tabs (`SourcesPanel` / `DatabasePanel`, selected via
  `?tab=documents|database` so each is linkable and survives reload). The older
  `/admin/sources` and `/admin/database` routes now redirect here.
  - *Documents & web* — **upload** a document (`POST …/sources`, multipart) or add a
    **web link** (`POST …/web-sources`) and watch it ingest: the list (`GET …/sources`)
    shows each source's status (`pending` → `processing` → `done` / `failed`) and
    **auto-polls** while anything is still ingesting, stopping once every source is
    terminal. Sources still ingesting also show a **progress bar and percentage**,
    derived from the source's `chunks_done` / `chunks_total` (see *[Ingestion
    progress](#ingestion-progress)*); the raw chunk counts stay out of the UI as an
    implementation detail. Progress advances one step per embedding request, so a
    document small enough to embed in a single request goes straight from 0% to
    done — the bar is meaningful for the long ingests it exists to cover. A failed
    source shows its ingestion error; each can be **deleted** (`DELETE
    /sources/{id}`).

    The poll re-arms after *failed* attempts as well as successful ones. Keying the
    reschedule solely off a change in the fetched list meant one rejected request
    ended polling permanently, so a status that changed afterwards — a quota failure,
    say — appeared only on a manual page refresh. `ReportsPage` shares the idiom and
    the fix.
  - *Database* — **connect** read-only reporting databases (`POST …/databases`; the
    password is write-only and never comes back — only name/host/database/username
    are shown once connected) and **test** the connection by saving it; **introspect**
    a connection's live schema (`POST …/databases/{id}/introspect`) into an editable
    **allow-list** of tables/columns with descriptions for the report LLM, saved with
    `PATCH …/databases/{id}/schema`. A connection can be **deleted**
    (`DELETE …/databases/{id}`, confirmation-gated).

    **A repository holds several databases (#201).** It held exactly one until then
    — `uq_database_connections_repository_id`, "slice 1" — which made a second
    *unrepresentable* rather than merely refused, and the API was singular to match.
    The routes are now a collection and the panel a list; the curation UI is
    unchanged and simply operates on the selected connection. A repository with
    exactly one still opens straight onto it rather than asking the admin to pick
    from a list of one.

    Each connection carries a **`name`, unique within its repository** — a
    connection was identified only by host and database, which is not something to
    choose between in a picker, and #202 hangs a generated description off the same
    row. Two repositories may each hold a "Financials"; one repository may not.

    **The 409 comes from a pre-check, and the guarantee comes from the
    constraint.** Catching the `IntegrityError` instead would be race-free and was
    tried first, but there is no way back from it here: Postgres aborts the
    transaction on a constraint violation, a plain `session.rollback()` unwinds
    whatever transaction the session is in (under the test fixture, a caller-owned
    outer one, leaving the session unusable for every statement after the 409), and
    a `SAVEPOINT` via `begin_nested` does not rescue it either — a *failed flush*
    marks the session's own transaction invalid, so the next statement raises
    `PendingRollbackError`. So the check races: losing it turns a sentence into a
    500. The window is two admins naming a connection identically in the same
    repository in the same milliseconds, and the data is correct either way,
    because `uq_database_connections_repository_name` — not the check — is what
    enforces it. The check excludes the row being updated, or every edit that left
    the name alone would collide with itself.

    **Every per-connection route resolves by the (repository, id) pair, never by id
    alone.** The id is the only thing a caller supplies and it is not secret, so a
    bare lookup would let any admin path reach any repository's connection.

    **#203 took the report half of that.** A section now names its database, so a
    report is no longer pinned to one connection; **#204 went further and let one
    section read up to three and merge their rows.** A *schedule* still names one,
    because it freezes one SQL query against one database.

    Two things #201 deliberately did *not* change. Reports and schedules still each
    name a **single** `connection_id`, which since #203 records *the first database
    the report read* rather than the only one it could. And deleting a connection still **cascades** to
    the reports and schedules that name it (`ON DELETE CASCADE`, unchanged): the
    repository's other connections and their reports are untouched, which is the
    behaviour holding several makes newly relevant.

    One route changed its answer: introspecting when nothing is configured used to
    be a 400 ("nothing to introspect"), which was right when the route named no
    particular connection. It now names one, so an unknown id is a 404.

    **Each connection describes itself (#202).** `POST …/databases/{id}/describe`
    writes a one-line summary of what the database holds, generated from its
    **allow-list** — `exposed_schema` is what the planner is shown in #203, so a
    description written from anything else would describe a database the planner
    cannot query. That also rules out generating on create, where `exposed_schema`
    is still empty. It is refused with a 400 before an allow-list exists rather
    than spending a model call to describe nothing.

    **It was explicit until #212, and the reason it was is why it now runs where
    it does.** Generating *inside* `PATCH …/schema` would make every allow-list
    save wait on a provider round-trip and fail when one is down, turning a fast
    local edit into a slow, quota-dependent one. #202 concluded a background job
    would be better and that there was no runner for it; #212 is that runner.

    **The description writes itself (#212).** Saving an allow-list queues
    `run_connection_description` through `BackgroundTasks` — the same shape
    `run_report_planning` uses: fresh session, load, delegate, never raise. #202's
    objection is satisfied rather than overruled, because the save commits and
    answers *before* the task runs: it is exactly as fast as it was and still
    succeeds with no provider reachable at all
    (`test_the_save_still_succeeds_with_no_provider_reachable` keeps that true by
    exploding if the endpoint body ever calls a provider itself).

    **The trigger condition is the fingerprint, not "is it empty".**
    `description_is_current` — asked by the dispatcher *and* by the worker, so the
    two cannot disagree — is false exactly when there is no description of *this*
    allow-list, which covers both cases the owner named: a connection nobody has
    described, and one whose tables just changed. A re-save that changed nothing
    dispatches nothing, so re-curating the same list spends no provider call.

    **A failure stays silent, and stores nothing.** The worker catches, logs and
    leaves the row untouched — previous description, previous fingerprint,
    connection still usable, which is #202's rule unchanged. No "last attempt
    failed" column: recording that would need a migration and a second staleness
    concept beside `description_schema_hash`, to say something an admin can get on
    demand. `POST …/describe` is still there and still returns the provider's real
    reason as a 400, so the screen distinguishes *not described yet* from *failed*
    by asking, not by the row remembering.

    **It is written in the reader's interface language, and says more (#215).**
    The prompt hard-coded *"Write in English"*, so a Ukrainian interface described
    every database in a language nobody had chosen — and the server had no way to
    know better, because the client sends no `Accept-Language` and the locale lives
    only in `localStorage`. The language therefore travels **on the request that
    triggers generation**: `PATCH …/schema` carries it (that save is what queues the
    task, and a background task has no reader to ask) and `POST …/describe` takes it
    as a query parameter. It is resolved through `resolve_language` — the one answer
    to "what language is this?" in the codebase — so an absent, regional (`uk-UA`)
    or nonsense value degrades to a default instead of turning an allow-list save
    into a 422. Nothing is stored: every trigger today is a request.

    `MAX_DESCRIPTION_CHARS` went 240 → **480** in the same change. At 240 a
    description had room for the subject matter *or* its coverage, not both, and
    this is the text #203 routes sections on — a thin description is a worse router
    as well as a worse label. Doubled rather than removed, because it is
    interpolated into the planner prompt once per connection; the column is `Text`,
    so no migration was needed. The cap moves in three places at once — the limit
    stated to the model, `parse_description`'s truncation, and
    `DescriptionUpdateRequest`'s `max_length`, so a hand-written correction is never
    rejected for being as long as what it replaces.

    **Hand-correction survives until the schema changes.** The owner's ruling was
    "auto, but still correctable": `PATCH …/description` keeps stamping the current
    fingerprint, so a correction is current and is not overwritten — until the
    allow-list changes, at which point it describes something that no longer exists
    and is replaced.

    **The panel looks again rather than trusting the save's answer.** That response
    predates the description by construction, so after a save that could produce
    one the panel re-reads the connection on a short interval until it reports a
    description for the current allow-list, or a small attempt budget runs out. The
    budget exists because there is deliberately no failure state to poll for: when
    it expires the panel stops claiming anything and leaves *Describe* as the retry
    that shows why.

    **Staleness is a fingerprint, not a flag.** `description_schema_hash` holds a
    sorted-key digest of the allow-list the description was written from; stale
    means it no longer matches. Sorted keys because the digest must track *what
    the allow-list says*, not how it happened to be serialised — a marker that
    fired whenever the same allow-list was rewritten in a different key order is
    one an admin learns to ignore. It is self-healing: curate a column away and
    back and the description is current again. **Absent is not out-of-date** — a
    connection nobody has described is not stale, or every new one would wear a
    warning before anyone had done anything wrong.

    **Nothing depends on a description existing.** It defaults to `""`, a failed
    generation is a 400 carrying the provider's message (matching `ModelListError`
    in `api/repositories.py`), and the connection keeps its allow-list and its
    usability. And it is **correctable by hand** — `PATCH …/description`, no
    re-test, mirroring `PATCH …/schema` — which is not a nicety: in #203 a wrong
    description silently misroutes a report. Clearing it clears the fingerprint
    with it, or the row would hold a hash of an allow-list no description was
    written from.

    Generated in English at 240 characters, because the load-bearing consumer is
    the planner prompt that `build_prompt` already composes in English.
    Introspection returns *every* table in the schema, and a single one can carry
    dozens of columns (Heroku ships `pg_stat_statements` with ~45), so **every
    table starts collapsed to one row** after an introspect — the admin expands
    only what they intend to curate, and including a table always reveals its
    columns. The per-table counter ("10 з 10 стовпців") reports how much of a
    table is exposed without opening it.
    Collapsed columns stay mounted behind the `hidden` attribute rather than being
    unmounted, so the editor keeps their state (and the panel's tests keep finding
    them).

- **Users & access** (`/admin/users`) — three panels over the account and grant
  APIs. *Invite* issues an onboarding invite (`POST /invitations`) and reveals the
  one-time token to hand over. *Accounts* lists every user (`GET /users`) with a
  "Password change owed" badge, a **reset-password** action (`POST …/reset-password`,
  showing the temp password once) and a **confirmation-gated delete** (type the
  username, then `DELETE /users/{id}`). *Repository access* picks a repo and manages
  its grants (`GET`/`POST /repositories/{id}/grants`, `DELETE …/grants/{user_id}`) with
  an optional expiry.

- **Insights** (`/admin/insights`) — the curation cockpit. A **knowledge-gap
  dashboard** (`GET …/knowledge-gaps`) ranks the questions a repo couldn't answer;
  "Answer this gap" opens an inline **Admin Note** editor prefilled with the question
  (`POST …/admin-notes`), which closes the gap once ingested. Alongside it, "Reject"
  opens a form requiring a reason (`POST …/knowledge-gaps/reject`) — either action
  removes the gap from the active list. A **Rejected gaps** list
  (`GET …/knowledge-gaps/rejected`) shows what was rejected, why, by whom, and when.
  An **analytics** panel (`GET /analytics`) shows totals + gap rate, per-repository
  volume, top questions, most-active users, and a by-day series.

All admin routes are guarded by `RequireAuth requireAdmin`, so non-admins are bounced home.

**Frontend Definition of Done** (run from `frontend/`):

```bash
npm run lint          # eslint
npm run format:check  # prettier
npm run typecheck     # tsc --noEmit
npm test              # vitest
npm run build         # tsc -b && vite build
```

CI runs these as a separate `frontend` job (see `.github/workflows/ci.yml`).

