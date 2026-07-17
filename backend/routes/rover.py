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
import os
import tempfile
import asyncio
import queue
import threading
import json
from typing import Dict, Any
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, BackgroundTasks, Body
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

from middleware import get_current_user

router = APIRouter()

_UPLOAD_DIR = "/app/data/rover_uploads"


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


def _process_pdf(dest: str):
    """Run ROVER + persist. Runs in a background threadpool so the request returns
    immediately and the (30-160s) pipeline never blocks the event loop."""
    try:
        from rover import pipeline_fast, store
        res = pipeline_fast.run(dest)
        store.save_document(res)
    except Exception as e:  # pragma: no cover
        print(f"[rover.upload] processing failed for {dest}: {e}")


@router.post("/upload")
async def upload(file: UploadFile = File(...),
                 user=Depends(get_current_user)):
    """Accept a PDF and stage it. Extraction is driven separately by the client
    via GET /extract/stream?pdf=<name>, which streams live progress over SSE."""
    try:
        os.makedirs(_UPLOAD_DIR, exist_ok=True)
        safe = os.path.basename(file.filename or "upload.pdf")
        dest = os.path.join(_UPLOAD_DIR, safe)
        with open(dest, "wb") as fh:
            fh.write(await file.read())
        return {"status": "staged", "pdf": safe}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/extract/stream")
async def extract_stream(pdf: str, user=Depends(get_current_user)):
    """Run ROVER on a staged PDF and stream live pipeline progress as SSE.
    Emits `log` events (one per pipeline checkpoint), then a terminal `done`
    (with doc_id) or `error` event. The heavy pipeline runs in a worker thread;
    its log callback hands lines to a queue drained by the async generator."""
    dest = os.path.join(_UPLOAD_DIR, os.path.basename(pdf))
    if not os.path.exists(dest):
        raise HTTPException(404, "staged PDF not found")

    q: "queue.Queue" = queue.Queue()

    def worker():
        try:
            from rover import pipeline_fast, store
            res = pipeline_fast.run(dest, on_log=lambda ev: q.put(("log", ev)))
            doc_id = store.save_document(res)
            q.put(("done", {
                "doc_id": doc_id,
                "needs_review": res.get("needs_review"),
                "n_items": res.get("n_items"),
            }))
        except Exception as e:
            q.put(("error", {"msg": str(e)}))
        finally:
            q.put((None, None))

    threading.Thread(target=worker, daemon=True).start()

    async def gen():
        loop = asyncio.get_event_loop()
        while True:
            kind, data = await loop.run_in_executor(None, q.get)
            if kind is None:
                break
            yield f"event: {kind}\ndata: {json.dumps(data)}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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


_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("/export.xlsx")
async def export_xlsx(user=Depends(get_current_user)):
    """Excel report of the whole store — Documents sheet + Products sheet."""
    try:
        from rover import excel
        data = excel.workbook_bytes()
        return Response(content=data, media_type=_XLSX,
                        headers={"Content-Disposition": "attachment; filename=rover_report.xlsx"})
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/documents/{doc_id}/export.xlsx")
async def export_doc_xlsx(doc_id: str, user=Depends(get_current_user)):
    """Excel report for one document — Fields sheet (with evidence) + Products."""
    try:
        from rover import store, excel
        doc = store.load_document(doc_id)
        if doc is None:
            raise HTTPException(404, "document not found")
        data = excel.one_doc_bytes(doc)
        return Response(content=data, media_type=_XLSX,
                        headers={"Content-Disposition": f"attachment; filename={doc_id}.xlsx"})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/documents/{doc_id}/pdf")
async def document_pdf(doc_id: str, user=Depends(get_current_user)):
    """Serve the original source PDF (all pages) for inline viewing."""
    try:
        from rover import store
        doc = store.load_document(doc_id)
        if doc is None:
            raise HTTPException(404, "document not found")
        name = doc.get("pdf") or ""
        for pdf_dir in (_UPLOAD_DIR, "/app/data/_uat_test"):
            path = os.path.join(pdf_dir, name)
            if name and os.path.exists(path):
                return FileResponse(path, media_type="application/pdf",
                                    headers={"Content-Disposition": f"inline; filename={name}"})
        raise HTTPException(404, "source PDF not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/annotate/{doc_id}")
