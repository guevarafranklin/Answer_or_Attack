"""Health views (spec §4): three saved queries over question_stats with a
serves >= 50 floor, and the dashboard summary.

    easy     correct/serves > 0.90            ratio DESC
    suspect  correct/serves < 0.25 OR reports > 3   (usually a wrong key)
    dead     timeouts/serves > 0.60           (usually too long to read)

Everything here reads question_stats — never question_serves, which is the
hot table the rollup exists to keep the dashboard away from (§3.1).
"""
from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy import ColumnElement, Float, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Category, Question, QuestionStats, QuestionTranslation
from app.models._common import LOCALES, QUESTION_STATUSES
from app.schemas.telemetry import (
    CategoryCounts,
    HealthCounts,
    HealthRow,
    HealthSummary,
    HealthView,
)

HEALTH_MIN_SERVES = 50
EASY_MIN_RATIO = 0.90
SUSPECT_MAX_RATIO = 0.25
SUSPECT_MIN_REPORTS = 3  # reports > 3
DEAD_MIN_RATIO = 0.60


@dataclass(frozen=True)
class _View:
    ratio: ColumnElement[float]
    where: ColumnElement[bool]
    descending: bool


_correct_ratio = cast(QuestionStats.correct, Float) / QuestionStats.serves
_timeout_ratio = cast(QuestionStats.timeouts, Float) / QuestionStats.serves

VIEWS: dict[HealthView, _View] = {
    "easy": _View(_correct_ratio, _correct_ratio > EASY_MIN_RATIO, descending=True),
    "suspect": _View(
        _correct_ratio,
        (_correct_ratio < SUSPECT_MAX_RATIO) | (QuestionStats.reports > SUSPECT_MIN_REPORTS),
        descending=False,  # worst key first
    ),
    "dead": _View(_timeout_ratio, _timeout_ratio > DEAD_MIN_RATIO, descending=True),
}


def _view_query(view: HealthView):
    spec = VIEWS[view]
    return (
        select(QuestionStats, spec.ratio.label("ratio"))
        .where(QuestionStats.serves >= HEALTH_MIN_SERVES, spec.where)
        .order_by(
            spec.ratio.desc() if spec.descending else spec.ratio.asc(), QuestionStats.question_id
        )
    )


async def health_view(
    db: AsyncSession, view: HealthView, *, page: int, page_size: int
) -> tuple[list[HealthRow], int]:
    """One page of a view plus its total row count."""
    query = _view_query(view)
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    result = await db.execute(
        query.options(
            selectinload(QuestionStats.question).selectinload(Question.translations)
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = [
        HealthRow(
            **{c.name: getattr(stats, c.name) for c in QuestionStats.__table__.columns},
            ratio=ratio,
            question=stats.question,
        )
        for stats, ratio in result
    ]
    return rows, total or 0


async def health_counts(db: AsyncSession) -> HealthCounts:
    """How many questions each view holds (question_stats only)."""
    counts = {
        view: await db.scalar(select(func.count()).select_from(_view_query(view).subquery()))
        for view in VIEWS
    }
    return HealthCounts(**{view: n or 0 for view, n in counts.items()})


async def health_summary(db: AsyncSession) -> HealthSummary:
    """Dashboard numbers over house content (pack_id IS NULL, like the
    review queue): questions by status, by category × status, by locale ×
    status (questions that have text in that locale), the pending backlog,
    and the health view counts."""
    house = Question.pack_id.is_(None)

    by_status: dict[str, int] = {s: 0 for s in QUESTION_STATUSES}
    rows = await db.execute(
        select(Question.status, func.count()).where(house).group_by(Question.status)
    )
    by_status.update({status: n for status, n in rows})

    by_category: dict[str, dict[str, int]] = defaultdict(
        lambda: {s: 0 for s in QUESTION_STATUSES}
    )
    rows = await db.execute(
        select(Category.slug, Question.status, func.count())
        .join(Question, Question.category_id == Category.id)
        .where(house)
        .group_by(Category.slug, Question.status)
        .order_by(Category.slug)
    )
    for slug, status, n in rows:
        by_category[slug][status] = n

    by_locale: dict[str, dict[str, int]] = {
        locale: {s: 0 for s in QUESTION_STATUSES} for locale in LOCALES
    }
    rows = await db.execute(
        select(QuestionTranslation.locale, Question.status, func.count())
        .join(Question, Question.id == QuestionTranslation.question_id)
        .where(house)
        .group_by(QuestionTranslation.locale, Question.status)
    )
    for locale, status, n in rows:
        by_locale[locale][status] = n

    return HealthSummary(
        pending_backlog=by_status["pending"],
        by_status=by_status,
        by_category=[
            CategoryCounts(slug=slug, counts=counts) for slug, counts in sorted(by_category.items())
        ],
        by_locale=by_locale,
        health=await health_counts(db),
    )
