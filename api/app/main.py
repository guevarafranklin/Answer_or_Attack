from fastapi import FastAPI

from app.routers import categories, generation

app = FastAPI(title="Answer or Attack — Content API")
app.include_router(categories.router)
app.include_router(generation.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
