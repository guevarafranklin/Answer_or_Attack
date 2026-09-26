"""§9 step 4: POST/GET /admin/generate, the stub generator, and the worker
job function (called directly — no Redis, no running worker)."""
import math
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.main import app
from app.models import Category, GenerationJob, Question, QuestionTranslation
from app.queue import GENERATE_JOB, get_queue
from app.schemas.generation import GenerationParams
from app.services import validation as v
from app.services.generator import (
    STUB_MIXED_COUNT,
    GeneratedChunk,
    StubGenerator,
    get_generator,
    topic_summary,
)
from app.services.validation import content_hash
from app.workers.generation import chunk_sizes, generate_questions
from tests.test_validation import item

# ---------- helpers ----------


class FakeQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.fail = False

    async def enqueue_job(self, function: str, *args, **kwargs):
        if self.fail:
            raise ConnectionError("redis is down")
        self.calls.append((function, args, kwargs))
        return None


class ListGenerator:
    """Returns chunk i of a fixed list; an Exception entry is raised instead."""

    name = "list"

    def __init__(self, chunks: list[list[Any] | Exception]):
        self.chunks = chunks
        self.avoid_seen: list[list[str]] = []  # the `avoid` hint passed per call
        self.counts: list[int] = []  # the chunk size asked for per call

    async def generate(self, params, count, chunk_index, avoid=()):
        self.avoid_seen.append(list(avoid))
        self.counts.append(count)
        chunk = self.chunks[chunk_index]
        if isinstance(chunk, Exception):
            raise chunk
        return GeneratedChunk(chunk)


@pytest.fixture
def queue():
    fake = FakeQueue()
    app.dependency_overrides[get_queue] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_queue, None)


async def _category(db: AsyncSession, slug: str = "math") -> Category:
    cat = Category(slug=slug)
    db.add(cat)
    await db.flush()
    return cat


async def _job(db: AsyncSession, count: int, slug: str = "math", **params) -> GenerationJob:
    p = GenerationParams(category_slug=slug, count=count, **params)
    job = GenerationJob(
        kind="category", prompt="test", params=p.model_dump(mode="json"), requested_count=count
    )
    db.add(job)
    await db.flush()
    await db.refresh(job)  # server defaults: status, counts, stats
    return job


async def _run(db: AsyncSession, job: GenerationJob, generator) -> GenerationJob:
    @asynccontextmanager
    async def factory():
        yield db

    await generate_questions({"session_factory": factory, "generator": generator}, str(job.id))
    await db.refresh(job)
    return job


async def _questions_for(db: AsyncSession, job: GenerationJob) -> list[Question]:
    result = await db.execute(select(Question).where(Question.generation_job_id == job.id))
    return list(result.scalars())


def good(i: int) -> dict:
    return item(**{"en.stem": f"Q{i}: what is {i} + {i}?", "es.stem": f"P{i}: ¿cuánto es {i} + {i}?"})


def params_json(count: int = 5, slug: str = "math") -> dict:
    return {"prompt": "make some", "params": {"category_slug": slug, "count": count}}


# ---------- router ----------


@pytest.mark.asyncio
async def test_generate_requires_admin(client: AsyncClient):
    assert (await client.post("/admin/generate", json=params_json())).status_code == 401
    assert (await client.get("/admin/generate")).status_code == 401


@pytest.mark.asyncio
async def test_post_creates_queued_job_and_enqueues(
    client: AsyncClient, admin_headers, db: AsyncSession, queue: FakeQueue
):
    await _category(db)
    resp = await client.post("/admin/generate", json=params_json(count=42), headers=admin_headers)
    assert resp.status_code == 202, resp.text
    job_id = uuid.UUID(resp.json()["job_id"])

    job = await db.get(GenerationJob, job_id)
    assert job.status == "queued"
    assert job.kind == "category"
    assert job.requested_count == 42
    assert job.params["category_slug"] == "math"
    assert job.params["count"] == 42
    assert job.params["locales"] == ["en", "es"]  # defaults are persisted, not implied

    assert queue.calls == [
        (GENERATE_JOB, (str(job_id),), {"_job_id": f"{GENERATE_JOB}:{job_id}"})
    ]


