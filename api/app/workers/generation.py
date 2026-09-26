"""The generation job (spec §5.2 worker flow).

    1. mark running
    2. chunks of settings.generator_chunk_size — never one big call
    3. generator produces raw items for a chunk
    4. validate + dedupe every item
    5. insert survivors as status='pending', source='ai', generation_job_id set
    6. counts, final status, stats

Each chunk is committed on its own, so a generator exception (or a crash)
in chunk N never loses chunks 0..N-1. Nothing this module writes is ever
`status='live'` — that requires a human (spec §1).
"""
import logging
import uuid
from collections import Counter
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Category, GenerationJob, Question, QuestionTranslation
from app.schemas.generation import GenerationParams, GenerationStats
from app.services.categories import get_category_by_slug
from app.services.generator import Generator, GeneratorError, Usage, topic_summary
from app.services.validation import (
    DUPLICATE_OF_EXISTING,
    CleanItem,
    RepeatCounter,
    dedupe,
    emitted_hash,
    existing_house_hashes,
    validate_item,
)

log = logging.getLogger(__name__)

TOP_REASONS_IN_ERROR = 5
TERMINAL_STATUSES = frozenset({"succeeded", "partial", "failed"})

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


def chunk_sizes(count: int, size: int | None = None) -> list[int]:
    """[10, 10, 5] for count=25 at the default chunk size."""
    size = size or settings.generator_chunk_size
    full, rest = divmod(count, size)
    return [size] * full + ([rest] if rest else [])


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _Tally:
    """Everything the job accumulates across chunks."""

    def __init__(self) -> None:
        self.produced = 0
        self.accepted = 0
        self.rejected = 0
        self.reasons: Counter[str] = Counter()
        self.repeats = RepeatCounter()
        self.seen_accepted: set[str] = set()
        # "Don't repeat these" hints for the generator, one per accepted item.
        self.topics: list[str] = []
        self.usage = Usage()
        self.chunks_total = 0
        self.chunk_errors: list[str] = []

    def reject(self, codes: list[str]) -> None:
        self.rejected += 1
        self.reasons.update(codes)

    def accept(self, item: CleanItem) -> None:
        self.accepted += 1
        self.topics.append(topic_summary(item))

    def status(self) -> str:
        if self.accepted == 0:
            return "failed"
        if self.rejected or self.chunk_errors:
            return "partial"
        return "succeeded"

    def stats(self) -> dict[str, Any]:
        return GenerationStats(
            rejections=dict(self.reasons.most_common()),
            emitted=self.repeats.emitted,
            repeated=self.repeats.repeated,
            repeat_rate=round(self.repeats.repeat_rate, 4),
            chunks_total=self.chunks_total,
            chunks_failed=len(self.chunk_errors),
            chunk_errors=self.chunk_errors,
            input_tokens=self.usage.input_tokens,
            output_tokens=self.usage.output_tokens,
        ).model_dump()

    def error_summary(self) -> str | None:
        """Human-readable `error`: top rejection reasons with counts, then any
        chunk failures. None when the job was clean."""
        parts: list[str] = []
        if self.rejected:
            top = ", ".join(
                f"{code} ×{n}" for code, n in self.reasons.most_common(TOP_REASONS_IN_ERROR)
            )
            parts.append(f"rejected {self.rejected}/{self.produced}: {top}")
        if self.repeats.repeated:
            parts.append(f"repeat_rate {self.repeats.repeat_rate:.2f}")
        parts.extend(self.chunk_errors)
        return "; ".join(parts) or None

    def apply_to(self, job: GenerationJob) -> None:
        job.produced_count = self.produced
        job.accepted_count = self.accepted
        job.rejected_count = self.rejected
        job.cost_cents = round(self.usage.cost_cents)
        job.stats = self.stats()
        job.error = self.error_summary()


async def generate_questions(ctx: dict[str, Any], job_id: str) -> None:
    """arq entry point. `ctx["session_factory"]` and `ctx["generator"]` are
    set by WorkerSettings.on_startup; tests pass their own."""
    session_factory: SessionFactory = ctx["session_factory"]
    generator: Generator = ctx["generator"]
    async with session_factory() as db:
        await run_job(db, generator, uuid.UUID(job_id))


async def run_job(db: AsyncSession, generator: Generator, job_id: uuid.UUID) -> None:
    job = await db.get(GenerationJob, job_id)
    if job is None:
        log.error("generation job %s not found", job_id)
        return
    if job.status in TERMINAL_STATUSES:
        log.info("generation job %s already %s; skipping", job_id, job.status)
        return

    params = GenerationParams.model_validate(job.params)
    category = await get_category_by_slug(db, params.category_slug)
    if category is None:
        job.status = "failed"
        job.error = f"unknown category slug: {params.category_slug}"
        job.finished_at = _now()
        await db.commit()
        return

    job.status = "running"
    job.started_at = _now()
    job.model = generator.name
    await db.commit()

    tally = _Tally()
    try:
        for chunk_index, size in enumerate(chunk_sizes(params.count)):
            tally.chunks_total += 1
            try:
                chunk = await generator.generate(params, size, chunk_index, avoid=tally.topics)
            except Exception as exc:  # one bad chunk must not lose the others
                log.exception("job %s chunk %d failed", job_id, chunk_index)
                tally.chunk_errors.append(f"chunk {chunk_index}: {type(exc).__name__}: {exc}")
                if isinstance(exc, GeneratorError):
                    tally.usage += exc.usage  # the failed call still cost money
            else:
                tally.usage += chunk.usage
                await _process_chunk(db, job, category, params, chunk.items, tally)
            tally.apply_to(job)  # progress is visible to the polling admin panel
            await db.commit()
    except Exception as exc:
        await db.rollback()
        job.status = "failed"
        job.error = f"{type(exc).__name__}: {exc}"
        job.finished_at = _now()
        await db.commit()
        raise

    tally.apply_to(job)
    job.status = tally.status()
    job.finished_at = _now()
    await db.commit()


async def _process_chunk(
    db: AsyncSession,
    job: GenerationJob,
    category: Category,
    params: GenerationParams,
    items: list[Any],
    tally: _Tally,
) -> None:
    tally.produced += len(items)

    clean: list[CleanItem] = []
    for raw in items:
        tally.repeats.record(emitted_hash(raw))  # diagnostic: every emission
        result = validate_item(raw)
        if isinstance(result, CleanItem):
            clean.append(result)
        else:
            tally.reject(result)

    existing = await existing_house_hashes(db, (c.content_hash for c in clean))
    for result in dedupe(clean, existing, tally.seen_accepted):  # the gate
        if not isinstance(result, CleanItem):
            tally.reject(result)
            continue
        try:
            async with db.begin_nested():
                db.add(_question_row(result, job, category, params))
        except IntegrityError:
            # Lost a race on questions_house_hash_uniq with another writer.
            tally.reject([DUPLICATE_OF_EXISTING])
            continue
        tally.accept(result)


def _question_row(
    item: CleanItem, job: GenerationJob, category: Category, params: GenerationParams
) -> Question:
    return Question(
        category_id=category.id,
        difficulty=item.difficulty,
        grade_band=item.grade_band,
        region=params.region,
        tags=item.tags,
        correct_index=item.correct_index,
        status="pending",  # never 'live' from here (spec §1)
        source="ai",
        pack_id=None,  # house content
        generation_job_id=job.id,
        content_hash=item.content_hash,
        translations=[
            QuestionTranslation(
                locale=locale, stem=t.stem, options=t.options, explanation=t.explanation
            )
            for locale, t in item.translations.items()
        ],
    )
