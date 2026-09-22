"""users & billing: users, subscriptions, ticket_ledger (spec §3)."""
import uuid
from datetime import date

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    false,
)
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models._common import (
    LOCALES,
    REGIONS,
    ROLES,
    SUBSCRIPTION_PLATFORMS,
    SUBSCRIPTION_STATUSES,
    Timestamp,
    TimestampNow,
    UuidPk,
    one_of,
)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email", name="users_email_key"),
        UniqueConstraint(
            "auth_provider", "auth_subject", name="users_auth_provider_auth_subject_key"
        ),
        one_of("locale", LOCALES, name="users_locale_check"),
        one_of("region", REGIONS, name="users_region_check"),
        one_of("role", ROLES, name="users_role_check"),
    )

    id: Mapped[UuidPk]
    email: Mapped[str | None] = mapped_column(CITEXT)
    display_name: Mapped[str] = mapped_column(Text)
    locale: Mapped[str] = mapped_column(Text, server_default="en")
    region: Mapped[str] = mapped_column(Text, server_default="global")
    auth_provider: Mapped[str | None] = mapped_column(Text)
    auth_subject: Mapped[str | None] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text, server_default="player")
    # 13+ attestation, see spec §7
    age_gate_passed: Mapped[bool] = mapped_column(Boolean, server_default=false())
    created_at: Mapped[TimestampNow]

    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="user")
    ticket_entries: Mapped[list["TicketLedgerEntry"]] = relationship(back_populates="user")


class Subscription(Base):
    __tablename__ = "subscriptions"
    __table_args__ = (
        UniqueConstraint(
            "original_transaction_id", name="subscriptions_original_transaction_id_key"
        ),
        one_of("platform", SUBSCRIPTION_PLATFORMS, name="subscriptions_platform_check"),
        one_of("status", SUBSCRIPTION_STATUSES, name="subscriptions_status_check"),
    )

    id: Mapped[UuidPk]
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE")
    )
    platform: Mapped[str] = mapped_column(Text)
    product_id: Mapped[str] = mapped_column(Text)
    original_transaction_id: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    current_period_end: Mapped[Timestamp]
    updated_at: Mapped[TimestampNow]

    user: Mapped[User] = relationship(back_populates="subscriptions")


Index("subscriptions_user_id_status_idx", Subscription.user_id, Subscription.status)


class TicketLedgerEntry(Base):
    """Auditable ticket balance. Never a bare counter: a failed generation
    must be refundable, and support needs to see why a balance is what it is."""

    __tablename__ = "ticket_ledger"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE")
    )
    delta: Mapped[int] = mapped_column(Integer)  # +5 monthly grant, -1 spend, +1 refund
    # 'monthly_grant','pack_generate','refund_failed','promo'
    reason: Mapped[str] = mapped_column(Text)
    pack_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("study_packs.id", ondelete="SET NULL")
    )
    period_start: Mapped[date | None] = mapped_column(Date)  # monthly grants; one per period
    created_at: Mapped[TimestampNow]

    user: Mapped[User] = relationship(back_populates="ticket_entries")


Index(
    "ticket_ledger_user_id_created_at_idx",
    TicketLedgerEntry.user_id,
    TicketLedgerEntry.created_at.desc(),
)
Index(
    "ticket_ledger_user_id_period_start_idx",
    TicketLedgerEntry.user_id,
    TicketLedgerEntry.period_start,
    unique=True,
    postgresql_where=TicketLedgerEntry.reason == "monthly_grant",
)