async def annotate(doc_id: str, user=Depends(get_current_user)):
    try:
        from rover import store, annotate as annot
        doc = store.load_document(doc_id)
        if doc is None:
            raise HTTPException(404, "document not found")
        # the source PDF may live in the uploads dir or the test corpus — try both
        paths = []
        for pdf_dir in (_UPLOAD_DIR, "/app/data/_uat_test"):
            if os.path.exists(os.path.join(pdf_dir, doc.get("pdf", ""))):
                paths = annot.annotate_result(doc, pdf_dir, "/app/data/rover_annot")
                if paths:
                    break
        if not paths:
            raise HTTPException(404, "no annotation available")
        return FileResponse(paths[0], media_type="image/png")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# --------------------------------------------------------------------------- #
# CUBE / REPORT ENGINE
# --------------------------------------------------------------------------- #
@router.get("/cube/fields")
async def cube_fields(grain: str = "document", user=Depends(get_current_user)):
    try:
        from rover import cube
        return cube.discover_fields(grain)
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/cube/run")
async def cube_run(spec: dict = Body(...), user=Depends(get_current_user)):
    try:
        from rover import cube
        return cube.run_cube(spec)
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/cube/save")
async def cube_save(spec: dict = Body(...), user=Depends(get_current_user)):
    try:
        from rover import cube
        return {"name": cube.save_cube(spec)}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/cube/list")
async def cube_list(user=Depends(get_current_user)):
    try:
        from rover import cube
        return cube.list_cubes()
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/cube/{name}")
async def cube_get(name: str, user=Depends(get_current_user)):
    try:
        from rover import cube
        spec = cube.load_cube(name)
        if spec is None:
            raise HTTPException(404, "cube not found")
        return spec
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@router.delete("/cube/{name}")
async def cube_delete(name: str, user=Depends(get_current_user)):
    try:
        from rover import cube
        return {"deleted": cube.delete_cube(name)}
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/cube/{name}/run")
async def cube_named_run(name: str, user=Depends(get_current_user)):
    try:
        from rover import cube
        spec = cube.load_cube(name)
        if spec is None:
            raise HTTPException(404, "cube not found")
        return cube.run_cube(spec)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


# --------------------------------------------------------------------------- #
# APPROVAL WORKFLOW — uploads are PENDING until a human approves; approved docs
# move to History. `approved` lives on each document (see rover/store_pg.py).
# --------------------------------------------------------------------------- #
class ApproveReq(BaseModel):
    corrections: dict = {}


@router.post("/documents/{doc_id}/approve")
async def approve(doc_id: str, req: ApproveReq, user=Depends(get_current_user)):
    """Approve a document (optionally applying {column: value} corrections),
    moving it out of the pending queue into History. 404 if the doc is absent."""
    try:
        from rover import store
        username = getattr(user, "username", None) or (
            user.get("username") if isinstance(user, dict) else "admin"
        )
        doc = store.approve_document(doc_id, req.corrections, username)
        if doc is None:
            raise HTTPException(404, "document not found")
        return doc
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/pending")
async def pending(user=Depends(get_current_user)):
    """Review inbox — documents awaiting human approval."""
    try:
        from rover import store
        return [r for r in store.overall_rows() if not r.get("approved")]
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/history")
async def history(user=Depends(get_current_user)):
    """Approved documents, newest approval first."""
    try:
        from rover import store
        rows = [r for r in store.overall_rows() if r.get("approved")]
        rows.sort(key=lambda r: r.get("approved_at") or "", reverse=True)
        return rows
    except Exception as e:
        raise HTTPException(500, str(e))


@router.get("/cube/{name}/export.csv")
async def cube_export_csv(name: str, user=Depends(get_current_user)):
    try:
        from rover import cube
        spec = cube.load_cube(name)
        if spec is None:
            raise HTTPException(404, "cube not found")
        csv_text = cube.to_csv(cube.run_cube(spec))
        fd, path = tempfile.mkstemp(suffix=".csv", prefix="rover_cube_")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(csv_text)
        return FileResponse(path, media_type="text/csv",
                            filename=cube._sanitize(name) + ".csv")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
