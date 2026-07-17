"""python -m rover.run_batch — run ROVER (fast) on ALL docs in _uat_test,
streaming one JSONL line per doc (header record + product items)."""
import json, os, sys, time, traceback
sys.path.insert(0, "/app")
from rover import pipeline_fast, store

PDF_DIR = "/app/data/_uat_test"
OUT = "/app/data/_uat_test/_rover_batch.jsonl"


def main():
    pdfs = sorted(f for f in os.listdir(PDF_DIR) if f.lower().endswith(".pdf"))
    open(OUT, "w").close()
    for i, fn in enumerate(pdfs, 1):
        rec = {"idx": i, "n": len(pdfs), "pdf": fn}
        t0 = time.time()
        try:
            res = pipeline_fast.run(f"{PDF_DIR}/{fn}", use_challenger=True)
            store.save_document(res)           # persist each extraction as durable data
            rec.update({"ok": True, "sec": res["sec"], "cost": res["cost"],
                        "pages_sent": res["pages_sent"], "pages_total": res["pages_total"],
                        "needs_review": res["needs_review"], "suspect": res["suspect"],
                        "values": res["values"], "items": res["items"],
                        "n_items": res["n_items"]})
        except Exception as e:
            rec.update({"ok": False, "sec": round(time.time() - t0, 1),
                        "error": str(e), "trace": traceback.format_exc()[-500:]})
        with open(OUT, "a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
        print(f"[{i}/{len(pdfs)}] {fn} ok={rec.get('ok')} ${rec.get('cost')} "
              f"{rec.get('sec')}s items={rec.get('n_items')} "
              f"review={rec.get('needs_review')}", flush=True)
    print("BATCH DONE", flush=True)


if __name__ == "__main__":
    main()
