"""Summarize transaction audit JSONL files, including daily rotations."""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
from collections import Counter
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
try:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env")
except Exception:
    pass

POSITIVE_EXPECTED = {"sahte", "supheli"}


def _parse_datetime(value: str | None, *, end_of_day: bool = False) -> datetime | None:
    if not value:
        return None
    try:
        if len(value) == 10:
            day = date.fromisoformat(value)
            moment = datetime.combine(day, time.max if end_of_day else time.min)
        else:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Geçersiz tarih: {value}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _record_time(record: dict[str, Any]) -> datetime | None:
    value = record.get("ts")
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment.astimezone(timezone.utc)


def read_jsonl_files(
    log_dir: Path,
    filename: str,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[dict[str, Any]]:
    base = log_dir / filename
    paths = sorted(base.parent.glob(base.name + "*"))
    records: list[dict[str, Any]] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            print(f"Uyarı: {path.name} okunamadı: {exc}", file=sys.stderr)
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            moment = _record_time(record)
            if start is not None and (moment is None or moment < start):
                continue
            if end is not None and (moment is None or moment > end):
                continue
            records.append(record)
    return records


def _mean_median(records: list[dict[str, Any]], field: str) -> tuple[float | None, float | None]:
    values: list[float] = []
    for record in records:
        value = record.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            values.append(float(value))
    if not values:
        return None, None
    return statistics.mean(values), statistics.median(values)


def _confusion_matrix(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    tp = fp = tn = fn = 0
    for record in records:
        expected = record.get("expected")
        if expected not in {"gercek", "sahte", "supheli"}:
            continue
        actual = str(record.get("classification", "UNKNOWN")).upper()
        predicted_positive = actual != "CANONICAL"
        expected_positive = expected in POSITIVE_EXPECTED
        if expected_positive and predicted_positive:
            tp += 1
        elif not expected_positive and predicted_positive:
            fp += 1
        elif not expected_positive and not predicted_positive:
            tn += 1
        else:
            fn += 1
    total = tp + fp + tn + fn
    precision_denominator = tp + fp
    recall_denominator = tp + fn
    return {
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "samples": total,
        "accuracy": (tp + tn) / total if total else None,
        "precision": tp / precision_denominator if precision_denominator else None,
        "recall": tp / recall_denominator if recall_denominator else None,
    }


def summarize_records(
    transactions: list[dict[str, Any]],
    detections: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    detections = detections or []
    classes = Counter(str(record.get("classification", "UNKNOWN")) for record in transactions)
    total = len(transactions)
    attempted = [record for record in transactions if isinstance(record.get("notified"), bool)]
    successful = sum(record.get("notified") is True for record in attempted)
    latency_mean, latency_median = _mean_median(transactions, "detection_latency_s")
    processing_mean, processing_median = _mean_median(transactions, "processing_ms")
    daily: Counter[str] = Counter()
    for record in transactions:
        moment = _record_time(record)
        if moment is not None:
            daily[moment.date().isoformat()] += 1
    return {
        "total_transactions": total,
        "classification_counts": dict(sorted(classes.items())),
        "classification_rates": {
            name: count / total if total else 0.0 for name, count in sorted(classes.items())
        },
        "detections_total": len(detections),
        "known_suspicious_detections": classes.get("VARIANT", 0) + classes.get("KNOWN_FRAUD", 0),
        "unknown_alerts": classes.get("UNKNOWN", 0),
        "average_detection_latency_s": latency_mean,
        "median_detection_latency_s": latency_median,
        "average_processing_ms": processing_mean,
        "median_processing_ms": processing_median,
        "daily_transactions": dict(sorted(daily.items())),
        "notification_success_rate": successful / len(attempted) if attempted else None,
        "notification_attempts": len(attempted),
        "notification_successes": successful,
        "error_count": sum(bool(record.get("error") or record.get("notify_error")) for record in transactions),
        "confusion_matrix": _confusion_matrix(transactions),
    }


def _table_rows(summary: dict[str, Any]) -> list[tuple[str, str, Any]]:
    rows: list[tuple[str, str, Any]] = [
        ("genel", "toplam_işlem", summary["total_transactions"]),
        ("genel", "tespit_kaydı", summary["detections_total"]),
        ("genel", "şüpheli_ve_sahte_tespit", summary["known_suspicious_detections"]),
        ("genel", "bilinmeyen_token_uyarısı", summary["unknown_alerts"]),
        ("genel", "ortalama_tespit_gecikmesi_s", summary["average_detection_latency_s"]),
        ("genel", "medyan_tespit_gecikmesi_s", summary["median_detection_latency_s"]),
        ("genel", "ortalama_analiz_süresi_ms", summary["average_processing_ms"]),
        ("genel", "medyan_analiz_süresi_ms", summary["median_processing_ms"]),
        ("genel", "bildirim_başarı_oranı", summary["notification_success_rate"]),
        ("genel", "bildirim_deneme_sayısı", summary["notification_attempts"]),
        ("genel", "hata_sayısı", summary["error_count"]),
    ]
    rows.extend(("sınıflandırma", name, count) for name, count in summary["classification_counts"].items())
    rows.extend(("sınıflandırma_oranı", name, rate) for name, rate in summary["classification_rates"].items())
    rows.extend(("günlük", day, count) for day, count in summary["daily_transactions"].items())
    rows.extend(("confusion_matrix", name, value) for name, value in summary["confusion_matrix"].items())
    return rows


def render_markdown(summary: dict[str, Any]) -> str:
    lines = ["| Kapsam | Ölçüm | Değer |", "|---|---|---:|"]
    for group, metric, value in _table_rows(summary):
        rendered = "—" if value is None else f"{value:.4f}" if isinstance(value, float) else str(value)
        lines.append(f"| {group} | {metric} | {rendered} |")
    return "\n".join(lines)


def render_csv(summary: dict[str, Any]) -> str:
    from io import StringIO

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(("category", "metric", "value"))
    writer.writerows(_table_rows(summary))
    return output.getvalue()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Döndürülmüş audit loglarından tez özeti çıkar.")
    parser.add_argument("--log-dir", default=os.getenv("LOG_DIR", "./logs"))
    parser.add_argument("--start", help="Başlangıç günü veya UTC ISO zamanı")
    parser.add_argument("--end", help="Bitiş günü veya UTC ISO zamanı")
    parser.add_argument("--format", choices=("markdown", "csv"), default="markdown")
    parser.add_argument("--output", help="Çıktı dosyası; verilmezse stdout")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        start = _parse_datetime(args.start)
        end = _parse_datetime(args.end, end_of_day=True)
        log_dir = Path(args.log_dir)
        if not log_dir.is_absolute():
            log_dir = PROJECT_ROOT / log_dir
        transactions = read_jsonl_files(log_dir, "transactions.log", start, end)
        detections = read_jsonl_files(log_dir, "detections.log", start, end)
        summary = summarize_records(transactions, detections)
        output = render_csv(summary) if args.format == "csv" else render_markdown(summary)
        if args.output:
            Path(args.output).write_text(output + ("" if output.endswith("\n") else "\n"), encoding="utf-8")
        else:
            print(output)
        return 0
    except (OSError, ValueError) as exc:
        print(f"Hata: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
