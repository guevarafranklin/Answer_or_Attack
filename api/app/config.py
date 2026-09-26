from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    redis_url: str = "redis://localhost:6379/0"
    anthropic_api_key: str = ""
    # Bearer token for /admin/* (spec §4: simple token now, real auth in Phase 4).
    # Empty means admin routes reject everything — there is no default secret.
    admin_token: str = ""
    # Which Generator the worker uses: "stub" (fixed items, no network) or
    # "claude" (app.services.claude_generator). Defaults to the stub so
    # nothing calls a model by accident.
    generator_backend: str = "stub"
    generator_model: str = "claude-sonnet-5"
    # Questions per model call. Smaller chunks finish sooner and a truncated
    # or malformed response loses fewer questions.
    generator_chunk_size: int = 10
    # Output ceiling per call. A response cut off here shows up as a chunk
    # error with stop_reason=max_tokens.
    generator_max_tokens: int = 16000
    # List price of generator_model in USD per million tokens; this is what
    # turns token usage into generation_jobs.cost_cents. Check these against
    # the current price sheet whenever generator_model changes.
    generator_price_input_per_mtok: float = 3.0
    generator_price_output_per_mtok: float = 15.0
    # Ask the API to enforce the response schema (structured outputs). Turn
    # off if generator_model doesn't support it; the JSON is validated
    # either way.
    generator_structured_output: bool = True

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()