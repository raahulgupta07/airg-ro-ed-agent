#!/usr/bin/env python3
"""ROVER route — HTTP surface for the ROVER extraction engine.

Thin wrapper over the `rover` package (imported lazily inside each handler so a
missing/broken rover import can never crash app startup). All endpoints require
an authenticated user; every body is wrapped in try/except → HTTPException(500).

  POST /extract               run pipeline_fast + persist → doc summary
  GET  /documents             overall rows
  GET  /documents/{doc_id}    one document (404 if absent)
  GET  /products              product rows
  GET  /review                review queue
  POST /review/{doc_id}/apply apply corrections (404 if absent)
  GET  /stats                 review stats
  GET  /export.csv            review CSV download
  GET  /annotate/{doc_id}     first annotated page PNG
"""
from typing import Dict, Any
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from middleware import get_current_user

router = APIRouter()


class ExtractReq(BaseModel):
    pdf_path: str


class ApplyReq(BaseModel):
    corrections: Dict[str, Any]


@router.post("/extract")
async def extract(req: ExtractReq, user=Depends(get_current_user)):
    try:
        from rover import pipeline_fast, store, review
        res = pipeline_fast.run(req.pdf_path)
        doc_id = store.save_document(res)
        return {
            "doc_id": doc_id,
            "needs_review": res.get("needs_review"),
            "n_items": res.get("n_items"),
            "values": res.get("values"),
            "review": review.review_item(res),
        }
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/documents")
async def documents(user=Depends(get_current_user)):
    try:
        from rover import store
        return store.overall_rows()
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/documents/{doc_id}")
async def document(doc_id: str, user=Depends(get_current_user)):
    try:
        from rover import store
        doc = store.load_document(doc_id)
        if doc is None:
            raise HTTPException(404, "document not found")
        return doc
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/products")
async def products(user=Depends(get_current_user)):
    try:
        from rover import store
        return store.product_rows()
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/review")
async def review_queue(user=Depends(get_current_user)):
    try:
        from rover import review
        return review.review_queue()
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/review/{doc_id}/apply")
async def review_apply(doc_id: str, req: ApplyReq, user=Depends(get_current_user)):
    try:
        from rover import review
        res = review.apply_corrections(doc_id, req.corrections)
        if res is None:
            raise HTTPException(404, "document not found")
        return res
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/stats")
async def stats(user=Depends(get_current_user)):
    try:
        from rover import review
        return review.stats()
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/export.csv")
async def export_csv(user=Depends(get_current_user)):
    try:
        from rover import review
        path = "/tmp/rover_review.csv"
        review.export_review_csv(path)
        return FileResponse(path, media_type="text/csv", filename="rover_review.csv")
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/annotate/{doc_id}")
async def annotate(doc_id: str, user=Depends(get_current_user)):
    try:
        from rover import store, annotate as annot
        doc = store.load_document(doc_id)
        if doc is None:
            raise HTTPException(404, "document not found")
        paths = annot.annotate_result(doc, "/app/data/_uat_test", "/app/data/rover_annot")
        if not paths:
            raise HTTPException(404, "no annotation available")
        return FileResponse(paths[0], media_type="image/png")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
