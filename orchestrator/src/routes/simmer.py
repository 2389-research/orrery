import uuid
from fastapi import APIRouter, HTTPException, Depends, Query
from ..dependencies import get_auth_store, require_writable, AuthStore
from ..repositories.factory import get_store

router = APIRouter()

@router.post("/simmer/general")
def trigger_general_simmer(
    resume: bool = Query(False, description="Skip golden set phase if one exists from a previous run"),
    auth: AuthStore = Depends(require_writable),
):
    store = auth.store
    existing = store.jobs.get_existing("simmer_general", "general", ["queued", "running"])
    if existing:
        store.close()
        raise HTTPException(status_code=409, detail="General simmer already in progress")
    job_id = str(uuid.uuid4())
    config = {"resume": True} if resume else None
    store.jobs.create(job_id, "simmer_general", "general", config=config)
    store.close()
    return {"job_id": job_id, "status": "queued", "resume": resume}

@router.post("/simmer/{domain_path:path}/image")
def trigger_domain_image_simmer(
    domain_path: str,
    auth: AuthStore = Depends(require_writable),
):
    """Per-domain image spec simmering.

    Adds domain recognition context to the static general image spec.
    Image entity types are universal — only the recognition vocabulary needs simmering.
    """
    store = auth.store
    domain = store.domains.get(domain_path)
    if not domain:
        store.close()
        raise HTTPException(status_code=404, detail=f"Domain not found: {domain_path}")
    existing = store.jobs.get_existing("simmer_domain_image", domain_path, ["queued", "running"])
    if existing:
        store.close()
        raise HTTPException(status_code=409, detail=f"Domain image simmer already in progress for {domain_path}")
    job_id = str(uuid.uuid4())
    store.jobs.create(job_id, "simmer_domain_image", domain_path, config={"domain": domain_path})
    store.close()
    return {"job_id": job_id, "status": "queued"}


@router.post("/simmer/{domain_path:path}")
def trigger_domain_simmer(
    domain_path: str,
    resume: bool = Query(False, description="Skip golden set phase if one exists from a previous run"),
    auth: AuthStore = Depends(require_writable),
):
    store = auth.store
    domain = store.domains.get(domain_path)
    if not domain:
        store.close()
        raise HTTPException(status_code=404, detail=f"Domain not found: {domain_path}")
    existing = store.jobs.get_existing("simmer_domain", domain_path, ["queued", "running"])
    if existing:
        store.close()
        raise HTTPException(status_code=409, detail=f"Domain simmer already in progress for {domain_path}")
    job_id = str(uuid.uuid4())
    config = {"domain": domain_path, "resume": True} if resume else {"domain": domain_path}
    store.jobs.create(job_id, "simmer_domain", domain_path, config=config)
    store.close()
    return {"job_id": job_id, "status": "queued", "resume": resume}
