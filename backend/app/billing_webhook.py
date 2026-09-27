from fastapi import APIRouter, Depends, Request, HTTPException, Header
from starlette.concurrency import run_in_threadpool
from .billing_service import BillingService
from .database import get_db
from sqlalchemy.orm import Session

router = APIRouter()

@router.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(..., alias="Stripe-Signature"),
    db: Session = Depends(get_db)
):
    payload = await request.body()
    service = BillingService(db)
    try:
        # The database work is blocking; keep it off the event loop.
        return await run_in_threadpool(service.process_webhook, payload, stripe_signature)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