@pytest.mark.asyncio
async def test_post_unknown_category_is_422(client: AsyncClient, admin_headers, queue: FakeQueue):
    resp = await client.post("/admin/generate", json=params_json(slug="nope"), headers=admin_headers)
    assert resp.status_code == 422
    assert "nope" in resp.json()["detail"]
    assert queue.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 201])
async def test_post_count_out_of_range_is_422(
    client: AsyncClient, admin_headers, db: AsyncSession, queue: FakeQueue, count: int
):
    await _category(db)
    resp = await client.post("/admin/generate", json=params_json(count=count), headers=admin_headers)
    assert resp.status_code == 422
    assert queue.calls == []


@pytest.mark.asyncio
async def test_post_when_enqueue_fails_marks_job_failed(
    client: AsyncClient, admin_headers, db: AsyncSession, queue: FakeQueue
):
    await _category(db)
    queue.fail = True
    resp = await client.post("/admin/generate", json=params_json(), headers=admin_headers)
    assert resp.status_code == 503
    job = (await db.execute(select(GenerationJob))).scalar_one()
    assert job.status == "failed"
    assert "enqueue failed" in job.error


@pytest.mark.asyncio
async def test_get_job_and_list(client: AsyncClient, admin_headers, db: AsyncSession):
    await _category(db)
    older = await _job(db, 5)
    newer = await _job(db, 7)
    # now() is fixed for the whole test transaction; make the order explicit.
    older.created_at = newer.created_at - timedelta(minutes=1)
    await db.flush()

    resp = await client.get(f"/admin/generate/{newer.id}", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued"
    assert body["requested_count"] == 7
    assert body["params"]["count"] == 7
    assert body["stats"] == {
        "rejections": {},
        "emitted": 0,
        "repeated": 0,
        "repeat_rate": 0.0,
        "chunks_total": 0,
        "chunks_failed": 0,
        "chunk_errors": [],
        "input_tokens": 0,
        "output_tokens": 0,
    }

    resp = await client.get("/admin/generate", headers=admin_headers)
    assert [j["id"] for j in resp.json()] == [str(newer.id), str(older.id)]

    resp = await client.get("/admin/generate?limit=1", headers=admin_headers)
    assert [j["id"] for j in resp.json()] == [str(newer.id)]

    resp = await client.get(f"/admin/generate/{uuid.uuid4()}", headers=admin_headers)
    assert resp.status_code == 404


# ---------- stub generator ----------


def test_get_generator_stub_backend(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "generator_backend", "stub")
    assert isinstance(get_generator(), StubGenerator)


@pytest.mark.asyncio
async def test_stub_mixed_batch_hits_every_rejection_rule():
    """The mixed batch spans the first STUB_MIXED_COUNT items of the stream,
    however it is chunked."""
    p = GenerationParams(category_slug="math", count=STUB_MIXED_COUNT)
    items: list = []
    for chunk_index, size in enumerate(chunk_sizes(STUB_MIXED_COUNT, 10)):
        items += (await StubGenerator().generate(p, size, chunk_index)).items
    assert len(items) == STUB_MIXED_COUNT

    codes: set[str] = set()
    clean: list[v.CleanItem] = []
    for raw in items:
        result = v.validate_item(raw)
        if isinstance(result, v.CleanItem):
            clean.append(result)
        else:
            codes.update(c.split(":")[0] for c in result)
    for gated in v.dedupe(clean, existing=set(), seen=set()):
        if isinstance(gated, list):
            codes.update(gated)

    every_rule = {
        v.NOT_AN_OBJECT, v.MISSING_LOCALE, v.LOCALE_NOT_OBJECT, v.STEM_INVALID,
        "stem_empty", "stem_too_long", v.OPTIONS_INVALID, "options_count",
        "option_empty", "option_too_long", "options_duplicate", v.EXPLANATION_INVALID,
        v.FORBIDDEN_PHRASE, v.CORRECT_INDEX_INVALID, v.DIFFICULTY_INVALID,
        v.GRADE_BAND_INVALID, v.TAGS_INVALID, v.ANSWER_IN_STEM, v.DUPLICATE_IN_BATCH,
    }
    assert every_rule <= codes


@pytest.mark.asyncio
async def test_stub_later_chunks_are_clean_and_respect_params():
    p = GenerationParams(
        category_slug="math", count=30, difficulty_min=2, difficulty_max=3, grade_bands=["g4_g6"]
    )
    first_clean_chunk = math.ceil(STUB_MIXED_COUNT / 10)
    items = (await StubGenerator().generate(p, 10, first_clean_chunk)).items
    assert len(items) == 10
    for raw in items:
        clean = v.validate_item(raw)
        assert isinstance(clean, v.CleanItem), clean
        assert clean.difficulty in (2, 3)
        assert clean.grade_band == "g4_g6"
        assert clean.translations["en"].options[clean.correct_index] == clean.translations["es"].options[clean.correct_index]
    hashes = {v.validate_item(r).content_hash for r in items}
    assert len(hashes) == 10


# ---------- worker ----------


def test_chunk_sizes():
    assert chunk_sizes(25) == [10, 10, 5]  # default GENERATOR_CHUNK_SIZE
    assert chunk_sizes(10) == [10]
    assert chunk_sizes(1) == [1]
    assert chunk_sizes(45, size=20) == [20, 20, 5]


@pytest.mark.asyncio
async def test_chunk_size_setting_is_honored(db: AsyncSession, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "generator_chunk_size", 3)
    assert chunk_sizes(7) == [3, 3, 1]
    await _category(db)
    gen = ListGenerator([[good(1)], [good(2)], [good(3)]])
    job = await _run(db, await _job(db, 7), gen)
    assert gen.counts == [3, 3, 1]
    assert job.stats["chunks_total"] == 3
    assert job.accepted_count == 3


@pytest.mark.asyncio
async def test_mixed_batch_lands_right_counts_and_status(db: AsyncSession):
    cat = await _category(db)
    job = await _job(db, 6)
    batch = [
        good(1),
        good(2),
        good(1),                                            # duplicate_in_batch
        item(**{"en.stem": "Q3?", "es.stem": "P3?", "en.options": ["a", "b", "c"]}),  # options_count:en
        item(**{"en.stem": "Q4?", "es.stem": "P4?"}, difficulty=9),                   # difficulty_invalid
        item(**{"en.stem": "Q5?", "es.stem": "P5?"}, difficulty=9, correct_index=9),  # two faults
    ]
    job = await _run(db, job, ListGenerator([batch]))

    assert job.status == "partial"
    assert job.model == "list"
    assert (job.produced_count, job.accepted_count, job.rejected_count) == (6, 2, 4)
    assert job.started_at is not None and job.finished_at is not None
    assert job.stats["rejections"] == {
        "difficulty_invalid": 2,
        "duplicate_in_batch": 1,
        "options_count:en": 1,
        "correct_index_invalid": 1,
    }
    assert job.stats["chunks_total"] == 1 and job.stats["chunks_failed"] == 0
    assert job.stats["emitted"] == 6 and job.stats["repeated"] == 1
    assert job.stats["repeat_rate"] == pytest.approx(1 / 6, abs=1e-4)
    assert job.error.startswith("rejected 4/6: difficulty_invalid ×2")
    assert "repeat_rate 0.17" in job.error

    rows = await _questions_for(db, job)
    assert len(rows) == 2
    for q in rows:
        assert q.status == "pending"
        assert q.source == "ai"
        assert q.pack_id is None
        assert q.category_id == cat.id
        assert q.region == "global"
        assert q.generation_job_id == job.id
    stems = {
        (t.locale, t.stem)
        for t in (
            await db.execute(
                select(QuestionTranslation).join(Question).where(Question.generation_job_id == job.id)
            )
        ).scalars()
    }
    assert stems == {
        ("en", "Q1: what is 1 + 1?"), ("es", "P1: ¿cuánto es 1 + 1?"),
        ("en", "Q2: what is 2 + 2?"), ("es", "P2: ¿cuánto es 2 + 2?"),
    }


@pytest.mark.asyncio
async def test_chunk_that_raises_gives_partial_and_keeps_other_chunks(db: AsyncSession):
    await _category(db)
    job = await _job(db, 25)  # chunks: 10, 10, 5
    gen = ListGenerator([[good(1), good(2)], RuntimeError("model exploded"), [good(3)]])
    job = await _run(db, job, gen)

    assert job.status == "partial"
    assert (job.produced_count, job.accepted_count, job.rejected_count) == (3, 3, 0)
    assert job.stats["chunks_total"] == 3
    assert job.stats["chunks_failed"] == 1
    assert job.stats["chunk_errors"] == ["chunk 1: RuntimeError: model exploded"]
    assert job.stats["rejections"] == {}
    assert job.error == "chunk 1: RuntimeError: model exploded"
    assert len(await _questions_for(db, job)) == 3


@pytest.mark.asyncio
async def test_all_accepted_is_succeeded(db: AsyncSession):
    await _category(db)
    job = await _job(db, 2)
    job = await _run(db, job, ListGenerator([[good(1), good(2)]]))
    assert job.status == "succeeded"
    assert job.error is None
    assert job.stats["repeat_rate"] == 0.0


@pytest.mark.asyncio
async def test_none_accepted_is_failed(db: AsyncSession):
    await _category(db)
    job = await _job(db, 2)
    job = await _run(db, job, ListGenerator([[item(difficulty=9), "junk"]]))
    assert job.status == "failed"
    assert (job.produced_count, job.accepted_count, job.rejected_count) == (2, 0, 2)
    assert job.stats["rejections"] == {"difficulty_invalid": 1, "not_an_object": 1}
    assert await _questions_for(db, job) == []


@pytest.mark.asyncio
async def test_every_chunk_raising_is_failed(db: AsyncSession):
    await _category(db)
    job = await _job(db, 20)
    job = await _run(db, job, ListGenerator([RuntimeError("a"), ValueError("b")]))
    assert job.status == "failed"
    assert job.produced_count == 0
    assert job.stats["chunks_failed"] == 2
    assert job.error == "chunk 0: RuntimeError: a; chunk 1: ValueError: b"


@pytest.mark.asyncio
async def test_duplicate_of_existing_house_content_is_rejected(db: AsyncSession):
    cat = await _category(db)
    existing = Question(
        category_id=cat.id, difficulty=1, correct_index=0, source="manual",
        content_hash=content_hash("Q1: what is 1 + 1?"),
    )
    db.add(existing)
    await db.flush()

    job = await _job(db, 2)
    job = await _run(db, job, ListGenerator([[good(1), good(2)]]))
    assert job.status == "partial"
    assert (job.accepted_count, job.rejected_count) == (1, 1)
    assert job.stats["rejections"] == {v.DUPLICATE_OF_EXISTING: 1}


@pytest.mark.asyncio
async def test_pack_question_with_same_hash_does_not_block(db: AsyncSession):
    cat = await _category(db)
    from app.models import StudyPack, User

    user = User(display_name="u")
    db.add(user)
    await db.flush()
    pack = StudyPack(owner_id=user.id, title="p", locale="en")
    db.add(pack)
    await db.flush()
    db.add(
        Question(
            category_id=cat.id, difficulty=1, correct_index=0, source="user", pack_id=pack.id,
            content_hash=content_hash("Q1: what is 1 + 1?"),
        )
    )
    await db.flush()

    job = await _job(db, 1)
    job = await _run(db, job, ListGenerator([[good(1)]]))
    assert job.status == "succeeded"
    assert job.accepted_count == 1


@pytest.mark.asyncio
async def test_malformed_twin_first_then_valid_is_accepted_and_counted(db: AsyncSession):
    """Gate vs diagnostic through the worker: the malformed twin is rejected
    on its own fault, the valid twin is inserted, and repeat_rate still
    counts the repeated emission."""
    await _category(db)
    job = await _job(db, 3)
    batch = [item(difficulty=9), item(), item(**{"en.stem": "Other?", "es.stem": "Otra?"})]
    job = await _run(db, job, ListGenerator([batch]))

    assert job.status == "partial"
    assert (job.accepted_count, job.rejected_count) == (2, 1)
    assert job.stats["rejections"] == {"difficulty_invalid": 1}  # no duplicate_in_batch
    assert (job.stats["emitted"], job.stats["repeated"]) == (3, 1)
    assert job.stats["repeat_rate"] == pytest.approx(1 / 3, abs=1e-4)


@pytest.mark.asyncio
async def test_seen_carries_across_chunks(db: AsyncSession):
    await _category(db)
    job = await _job(db, 11)
    job = await _run(db, job, ListGenerator([[good(1)], [good(1)]]))
    assert (job.accepted_count, job.rejected_count) == (1, 1)
    # Chunk 0 committed the row, so the repeat is both an in-batch duplicate
    # and a collision with house content; both codes are reported.
    assert job.stats["rejections"] == {v.DUPLICATE_IN_BATCH: 1, v.DUPLICATE_OF_EXISTING: 1}


@pytest.mark.asyncio
async def test_unknown_category_in_params_fails_job(db: AsyncSession):
    job = await _job(db, 2, slug="ghost")
    job = await _run(db, job, ListGenerator([[good(1)]]))
    assert job.status == "failed"
    assert "ghost" in job.error


@pytest.mark.asyncio
async def test_terminal_job_is_not_rerun(db: AsyncSession):
    await _category(db)
    job = await _job(db, 1)
    job.status = "succeeded"
    await db.flush()
    job = await _run(db, job, ListGenerator([[good(1)]]))
    assert job.produced_count == 0
    assert await _questions_for(db, job) == []


@pytest.mark.asyncio
async def test_stub_end_to_end(db: AsyncSession):
    """A stub job = the mixed batch (1 accepted) + 5 clean items, chunked
    by 10, so the clean tail shares a chunk with the end of the batch."""
    await _category(db)
    count = STUB_MIXED_COUNT + 5
    job = await _job(db, count)
    job = await _run(db, job, StubGenerator())

    assert job.status == "partial"
    assert job.model == "stub"
    assert (job.produced_count, job.accepted_count, job.rejected_count) == (count, 6, count - 6)
    assert job.stats["chunks_total"] == len(chunk_sizes(count, 10))
    assert job.stats["repeated"] == 1
    assert len(job.stats["rejections"]) >= 19
    rows = await _questions_for(db, job)
    assert len(rows) == 6


@pytest.mark.asyncio
async def test_nothing_is_ever_inserted_as_live(db: AsyncSession):
    """Across a stub run and a hand-built run, every generated row is pending."""
    await _category(db)
    for gen in (StubGenerator(), ListGenerator([[good(1), good(2)]])):
        await _run(db, await _job(db, 10), gen)
    non_pending = (
        await db.execute(
            select(func.count()).select_from(Question).where(Question.status != "pending")
        )
    ).scalar_one()
    assert non_pending == 0
    live = (
        await db.execute(select(func.count()).select_from(Question).where(Question.status == "live"))
    ).scalar_one()
    assert live == 0


@pytest.mark.asyncio
async def test_accepted_topics_are_passed_to_later_chunks_as_avoid_hints(db: AsyncSession):
    """Each chunk gets the topic summaries of everything accepted so far —
    rejected items contribute nothing."""
    await _category(db)
    job = await _job(db, 25)  # chunks: 10, 10, 5
    bad = item(difficulty=0)
    gen = ListGenerator([[good(1), bad], [good(2)], []])
    await _run(db, job, gen)

    q1, q2 = v.validate_item(good(1)), v.validate_item(good(2))
    assert isinstance(q1, v.CleanItem) and isinstance(q2, v.CleanItem)
    t1, t2 = topic_summary(q1), topic_summary(q2)
    assert t1 == f"{', '.join(q1.tags)}: {q1.translations['en'].options[q1.correct_index]}"
    assert gen.avoid_seen == [[], [t1], [t1, t2]]


@pytest.mark.asyncio
async def test_stub_job_costs_nothing(db: AsyncSession):
    await _category(db)
    job = await _run(db, await _job(db, 3), ListGenerator([[good(1), good(2), good(3)]]))
    assert job.cost_cents == 0
    assert (job.stats["input_tokens"], job.stats["output_tokens"]) == (0, 0)
