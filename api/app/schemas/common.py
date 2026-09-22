"""Shared schema building blocks: the ORM-reading base and the closed
vocabularies from the CHECK constraints (spec §3), as Literal types."""
from typing import Literal

from pydantic import BaseModel, ConfigDict


class ORMModel(BaseModel):
    """Read model populated from an ORM instance."""

    model_config = ConfigDict(from_attributes=True)


Locale = Literal["en", "es"]
Region = Literal["us", "latam", "global"]
Role = Literal["player", "admin"]
SubscriptionPlatform = Literal["ios", "android", "promo"]
SubscriptionStatus = Literal["active", "grace", "expired", "refunded"]
StudyPackStatus = Literal["uploading", "processing", "ready", "failed"]
GenerationKind = Literal["category", "study_pack"]
GenerationStatus = Literal["queued", "running", "succeeded", "partial", "failed"]
GradeBand = Literal["g1_g3", "g4_g6", "g7_g9", "g10_g12", "adult"]
QuestionStatus = Literal["pending", "live", "archived", "rejected"]
QuestionSource = Literal["seed", "ai", "user", "manual"]
ServeOutcome = Literal["correct", "incorrect", "timeout", "absent"]
ReportReason = Literal["wrong_answer", "typo", "offensive", "confusing", "other"]
SessionMode = Literal["house", "study"]
SessionStatus = Literal["lobby", "running", "finished", "abandoned"]
