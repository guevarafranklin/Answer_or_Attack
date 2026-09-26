"""§9 step 6: the review queue (spec §4) — list, get, edit, approve, reject,
archive, bulk."""
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import Category, GenerationJob, Question, QuestionTranslation, User
from app.rules import MAX_STEM_LEN
from app.services.validation import content_hash

# ---------- fixtures ----------

ABCD = ["a", "b", "c", "d"]
TWO_FIVE = ["2", "3", "4", "5"]
PLANETS = ["Mars", "Venus", "Jupiter", "Saturn"]
PLANETAS = ["Marte", "Venus", "Júpiter", "Saturno"]
ES_EDIT = {"stem": "¿Cuánto es 1 + 1?", "options": TWO_FIVE}


async def _category(db: AsyncSession, slug: str = "math") -> Category:
    cat = Category(slug=slug)
    db.add(cat)
    await db.flush()
    return cat


def _translation(locale: str, i: int) -> QuestionTranslation:
    text = {
        "en": (f"Question {i}: what is {i} + {i}?", f"{i} + {i} = {2 * i}."),
        "es": (f"Pregunta {i}: ¿cuánto es {i} + {i}?", f"{i} + {i} = {2 * i}."),
    }[locale]
    return QuestionTranslation(
        locale=locale,
        stem=text[0],
        options=[str(2 * i), str(2 * i + 1), str(2 * i + 2), str(2 * i + 3)],
        explanation=text[1],
    )


async def _question(
    db: AsyncSession,
    category: Category,
    i: int = 1,
    *,
    status: str = "pending",
    locales: tuple[str, ...] = ("en", "es"),
    **fields,
) -> Question:
    q = Question(
        category_id=category.id,
        difficulty=fields.pop("difficulty", 2),
        correct_index=0,
        status=status,
        content_hash=content_hash(f"Question {i}: what is {i} + {i}?"),
        translations=[_translation(locale, i) for locale in locales],
        **fields,
    )
    db.add(q)
    await db.flush()
    return q


async def _fresh(db: AsyncSession, question_id: uuid.UUID) -> Question:
    """Re-read from the DB, overwriting whatever the identity map holds."""
    result = await db.execute(
        select(Question)
        .options(selectinload(Question.translations))
        .where(Question.id == question_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


# ---------- auth ----------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/admin/questions"),
        ("GET", f"/admin/questions/{uuid.uuid4()}"),
        ("PATCH", f"/admin/questions/{uuid.uuid4()}"),
        ("POST", f"/admin/questions/{uuid.uuid4()}/approve"),
        ("POST", f"/admin/questions/{uuid.uuid4()}/reject"),
        ("POST", f"/admin/questions/{uuid.uuid4()}/archive"),
        ("POST", "/admin/questions/bulk"),
    ],
)
async def test_every_route_requires_admin(client: AsyncClient, method: str, path: str):
    resp = await client.request(method, path, json={})
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"


# ---------- list ----------


