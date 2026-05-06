#!/usr/bin/env python3
"""
Test fee shift fix — run verified PDFs through pipeline, compare declaration fees.
"""
import sys, os, json

# Setup path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "backend"))
os.chdir(os.path.join(os.path.dirname(__file__), "backend"))

# Load env
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import config

# Verified ground truth (from user's spreadsheet)
VERIFIED = {
    "100303470412": {
        "pdf": "/Users/rahulgupta/Downloads/RO List (1)/AZ  ID NO-100303470412.pdf",
        "label": "AZAY",
        "Commercial Tax (CT)": 0,
        "Advance Income Tax (AT)": 3136187,
        "Security Fee (SF)": 0,
        "MACCS Service Fee (MF)": 20000,
        "Exemption/Reduction": 8075683,
        "Invoice Number": "A-9518502163",
        "Invoice Price": 1649911.68,
    },
    "100314743761": {
        "pdf": "/Users/rahulgupta/Downloads/RO List (1)/PD ID NO-100314743761,PO15740.pdf",
        "label": "PREMIUM DIST",
        "Commercial Tax (CT)": 0,
        "Advance Income Tax (AT)": 2608987,
        "Security Fee (SF)": 20000,
        "MACCS Service Fee (MF)": 30000,
        "Exemption/Reduction": 6718143.42,
        "Invoice Number": "A- PDG25016",
        "Invoice Price": 2025360,
    },
}

FEE_FIELDS = [
    "Commercial Tax (CT)", "Advance Income Tax (AT)",
    "Security Fee (SF)", "MACCS Service Fee (MF)", "Exemption/Reduction",
]
OTHER_FIELDS = ["Invoice Number", "Invoice Price"]


def run_one(decl_no, info):
    """Run pipeline on one PDF and compare declaration vs verified."""
    from pipeline.pipeline import run_pipeline

    pdf_path = info["pdf"]
    label = info["label"]

    print(f"\n{'='*80}")
    print(f"  TESTING: {label} (Declaration {decl_no})")
    print(f"  PDF: {os.path.basename(pdf_path)}")
    print(f"{'='*80}\n")

    if not os.path.exists(pdf_path):
        print(f"  ❌ PDF not found: {pdf_path}")
        return None

    result = run_pipeline(pdf_path)
    if not result:
        print(f"  ❌ Pipeline returned None")
        return None

    decl = result.get("declaration", {})

    # Compare fees
    print(f"\n{'─'*60}")
    print(f"  FEE COMPARISON: {label}")
    print(f"{'─'*60}")
    print(f"  {'Field':<30} {'Verified':>15} {'AI Result':>15} {'Match':>7}")
    print(f"  {'─'*30} {'─'*15} {'─'*15} {'─'*7}")

    all_match = True
    for field in FEE_FIELDS:
        verified_val = info[field]
        ai_val = decl.get(field, "MISSING")
        try:
            ai_num = float(ai_val) if ai_val not in (None, "MISSING", "") else 0
        except (ValueError, TypeError):
            ai_num = 0

        # Allow 1% tolerance for rounding
        if verified_val == 0:
            match = ai_num == 0
        else:
            match = abs(ai_num - verified_val) / max(abs(verified_val), 1) < 0.01

        icon = "✅" if match else "❌"
        if not match:
            all_match = False
        print(f"  {field:<30} {verified_val:>15,.2f} {ai_num:>15,.2f} {icon:>7}")

    # Compare other fields
    print(f"\n  OTHER FIELDS:")
    for field in OTHER_FIELDS:
        verified_val = info[field]
        ai_val = decl.get(field, "MISSING")
        match = str(verified_val).strip() == str(ai_val).strip()
        if not match and field == "Invoice Price":
            try:
                match = abs(float(ai_val) - float(verified_val)) < 0.01
            except (ValueError, TypeError):
                pass
        icon = "✅" if match else "❌"
        print(f"  {field:<30} {str(verified_val):>15} {str(ai_val):>15} {icon:>7}")

    print(f"\n  {'✅ ALL FEES CORRECT' if all_match else '❌ FEE ERRORS FOUND'}")
    return {"all_match": all_match, "declaration": decl}


if __name__ == "__main__":
    results = {}
    for decl_no, info in VERIFIED.items():
        results[decl_no] = run_one(decl_no, info)

    # Summary
    print(f"\n\n{'='*80}")
    print(f"  SUMMARY")
    print(f"{'='*80}")
    for decl_no, r in results.items():
        label = VERIFIED[decl_no]["label"]
        if r is None:
            print(f"  {label}: ❌ FAILED (no result)")
        elif r["all_match"]:
            print(f"  {label}: ✅ ALL FEES CORRECT")
        else:
            print(f"  {label}: ❌ FEE ERRORS")
    print()
