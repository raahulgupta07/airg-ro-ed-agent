#!/usr/bin/env python3
"""Run ALL 13 PDFs through pipeline, report declaration fees for each."""
import sys, os, json, time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))
os.chdir(os.path.join(os.path.dirname(__file__), "backend"))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import config
from pipeline.pipeline import run_pipeline

PDF_DIR = "/Users/rahulgupta/Downloads/RO List (1)"

FEE_FIELDS = [
    "Commercial Tax (CT)", "Advance Income Tax (AT)",
    "Security Fee (SF)", "MACCS Service Fee (MF)", "Exemption/Reduction",
]
DECL_FIELDS = [
    "Declaration No", "Declaration Date", "Importer (Name)", "Consignor (Name)",
    "Invoice Number", "Invoice Price", "Currency", "Exchange Rate",
    "Currency 2", "Total Customs Value", "Import/Export Customs Duty",
    "Commercial Tax (CT)", "Advance Income Tax (AT)",
    "Security Fee (SF)", "MACCS Service Fee (MF)", "Exemption/Reduction",
]

# Verified ground truth (only for these 2)
VERIFIED = {
    "100303470412": {
        "label": "AZAY",
        "Commercial Tax (CT)": 0, "Advance Income Tax (AT)": 3136187,
        "Security Fee (SF)": 0, "MACCS Service Fee (MF)": 20000,
        "Exemption/Reduction": 8075683,
    },
    "100314743761": {
        "label": "PREMIUM DIST (PO15740)",
        "Commercial Tax (CT)": 0, "Advance Income Tax (AT)": 2608987,
        "Security Fee (SF)": 20000, "MACCS Service Fee (MF)": 30000,
        "Exemption/Reduction": 6718143.42,
    },
}

PDFS = [
    "ATS ID -100300885160.pdf",
    "ATS ID NO-100315393360.pdf",
    "ATS ID NO-100316740160.pdf",
    "ATS ID NO-100321333561.pdf",
    "ATS ID NO-100323772841.pdf",
    "AZ  ID NO-100303470412.pdf",
    "PD ID NO-100313868761,PO No 15675.pdf",
    "PD ID NO-100313870641,PO 15529.pdf",
    "PD ID NO-100314743761,PO15740.pdf",
    "PD ID NO-100319576711 (EXBOND).pdf",
    "PD ID NO-100319699762.pdf",
    "PD-005 RO 100276090831.pdf",
    "Rch ID -100319165001.pdf",
]

all_results = []

for i, pdf_name in enumerate(PDFS):
    pdf_path = os.path.join(PDF_DIR, pdf_name)
    print(f"\n{'='*80}")
    print(f"  [{i+1}/13] {pdf_name}")
    print(f"{'='*80}")

    start = time.time()
    try:
        result = run_pipeline(pdf_path)
    except Exception as e:
        print(f"  ❌ PIPELINE ERROR: {e}")
        all_results.append({"pdf": pdf_name, "status": "ERROR", "error": str(e)})
        continue

    if not result:
        print(f"  ❌ Pipeline returned None")
        all_results.append({"pdf": pdf_name, "status": "FAILED"})
        continue

    elapsed = time.time() - start
    decl = result.get("declaration", {})
    items_count = result.get("items_count", 0)
    cost = result.get("cost_usd", 0)

    decl_no = str(decl.get("Declaration No", "?")).replace(".0", "")

    print(f"\n  {'─'*60}")
    print(f"  DECLARATION: {decl_no}")
    print(f"  {'─'*60}")
    for field in DECL_FIELDS:
        val = decl.get(field, "—")
        print(f"  {field:<35} = {val}")

    # Check against verified if available
    verified = VERIFIED.get(decl_no)
    fee_ok = None
    if verified:
        print(f"\n  ── VERIFIED CHECK ({verified['label']}) ──")
        fee_ok = True
        for field in FEE_FIELDS:
            expected = verified[field]
            ai_val = float(decl.get(field, 0) or 0)
            if expected == 0:
                match = ai_val == 0
            else:
                match = abs(ai_val - expected) / max(abs(expected), 1) < 0.01
            icon = "✅" if match else "❌"
            if not match:
                fee_ok = False
            print(f"  {icon} {field:<30} expected={expected:>14,.2f}  got={ai_val:>14,.2f}")

    all_results.append({
        "pdf": pdf_name,
        "decl_no": decl_no,
        "importer": decl.get("Importer (Name)", "?"),
        "items": items_count,
        "duration": round(elapsed, 1),
        "cost": round(cost, 4),
        "status": "OK",
        "fee_verified": fee_ok,
        "fees": {f: decl.get(f, 0) for f in FEE_FIELDS},
    })

# ══════════════════════════════════════════════════════════════
# FINAL SUMMARY
# ══════════════════════════════════════════════════════════════
print(f"\n\n{'='*100}")
print(f"  FINAL SUMMARY — ALL 13 PDFs")
print(f"{'='*100}")
print(f"  {'#':<3} {'PDF':<45} {'Decl No':<18} {'Items':>5} {'Time':>6} {'Cost':>7} {'Status':>10}")
print(f"  {'─'*3} {'─'*45} {'─'*18} {'─'*5} {'─'*6} {'─'*7} {'─'*10}")

ok_count = 0
for i, r in enumerate(all_results):
    status = r["status"]
    verified_tag = ""
    if r.get("fee_verified") is True:
        verified_tag = " ✅ VERIFIED"
    elif r.get("fee_verified") is False:
        verified_tag = " ❌ FEE ERR"

    if status == "OK":
        ok_count += 1
    print(f"  {i+1:<3} {r['pdf']:<45} {r.get('decl_no','?'):<18} {r.get('items','?'):>5} {r.get('duration','?'):>5}s ${r.get('cost','?'):>6} {status:>10}{verified_tag}")

print(f"\n  Result: {ok_count}/{len(all_results)} succeeded")
total_cost = sum(r.get("cost", 0) for r in all_results if r["status"] == "OK")
total_time = sum(r.get("duration", 0) for r in all_results if r["status"] == "OK")
print(f"  Total cost: ${total_cost:.4f}")
print(f"  Total time: {total_time:.0f}s ({total_time/60:.1f}min)")

# Fee summary table
print(f"\n  {'─'*100}")
print(f"  FEE VALUES PER DECLARATION")
print(f"  {'─'*100}")
print(f"  {'Decl No':<18} {'Importer':<35} {'CT':>12} {'AT':>12} {'SF':>10} {'MF':>10} {'Exempt':>12}")
print(f"  {'─'*18} {'─'*35} {'─'*12} {'─'*12} {'─'*10} {'─'*10} {'─'*12}")
for r in all_results:
    if r["status"] != "OK":
        continue
    fees = r.get("fees", {})
    ct = float(fees.get("Commercial Tax (CT)", 0) or 0)
    at = float(fees.get("Advance Income Tax (AT)", 0) or 0)
    sf = float(fees.get("Security Fee (SF)", 0) or 0)
    mf = float(fees.get("MACCS Service Fee (MF)", 0) or 0)
    ex = float(fees.get("Exemption/Reduction", 0) or 0)
    print(f"  {r['decl_no']:<18} {r['importer'][:35]:<35} {ct:>12,.0f} {at:>12,.0f} {sf:>10,.0f} {mf:>10,.0f} {ex:>12,.0f}")

print()
