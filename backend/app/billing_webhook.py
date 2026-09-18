from fastapi import APIRouter, Request, HTTPException, Header
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
    result = service.process_webhook(payload, stripe_signature)
    return result
