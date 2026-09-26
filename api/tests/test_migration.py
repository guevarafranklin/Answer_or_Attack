"""§9 step 1: the initial migration creates the §3 schema and its invariants."""
import uuid

import pytest
from alembic import command
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import alembic_config

EXPECTED_TABLES = {
    "users",
    "subscriptions",
    "study_packs",
    "ticket_ledger",
    "categories",
    "category_translations",
    "generation_jobs",
    "questions",
    "question_translations",
    "question_serves",
    "question_stats",
    "question_reports",
    "rollup_watermarks",
    "sessions",
    "session_players",
    "session_questions",
}


async def _table_names(db: AsyncSession) -> set[str]:
    rows = await db.execute(
        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    )
    return {r[0] for r in rows} - {"alembic_version"}


@pytest.mark.asyncio
async def test_all_spec_tables_exist(db: AsyncSession):
    assert await _table_names(db) == EXPECTED_TABLES


@pytest.mark.asyncio
async def test_named_partial_indexes_exist(db: AsyncSession):
    rows = await db.execute(
        text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'questions'")
    )
    indexes = {name: definition for name, definition in rows}
    assert "questions_house_hash_uniq" in indexes
    assert "WHERE (pack_id IS NULL)" in indexes["questions_house_hash_uniq"]
    assert "questions_house_draw" in indexes
    assert "(status = 'live'" in indexes["questions_house_draw"]
    assert "pack_id IS NULL" in indexes["questions_house_draw"]
    assert "questions_review_queue" in indexes


async def _insert_question(db: AsyncSession, category_id, content_hash, pack_id=None):
    await db.execute(
        text(
            """
            INSERT INTO questions (category_id, difficulty, correct_index, content_hash, pack_id)
            VALUES (:category_id, 3, 0, :content_hash, :pack_id)
            """
        ),
        {"category_id": category_id, "content_hash": content_hash, "pack_id": pack_id},
    )


async def _fixture_category_user_pack(db: AsyncSession):
    category_id = (
        await db.execute(text("INSERT INTO categories (slug) VALUES ('t-math') RETURNING id"))
    ).scalar_one()
    user_id = (
        await db.execute(
            text("INSERT INTO users (display_name) VALUES ('tester') RETURNING id")
        )
    ).scalar_one()
    pack_id = (
        await db.execute(
            text(
                "INSERT INTO study_packs (owner_id, title, locale) "
                "VALUES (:owner, 'pack', 'en') RETURNING id"
            ),
            {"owner": user_id},
        )
    ).scalar_one()
    return category_id, pack_id


@pytest.mark.asyncio
async def test_house_content_hash_is_unique(db: AsyncSession):
    """Two house questions (pack_id IS NULL) with the same hash must collide."""
    category_id, _ = await _fixture_category_user_pack(db)
    await _insert_question(db, category_id, "hash-a")
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await _insert_question(db, category_id, "hash-a")


@pytest.mark.asyncio
async def test_pack_content_hash_may_repeat(db: AsyncSession):
    """The uniqueness constraint is partial: pack content may share a hash
    with house content and with other pack content."""
    category_id, pack_id = await _fixture_category_user_pack(db)
    await _insert_question(db, category_id, "hash-b")            # house
    await _insert_question(db, category_id, "hash-b", pack_id)   # pack, same hash
    await _insert_question(db, category_id, "hash-b", pack_id)   # pack again
    count = (
        await db.execute(
            text("SELECT count(*) FROM questions WHERE content_hash = 'hash-b'")
        )
    ).scalar_one()
    assert count == 3


@pytest.mark.asyncio
async def test_check_constraints_enforced(db: AsyncSession):
    category_id, _ = await _fixture_category_user_pack(db)
    bad_rows = [
        # difficulty out of 1..5
        "INSERT INTO questions (category_id, difficulty, correct_index, content_hash) VALUES (:c, 6, 0, 'x1')",
        # correct_index out of 0..3
        "INSERT INTO questions (category_id, difficulty, correct_index, content_hash) VALUES (:c, 3, 4, 'x2')",
        # invalid status
        "INSERT INTO questions (category_id, difficulty, correct_index, content_hash, status) VALUES (:c, 3, 0, 'x3', 'draft')",
    ]
    for sql in bad_rows:
        with pytest.raises(IntegrityError):
            async with db.begin_nested():
                await db.execute(text(sql), {"c": category_id})


@pytest.mark.asyncio
async def test_translation_requires_exactly_four_options(db: AsyncSession):
    category_id, _ = await _fixture_category_user_pack(db)
    qid = (
        await db.execute(
            text(
                "INSERT INTO questions (category_id, difficulty, correct_index, content_hash) "
                "VALUES (:c, 3, 0, 'x4') RETURNING id"
            ),
            {"c": category_id},
        )
    ).scalar_one()
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            await db.execute(
                text(
                    "INSERT INTO question_translations (question_id, locale, stem, options) "
                    "VALUES (:q, 'en', 'stem', '[\"a\",\"b\",\"c\"]'::jsonb)"
                ),
                {"q": qid},
            )


def test_downgrade_and_upgrade_round_trip(migrated_db: str):
    """downgrade base removes every spec table; upgrade head restores them."""
    cfg = alembic_config()
    command.downgrade(cfg, "base")
    import psycopg

    from tests.conftest import TEST_URL

    sync_url = TEST_URL.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(sync_url) as conn:
        remaining = {
            r[0]
            for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        }
    assert remaining <= {"alembic_version"}
    command.upgrade(cfg, "head")
    with psycopg.connect(sync_url) as conn:
        restored = {
            r[0]
            for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
            )
        } - {"alembic_version"}
    assert restored == EXPECTED_TABLES