@pytest.mark.asyncio
async def test_list_defaults_to_house_questions_newest_first(
    client: AsyncClient, admin_headers, db: AsyncSession
):
    cat = await _category(db)
    first = await _question(db, cat, 1)
    second = await _question(db, cat, 2, status="live")
    second.created_at = datetime(2030, 1, 1, tzinfo=timezone.utc)
    await db.flush()

    resp = await client.get("/admin/questions", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert [q["id"] for q in body["items"]] == [str(second.id), str(first.id)]
    assert (body["page"], body["page_size"], body["total"]) == (1, 50, 2)
    assert {t["locale"] for t in body["items"][0]["translations"]} == {"en", "es"}


@pytest.mark.asyncio
async def test_list_filters(client: AsyncClient, admin_headers, db: AsyncSession):
    math, bible = await _category(db, "math"), await _category(db, "bible")
    job = GenerationJob(
        kind="category", prompt="t", params={"category_slug": "math", "count": 1}, requested_count=1
    )
    db.add(job)
    await db.flush()
    pending_math = await _question(db, math, 1, generation_job_id=job.id)
    live_math = await _question(db, math, 2, status="live", difficulty=4)
    pending_bible = await _question(db, bible, 3, difficulty=4)
    en_only = await _question(db, math, 4, locales=("en",))

    async def ids(**params) -> set[str]:
        resp = await client.get("/admin/questions", params=params, headers=admin_headers)
        assert resp.status_code == 200, resp.text
        return {q["id"] for q in resp.json()["items"]}

    assert await ids(status="pending") == {
        str(pending_math.id), str(pending_bible.id), str(en_only.id)
    }
    assert await ids(status="live") == {str(live_math.id)}
    assert await ids(category="bible") == {str(pending_bible.id)}
    assert await ids(category="nope") == set()
    assert await ids(locale="es") == {
        str(pending_math.id), str(live_math.id), str(pending_bible.id)
    }
    assert await ids(difficulty=4) == {str(live_math.id), str(pending_bible.id)}
    assert await ids(difficulty=4, status="pending") == {str(pending_bible.id)}
    assert await ids(job_id=str(job.id)) == {str(pending_math.id)}
    assert await ids(job_id=str(uuid.uuid4())) == set()
    assert await ids(status="pending", category="math", locale="es") == {str(pending_math.id)}
    for bad in ({"difficulty": 0}, {"difficulty": 6}, {"job_id": "nope"}, {"locale": "fr"}):
        resp = await client.get("/admin/questions", params=bad, headers=admin_headers)
        assert resp.status_code == 422, bad


@pytest.mark.asyncio
async def test_list_paginates(client: AsyncClient, admin_headers, db: AsyncSession):
    cat = await _category(db)
    for i in range(1, 6):
        await _question(db, cat, i)

    page1 = await client.get(
        "/admin/questions", params={"page": 1, "page_size": 2}, headers=admin_headers
    )
    page3 = await client.get(
        "/admin/questions", params={"page": 3, "page_size": 2}, headers=admin_headers
    )
    assert page1.json()["total"] == 5
    assert len(page1.json()["items"]) == 2
    assert len(page3.json()["items"]) == 1
    assert (await client.get("/admin/questions?page=0", headers=admin_headers)).status_code == 422
    assert (
        await client.get("/admin/questions?status=bogus", headers=admin_headers)
    ).status_code == 422


@pytest.mark.asyncio
async def test_list_excludes_pack_questions(client: AsyncClient, admin_headers, db: AsyncSession):
    from app.models import StudyPack

    cat = await _category(db)
    owner = User(display_name="owner")
    db.add(owner)
    await db.flush()
    pack = StudyPack(owner_id=owner.id, title="notes", locale="en")
    db.add(pack)
    await db.flush()
    house = await _question(db, cat, 1)
    packed = await _question(db, cat, 2, pack_id=pack.id, source="user")

    resp = await client.get("/admin/questions", headers=admin_headers)
    assert [q["id"] for q in resp.json()["items"]] == [str(house.id)]
    # ...but it is still reachable by id.
    resp = await client.get(f"/admin/questions/{packed.id}", headers=admin_headers)
    assert resp.status_code == 200


# ---------- get ----------


@pytest.mark.asyncio
async def test_get_question(client: AsyncClient, admin_headers, db: AsyncSession):
    cat = await _category(db)
    q = await _question(db, cat, 7, tags=["sums"])

    resp = await client.get(f"/admin/questions/{q.id}", headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(q.id)
    assert body["category_id"] == str(cat.id)
    assert body["status"] == "pending"
    assert body["tags"] == ["sums"]
    assert body["reviewed_by"] is None and body["reviewed_at"] is None
    en = next(t for t in body["translations"] if t["locale"] == "en")
    assert en["stem"] == "Question 7: what is 7 + 7?"
    assert en["options"] == ["14", "15", "16", "17"]


@pytest.mark.asyncio
async def test_unknown_id_is_404(client: AsyncClient, admin_headers):
    missing = uuid.uuid4()
    for method, path in [
        ("GET", f"/admin/questions/{missing}"),
        ("PATCH", f"/admin/questions/{missing}"),
        ("POST", f"/admin/questions/{missing}/approve"),
        ("POST", f"/admin/questions/{missing}/reject"),
        ("POST", f"/admin/questions/{missing}/archive"),
    ]:
        resp = await client.request(method, path, json={}, headers=admin_headers)
        assert resp.status_code == 404, (method, path)


# ---------- edit ----------


@pytest.mark.asyncio
async def test_patch_edits_fields_and_merges_translations(
    client: AsyncClient, admin_headers, db: AsyncSession
):
    cat = await _category(db)
    q = await _question(db, cat, 1)
    old_hash = q.content_hash

    resp = await client.patch(
        f"/admin/questions/{q.id}",
        json={
            "difficulty": 4,
            "correct_index": 2,
            "region": "latam",
            "tags": ["sums", "easy"],
            "grade_band": "g4_g6",
            "translations": {
                "en": {
                    "stem": "What is  1 + 1?",
                    "options": ["0", "1", "2", "3"],
                    "explanation": "One and one.",
                }
            },
        },
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["difficulty"], body["correct_index"], body["region"]) == (4, 2, "latam")
    assert body["tags"] == ["sums", "easy"] and body["grade_band"] == "g4_g6"
    by_locale = {t["locale"]: t for t in body["translations"]}
    assert by_locale["en"]["stem"] == "What is  1 + 1?"
    assert by_locale["en"]["options"] == ["0", "1", "2", "3"]
    assert by_locale["en"]["explanation"] == "One and one."
    # The untouched locale is kept as-is.
    assert by_locale["es"]["stem"] == "Pregunta 1: ¿cuánto es 1 + 1?"
    # Editing the en stem re-derives the dedupe hash.
    fresh = await _fresh(db, q.id)
    assert fresh.content_hash == content_hash("What is  1 + 1?") != old_hash
    assert fresh.status == "pending"


@pytest.mark.asyncio
async def test_patch_es_only_keeps_content_hash(
    client: AsyncClient, admin_headers, db: AsyncSession
):
    cat = await _category(db)
    q = await _question(db, cat, 1)
    old_hash = q.content_hash
    resp = await client.patch(
        f"/admin/questions/{q.id}",
        json={"translations": {"es": ES_EDIT}},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert (await _fresh(db, q.id)).content_hash == old_hash


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload, needle",
    [
        ({"translations": {"en": {"stem": "", "options": ABCD}}}, "empty"),
        (
            {"translations": {"en": {"stem": "x" * (MAX_STEM_LEN + 1), "options": ABCD}}},
            f"at most {MAX_STEM_LEN}",
        ),
        ({"translations": {"en": {"stem": "Q?", "options": ["a", "b", "c"]}}}, "exactly 4"),
        ({"translations": {"en": {"stem": "Q?", "options": ["a", "b", "c", "A "]}}}, "distinct"),
        ({"translations": {"en": {"stem": "Q?", "options": ["a", "b", "c", "  "]}}}, "empty"),
        (
            {"translations": {"en": {"stem": "Q?", "options": [*ABCD[:3], "None of the above"]}}},
            "above",
        ),
        ({"translations": {"es": {"stem": "¿Todas las anteriores?", "options": ABCD}}}, "above"),
        ({"translations": {"fr": {"stem": "Q?", "options": ABCD}}}, "fr"),
        ({"correct_index": 4}, "less than or equal to 3"),
        ({"difficulty": 0}, "greater than or equal to 1"),
        ({"difficulty": 6}, "less than or equal to 5"),
        ({"region": "mars"}, "region"),
        ({"grade_band": "g13"}, "grade_band"),
    ],
)
async def test_patch_rejects_rule_violations(
    client: AsyncClient, admin_headers, db: AsyncSession, payload: dict, needle: str
):
    """Edits go through app.rules — the same limits the generator's
    validator enforces — and nothing is written when they fail."""
    cat = await _category(db)
    q = await _question(db, cat, 1)
    resp = await client.patch(f"/admin/questions/{q.id}", json=payload, headers=admin_headers)
    assert resp.status_code == 422, resp.text
    assert needle in resp.text
    fresh = await _fresh(db, q.id)
    assert fresh.difficulty == 2 and fresh.correct_index == 0
    assert {t.locale: t.stem for t in fresh.translations} == {
        "en": "Question 1: what is 1 + 1?",
        "es": "Pregunta 1: ¿cuánto es 1 + 1?",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload, locale",
    [
        # New stem gives the stored answer (index 0, "Mars") away.
        ({"translations": {"en": {"stem": "Which planet is Mars?", "options": PLANETS}}}, "en"),
        ({"translations": {"es": {"stem": "¿Qué planeta es Marte?", "options": PLANETAS}}}, "es"),
        # Only the answer key moves, onto an option the stored stem contains.
        ({"correct_index": 3}, "en"),
    ],
)
async def test_patch_rejects_answer_in_stem_on_the_merged_question(
    client: AsyncClient, admin_headers, db: AsyncSession, payload: dict, locale: str
):
    """The rule needs stem + options + correct_index together, so it runs on
    the question as it would be after the edit, whichever part changed."""
    cat = await _category(db)
    q = await _question(db, cat, 1)
    en = next(t for t in q.translations if t.locale == "en")
    en.stem, en.options = "Which planet is next to Saturn?", PLANETS  # answer: Mars
    await db.flush()
    q_id = q.id

    resp = await client.patch(f"/admin/questions/{q_id}", json=payload, headers=admin_headers)
    assert resp.status_code == 422, resp.text
    assert f"answer_in_stem:{locale}" in resp.json()["detail"]
    fresh = await _fresh(db, q_id)
    assert fresh.correct_index == 0
    assert {t.locale: t.stem for t in fresh.translations}["en"] == "Which planet is next to Saturn?"


@pytest.mark.asyncio
async def test_patch_en_stem_colliding_with_another_house_question_is_409(
    client: AsyncClient, admin_headers, db: AsyncSession
):
    cat = await _category(db)
    q_id = (await _question(db, cat, 1)).id
    await _question(db, cat, 2)
    resp = await client.patch(
        f"/admin/questions/{q_id}",
        json={
            "translations": {
                "en": {"stem": "  question 2: WHAT is 2 + 2?", "options": ["4", "5", "6", "7"]}
            }
        },
        headers=admin_headers,
    )
    assert resp.status_code == 409
    assert "en stem" in resp.json()["detail"]
    # The rejected edit left nothing behind and the session is still usable.
    fresh = await _fresh(db, q_id)
    assert fresh.content_hash == content_hash("Question 1: what is 1 + 1?")
    assert {t.locale: t.stem for t in fresh.translations}["en"] == "Question 1: what is 1 + 1?"


@pytest.mark.asyncio
async def test_patch_can_add_a_missing_locale(
    client: AsyncClient, admin_headers, db: AsyncSession
):
    cat = await _category(db)
    q = await _question(db, cat, 1, locales=("en",))
    resp = await client.patch(
        f"/admin/questions/{q.id}",
        json={"translations": {"es": ES_EDIT}},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert {t["locale"] for t in resp.json()["translations"]} == {"en", "es"}


# ---------- approve / reject / archive ----------


@pytest.mark.asyncio
async def test_approve_goes_live_and_stamps_review(
    client: AsyncClient, admin_headers, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    reviewer = User(display_name="admin", role="admin")
    db.add(reviewer)
    await db.flush()
    monkeypatch.setattr(settings, "admin_user_id", reviewer.id)
    cat = await _category(db)
    q = await _question(db, cat, 1)

    before = datetime.now(timezone.utc)
    resp = await client.post(f"/admin/questions/{q.id}/approve", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "live"
    assert body["reviewed_by"] == str(reviewer.id)
    assert datetime.fromisoformat(body["reviewed_at"]) >= before
    fresh = await _fresh(db, q.id)
    assert fresh.status == "live" and fresh.reviewed_by == reviewer.id


@pytest.mark.asyncio
async def test_approve_without_admin_user_id_leaves_reviewed_by_null(
    client: AsyncClient, admin_headers, db: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "admin_user_id", None)
    cat = await _category(db)
    q = await _question(db, cat, 1)
    resp = await client.post(f"/admin/questions/{q.id}/approve", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["reviewed_by"] is None
    assert resp.json()["reviewed_at"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("locales, missing", [(("en",), "es"), (("es",), "en"), ((), "en, es")])
async def test_approve_refuses_a_question_missing_a_locale(
    client: AsyncClient, admin_headers, db: AsyncSession, locales: tuple, missing: str
):
    cat = await _category(db)
    q = await _question(db, cat, 1, locales=locales)
    resp = await client.post(f"/admin/questions/{q.id}/approve", headers=admin_headers)
    assert resp.status_code == 409
    assert resp.json()["detail"] == f"cannot approve: missing translation for locale(s): {missing}"
    fresh = await _fresh(db, q.id)
    assert fresh.status == "pending" and fresh.reviewed_at is None


@pytest.mark.asyncio
async def test_reject_and_archive(client: AsyncClient, admin_headers, db: AsyncSession):
    cat = await _category(db)
    to_reject = await _question(db, cat, 1)
    to_archive = await _question(db, cat, 2, status="live")

    resp = await client.post(f"/admin/questions/{to_reject.id}/reject", headers=admin_headers)
    assert resp.status_code == 200 and resp.json()["status"] == "rejected"
    assert resp.json()["reviewed_at"] is not None  # a verdict

    resp = await client.post(f"/admin/questions/{to_archive.id}/archive", headers=admin_headers)
    assert resp.status_code == 200 and resp.json()["status"] == "archived"
    assert resp.json()["reviewed_at"] is None  # retirement, not a review

    # Archive works from either locale state: it never gates on translations.
    half = await _question(db, cat, 3, locales=("en",))
    resp = await client.post(f"/admin/questions/{half.id}/archive", headers=admin_headers)
    assert resp.status_code == 200 and resp.json()["status"] == "archived"


@pytest.mark.asyncio
async def test_repeat_approve_is_idempotent(client: AsyncClient, admin_headers, db: AsyncSession):
    cat = await _category(db)
    q = await _question(db, cat, 1)
    first = await client.post(f"/admin/questions/{q.id}/approve", headers=admin_headers)
    second = await client.post(f"/admin/questions/{q.id}/approve", headers=admin_headers)
    assert second.status_code == 200
    assert second.json()["reviewed_at"] == first.json()["reviewed_at"]


@pytest.mark.asyncio
async def test_rejected_question_can_be_approved_later(
    client: AsyncClient, admin_headers, db: AsyncSession
):
    cat = await _category(db)
    q = await _question(db, cat, 1, status="rejected")
    resp = await client.post(f"/admin/questions/{q.id}/approve", headers=admin_headers)
    assert resp.status_code == 200 and resp.json()["status"] == "live"


# ---------- bulk ----------


@pytest.mark.asyncio
async def test_bulk_approve_reports_per_id_outcomes(
    client: AsyncClient, admin_headers, db: AsyncSession
):
    cat = await _category(db)
    ok1 = await _question(db, cat, 1)
    ok2 = await _question(db, cat, 2)
    half = await _question(db, cat, 3, locales=("en",))
    missing = uuid.uuid4()

    resp = await client.post(
        "/admin/questions/bulk",
        json={
            "ids": [str(ok1.id), str(half.id), str(missing), str(ok2.id), str(ok1.id)],
            "action": "approve",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["updated"] == [str(ok1.id), str(ok2.id)]
    assert body["failed"] == [
        {"id": str(half.id), "detail": "cannot approve: missing translation for locale(s): es"},
        {"id": str(missing), "detail": "question not found"},
    ]
    for q in (ok1, ok2):
        fresh = await _fresh(db, q.id)
        assert fresh.status == "live" and fresh.reviewed_at is not None
    assert (await _fresh(db, half.id)).status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("action, expected", [("reject", "rejected"), ("archive", "archived")])
async def test_bulk_reject_and_archive(
    client: AsyncClient, admin_headers, db: AsyncSession, action: str, expected: str
):
    cat = await _category(db)
    qs = [await _question(db, cat, i) for i in range(1, 4)]
    resp = await client.post(
        "/admin/questions/bulk",
        json={"ids": [str(q.id) for q in qs], "action": action},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.json() == {"updated": [str(q.id) for q in qs], "failed": []}
    for q in qs:
        assert (await _fresh(db, q.id)).status == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"ids": [], "action": "approve"},
        {"ids": [str(uuid.uuid4())], "action": "delete"},
        {"ids": ["not-a-uuid"], "action": "approve"},
        {"action": "approve"},
    ],
)
async def test_bulk_validates_payload(client: AsyncClient, admin_headers, payload: dict):
    resp = await client.post("/admin/questions/bulk", json=payload, headers=admin_headers)
    assert resp.status_code == 422
