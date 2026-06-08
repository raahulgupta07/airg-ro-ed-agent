#!/usr/bin/env python3
"""
v2 Confidence — Per-field confidence scoring.
Computes confidence for each extracted value based on multiple signals.
No LLM call — purely computational.
"""

from typing import Dict, List


def compute_field_confidence(
    declaration: Dict,
    items: List[Dict],
    page_results: List[Dict] = None,
    self_review: Dict = None,
    anomalies: Dict = None,
    fixes_log: List[Dict] = None,
    correction_stats: List[Dict] = None,
) -> Dict:
    """
    Compute per-field confidence scores.
    Returns { "declaration": { field: {value, confidence, signals} }, "items": [ {field: {value, confidence, signals}} ] }
    """

    # Get self-review errors
    review_errors = {}
    if self_review and self_review.get("errors"):
        for err in self_review["errors"]:
            field = err.get("field", "")
            item_idx = err.get("item")
            key = f"item_{item_idx}_{field}" if item_idx is not None else f"decl_{field}"
            review_errors[key] = err

    # Get anomaly flags
    anomaly_fields = set()
    if anomalies and anomalies.get("anomalies"):
        for a in anomalies["anomalies"]:
            anomaly_fields.add(a.get("field", ""))

    # Get fixer changes
    fixed_fields = set()
    if fixes_log:
        for fix in fixes_log:
            fixed_fields.add(fix.get("field", ""))

    # Get correction history counts
    correction_counts = {}
    if correction_stats:
        for stat in correction_stats:
            key = f"{stat.get('table_key', '')}.{stat.get('field_key', '')}"
            correction_counts[key] = stat.get("count", 0)

    # Count how many pages have each field (cross-validation)
    field_page_counts = {}
    if page_results:
        for pr in page_results:
            if pr.get("status") != "ok":
                continue
            fields = pr.get("parsed", {}).get("fields", {})
            for k in fields:
                if fields[k] is not None:
                    field_page_counts[k] = field_page_counts.get(k, 0) + 1

    # --- Declaration confidence ---
    decl_confidence = {}
    for field, value in declaration.items():
        conf = 0.90  # Base: LLM extracted
        signals = []

        if value is None:
            conf = 0.0
            signals.append("missing")
        elif value == 0 or str(value).strip() in ("0", "0.0"):
            # Could be legitimately 0 (e.g., duty=FREE) or actually missing
            # Check if the field commonly has 0
            zero_ok_fields = {"Import/Export Customs Duty", "Commercial Tax (CT)", "Security Fee (SF)",
                              "MACCS Service Fee (MF)", "Exemption/Reduction"}
            if field in zero_ok_fields:
                conf = 0.85
                signals.append("zero-ok")
            else:
                conf = 0.50
                signals.append("suspicious-zero")

        # Cross-validation: found on multiple pages
        matching_keys = [k for k in field_page_counts if field.lower().replace(" ", "") in k.lower().replace(" ", "")]
        if matching_keys and max(field_page_counts.get(k, 0) for k in matching_keys) >= 2:
            conf = min(conf + 0.05, 1.0)
            signals.append("cross-validated")

        # Self-review flagged
        review_key = f"decl_{field}"
        if review_key in review_errors:
            conf = max(conf - 0.15, 0.0)
            signals.append("review-flagged")

        # Anomaly detected
        if field in anomaly_fields or any(field.lower() in af.lower() for af in anomaly_fields):
            conf = max(conf - 0.10, 0.0)
            signals.append("anomaly")

        # Fixer changed it
        if field in fixed_fields:
            conf = 0.85  # Reset to fixer confidence
            signals.append("fixer-corrected")

        # Historical corrections
        corr_key = f"declaration.{field}"
        if correction_counts.get(corr_key, 0) >= 3:
            conf = max(conf - 0.20, 0.0)
            signals.append("often-corrected")

        decl_confidence[field] = {
            "value": value,
            "confidence": round(conf, 2),
            "signals": signals,
            "level": "high" if conf >= 0.8 else "medium" if conf >= 0.5 else "low",
        }

    # --- Items confidence ---
    items_confidence = []
    for i, item in enumerate(items):
        item_conf = {}
        for field, value in item.items():
            conf = 0.90
            signals = []

            if value is None:
                conf = 0.0
                signals.append("missing")
            elif value == 0 or str(value).strip() in ("0", "0.0"):
                zero_ok_item_fields = {"Customs duty rate", "Commercial tax %"}
                if field in zero_ok_item_fields:
                    conf = 0.85
                    signals.append("zero-ok")
                else:
                    conf = 0.50
                    signals.append("suspicious-zero")

            # Self-review flagged
            review_key = f"item_{i}_{field}"
            if review_key in review_errors:
                conf = max(conf - 0.15, 0.0)
                signals.append("review-flagged")

            # Fixer changed it
            if field in fixed_fields:
                conf = 0.85
                signals.append("fixer-corrected")

            # Historical corrections
            corr_key = f"product_items.{field}"
            if correction_counts.get(corr_key, 0) >= 3:
                conf = max(conf - 0.20, 0.0)
                signals.append("often-corrected")

            item_conf[field] = {
                "value": value,
                "confidence": round(conf, 2),
                "signals": signals,
                "level": "high" if conf >= 0.8 else "medium" if conf >= 0.5 else "low",
            }
        items_confidence.append(item_conf)

    # --- Overall stats ---
    all_confs = [v["confidence"] for v in decl_confidence.values() if v["confidence"] > 0]
    for ic in items_confidence:
        all_confs.extend(v["confidence"] for v in ic.values() if v["confidence"] > 0)

    avg_conf = sum(all_confs) / len(all_confs) if all_confs else 0
    low_count = sum(1 for c in all_confs if c < 0.5)
    medium_count = sum(1 for c in all_confs if 0.5 <= c < 0.8)
    high_count = sum(1 for c in all_confs if c >= 0.8)

    return {
        "declaration": decl_confidence,
        "items": items_confidence,
        "summary": {
            "average_confidence": round(avg_conf, 2),
            "total_fields": len(all_confs),
            "high": high_count,
            "medium": medium_count,
            "low": low_count,
        }
    }
