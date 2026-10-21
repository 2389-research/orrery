import uuid
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from ..db import mark_graph_dirty
from ..dependencies import get_auth_store, require_writable, AuthStore
from ..pipeline.graph_repair import propose_correction, get_pending_issues, resolve_correction

router = APIRouter()


class ProposalRequest(BaseModel):
    action: str
    entity: str
    rationale: str = ""
    proposer: str = "unknown"
    target_b: str | None = None
    proposed_type: str | None = None
    proposed_name: str | None = None


@router.post("/corrections/propose")
def propose(req: ProposalRequest, auth: AuthStore = Depends(require_writable)):
    store = auth.store
    try:
        return propose_correction(
            store.conn,
            action=req.action, entity=req.entity, rationale=req.rationale,
            proposer=req.proposer, target_b=req.target_b,
            proposed_type=req.proposed_type, proposed_name=req.proposed_name,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        store.close()


@router.get("/corrections")
def list_corrections(auth: AuthStore = Depends(get_auth_store)):
    store = auth.store
    try:
        return get_pending_issues(store.conn)
    finally:
        store.close()


@router.post("/corrections/review/{issue_id}")
def review(issue_id: str, action: str, auth: AuthStore = Depends(require_writable)):
    if action not in ("approve", "reject"):
        raise HTTPException(status_code=400, detail="action must be 'approve' or 'reject'")
    store = auth.store
    try:
        result = resolve_correction(store.conn, issue_id, action)
        # Approving reversibly edits the active graph — flag the snapshot.
        if action == "approve":
            mark_graph_dirty(store.conn)
            store.conn.commit()
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        store.close()


@router.post("/corrections/judge")
def trigger_judge(auth: AuthStore = Depends(require_writable)):
    store = auth.store
    try:
        if store.jobs.get_existing("judge_corrections", "all", ["queued", "running"]):
            raise HTTPException(status_code=409, detail="A correction-judge job is already queued or running")
        job_id = str(uuid.uuid4())
        store.jobs.create(job_id, "judge_corrections", "all")
        return {"job_id": job_id, "status": "queued"}
    finally:
        store.close()
