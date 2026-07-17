"""Container-side runner: python -m rover.run_one <doc_id> [--no-challenger]
Runs the fleet on /app/data/_uat_test/<doc_id>.pdf, writes JSON result."""
import json
import sys

sys.path.insert(0, "/app")
from rover import pipeline

PDF_DIR = "/app/data/_uat_test"


def main():
    doc = sys.argv[1]
    use_ch = "--no-challenger" not in sys.argv
    path = f"{PDF_DIR}/{doc}.pdf"
    res = pipeline.run(path, use_challenger=use_ch)
    out = f"{PDF_DIR}/_fleet_{doc}.json"
    open(out, "w").write(json.dumps(res, default=str, indent=2))
    print(f"ROVER DONE {doc}  sec={res['sec']} cost=${res['cost']} "
          f"needs_review={res['needs_review']} suspect={res['suspect']}")


if __name__ == "__main__":
    main()
