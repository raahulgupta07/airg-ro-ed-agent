"""python -m rover.run_fast <doc_id> [--no-challenger] — run ROVER (fast)."""
import json, sys
sys.path.insert(0, "/app")
from rover import pipeline_fast, store

PDF_DIR = "/app/data/_uat_test"


def main():
    doc = sys.argv[1]
    use_ch = "--no-challenger" not in sys.argv
    res = pipeline_fast.run(f"{PDF_DIR}/{doc}.pdf", use_challenger=use_ch)
    open(f"{PDF_DIR}/_rover_{doc}.json", "w").write(json.dumps(res, default=str, indent=2))
    doc_id = store.save_document(res)          # persist extraction as durable data
    print(f"stored → {doc_id}")
    print(f"ROVER {doc}  sec={res['sec']} cost=${res['cost']} "
          f"pages_sent={res['pages_sent']}/{res['pages_total']} "
          f"needs_review={res['needs_review']} suspect={res['suspect']}")


if __name__ == "__main__":
    main()
