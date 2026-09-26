"""Review queue (spec §4): list, edit and change the status of questions.

Functions here flush but do not commit; the caller decides the transaction
boundary. Status changes are the only way a question becomes 'live' (spec
§1: a human approves), so `approve` is where the "both locales present"
guarantee for the game is enforced.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import Category, Question, QuestionTranslation
from app.models._common import LOCALES
from app.schemas.content import (
    BulkAction,
    QuestionListQuery,
    QuestionTranslationIn,
    QuestionUpdate,
)
from app.rules import RuleViolation, validate_answer_not_in_stem
from app.services.validation import content_hash


class QuestionError(Exception):
    """A review action that cannot be applied to this question."""


class InvalidEdit(QuestionError):
    """The edited question breaks an app.rules rule that needs the whole
    question to check (the schema already covered per-locale text)."""

    def __init__(self, code: str, locale: str, message: str) -> None:
        super().__init__(f"{message} ({code}:{locale})")
        self.code = code
        self.locale = locale


class DuplicateQuestion(QuestionError):
    def __init__(self) -> None:
        super().__init__("another house question already has this en stem")


class MissingLocale(QuestionError):
    def __init__(self, missing: list[str]) -> None:
        super().__init__(f"cannot approve: missing translation for locale(s): {', '.join(missing)}")
        self.missing = missing


def _with_translations():
    return select(Question).options(selectinload(Question.translations))


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def get_question(db: AsyncSession, question_id: uuid.UUID) -> Question | None:
    result = await db.execute(_with_translations().where(Question.id == question_id))
    return result.scalar_one_or_none()


async def list_questions(
    db: AsyncSession, query: QuestionListQuery
) -> tuple[list[Question], int]:
    """House questions (pack_id IS NULL — the review queue is for house
    content; pack questions belong to their owner), newest first, filtered
    by the query. Returns the page and the total match count."""
    where = [Question.pack_id.is_(None)]
    if query.status is not None:
        where.append(Question.status == query.status)
    if query.difficulty is not None:
        where.append(Question.difficulty == query.difficulty)
    if query.job_id is not None:
        where.append(Question.generation_job_id == query.job_id)
    if query.category is not None:
        category_id = select(Category.id).where(Category.slug == query.category)
        where.append(Question.category_id == category_id.scalar_subquery())
    if query.locale is not None:
        where.append(
            exists().where(
                QuestionTranslation.question_id == Question.id,
                QuestionTranslation.locale == query.locale,
            )
        )

    total = await db.scalar(select(func.count()).select_from(Question).where(*where))
    result = await db.execute(
        _with_translations()
        .where(*where)
        .order_by(Question.created_at.desc(), Question.id)
        .offset((query.page - 1) * query.page_size)
        .limit(query.page_size)
    )
    return list(result.scalars()), int(total or 0)


async def update_question(db: AsyncSession, question: Question, data: QuestionUpdate) -> Question:
    """Apply only the fields the client sent. Translations are merged per
    locale: a locale that is sent is replaced, one that is omitted is kept.
    Per-locale text limits were already enforced by the schema (app.rules);
    the answer-in-stem rule needs correct_index too, so it is checked here
    on the merged result and raises InvalidEdit. Changing the en stem
    re-derives content_hash; a collision with another house question raises
    DuplicateQuestion. Either failure rolls back to the pre-call state (a
    savepoint), so the caller's transaction stays usable."""
    fields = data.model_dump(exclude_unset=True)
    translations: dict[str, QuestionTranslationIn] = data.translations or {}
    try:
        # Every change is made inside the savepoint: begin_nested() flushes
        # first, so anything dirtied before it would fail in the outer
        # transaction instead.
        async with db.begin_nested():
            for name, value in fields.items():
                if name != "translations":
                    setattr(question, name, value)
            by_locale = {t.locale: t for t in question.translations}
            for locale, incoming in translations.items():
                if locale in by_locale:
                    existing = by_locale[locale]
                    existing.stem = incoming.stem
                    existing.options = incoming.options
                    existing.explanation = incoming.explanation
                else:
                    question.translations.append(
                        QuestionTranslation(
                            locale=locale,
                            stem=incoming.stem,
                            options=incoming.options,
                            explanation=incoming.explanation,
                        )
                    )
            if "en" in translations:
                question.content_hash = content_hash(translations["en"].stem)
            for t in question.translations:
                try:
                    validate_answer_not_in_stem(t.stem, t.options[question.correct_index])
                except RuleViolation as exc:
                    raise InvalidEdit(exc.code, t.locale, str(exc)) from exc
    except IntegrityError as exc:
        if _constraint_name(exc) == "questions_house_hash_uniq":
            raise DuplicateQuestion() from exc
        raise
    await db.refresh(question, attribute_names=["updated_at"])  # onupdate=now() ran server-side
    return question


def approve(question: Question) -> None:
    """→ 'live'. Refuses a question that lacks a locale: the game serves
    both, so a half-translated row must never reach the pool."""
    present = {t.locale for t in question.translations}
    missing = [locale for locale in LOCALES if locale not in present]
    if missing:
        raise MissingLocale(missing)
    _set_status(question, "live", reviewed=True)


def reject(question: Question) -> None:
    _set_status(question, "rejected", reviewed=True)


def archive(question: Question) -> None:
    """→ 'archived'. Retires a question without a review verdict, so
    reviewed_by/at are left as they were."""
    _set_status(question, "archived", reviewed=False)


ACTIONS = {"approve": approve, "reject": reject, "archive": archive}


def _set_status(question: Question, status: str, *, reviewed: bool) -> None:
    if question.status == status:
        return  # idempotent; don't re-stamp a decision that was already made
    question.status = status
    if reviewed:
        question.reviewed_by = settings.admin_user_id
        question.reviewed_at = _now()


async def apply_action(db: AsyncSession, question: Question, action: BulkAction) -> Question:
    ACTIONS[action](question)
    await db.flush()
    await db.refresh(question, attribute_names=["updated_at"])
    return question


async def apply_bulk(
    db: AsyncSession, ids: list[uuid.UUID], action: BulkAction
) -> tuple[list[uuid.UUID], list[tuple[uuid.UUID, str]]]:
    """Apply `action` to every id it can be applied to. Returns the ids that
    changed and (id, reason) for each that did not; one bad id never blocks
    the rest."""
    result = await db.execute(_with_translations().where(Question.id.in_(ids)))
    found = {q.id: q for q in result.scalars()}
    updated: list[uuid.UUID] = []
    failed: list[tuple[uuid.UUID, str]] = []
    for question_id in dict.fromkeys(ids):  # keep order, drop repeats
        question = found.get(question_id)
        if question is None:
            failed.append((question_id, "question not found"))
            continue
        try:
            ACTIONS[action](question)
        except QuestionError as exc:
            failed.append((question_id, str(exc)))
        else:
            updated.append(question_id)
    await db.flush()
    return updated, failed


def _constraint_name(exc: IntegrityError) -> str | None:
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "constraint_name", None)
