"""Business logic that routers and scripts share. Services take an
AsyncSession and never commit; the caller owns the transaction."""
