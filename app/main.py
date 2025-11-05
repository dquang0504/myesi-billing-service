from fastapi import FastAPI
from app.api.billing_routes import router as billing_router

app = FastAPI(title="MyESI Billing Service", docs_url="/docs")


@app.get("/")
def root():
    return {"message": "Billing Service Running 🚀"}


app.include_router(billing_router)
