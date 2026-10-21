# ABOUTME: Reclassify route — re-runs the classifier on all existing documents.
# ABOUTME: Additive: adds new domain assignments without removing existing ones.

from fastapi import APIRouter, Depends
from orrery_relay import Relay
from ..config import get_settings
from ..dependencies import get_auth_store, require_writable, AuthStore
from ..pipeline.excerpt import build_classification_excerpt
from ..pipeline.classifier import classify_document
from ..pipeline.domain_normalizer import normalize_domain_label

router = APIRouter()


@router.post("/reclassify")
async def reclassify_all(auth: AuthStore = Depends(require_writable)):
    """Re-run classification on all documents. Adds new domains without removing existing ones."""
    settings = get_settings()
    store = auth.store
    relay = Relay.from_settings(settings)

    docs = store.documents.list(limit=10000, offset=0)
    results = {"docs_processed": 0, "new_domains": [], "new_assignments": 0}

    for doc in docs:
        doc_id = doc.id
        title = doc.title
        content = doc.content

        # Get existing assignments
        existing = set(d.domain_path for d in store.domains.get_domains_for_document(doc_id))

        # Get current taxonomy
        taxonomy = store.domains.get_all_paths()

        # Classify
        excerpt = build_classification_excerpt(title, content)
        try:
            classification = await classify_document(
                relay=relay, title=title, excerpt=excerpt,
                existing_taxonomy=taxonomy, model=settings.classification_model,
            )
        except Exception as e:
            print(f"  Skip {title}: {e}", flush=True)
            continue

        # Add new domain assignments (don't remove existing)
        all_domains = []
        primary = classification.get("primary_domain")
        if primary:
            all_domains.append(primary)
        for sec in classification.get("secondary_domains", []):
            all_domains.append(sec)

        for domain_path in all_domains:
            if domain_path not in existing:
                path = normalize_domain_label(store, domain_path)
                store.domains.assign_document(doc_id, path, False, 0.7)
                store.domains.increment_doc_count(path)
                results["new_assignments"] += 1
                if path not in existing and path not in results["new_domains"]:
                    if path not in taxonomy:
                        results["new_domains"].append(path)

        results["docs_processed"] += 1
        print(f"  Reclassified {results['docs_processed']}/{len(docs)}: {title[:40]}", flush=True)

        import asyncio
        await asyncio.sleep(0.5)  # Rate limit

    store.close()
    return results
