from fastapi import FastAPI
from app.api.billing_routes import router as billing_router
from app.api.billing_addresses_routes import router as billing_addresses_router
from app.api.paddle_webhook_routes import router as paddle_webhook_router
from app.api.payment_method_routes import router as payment_method_router

app = FastAPI(title="MyESI Billing Service", docs_url="/docs")


@app.get("/")
def root():
    return {"message": "Billing Service Running 🚀"}


app.include_router(billing_router)
app.include_router(billing_addresses_router)
app.include_router(paddle_webhook_router)
app.include_router(payment_method_router)
