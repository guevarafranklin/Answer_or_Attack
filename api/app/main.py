from fastapi import FastAPI

from app.routers import categories

app = FastAPI(title="Answer or Attack — Content API")
app.include_router(categories.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
