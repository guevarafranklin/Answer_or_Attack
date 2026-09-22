from fastapi import FastAPI

app = FastAPI(title="Answer or Attack — Content API")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
