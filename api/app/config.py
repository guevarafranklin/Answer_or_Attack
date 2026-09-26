from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    redis_url: str = "redis://localhost:6379/0"
    anthropic_api_key: str = ""
    # Bearer token for /admin/* (spec §4: simple token now, real auth in Phase 4).
    # Empty means admin routes reject everything — there is no default secret.
    admin_token: str = ""
    # Which Generator the worker uses: "stub" (fixed items, no network) or
    # "claude" (§9 step 5). Defaults to the stub so nothing calls a model by
    # accident.
    generator_backend: str = "stub"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()