from fastapi import FastAPI

from app.routers import categories, generation, questions

app = FastAPI(title="Answer or Attack — Content API")
app.include_router(categories.router)
app.include_router(generation.router)
app.include_router(questions.router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
