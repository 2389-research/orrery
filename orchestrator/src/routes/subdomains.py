# ABOUTME: Subdomain discovery route — triggers additive subdomain tagging for all extracted docs.
# ABOUTME: Delegates to the subdomain_discovery pipeline with a Relay instance.

from fastapi import APIRouter, Depends
from orrery_relay import Relay
from ..config import get_settings
from ..dependencies import get_auth_store, require_writable, AuthStore
from ..pipeline.subdomain_discovery import run_subdomain_discovery

router = APIRouter()


@router.post("/discover-subdomains")
async def trigger_subdomain_discovery(auth: AuthStore = Depends(require_writable)):
    """Run subdomain discovery on all extracted docs."""
    settings = get_settings()
    store = auth.store
    relay = Relay.from_settings(settings)
    try:
        results = await run_subdomain_discovery(
            relay=relay,
            model=settings.classification_model,
            conn=store.conn,
        )
    finally:
        store.close()
    return results
