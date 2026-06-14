import argparse
import csv
import hashlib
import importlib.util
import json
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from pathlib import Path

import fitz

try:
    import cv2
except ImportError:  # pragma: no cover - optional runtime dependency
    cv2 = None

try:
    from rapidocr_onnxruntime import RapidOCR
except ImportError:  # pragma: no cover - optional runtime dependency
    RapidOCR = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sanitize_message_id(value: str | None) -> str:
    raw = (value or "no-message-id").strip()
    raw = raw.strip("<>")
    return raw.replace("/", "_").replace("\\", "_").replace(":", "_")


ROOT = Path(__file__).resolve().parents[1]
INTAKE_SCRIPT_PATH = ROOT / "scripts" / "intake_demo_documents.py"
_RAPID_OCR_ENGINE = None


def resolve_stored_path(path_value: str | Path | None) -> Path | None:
    if path_value is None:
        return None
    path = Path(path_value)
    if path.is_absolute():
        return path
    if path.exists():
        return path
    return (ROOT / path).resolve()


def load_checklist_runtime_config(*, checklist_db_path: str | Path) -> dict[str, object]:
    conn = sqlite3.connect(Path(checklist_db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT config_key, config_value_json FROM checklist_runtime_config ORDER BY config_key"
        ).fetchall()
    finally:
        conn.close()

    config: dict[str, object] = {}
    for row in rows:
        raw_value = row["config_value_json"]
        try:
            config[str(row["config_key"])] = json.loads(raw_value) if raw_value is not None else None
        except json.JSONDecodeError:
            config[str(row["config_key"])] = raw_value
    return config


def load_hsbc_continuity_excluded_statement_dates(*, checklist_db_path: str | Path) -> list[str]:
    config = load_checklist_runtime_config(checklist_db_path=checklist_db_path)
    institution_rules = config.get("institution_statement_rules")
    if not isinstance(institution_rules, dict):
        return []
    hsbc_rules = institution_rules.get("hsbc_hk")
    if not isinstance(hsbc_rules, dict):
        return []
    excluded = hsbc_rules.get("continuity_excluded_statement_dates")
    if not isinstance(excluded, list):
        return []
    return sorted({str(value) for value in excluded if str(value).strip()})


def ensure_database(db_path: Path, schema_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(schema_path.read_text(encoding="utf-8"))
    transaction_columns = {row["name"] for row in conn.execute("PRAGMA table_info(transactions)").fetchall()}
    if "counterparty_phone_raw" not in transaction_columns:
        conn.execute("ALTER TABLE transactions ADD COLUMN counterparty_phone_raw TEXT")
    return conn


def load_intake_module():
    spec = importlib.util.spec_from_file_location("intake_demo_documents", INTAKE_SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_statement_date_from_text(text: str) -> str | None:
    month_map = {
        'Jan': '01', 'Feb': '02', 'Mar': '03', 'Apr': '04', 'May': '05', 'Jun': '06',
        'Jul': '07', 'Aug': '08', 'Sep': '09', 'Oct': '10', 'Nov': '11', 'Dec': '12',
        'January': '01', 'February': '02', 'March': '03', 'April': '04', 'May': '05', 'June': '06',
        'July': '07', 'August': '08', 'September': '09', 'October': '10', 'November': '11', 'December': '12',
    }
    month_regex = "|".join(sorted(month_map.keys(), key=len, reverse=True))

    patterns = [
        r"Statement Date\s+(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})",
        r"Statement Date\b.*?(\d{1,2})-([A-Za-z]{3})-(\d{4})",
        r"Statement Date\b.*?:\s*(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})",
        r"Statement Date\b.*?(?:截數日期|結單日期)?\s*(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})",
        r"Statement Date\b.*?結單日期\s*:?\s*(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})",
        r"Statement Date\b.*?[:：]?\s*(\d{4})-(\d{2})-(\d{2})",
        r"Statement Date\b.*?[:：]?\s*(\d{4})/(\d{2})/(\d{2})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        groups = match.groups()
        if len(groups) == 3 and len(groups[0]) == 4 and groups[1].isdigit() and groups[2].isdigit():
            year, month, day = groups
            return f"{year}-{month}-{day}"
        day, month_abbr, year = groups
        month = month_map.get(month_abbr.title())
        if month is not None:
            return f"{year}-{month}-{int(day):02d}"

    date_match = re.search(rf"\b(\d{{1,2}})\s+({month_regex})\s+(\d{{4}})\b", text, re.IGNORECASE)
    if date_match:
        matched_fragment = date_match.group(0)
        if not re.search(rf"Statement Period\b.*{re.escape(matched_fragment)}", text, re.IGNORECASE | re.DOTALL):
            day, month_name, year = date_match.groups()
            month = month_map.get(month_name.title())
            if month is not None:
                return f"{year}-{month}-{int(day):02d}"

    issue_date_match = re.search(rf"Issue Date\b.*?[:：]?\s*(\d{{1,2}})\s+({month_regex})\s+(\d{{4}})", text, re.IGNORECASE | re.DOTALL)
    if issue_date_match and ("Libra Savings Account Number" not in text):
        day, month_name, year = issue_date_match.groups()
        month = month_map.get(month_name.title())
        if month is not None:
            return f"{year}-{month}-{int(day):02d}"

    period_match = re.search(
        rf"Statement Period\b.*?[:：]?\s*(\d{{1,2}})\s+({month_regex})\s+(\d{{4}})\s*[-–]\s*(\d{{1,2}})\s+({month_regex})\s+(\d{{4}})",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    if period_match:
        _start_day, _start_month_name, _start_year, end_day, end_month_name, end_year = period_match.groups()
        end_month = month_map.get(end_month_name.title())
        if end_month is not None:
            return f"{end_year}-{end_month}-{int(end_day):02d}"

    return None


def parse_account_masked_from_text(text: str) -> str | None:
    if "Libra Savings Account Number" in text or "Savings Account Number" in text:
        return None

    normalized_text = _normalize_ocr_line_text(text)
    patterns = [
        r"Account Number\s+([0-9]{3}-[0-9]{6}-[0-9]{3})",
        r"Account Number\b.*?([0-9]{3}-[0-9]-[0-9]{6}-[0-9])",
        r"(?:INTEGRATED ACCOUNT|INTEGRATED DEPOSITS ACCOUNT).*?:\s*([0-9]{3}-[0-9]-[0-9]{6}-[0-9])",
        r"Account Number\b.*?:\s*([0-9]{9}-[0-9]{3})",
        r"Account Number\b.*?[賬账][戶户]號碼\s*:?\s*([0-9]{9}-[0-9]{3})",
        r"Very Important Client Number\b.*?\b([A-Z]\d{8})\b",
        r"Number\s*:?\s*([0-9]{3}-[0-9]{6}-[0-9]{3})",
        r"Number[^0-9\n]{0,24}:?\s*([0-9]{3}-[0-9]{6}-[0-9]{3})",
        r"Number[^0-9\n]{0,24}:?\s*([0-9]{3}-[0-9]{6}[0-9]{3})",
        r"Account Number\b.*?[賬账][戶户]號碼\s*:?\s*([0-9]{10})",
        r"Account Number\b.*?[:：]?\s*([0-9]{10})",
    ]
    for pattern in patterns:
        match = re.search(pattern, normalized_text, re.IGNORECASE | re.DOTALL)
        if match:
            value = match.group(1)
            compact = re.sub(r"[^0-9]", "", value)
            if len(compact) == 12:
                return f"{compact[:3]}-{compact[3:9]}-{compact[9:]}"
            return value

    return None


def parse_ant_account_bundle_from_text(text: str) -> str | None:
    libra_match = re.search(r"Libra Savings Account Number\s*[:：·]?\s*([0-9]{17})", text, re.IGNORECASE | re.DOTALL)
    savings_match = re.search(r"(?<!Libra )Savings Account Number\s*[:：·]?\s*([0-9]{17})", text, re.IGNORECASE | re.DOTALL)
    if libra_match and savings_match:
        return f"{libra_match.group(1)}|{savings_match.group(1)}"
    return None


def is_probably_pdf(path: Path, mime_type: str) -> bool:
    if mime_type == "application/pdf":
        return True
    if path.suffix.lower() == ".pdf":
        return True
    try:
        return path.read_bytes()[:5] == b"%PDF-"
    except OSError:
        return False


def _get_rapid_ocr_engine():
    global _RAPID_OCR_ENGINE
    if RapidOCR is None:
        return None
    if _RAPID_OCR_ENGINE is None:
        _RAPID_OCR_ENGINE = RapidOCR()
    return _RAPID_OCR_ENGINE


def _normalize_ocr_line_text(text: str) -> str:
    compact = re.sub(r"\s+", " ", text or "").strip()
    compact = compact.replace("户口", "戶口").replace("號码", "號碼").replace("號玛", "號碼")
    compact = re.sub(r"(?i)(\d{1,2})\s*(January|February|March|April|May|June|July|August|September|October|November|December)(\d{4})", r"\1 \2 \3", compact)
    compact = re.sub(r"(?i)(\d{1,2})\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)(\d{4})", r"\1 \2 \3", compact)
    compact = re.sub(r"(?i)\b(\d{1,2})\s*O[ec]t\b", r"\1 Oct", compact)
    compact = re.sub(r"(?i)\b(\d{1,2})(Nov|Dec|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct)\b", r"\1 \2", compact)
    compact = compact.replace("CREDIT NTEREST", "CREDIT INTEREST").replace("CREDITNTEREST", "CREDIT INTEREST")
    compact = compact.replace("BF BALANCE", "B/F BALANCE")
    compact = compact.replace("NANCAL", "NANCIAL").replace("HODING", "HOLDING").replace("F NANCIAL", "FINANCIAL")
    compact = compact.replace("REPAYDRLOAN", "REPAYDIRLOAN")
    compact = compact.replace("MROHENGENG", "MRCHEN GENG").replace("T'ECH", "TECH").replace("TEOH", "TECH")
    compact = compact.replace("TECHCL", "TECH C L").replace("TECHC L", "TECH C L")
    compact = compact.replace("HUATAIFNANCIALHOLDING", "HUATAI FINANCIAL HOLDING")
    compact = compact.replace("利依入", "CREDIT INTEREST").replace("神期收入", "轉賬收入").replace("联收入", "轉賬收入").replace("用收入", "轉收入")
    compact = compact.replace("00'000'00", "100,000.00")
    compact = re.sub(r"(?<=\d)\.(?=\d{3}\.\d{2}\b)", ",", compact)
    return compact


def _compact_hsbc_marker_text(text: str) -> str:
    return re.sub(r"\s+", "", (text or "")).upper()


def extract_visual_ocr_header_text(document_path: Path, page_no: int = 0) -> str | None:
    if cv2 is None:
        return None
    ocr_engine = _get_rapid_ocr_engine()
    if ocr_engine is None:
        return None

    try:
        doc = fitz.open(document_path)
    except Exception:
        return None

    try:
        if page_no < 0 or page_no >= doc.page_count:
            return None
        page = doc.load_page(page_no)
        # HSBC header OCR is a fallback for PDFs whose text layer omits the
        # statement date/account number.  Rendering the whole page at 4x makes
        # RapidOCR process ~1200px-wide crops and can exceed the per-test
        # timeout on real batch samples; 2x still preserves the header text the
        # fallback needs while keeping monthly batch runs responsive.
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        image_bytes = pix.tobytes("png")
    finally:
        doc.close()

    image = cv2.imdecode(
        __import__("numpy").frombuffer(image_bytes, dtype=__import__("numpy").uint8),
        cv2.IMREAD_COLOR,
    )
    if image is None:
        return None

    height, width = image.shape[:2]
    crop_specs = [
        (0, 0, width, max(int(height * 0.5), 1)),
        (int(width * 0.5), 0, width, max(int(height * 0.34), 1)),
        (int(width * 0.55), 0, max(int(width * 0.995), int(width * 0.55) + 1), max(int(height * 0.22), 1)),
        (int(width * 0.58), max(int(height * 0.02), 0), max(int(width * 0.98), int(width * 0.58) + 1), max(int(height * 0.18), 1)),
    ]

    candidates: list[str] = []
    seen: set[str] = set()
    for x1, y1, x2, y2 in crop_specs:
        crop = image[y1:y2, x1:x2]
        if crop.size == 0:
            continue
        result, _ = ocr_engine(crop)
        if not result:
            continue
        lines: list[str] = []
        for item in result:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            text_part = item[1]
            if isinstance(text_part, (list, tuple)):
                text_part = text_part[0]
            normalized = _normalize_ocr_line_text(str(text_part))
            if normalized:
                lines.append(normalized)
        if not lines:
            continue
        joined = "\n".join(lines)
        if joined not in seen:
            seen.add(joined)
            candidates.append(joined)

    if not candidates:
        return None

    best = max(
        candidates,
        key=lambda text: (
            int(bool(parse_account_masked_from_text(text))),
            int(bool(parse_statement_date_from_text(text))),
            len(text),
        ),
    )
    return best


def extract_visual_ocr_page_lines(document_path: Path, page_no: int = 0) -> list[str]:
    if cv2 is None:
        return []
    ocr_engine = _get_rapid_ocr_engine()
    if ocr_engine is None:
        return []

    try:
        doc = fitz.open(document_path)
    except Exception:
        return []

    try:
        if page_no < 0 or page_no >= doc.page_count:
            return []
        page = doc.load_page(page_no)
        # Transaction page OCR is a last-resort fallback for weak/scanned HSBC
        # tables. 1x keeps real monthly batches under pytest/operator timeouts;
        # text-layer extraction remains the primary path for normal PDFs.
        pix = page.get_pixmap(matrix=fitz.Matrix(1, 1), alpha=False)
        image_bytes = pix.tobytes("png")
    finally:
        doc.close()

    image = cv2.imdecode(
        __import__("numpy").frombuffer(image_bytes, dtype=__import__("numpy").uint8),
        cv2.IMREAD_COLOR,
    )
    if image is None:
        return []

    result, _ = ocr_engine(image)
    if not result:
        return []

    lines: list[str] = []
    for item in result:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        text_part = item[1]
        if isinstance(text_part, (list, tuple)):
            text_part = text_part[0]
        normalized = _normalize_ocr_line_text(str(text_part))
        if normalized:
            lines.append(normalized)
    return lines


_MONTH_NAME_TO_NUMBER = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}


def _normalize_statement_account_key(account_masked: str | None) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", account_masked or "unknown")


def _is_amount_line(text: str) -> bool:
    return bool(re.fullmatch(r"\d{1,3}(?:,\d{3})*(?:\.\d{2})", text.strip()))


def _looks_like_hsbc_ocr_account_number(text: str) -> bool:
    return bool(re.fullmatch(r"\d{3}-?\d{6}-?\d{3}", text.strip()))


def _parse_amount_value(text: str) -> float:
    return float(text.replace(",", "").strip())


def _is_huatai_monthly_statement(filename: str) -> bool:
    return filename.upper().endswith("_0M.PDF")


def _parse_huatai_period(statement_date: str | None, filename: str) -> tuple[str | None, str | None]:
    if not statement_date:
        return None, None
    if _is_huatai_monthly_statement(filename):
        return f"{statement_date[:7]}-01", statement_date
    if filename.upper().endswith("_0D.PDF"):
        return statement_date, statement_date
    return None, None


def _parse_huatai_document_currency(raw_text_blob: str) -> str | None:
    for candidate in re.findall(r"\b([A-Z]{3})\b", raw_text_blob):
        if candidate in {"HKD", "USD", "CNY", "RMB", "EUR", "GBP", "JPY", "AUD", "CAD", "SGD"}:
            return candidate
    return None


def _parse_huatai_net_assets_summary(raw_text_blob: str) -> tuple[float | None, float | None]:
    match = re.search(
        r"Net Assets\s+.*?(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)\s+(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)",
        raw_text_blob,
        re.IGNORECASE,
    )
    if not match:
        return None, None
    closing_balance = _parse_amount_value(match.group(1))
    opening_balance = _parse_amount_value(match.group(2))
    return opening_balance, closing_balance


def _parse_hsbc_partial_date(text: str, statement_date: str | None) -> str | None:
    normalized_text = _normalize_ocr_line_text(text)
    match = re.fullmatch(r"(\d{1,2})\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)", normalized_text.strip(), re.IGNORECASE)
    if not match or not statement_date:
        return None
    day = int(match.group(1))
    month = _MONTH_NAME_TO_NUMBER[match.group(2).upper()]
    year = int(statement_date[:4])
    statement_month = int(statement_date[5:7])
    statement_day = int(statement_date[8:10])
    if month > statement_month or (month == statement_month and day > statement_day):
        year -= 1
    return f"{year:04d}-{month:02d}-{day:02d}"


def _is_hsbc_transaction_history_header(text: str) -> bool:
    normalized = _compact_hsbc_marker_text(text)
    return "TRANSACTIONHISTORY" in normalized


def _is_hsbc_stop_line(text: str) -> bool:
    upper = _compact_hsbc_marker_text(text)
    stop_markers = (
        "TOTALRELATIONSHIPBALANCE",
        "IMPORTANTNOTICE",
        "THANKYOUFORCHOOSINGHSBC",
        "WENOTICETHATTHEREHAVEBEENFRAUDULENT",
        "YOURAVERAGETOTALRELATIONSHIPBALANCE",
        "PLEASENOTETHATYOURHSBCPREMIERACCOUNT",
        "DID YOU KNOW?",
    )
    return any(_compact_hsbc_marker_text(marker) in upper for marker in stop_markers)


def _is_hsbc_table_header_line(text: str) -> bool:
    normalized = text.upper().replace(" ", "")
    return normalized in {
        "HKDSAVINGS",
        "DATE",
        "TRANSACTIONDETAILS",
        "DEPOSIT",
        "WITHDRAWAL",
        "BALANCE",
        "日期",
        "進支详情",
        "進支詳情",
        "存入",
        "支出",
        "結余",
        "結餘",
    }


def _looks_like_reference(text: str) -> bool:
    compact = text.strip().upper()
    if compact.startswith("CASHIER ORDER"):
        return True
    return bool(
        re.fullmatch(r"HC[0-9A-Z]+(?:\s+\d{2}[A-Z]{3}|\s+\d{1,2}\s+[A-Z]{3})?", compact)
        or re.fullmatch(r"HK[0-9A-Z]+(?:\s+[A-Z0-9]+)?", compact)
        or re.fullmatch(r"[NT]\d[0-9A-Z]+\([0-9A-Z]+\)", compact)
        or re.fullmatch(r"T\d[0-9A-Z]+\([0-9A-Z]+\)", compact)
    )


def _extract_reference_no(desc_lines: list[str]) -> str | None:
    for line in desc_lines:
        compact = line.strip()
        upper = compact.upper()
        if upper.startswith("CASHIER ORDER"):
            return compact
        if re.fullmatch(r"HC[0-9A-Z]+\s+\d{2}[A-Z]{3}", upper):
            return compact.split()[0]
        if re.fullmatch(r"HC[0-9A-Z]+\s+\d{1,2}\s+[A-Z]{3}", upper):
            return compact.split()[0]
        if re.fullmatch(r"HC[0-9A-Z]+", upper):
            return compact
        if re.fullmatch(r"HK[0-9A-Z]+(?:\s+[A-Z0-9]+)?", upper):
            return compact.split()[0]
        if re.fullmatch(r"[NT]\d[0-9A-Z]+\([0-9A-Z]+\)", upper):
            return compact
    return None


def _extract_counterparty(desc_lines: list[str]) -> str | None:
    if not desc_lines:
        return None
    first = desc_lines[0].strip()
    second = desc_lines[1].strip() if len(desc_lines) > 1 else ""
    upper_first = first.upper()
    upper_second = second.upper()
    if _looks_like_reference(first) and second and not _looks_like_reference(second):
        if upper_second == "DEBIT AS ADVISED":
            return None
        return second
    if second and _looks_like_reference(second):
        if upper_first.startswith("CR TO "):
            return None
        return first
    if upper_second == "DEBIT AS ADVISED":
        return None
    if upper_first.startswith("CR TO "):
        return None
    if "LEGEND" in upper_first or "HUATAI" in upper_first or upper_first.startswith(("CHEN GENG", "CHEN, GENG", "MR CHEN GENG")):
        return first
    return None


def _infer_hsbc_channel(desc_lines: list[str]) -> str | None:
    upper = " | ".join(line.upper() for line in desc_lines)
    compact_upper = _compact_hsbc_marker_text(upper)
    if "CREDITINTEREST" in compact_upper:
        return "INTEREST"
    if "PAYME" in compact_upper:
        return "PAYME"
    if "SALARY" in compact_upper:
        return "SALARY"
    if "REPAYDIRLOAN" in compact_upper:
        return "LOAN"
    if "CASHIER ORDER" in upper or "CR TO" in upper or any(_looks_like_reference(line) for line in desc_lines):
        return "TRANSFER"
    return None


def _infer_hsbc_direction(desc_lines: list[str]) -> str | None:
    upper = " | ".join(line.upper() for line in desc_lines)
    compact_upper = _compact_hsbc_marker_text(upper)
    if "B/FBALANCE" in compact_upper:
        return None
    debit_markers = ("TOPAYME", "SERVICECHARGE", "DEBITASADVISED", "CRTO", "CASHIERORDER")
    if any(marker in compact_upper for marker in debit_markers):
        return "debit"
    credit_markers = ("CREDITINTEREST", "SALARY", "REPAYDIRLOAN")
    if any(marker in compact_upper for marker in credit_markers):
        return "credit"
    if any(_looks_like_reference(line) for line in desc_lines):
        return "credit"
    return "credit"


def _count_hsbc_transaction_markers(lines: list[dict[str, object]]) -> tuple[int, int]:
    inside = False
    date_count = 0
    amount_count = 0
    for line in lines:
        text = _normalize_ocr_line_text(str(line["text"]))
        if not inside:
            if _is_hsbc_transaction_history_header(text):
                inside = True
            continue
        if _is_hsbc_stop_line(text):
            break
        if re.fullmatch(r"\d{1,2}\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)", text, re.IGNORECASE):
            date_count += 1
        if _is_amount_line(text):
            amount_count += 1
    return date_count, amount_count


def _has_usable_hsbc_transaction_lines(lines: list[dict[str, object]]) -> bool:
    date_count, amount_count = _count_hsbc_transaction_markers(lines)
    return date_count >= 1 and amount_count >= 2


def _should_try_hsbc_transaction_page_ocr(lines: list[dict[str, object]]) -> bool:
    date_count, amount_count = _count_hsbc_transaction_markers(lines)
    # If a normal-size raw layer already exposes the HSBC transaction table
    # header but no transaction-like dates/amounts before the stop line, the PDF
    # is a portfolio summary shell (seen in real monthly samples) rather than a
    # weak OCR table. Full-page RapidOCR on those shells is both expensive and
    # non-contributory. Tiny synthetic/scan-like text layers can still use the
    # OCR fallback covered by the weak-text regression test.
    if date_count > 0 or amount_count > 0 or len(lines) < 9:
        return True
    return not any(_is_hsbc_transaction_history_header(str(line["text"])) for line in lines)


def _collect_hsbc_line_items(conn: sqlite3.Connection, document_id: str, attachment_path: Path | None) -> list[dict[str, object]]:
    raw_rows = conn.execute(
        "SELECT page_no, line_no, raw_text FROM raw_document_lines WHERE document_id = ? ORDER BY page_no, line_no",
        (document_id,),
    ).fetchall()
    line_items = [
        {
            "page_no": int(row["page_no"]),
            "line_no": int(row["line_no"]),
            "text": _normalize_ocr_line_text(str(row["raw_text"])),
            "source": "raw",
        }
        for row in raw_rows
        if str(row["raw_text"]).strip()
    ]
    if _has_usable_hsbc_transaction_lines(line_items):
        return line_items
    if attachment_path is None or not attachment_path.exists():
        return line_items

    page_rows = conn.execute(
        "SELECT page_no FROM raw_document_pages WHERE document_id = ? ORDER BY page_no",
        (document_id,),
    ).fetchall()
    page_numbers = [int(row["page_no"]) for row in page_rows] or [1]
    ocr_items: list[dict[str, object]] = []
    for page_no in page_numbers:
        ocr_lines = extract_visual_ocr_page_lines(attachment_path, page_no=page_no - 1)
        for index, text in enumerate(ocr_lines, start=1):
            if text.strip():
                ocr_items.append(
                    {
                        "page_no": page_no,
                        "line_no": index,
                        "text": _normalize_ocr_line_text(text),
                        "source": "ocr",
                    }
                )
    return ocr_items or line_items


def _extract_hsbc_transaction_section_lines(line_items: list[dict[str, object]], statement_date: str | None) -> list[dict[str, object]]:
    inside = False
    collected: list[dict[str, object]] = []
    for item in line_items:
        text = str(item["text"]).strip()
        if not text:
            continue
        if not inside:
            if _is_hsbc_transaction_history_header(text):
                inside = True
            continue
        if _is_hsbc_stop_line(text):
            break
        if _is_hsbc_table_header_line(text):
            continue
        if _looks_like_hsbc_ocr_account_number(text):
            continue
        if statement_date and text == statement_date:
            continue
        collected.append(item)
    return collected


def _build_hsbc_source_line_ref(items: list[dict[str, object]]) -> tuple[int | None, str | None]:
    if not items:
        return None, None
    page_no = int(items[0]["page_no"])
    start_line = int(items[0]["line_no"])
    end_line = int(items[-1]["line_no"])
    return page_no, f"{page_no}:{start_line}-{end_line}"


def _iter_hsbc_transaction_groups(section_lines: list[dict[str, object]], statement_date: str | None) -> list[tuple[str, list[dict[str, object]]]]:
    groups: list[tuple[str, list[dict[str, object]]]] = []
    current_date: str | None = None
    current_items: list[dict[str, object]] = []
    for item in section_lines:
        txn_date = _parse_hsbc_partial_date(str(item["text"]), statement_date)
        if txn_date:
            if current_date is not None:
                groups.append((current_date, current_items))
            current_date = txn_date
            current_items = []
            continue
        if current_date is not None:
            current_items.append(item)
    if current_date is not None:
        groups.append((current_date, current_items))
    return groups


def _round_money(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 2)


def _signed_amount(amount: float, direction: str | None) -> float | None:
    if direction == "credit":
        return amount
    if direction == "debit":
        return -amount
    return None


def _parse_hsbc_group_records(txn_date: str, items: list[dict[str, object]]) -> dict[str, object]:
    texts = [str(item["text"]).strip() for item in items if str(item["text"]).strip()]
    if not texts:
        return {"opening_balance": None, "transactions": []}

    desc_groups: list[list[str]] = []
    amounts: list[float] = []
    current_desc: list[str] = []
    for text in texts:
        if _is_amount_line(text):
            desc_groups.append(current_desc)
            amounts.append(_parse_amount_value(text))
            current_desc = []
        else:
            current_desc.append(text)
    non_empty_desc_groups = [group for group in desc_groups if group]
    if not non_empty_desc_groups or not amounts:
        return {"opening_balance": None, "transactions": []}

    if non_empty_desc_groups == [["B/F BALANCE"]]:
        return {"opening_balance": amounts[0], "transactions": []}

    trailing_balance = amounts[len(non_empty_desc_groups)] if len(amounts) > len(non_empty_desc_groups) else None
    transactions: list[dict[str, object]] = []
    for index_within_date, desc_lines in enumerate(non_empty_desc_groups, start=1):
        amount = amounts[index_within_date - 1]
        explicit_balance = trailing_balance if index_within_date == len(non_empty_desc_groups) else None
        direction = _infer_hsbc_direction(desc_lines)
        if direction is None:
            continue
        transactions.append(
            {
                "txn_date": txn_date,
                "description_raw": " | ".join(desc_lines),
                "amount": amount,
                "direction": direction,
                "explicit_balance": explicit_balance,
            }
        )
    return {"opening_balance": None, "transactions": transactions}


def _derive_hsbc_statement_balances(
    line_items: list[dict[str, object]],
    statement_date: str | None,
) -> dict[str, object]:
    section_lines = _extract_hsbc_transaction_section_lines(line_items, statement_date)
    groups = _iter_hsbc_transaction_groups(section_lines, statement_date)

    explicit_opening_balance: float | None = None
    transactions: list[dict[str, object]] = []
    for txn_date, items in groups:
        parsed = _parse_hsbc_group_records(txn_date, items)
        opening_balance = parsed.get("opening_balance")
        if opening_balance is not None and explicit_opening_balance is None:
            explicit_opening_balance = float(opening_balance)
        transactions.extend(list(parsed.get("transactions") or []))

    derived_opening_balance = explicit_opening_balance
    if derived_opening_balance is None and transactions:
        first_txn = transactions[0]
        first_explicit_balance = first_txn.get("explicit_balance")
        first_signed_amount = _signed_amount(float(first_txn["amount"]), str(first_txn.get("direction")))
        if first_explicit_balance is not None and first_signed_amount is not None:
            derived_opening_balance = float(first_explicit_balance) - first_signed_amount

    running_balance = derived_opening_balance
    last_explicit_balance: float | None = explicit_opening_balance
    for txn in transactions:
        signed_amount = _signed_amount(float(txn["amount"]), str(txn.get("direction")))
        if running_balance is not None and signed_amount is not None:
            running_balance = _round_money(running_balance + signed_amount)
        explicit_balance = txn.get("explicit_balance")
        if explicit_balance is not None:
            last_explicit_balance = float(explicit_balance)
            running_balance = float(explicit_balance)

    derived_closing_balance = last_explicit_balance
    if derived_closing_balance is None:
        derived_closing_balance = running_balance

    return {
        "statement_date": statement_date,
        "derived_opening_balance": _round_money(derived_opening_balance),
        "derived_closing_balance": _round_money(derived_closing_balance),
        "transaction_count": len(transactions),
        "transactions": transactions,
    }


def validate_hsbc_statement_balance_chain(
    *,
    db_path: str | Path,
    only_facts_built: bool = True,
    excluded_statement_dates: list[str] | None = None,
) -> dict[str, object]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        excluded_statement_dates = sorted({str(value) for value in (excluded_statement_dates or []) if str(value).strip()})
        excluded_statement_dates_set = set(excluded_statement_dates)
        query = (
            """
            SELECT d.document_id, d.filename, d.processing_status,
                   a.stored_path,
                   df.account_masked_raw, df.statement_date
            FROM documents d
            JOIN email_attachments a ON a.attachment_id = d.attachment_id
            JOIN document_facts df ON df.document_id = d.document_id
            WHERE d.source_type = 'email_attachment'
              AND d.institution_id = 'hsbc_hk'
            """
        )
        if only_facts_built:
            query += " AND d.processing_status = 'document_facts_built'"
        query += " ORDER BY df.account_masked_raw, df.statement_date, d.document_id"
        rows = conn.execute(query).fetchall()

        statements_by_account: dict[str, list[dict[str, object]]] = {}
        statements_scanned = 0
        statements_derived = 0
        for row in rows:
            statement_date = str(row["statement_date"] or "") or None
            if statement_date in excluded_statement_dates_set:
                continue
            statements_scanned += 1
            attachment_path = resolve_stored_path(row["stored_path"])
            line_items = _collect_hsbc_line_items(conn, str(row["document_id"]), attachment_path)
            derived = _derive_hsbc_statement_balances(line_items, statement_date)
            if derived["derived_opening_balance"] is not None and derived["derived_closing_balance"] is not None:
                statements_derived += 1
            account_masked = str(row["account_masked_raw"] or "unknown")
            statements_by_account.setdefault(account_masked, []).append(
                {
                    "document_id": str(row["document_id"]),
                    "filename": str(row["filename"]),
                    "statement_date": statement_date,
                    "derived_opening_balance": derived["derived_opening_balance"],
                    "derived_closing_balance": derived["derived_closing_balance"],
                    "transaction_count": derived["transaction_count"],
                }
            )

        account_summaries: list[dict[str, object]] = []
        first_statement_zero_opening_passed = 0
        first_statement_zero_opening_failed = 0
        links_checked = 0
        links_matched = 0
        links_mismatched = 0
        links_with_missing_balances = 0

        for account_masked, statements in statements_by_account.items():
            sorted_statements = sorted(
                statements,
                key=lambda item: (str(item.get("statement_date") or ""), str(item.get("document_id") or "")),
            )
            statements_by_date: dict[str, dict[str, object]] = {}
            undated_statements: list[dict[str, object]] = []
            for statement in sorted_statements:
                statement_date = str(statement.get("statement_date") or "")
                if not statement_date:
                    undated_statements.append(statement)
                    continue
                existing = statements_by_date.get(statement_date)
                if existing is None:
                    statement["duplicate_copy_count"] = 1
                    statement["duplicate_document_ids"] = [str(statement.get("document_id") or "")]
                    statement["duplicate_filenames"] = [str(statement.get("filename") or "")]
                    statements_by_date[statement_date] = statement
                    continue
                existing["duplicate_copy_count"] = int(existing.get("duplicate_copy_count") or 1) + 1
                existing.setdefault("duplicate_document_ids", [str(existing.get("document_id") or "")])
                existing.setdefault("duplicate_filenames", [str(existing.get("filename") or "")])
                existing["duplicate_document_ids"].append(str(statement.get("document_id") or ""))  # type: ignore[index, union-attr]
                existing["duplicate_filenames"].append(str(statement.get("filename") or ""))  # type: ignore[index, union-attr]
                if (
                    existing.get("derived_opening_balance") != statement.get("derived_opening_balance")
                    or existing.get("derived_closing_balance") != statement.get("derived_closing_balance")
                ):
                    existing["duplicate_balance_conflict"] = True
            statements = [*statements_by_date.values(), *undated_statements]
            statements = sorted(
                statements,
                key=lambda item: (str(item.get("statement_date") or ""), str(item.get("document_id") or "")),
            )
            if statements:
                first_opening_balance = statements[0].get("derived_opening_balance")
                if first_opening_balance is not None and abs(float(first_opening_balance)) <= 0.01:
                    first_statement_zero_opening_passed += 1
                else:
                    first_statement_zero_opening_failed += 1

            links: list[dict[str, object]] = []
            for previous, current in zip(statements, statements[1:]):
                previous_closing = previous.get("derived_closing_balance")
                next_opening = current.get("derived_opening_balance")
                possible_missing_intermediate_statements = False
                if previous.get("statement_date") and current.get("statement_date"):
                    gap_days = (datetime.fromisoformat(str(current["statement_date"])) - datetime.fromisoformat(str(previous["statement_date"]))).days
                    possible_missing_intermediate_statements = gap_days > 40

                if previous_closing is None or next_opening is None:
                    links_with_missing_balances += 1
                    links.append(
                        {
                            "from_statement_date": previous.get("statement_date"),
                            "to_statement_date": current.get("statement_date"),
                            "previous_closing_balance": previous_closing,
                            "next_opening_balance": next_opening,
                            "difference": None,
                            "status": "missing_balance",
                            "possible_missing_intermediate_statements": possible_missing_intermediate_statements,
                        }
                    )
                    continue

                links_checked += 1
                difference = _round_money(float(next_opening) - float(previous_closing))
                status = "matched" if difference is not None and abs(float(difference)) <= 0.01 else "mismatched"
                if status == "matched":
                    links_matched += 1
                else:
                    links_mismatched += 1
                links.append(
                    {
                        "from_statement_date": previous.get("statement_date"),
                        "to_statement_date": current.get("statement_date"),
                        "previous_closing_balance": _round_money(float(previous_closing)),
                        "next_opening_balance": _round_money(float(next_opening)),
                        "difference": difference,
                        "status": status,
                        "possible_missing_intermediate_statements": possible_missing_intermediate_statements,
                    }
                )

            account_summaries.append(
                {
                    "account_masked": account_masked,
                    "statements": statements,
                    "links": links,
                }
            )

        return {
            "accounts_scanned": len(statements_by_account),
            "statements_scanned": statements_scanned,
            "statements_derived": statements_derived,
            "excluded_statement_dates": excluded_statement_dates,
            "first_statement_zero_opening_passed": first_statement_zero_opening_passed,
            "first_statement_zero_opening_failed": first_statement_zero_opening_failed,
            "links_checked": links_checked,
            "links_matched": links_matched,
            "links_mismatched": links_mismatched,
            "links_with_missing_balances": links_with_missing_balances,
            "account_summaries": account_summaries,
        }
    finally:
        conn.close()


def _ensure_hsbc_seed_records(conn: sqlite3.Connection, account_masked: str | None, created_at: str) -> str:
    account_masked = account_masked or "186-770350-833"
    account_key = _normalize_statement_account_key(account_masked)
    account_id = f"acct_hsbc_{account_key}_hkd_savings"
    conn.execute(
        """
        INSERT OR IGNORE INTO institutions (
            institution_id, institution_code, institution_name, institution_type,
            country_or_region, default_base_currency, is_active, notes, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, NULL)
        """,
        (
            "hsbc_hk",
            "hsbc_hk",
            "HSBC Hong Kong",
            "bank",
            "Hong Kong",
            "HKD",
            "Seeded by email gateway HSBC transaction builder",
            created_at,
        ),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO accounts (
            account_id, institution_id, parent_account_id, account_masked, account_number_hash,
            account_name, account_type, product_type, asset_class, base_currency,
            owner_entity, is_active, opened_at, closed_at, notes, created_at, updated_at
        ) VALUES (?, ?, NULL, ?, NULL, ?, ?, ?, ?, ?, ?, 1, NULL, NULL, ?, ?, NULL)
        """,
        (
            account_id,
            "hsbc_hk",
            account_masked,
            "HSBC Hong Kong HKD Savings",
            "HKD Savings",
            "statement_savings",
            "cash",
            "HKD",
            "CHEN GENG",
            "Seeded by email gateway HSBC transaction builder",
            created_at,
        ),
    )
    return account_id


def _build_hsbc_dedupe_key(account_masked: str | None, txn_date: str, direction: str, amount: float, description_raw: str, reference_no: str | None) -> str:
    payload = "|".join(
        [
            account_masked or "",
            txn_date,
            direction,
            f"{amount:.2f}",
            description_raw,
            reference_no or "",
        ]
    )
    return sha256_bytes(payload.encode("utf-8"))


def extract_document_preview(path: Path, mime_type: str, max_chars: int = 280) -> tuple[int | None, str]:
    if is_probably_pdf(path, mime_type):
        try:
            doc = fitz.open(path)
        except (RuntimeError, ValueError, OSError):
            return None, ""
        try:
            pages = doc.page_count
            if doc.is_encrypted:
                return pages, ""
            text = ""
            if pages:
                text = doc.load_page(0).get_text("text") or ""
            compact = " ".join(text.split())
            return pages, compact[:max_chars]
        except (RuntimeError, ValueError):
            return doc.page_count if not doc.is_closed else None, ""
        finally:
            doc.close()

    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None, ""
    compact = " ".join(text.split())
    return None, compact[:max_chars]


def extract_page_texts(path: Path, mime_type: str) -> list[str]:
    if is_probably_pdf(path, mime_type):
        try:
            doc = fitz.open(path)
        except (RuntimeError, ValueError, OSError):
            return []
        try:
            if doc.is_encrypted:
                return []
            return [page.get_text("text") or "" for page in doc]
        except (RuntimeError, ValueError):
            return []
        finally:
            doc.close()
    try:
        return [path.read_text(encoding="utf-8", errors="ignore")]
    except OSError:
        return []


def stage_document(source_path: Path, staged_path: Path, link_mode: str) -> None:
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    if staged_path.exists() or staged_path.is_symlink():
        staged_path.unlink()
    if link_mode == "copy":
        shutil.copy2(source_path, staged_path)
    elif link_mode == "symlink":
        staged_path.symlink_to(source_path)
    else:
        raise ValueError(f"Unsupported link_mode: {link_mode}")


def write_manifest_csv(path: Path, rows: list[dict[str, object]]) -> None:
    if not rows:
        fieldnames = [
            "document_id",
            "email_id",
            "attachment_id",
            "file_name",
            "source_path",
            "stored_path",
            "mime_type",
            "file_size_bytes",
            "sha256",
            "page_count",
            "first_page_preview",
            "institution_code",
            "institution_name",
            "institution_type",
            "document_type",
            "classification_confidence",
            "recommended_for_profile_stub",
            "source_type",
            "staged_relative_path",
        ]
    else:
        fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def extract_body_text(message: EmailMessage) -> str | None:
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_disposition() == "attachment":
                continue
            if part.get_content_type() == "text/plain":
                return part.get_content()
        return None
    if message.get_content_type() == "text/plain":
        return message.get_content()
    return None


def extract_body_html(message: EmailMessage) -> str | None:
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_disposition() == "attachment":
                continue
            if part.get_content_type() == "text/html":
                return part.get_content()
        return None
    if message.get_content_type() == "text/html":
        return message.get_content()
    return None


def attachment_parts(message: EmailMessage) -> list[tuple[int, object]]:
    parts = []
    attachment_index = 0
    for part in message.iter_attachments():
        filename = part.get_filename()
        disposition = part.get_content_disposition()
        if disposition == "attachment" or filename:
            attachment_index += 1
            parts.append((attachment_index, part))
    return parts


def register_document_for_attachment(
    conn: sqlite3.Connection,
    *,
    attachment_id: str,
    email_id: str,
    filename: str,
    mime_type: str,
    file_size: int,
    sha256: str,
    created_at: str,
) -> None:
    document_id = f"doc_{attachment_id}"
    conn.execute(
        """
        INSERT INTO documents (
            document_id, attachment_id, source_type, source_ref, document_hash,
            filename, file_ext, mime_type, page_count, document_class, document_subclass,
            institution_id, account_id, statement_date, issue_date, period_start,
            period_end, currency_hint, language_hint, extraction_method,
            classification_confidence, processing_status, registered_at, notes
        ) VALUES (?, ?, 'email_attachment', ?, ?, ?, ?, ?, NULL, 'unclassified_document', NULL,
                  NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, 'email_gateway_raw_ingest',
                  0.0, 'registered', ?, ?)
        """,
        (
            document_id,
            attachment_id,
            f"{email_id}:{attachment_id.rsplit('_', 1)[-1]}",
            sha256,
            filename,
            Path(filename).suffix.lower(),
            mime_type,
            created_at,
            f"Auto-registered from inbound email attachment ({file_size} bytes).",
        ),
    )


def ingest_email_message(
    *,
    message: EmailMessage,
    db_path: str | Path,
    schema_path: str | Path,
    attachments_dir: str | Path,
    raw_email_dir: str | Path,
    source_channel: str = "email_gateway",
    overwrite: bool = False,
) -> dict[str, int]:
    db_file = Path(db_path)
    schema_file = Path(schema_path)
    attachments_root = Path(attachments_dir)
    raw_root = Path(raw_email_dir)
    created_at = now_iso()

    email_key = sanitize_message_id(message.get("Message-ID"))
    email_id = f"email_{email_key}"

    attachments_root.mkdir(parents=True, exist_ok=True)
    raw_root.mkdir(parents=True, exist_ok=True)
    raw_email_path = raw_root / f"{email_key}.eml"

    conn = ensure_database(db_file, schema_file)
    try:
        existing = conn.execute("SELECT email_id FROM inbound_emails WHERE email_id = ?", (email_id,)).fetchone()
        if existing is not None:
            if not overwrite:
                raise FileExistsError(f"email already registered: {email_id}")
            attachment_rows = conn.execute(
                "SELECT stored_path FROM email_attachments WHERE email_id = ?", (email_id,)
            ).fetchall()
            for row in attachment_rows:
                stored = resolve_stored_path(row["stored_path"])
                if stored and stored.exists():
                    stored.unlink()
            conn.execute("DELETE FROM documents WHERE attachment_id IN (SELECT attachment_id FROM email_attachments WHERE email_id = ?)", (email_id,))
            conn.execute("DELETE FROM email_attachments WHERE email_id = ?", (email_id,))
            conn.execute("DELETE FROM inbound_emails WHERE email_id = ?", (email_id,))
            if raw_email_path.exists():
                raw_email_path.unlink()

        raw_bytes = message.as_bytes(policy=policy.default)
        raw_email_path.write_bytes(raw_bytes)

        body_html = extract_body_html(message)
        body_html_path = None
        if body_html:
            body_html_path = str(raw_root / f"{email_key}.html")
            Path(body_html_path).write_text(body_html, encoding="utf-8")

        parts = attachment_parts(message)
        conn.execute(
            """
            INSERT INTO inbound_emails (
                email_id, gateway_received_at, message_id_header, thread_id_header,
                from_address, to_address, subject, sent_at, raw_email_path,
                body_text, body_html_path, attachment_count, source_channel,
                processing_status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                email_id,
                created_at,
                message.get("Message-ID"),
                message.get("Thread-Index"),
                message.get("From"),
                message.get("To"),
                message.get("Subject"),
                message.get("Date"),
                str(raw_email_path),
                extract_body_text(message),
                body_html_path,
                len(parts),
                source_channel,
                "pending_document_classification",
                created_at,
            ),
        )

        for attachment_index, part in parts:
            filename = part.get_filename() or f"attachment_{attachment_index}"
            payload = part.get_payload(decode=True) or b""
            attachment_id = f"att_{email_id}_{attachment_index}"
            stored_path = attachments_root / email_key / f"{attachment_index:02d}_{filename}"
            stored_path.parent.mkdir(parents=True, exist_ok=True)
            stored_path.write_bytes(payload)
            sha256 = sha256_bytes(payload)
            mime_type = part.get_content_type()
            conn.execute(
                """
                INSERT INTO email_attachments (
                    attachment_id, email_id, filename_original, mime_type, file_size,
                    sha256, stored_path, attachment_index, is_inline, processing_status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'saved', ?)
                """,
                (
                    attachment_id,
                    email_id,
                    filename,
                    mime_type,
                    len(payload),
                    sha256,
                    str(stored_path),
                    attachment_index,
                    1 if part.get_content_disposition() == "inline" else 0,
                    created_at,
                ),
            )
            register_document_for_attachment(
                conn,
                attachment_id=attachment_id,
                email_id=email_id,
                filename=filename,
                mime_type=mime_type,
                file_size=len(payload),
                sha256=sha256,
                created_at=created_at,
            )

        conn.commit()
        return {
            "inbound_emails": conn.execute("SELECT COUNT(*) FROM inbound_emails").fetchone()[0],
            "email_attachments": conn.execute("SELECT COUNT(*) FROM email_attachments").fetchone()[0],
            "documents": conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
        }
    finally:
        conn.close()


def classify_ingested_email_documents(
    *,
    db_path: str | Path,
    output_dir: str | Path,
    link_mode: str = "copy",
) -> dict[str, object]:
    intake_module = load_intake_module()
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT d.document_id, d.attachment_id, d.source_type, d.filename, d.mime_type,
                   a.email_id, a.stored_path, a.file_size, a.sha256
            FROM documents d
            JOIN email_attachments a ON a.attachment_id = d.attachment_id
            ORDER BY a.attachment_index
            """
        ).fetchall()

        manifest_rows: list[dict[str, object]] = []
        grouped: dict[str, list[dict[str, object]]] = {}
        missing_source_documents = 0

        for row in rows:
            source_path = resolve_stored_path(row["stored_path"])
            if source_path is None or not source_path.exists():
                missing_source_documents += 1
                continue
            page_count, first_page_preview = extract_document_preview(source_path, row["mime_type"] or "")
            email_row = conn.execute(
                "SELECT subject FROM inbound_emails WHERE email_id = ?",
                (row["email_id"],),
            ).fetchone()
            classification = intake_module.classify_document(
                row["filename"],
                first_page_preview,
                subject=(email_row["subject"] if email_row else None),
            )
            staged_relative_path = (
                Path("by_institution")
                / str(classification["institution_code"])
                / str(classification["document_type"])
                / str(row["filename"])
            )
            stage_document(source_path, output_path / staged_relative_path, link_mode=link_mode)

            manifest_row = {
                "document_id": row["document_id"],
                "email_id": row["email_id"],
                "attachment_id": row["attachment_id"],
                "file_name": row["filename"],
                "source_path": str(source_path),
                "stored_path": str(source_path),
                "mime_type": row["mime_type"],
                "file_size_bytes": row["file_size"],
                "sha256": row["sha256"] or sha256_file(source_path),
                "page_count": page_count,
                "first_page_preview": first_page_preview,
                "source_type": row["source_type"],
                **classification,
                "staged_relative_path": staged_relative_path.as_posix(),
            }
            manifest_rows.append(manifest_row)

            confidence_numeric = 1.0 if classification["classification_confidence"] == "high" else 0.5 if classification["classification_confidence"] == "medium" else 0.0
            if classification["institution_code"] == "unknown":
                processing_status = "unclassified"
            elif classification["institution_code"] == "ignored":
                processing_status = "ignored"
            else:
                processing_status = "classified"
            conn.execute(
                """
                UPDATE documents
                SET document_class = ?,
                    institution_id = ?,
                    extraction_method = 'email_gateway_intake_manifest',
                    classification_confidence = ?,
                    processing_status = ?
                WHERE document_id = ?
                """,
                (
                    classification["document_type"],
                    classification["institution_code"] if classification["institution_code"] not in {"unknown", "ignored"} else None,
                    confidence_numeric,
                    processing_status,
                    row["document_id"],
                ),
            )

            if classification["recommended_for_profile_stub"]:
                grouped.setdefault(str(classification["institution_code"]), []).append(manifest_row)

        profile_stub_dir = output_path / "profile_stubs"
        profile_stub_dir.mkdir(parents=True, exist_ok=True)
        written_stubs: list[str] = []
        for institution_code, grouped_rows in grouped.items():
            stub_path = profile_stub_dir / f"{institution_code}.profile.stub.json"
            stub_path.write_text(
                json.dumps(intake_module.build_profile_stub(institution_code, grouped_rows), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            written_stubs.append(str(stub_path))

        conn.execute(
            "UPDATE inbound_emails SET processing_status = 'documents_classified' WHERE email_id IN (SELECT DISTINCT email_id FROM email_attachments)"
        )
        conn.commit()

        summary = {
            "generated_at": now_iso(),
            "documents_scanned": len(manifest_rows),
            "classified_documents": sum(1 for row in manifest_rows if row["institution_code"] not in {"unknown", "ignored"}),
            "unclassified_documents": sum(1 for row in manifest_rows if row["institution_code"] == "unknown"),
            "ignored_documents": sum(1 for row in manifest_rows if row["institution_code"] == "ignored"),
            "missing_source_documents": missing_source_documents,
            "profile_stubs_written": len(written_stubs),
            "recommended_institutions": list(grouped.keys()),
            "manifest_json": str(output_path / "inbox_manifest.json"),
            "manifest_csv": str(output_path / "inbox_manifest.csv"),
            "profile_stub_dir": str(profile_stub_dir),
            "link_mode": link_mode,
        }
        manifest_json = {
            "summary": summary,
            "documents": manifest_rows,
            "profile_stubs": written_stubs,
        }
        (output_path / "inbox_manifest.json").write_text(
            json.dumps(manifest_json, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        write_manifest_csv(output_path / "inbox_manifest.csv", manifest_rows)
        return summary
    finally:
        conn.close()


def extract_raw_text_for_ingested_email_documents(
    *,
    db_path: str | Path,
    only_classified: bool = True,
) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        query = (
            """
            SELECT d.document_id, d.filename, d.mime_type, d.processing_status, a.email_id, a.stored_path
            FROM documents d
            JOIN email_attachments a ON a.attachment_id = d.attachment_id
            WHERE d.source_type = 'email_attachment'
            """
        )
        params: tuple[object, ...] = ()
        if only_classified:
            query += " AND d.processing_status = 'classified'"
        rows = conn.execute(query + " ORDER BY d.document_id", params).fetchall()

        pages_written = 0
        lines_written = 0
        documents_updated = 0
        touched_email_ids: set[str] = set()
        created_at = now_iso()

        for row in rows:
            source_path = resolve_stored_path(row["stored_path"])
            if source_path is None:
                continue
            page_texts = extract_page_texts(source_path, row["mime_type"] or "")
            if not page_texts:
                continue

            conn.execute("DELETE FROM raw_document_lines WHERE document_id = ?", (row["document_id"],))
            conn.execute("DELETE FROM raw_document_pages WHERE document_id = ?", (row["document_id"],))

            document_line_count = 0
            for page_index, page_text in enumerate(page_texts, start=1):
                page_hash = sha256_bytes(page_text.encode("utf-8"))
                conn.execute(
                    """
                    INSERT INTO raw_document_pages (
                        page_id, document_id, page_no, text_layer_text, ocr_text,
                        rendered_image_path, page_hash, extraction_confidence, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"page_{row['document_id']}_{page_index}",
                        row["document_id"],
                        page_index,
                        page_text,
                        None,
                        None,
                        page_hash,
                        1.0,
                        created_at,
                    ),
                )
                pages_written += 1

                line_no = 0
                for raw_line in page_text.splitlines():
                    stripped = raw_line.strip()
                    if not stripped:
                        continue
                    line_no += 1
                    document_line_count += 1
                    conn.execute(
                        """
                        INSERT INTO raw_document_lines (
                            line_id, document_id, section_id, page_no, line_no, raw_text,
                            bbox_or_position, parser_tag, candidate_group_id, extraction_source, created_at
                        ) VALUES (?, ?, NULL, ?, ?, ?, NULL, NULL, NULL, 'pymupdf_text', ?)
                        """,
                        (
                            f"line_{row['document_id']}_{page_index}_{line_no}",
                            row["document_id"],
                            page_index,
                            line_no,
                            stripped,
                            created_at,
                        ),
                    )
                    lines_written += 1

            if document_line_count > 0:
                conn.execute(
                    "UPDATE documents SET processing_status = 'raw_extracted' WHERE document_id = ?",
                    (row["document_id"],),
                )
                touched_email_ids.add(str(row["email_id"]))
                documents_updated += 1

        for email_id in touched_email_ids:
            conn.execute(
                "UPDATE inbound_emails SET processing_status = 'raw_text_extracted' WHERE email_id = ?",
                (email_id,),
            )

        conn.commit()
        return {
            "raw_document_pages": pages_written,
            "raw_document_lines": lines_written,
            "documents_updated": documents_updated,
        }
    finally:
        conn.close()


def build_document_facts_from_ingested_email_documents(
    *,
    db_path: str | Path,
    only_raw_extracted: bool = True,
) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        query = (
            """
            SELECT d.document_id, d.filename, d.document_class, d.institution_id, d.processing_status,
                   a.email_id,
                   GROUP_CONCAT(rdl.raw_text, char(10)) AS raw_text_blob
            FROM documents d
            JOIN email_attachments a ON a.attachment_id = d.attachment_id
            LEFT JOIN raw_document_lines rdl ON rdl.document_id = d.document_id
            WHERE d.source_type = 'email_attachment'
            """
        )
        if only_raw_extracted:
            query += " AND d.processing_status = 'raw_extracted'"
        query += " GROUP BY d.document_id, d.filename, d.document_class, d.institution_id, d.processing_status, a.email_id ORDER BY d.document_id"
        rows = conn.execute(query).fetchall()

        created = 0
        updated = 0
        touched_email_ids: set[str] = set()
        created_at = now_iso()

        for row in rows:
            raw_text_blob = row["raw_text_blob"] or ""
            if not raw_text_blob.strip():
                continue

            conn.execute("DELETE FROM document_facts WHERE document_id = ?", (row["document_id"],))

            statement_date = parse_statement_date_from_text(raw_text_blob)
            account_masked = parse_account_masked_from_text(raw_text_blob)
            period_start = None
            period_end = None
            document_currency = None
            opening_balance = None
            closing_balance = None
            if row["institution_id"] == "ant_bank" and not account_masked:
                account_masked = parse_ant_account_bundle_from_text(raw_text_blob)
            if row["institution_id"] == "huatai_hk":
                period_start, period_end = _parse_huatai_period(statement_date, str(row["filename"]))
                document_currency = _parse_huatai_document_currency(raw_text_blob)
                opening_balance, closing_balance = _parse_huatai_net_assets_summary(raw_text_blob)

            if row["institution_id"] == "hsbc_hk" and (not statement_date or not account_masked):
                raw_line_items = [
                    {"text": _normalize_ocr_line_text(line)}
                    for line in raw_text_blob.splitlines()
                    if line.strip()
                ]
                attachment_row = conn.execute(
                    "SELECT stored_path FROM email_attachments WHERE attachment_id = (SELECT attachment_id FROM documents WHERE document_id = ?)",
                    (row["document_id"],),
                ).fetchone()
                if attachment_row is not None:
                    resolved_attachment_path = resolve_stored_path(attachment_row["stored_path"])
                    ocr_text = extract_visual_ocr_header_text(resolved_attachment_path) or ""
                    if ocr_text.strip():
                        statement_date = statement_date or parse_statement_date_from_text(ocr_text)
                        account_masked = account_masked or parse_account_masked_from_text(ocr_text)
                    if (not statement_date or not account_masked) and _should_try_hsbc_transaction_page_ocr(raw_line_items):
                        page_text = "\n".join(extract_visual_ocr_page_lines(resolved_attachment_path, page_no=0))
                        if page_text.strip():
                            statement_date = statement_date or parse_statement_date_from_text(page_text)
                            account_masked = account_masked or parse_account_masked_from_text(page_text)
            institution_name = None
            if row["institution_id"] == "hsbc_hk":
                institution_name = "HSBC Hong Kong"
            elif row["institution_id"] == "scb_hk":
                institution_name = "Standard Chartered Hong Kong"
                document_currency = document_currency or ("HKD" if "HKD" in raw_text_blob.upper() else None)
            elif row["institution_id"] == "huatai_hk":
                institution_name = "Huatai Financial Holdings (Hong Kong)"
            elif row["institution_id"] == "hang_seng":
                institution_name = "Hang Seng Bank"
            elif row["institution_id"]:
                institution_name = str(row["institution_id"])

            conn.execute(
                """
                INSERT INTO document_facts (
                    document_fact_id, document_id, fact_type, institution_name_raw,
                    account_masked_raw, account_type_raw, statement_date, issue_date,
                    period_start, period_end, document_currency, opening_balance,
                    closing_balance, header_payload_json, confidence, created_at
                ) VALUES (?, ?, 'statement_header', ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"fact_{row['document_id']}",
                    row["document_id"],
                    institution_name,
                    account_masked,
                    row["document_class"],
                    statement_date,
                    period_start,
                    period_end,
                    document_currency,
                    opening_balance,
                    closing_balance,
                    json.dumps(
                        {
                            "document_id": row["document_id"],
                            "filename": row["filename"],
                            "raw_text_excerpt": raw_text_blob[:280],
                        },
                        ensure_ascii=False,
                    ),
                    1.0,
                    created_at,
                ),
            )
            conn.execute(
                """
                UPDATE documents
                SET statement_date = ?,
                    extraction_method = 'email_gateway_document_fact_builder',
                    processing_status = 'document_facts_built'
                WHERE document_id = ?
                """,
                (
                    statement_date,
                    row["document_id"],
                ),
            )
            touched_email_ids.add(str(row["email_id"]))
            created += 1
            updated += 1

        for email_id in touched_email_ids:
            conn.execute(
                "UPDATE inbound_emails SET processing_status = 'document_facts_built' WHERE email_id = ?",
                (email_id,),
            )

        conn.commit()
        return {
            "document_facts": created,
            "documents_updated": updated,
        }
    finally:
        conn.close()


def build_document_facts_from_ingested_email_documents(
    *,
    db_path: str | Path,
    only_raw_extracted: bool = True,
) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        query = (
            """
            SELECT d.document_id, d.filename, d.document_class, d.institution_id, d.processing_status,
                   a.email_id,
                   GROUP_CONCAT(rdl.raw_text, char(10)) AS raw_text_blob
            FROM documents d
            JOIN email_attachments a ON a.attachment_id = d.attachment_id
            LEFT JOIN raw_document_lines rdl ON rdl.document_id = d.document_id
            WHERE d.source_type = 'email_attachment'
            """
        )
        if only_raw_extracted:
            query += " AND d.processing_status = 'raw_extracted'"
        query += " GROUP BY d.document_id, d.filename, d.document_class, d.institution_id, d.processing_status, a.email_id ORDER BY d.document_id"
        rows = conn.execute(query).fetchall()

        created = 0
        updated = 0
        touched_email_ids: set[str] = set()
        created_at = now_iso()

        for row in rows:
            raw_text_blob = row["raw_text_blob"] or ""
            if not raw_text_blob.strip():
                continue

            conn.execute("DELETE FROM document_facts WHERE document_id = ?", (row["document_id"],))

            statement_date = parse_statement_date_from_text(raw_text_blob)
            account_masked = parse_account_masked_from_text(raw_text_blob)
            period_start = None
            period_end = None
            document_currency = None
            opening_balance = None
            closing_balance = None
            if row["institution_id"] == "ant_bank" and not account_masked:
                account_masked = parse_ant_account_bundle_from_text(raw_text_blob)
            if row["institution_id"] == "huatai_hk":
                period_start, period_end = _parse_huatai_period(statement_date, str(row["filename"]))
                document_currency = _parse_huatai_document_currency(raw_text_blob)
                opening_balance, closing_balance = _parse_huatai_net_assets_summary(raw_text_blob)

            if row["institution_id"] == "hsbc_hk" and (not statement_date or not account_masked):
                raw_line_items = [
                    {"text": _normalize_ocr_line_text(line)}
                    for line in raw_text_blob.splitlines()
                    if line.strip()
                ]
                attachment_row = conn.execute(
                    "SELECT stored_path FROM email_attachments WHERE attachment_id = (SELECT attachment_id FROM documents WHERE document_id = ?)",
                    (row["document_id"],),
                ).fetchone()
                if attachment_row is not None:
                    resolved_attachment_path = resolve_stored_path(attachment_row["stored_path"])
                    ocr_text = extract_visual_ocr_header_text(resolved_attachment_path) or ""
                    if ocr_text.strip():
                        statement_date = statement_date or parse_statement_date_from_text(ocr_text)
                        account_masked = account_masked or parse_account_masked_from_text(ocr_text)
                    if (not statement_date or not account_masked) and _should_try_hsbc_transaction_page_ocr(raw_line_items):
                        page_text = "\n".join(extract_visual_ocr_page_lines(resolved_attachment_path, page_no=0))
                        if page_text.strip():
                            statement_date = statement_date or parse_statement_date_from_text(page_text)
                            account_masked = account_masked or parse_account_masked_from_text(page_text)
            institution_name = None
            if row["institution_id"] == "hsbc_hk":
                institution_name = "HSBC Hong Kong"
            elif row["institution_id"] == "scb_hk":
                institution_name = "Standard Chartered Hong Kong"
                document_currency = document_currency or ("HKD" if "HKD" in raw_text_blob.upper() else None)
            elif row["institution_id"] == "huatai_hk":
                institution_name = "Huatai Financial Holdings (Hong Kong)"
            elif row["institution_id"] == "hang_seng":
                institution_name = "Hang Seng Bank"
            elif row["institution_id"]:
                institution_name = str(row["institution_id"])

            conn.execute(
                """
                INSERT INTO document_facts (
                    document_fact_id, document_id, fact_type, institution_name_raw,
                    account_masked_raw, account_type_raw, statement_date, issue_date,
                    period_start, period_end, document_currency, opening_balance,
                    closing_balance, header_payload_json, confidence, created_at
                ) VALUES (?, ?, 'statement_header', ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"fact_{row['document_id']}",
                    row["document_id"],
                    institution_name,
                    account_masked,
                    row["document_class"],
                    statement_date,
                    period_start,
                    period_end,
                    document_currency,
                    opening_balance,
                    closing_balance,
                    json.dumps(
                        {
                            "document_id": row["document_id"],
                            "filename": row["filename"],
                            "raw_text_excerpt": raw_text_blob[:280],
                        },
                        ensure_ascii=False,
                    ),
                    1.0,
                    created_at,
                ),
            )
            conn.execute(
                """
                UPDATE documents
                SET statement_date = ?,
                    extraction_method = 'email_gateway_document_fact_builder',
                    processing_status = 'document_facts_built'
                WHERE document_id = ?
                """,
                (
                    statement_date,
                    row["document_id"],
                ),
            )
            touched_email_ids.add(str(row["email_id"]))
            created += 1
            updated += 1

        for email_id in touched_email_ids:
            conn.execute(
                "UPDATE inbound_emails SET processing_status = 'document_facts_built' WHERE email_id = ?",
                (email_id,),
            )

        conn.commit()
        return {
            "document_facts": created,
            "documents_updated": updated,
        }
    finally:
        conn.close()


def _is_za_transaction_history_header(text: str) -> bool:
    upper = text.upper()
    return "TRANSACTION HISTORY" in upper


def _is_za_stop_line(text: str) -> bool:
    upper = text.upper()
    return upper.startswith(("IMPORTANT NOTICE", "STATEMENT DATE"))


def _is_za_table_header_line(text: str) -> bool:
    normalized = text.upper().replace(" ", "")
    return normalized in {
        "DATE",
        "日期",
        "DESCRIPTION",
        "TRANSACTIONDETAILS",
        "交易詳情",
        "DEPOSIT",
        "存入",
        "WITHDRAWAL",
        "支出",
        "BALANCE",
        "結餘",
        "PERIOD01APR2026TO30APR2026",
        "HKDSAVINGS港元活期儲蓄",
        "HKDSAVINGS",
    }


def _parse_za_full_date(text: str) -> str | None:
    match = re.fullmatch(r"(\d{1,2})\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{4})", text.strip(), re.IGNORECASE)
    if not match:
        return None
    day = int(match.group(1))
    month = _MONTH_NAME_TO_NUMBER[match.group(2).upper()]
    year = int(match.group(3))
    return f"{year:04d}-{month:02d}-{day:02d}"


def _split_za_date_prefix(text: str) -> tuple[str, str] | None:
    match = re.match(r"^(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4})(?:\s+(.*))?$", text.strip(), re.IGNORECASE)
    if not match:
        return None
    txn_date = _parse_za_full_date(match.group(1))
    if txn_date is None:
        return None
    return txn_date, (match.group(2) or "").strip()


def _extract_za_transaction_section_lines(line_items: list[dict[str, object]]) -> list[dict[str, object]]:
    inside = False
    collected: list[dict[str, object]] = []
    for item in line_items:
        text = str(item["text"]).strip()
        if not text:
            continue
        if not inside:
            if _is_za_transaction_history_header(text):
                inside = True
            continue
        if _is_za_stop_line(text):
            if collected:
                inside = False
            continue
        if _is_za_transaction_history_header(text):
            inside = True
            continue
        if _is_za_table_header_line(text):
            continue
        if text.upper().startswith("ACCOUNT NUMBER") or text.upper().startswith("DEPOSIT SUMMARY"):
            continue
        if text.upper().startswith(("CONSOLIDATED MONTHLY STATEMENT", "ZA BANK LIMITED", "BANK.ZA.GROUP", "P.")):
            continue
        if text.upper().startswith(("FOREIGN-CURRENCY SAVINGS", "CNY ", "USD ")):
            continue
        if any(marker in text.upper() for marker in ("UNIT 1301", "CYBERPORT", "HONG KONG ISLAND")):
            continue
        if re.fullmatch(r"\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}\s*-\s*\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}", text, re.IGNORECASE):
            continue
        collected.append(item)
    return collected


def _parse_za_amounts_from_tail(text: str) -> tuple[str, str | None, float, float | None] | None:
    match = re.match(r"^(?P<body>.+?)\s+(?P<amount>\d{1,3}(?:,\d{3})*\.\d{2})(?:\s+(?P<balance>\d{1,3}(?:,\d{3})*\.\d{2}))?$", text.strip())
    if not match:
        return None
    body = match.group("body").strip()
    amount = _parse_amount_value(match.group("amount"))
    balance = _parse_amount_value(match.group("balance")) if match.group("balance") else None
    reference_no = None
    body_match = re.match(r"^(?P<desc>.+?)\s+(?P<ref>ZA-[A-Z0-9-]+)$", body, re.IGNORECASE)
    if body_match:
        body = body_match.group("desc").strip()
        reference_no = body_match.group("ref").strip()
    return body, reference_no, amount, balance


def _split_za_transaction_payload(payload_lines: list[str]) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    current_lines: list[str] = []
    numeric_lines: list[str] = []

    def _is_za_ignorable_detail_line(text: str) -> bool:
        normalized = text.strip()
        if not normalized:
            return True
        if normalized in {"存入", "支出", "本地轉賬", "轉數快轉賬", "ZA Coin 兌換現金"}:
            return True
        if not re.search(r"[A-Za-z0-9]", normalized):
            return True
        return False

    def _normalize_za_description_parts(parts: list[str]) -> str | None:
        normalized_parts: list[str] = []
        for part in parts:
            compact = re.sub(r"\s+", " ", part or "").strip()
            if not compact:
                continue
            compact = re.sub(r"\s*[·•]{2,}\s*", " | ", compact)
            normalized_parts.extend(piece.strip() for piece in compact.split("|") if piece.strip())
        kept = [part for part in normalized_parts if not _is_za_ignorable_detail_line(part)]
        if not kept:
            return None
        if len(kept) == 1 and kept[0].upper().startswith("INTEREST"):
            return "Interest"
        return " | ".join(kept)

    def flush_current() -> None:
        nonlocal current_lines, numeric_lines
        if not current_lines and not numeric_lines:
            return
        entry_lines = [line for line in current_lines if line]
        reference_no = None
        if entry_lines and re.fullmatch(r"ZA-[A-Z0-9-]+", entry_lines[-1], re.IGNORECASE):
            reference_no = entry_lines.pop()
        description_raw = _normalize_za_description_parts(entry_lines)
        amount = _parse_amount_value(numeric_lines[0]) if numeric_lines else None
        running_balance = _parse_amount_value(numeric_lines[1]) if len(numeric_lines) > 1 else None
        entries.append(
            {
                "description_raw": description_raw,
                "reference_no": reference_no,
                "amount": amount,
                "running_balance": running_balance,
            }
        )
        current_lines = []
        numeric_lines = []

    for value in payload_lines:
        value = value.strip()
        if not value:
            continue
        if _is_amount_line(value):
            numeric_lines.append(value)
            if len(numeric_lines) >= 2:
                flush_current()
            continue
        inline_amounts = _parse_za_amounts_from_tail(value)
        if inline_amounts is not None:
            body, reference_no, amount, balance = inline_amounts
            if numeric_lines:
                flush_current()
            current_lines.append(body)
            if reference_no:
                current_lines.append(reference_no)
            numeric_lines.append(f"{amount:.2f}")
            if balance is not None:
                numeric_lines.append(f"{balance:.2f}")
            flush_current()
            continue
        if numeric_lines:
            flush_current()
        current_lines.append(value)

    flush_current()
    return [entry for entry in entries if entry.get("description_raw") and entry.get("amount") is not None]


def _parse_za_opening_balance(payload_lines: list[str]) -> float | None:
    if not payload_lines:
        return None
    first = payload_lines[0].strip()
    if not first.upper().startswith("OPENING BALANCE"):
        return None
    inline_match = re.match(r"^Opening balance.*?(\d{1,3}(?:,\d{3})*\.\d{2})$", first, re.IGNORECASE)
    if inline_match:
        return _parse_amount_value(inline_match.group(1))
    for value in payload_lines[1:]:
        if _is_amount_line(value):
            return _parse_amount_value(value)
    return None


def _infer_za_direction(description_raw: str, amount: float, running_balance: float | None, previous_balance: float | None) -> str | None:
    if running_balance is not None and previous_balance is not None:
        delta = round(running_balance - previous_balance, 2)
        if abs(abs(delta) - amount) <= 0.01:
            return "credit" if delta >= 0 else "debit"
    upper = description_raw.upper()
    credit_markers = (
        "INTEREST",
        "SALARY",
        "DEPOSIT",
        "CREDIT",
        "TRANSFER IN",
        "INWARD FUND TRANSFER",
        "ZA COIN",
        "CASH REBATE",
    )
    debit_markers = ("LOCAL TRANSFER", "WITHDRAWAL", "FPS OUT", "PAYMENT", "FEE", "CHARGE", "DEBIT")
    if any(marker in upper for marker in credit_markers):
        return "credit"
    if any(marker in upper for marker in debit_markers):
        return "debit"
    return None


def _infer_za_channel(description_raw: str) -> str | None:
    upper = description_raw.upper()
    if "INTEREST" in upper:
        return "INTEREST"
    if "TRANSFER" in upper:
        return "TRANSFER"
    return None


def _parse_za_counterparty(description_raw: str) -> tuple[str, str | None, str | None, str | None, str | None]:
    if " | " not in description_raw:
        return description_raw, None, None, None, None
    head, tail = description_raw.split(" | ", 1)
    tail = tail.strip()
    if not tail:
        return description_raw, None, None, None, None
    phone_match = re.search(r"(?:\+\d{1,4}[-\s]?)?\d{7,}$", tail)
    counterparty_phone_raw = phone_match.group(0) if phone_match else None
    account_match = None if phone_match else re.search(r"(\d[\d*]{3,})$", tail)
    counterparty_account_masked = account_match.group(1) if account_match else None
    counterparty_name_raw = tail
    if phone_match:
        counterparty_name_raw = tail[: phone_match.start()].strip()
    elif counterparty_account_masked:
        counterparty_name_raw = tail[: tail.rfind(counterparty_account_masked)].strip()
    counterparty_name_raw = counterparty_name_raw.strip(" ,|-") or None
    return head.strip(), tail, counterparty_name_raw, counterparty_account_masked, counterparty_phone_raw


def _build_za_dedupe_key(
    account_masked: str | None,
    txn_date: str,
    direction: str,
    amount: float,
    description_raw: str,
    reference_no: str | None,
    running_balance: float | None,
) -> str:
    payload = "|".join(
        [
            account_masked or "",
            txn_date,
            direction,
            f"{amount:.2f}",
            description_raw,
            reference_no or "",
            "" if running_balance is None else f"{running_balance:.2f}",
        ]
    )
    return sha256_bytes(payload.encode("utf-8"))


def _parse_ant_primary_account(account_bundle: str | None) -> str | None:
    if not account_bundle:
        return None
    return str(account_bundle).split("|", 1)[0].strip() or None


def _parse_ant_full_date(text: str) -> str | None:
    normalized = text.strip().upper().replace("-SEPT-", "-SEP-")
    try:
        return datetime.strptime(normalized, "%d-%b-%Y").date().isoformat()
    except ValueError:
        return None


def _split_ant_date_prefix(text: str) -> tuple[str, str] | None:
    match = re.match(r"^(\d{1,2}-[A-Z]{3,4}-\d{4})(?:\s+(.*))?$", text.strip(), re.IGNORECASE)
    if not match:
        return None
    txn_date = _parse_ant_full_date(match.group(1).upper())
    if txn_date is None:
        return None
    return txn_date, (match.group(2) or "").strip()


def _trim_ant_payload_suffix(text: str) -> str:
    compact = re.sub(r"\s+", " ", text or "").strip()
    compact = re.sub(r"\s+--(?:\s+.*)?$", "", compact)
    return compact.strip()


def _parse_ant_amount_tail(text: str) -> tuple[str, float] | None:
    match = re.match(r"^(?P<body>.+?)\s+(?P<amount>-?\d{1,3}(?:,\d{3})*\.\d{2})$", _trim_ant_payload_suffix(text))
    if not match:
        return None
    return match.group("body").strip(), _parse_amount_value(match.group("amount"))


def _parse_ant_transaction_payload(payload: str) -> tuple[str, float, float] | None:
    match = re.match(
        r"^(?P<desc>.+?)\s+(?P<amount>-?\d{1,3}(?:,\d{3})*\.\d{2})\s+(?P<balance>\d{1,3}(?:,\d{3})*\.\d{2})$",
        _trim_ant_payload_suffix(payload),
    )
    if not match:
        return None
    return (
        match.group("desc").strip(),
        _parse_amount_value(match.group("amount")),
        _parse_amount_value(match.group("balance")),
    )


def _group_ant_statement_lines(line_items: list[dict[str, object]]) -> list[dict[str, object]]:
    groups: list[dict[str, object]] = []
    current: dict[str, object] | None = None

    def flush_current() -> None:
        nonlocal current
        if current is None:
            return
        payload_parts = [str(part).strip() for part in current.get("payload_parts", []) if str(part).strip()]
        current["payload"] = " ".join(payload_parts).strip()
        groups.append(current)
        current = None

    for item in line_items:
        text = str(item["text"]).strip()
        if not text:
            continue
        split = _split_ant_date_prefix(text)
        if split is not None:
            flush_current()
            txn_date, payload = split
            current = {
                "txn_date": txn_date,
                "payload_parts": [payload] if payload else [],
                "items": [item],
            }
            continue
        if current is not None:
            current.setdefault("payload_parts", []).append(text)
            current.setdefault("items", []).append(item)
            continue
        groups.append({"txn_date": None, "payload": text, "items": [item]})

    flush_current()
    return groups


def _compact_ant_text(text: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "", (text or "").upper())


def _extract_ant_document_closing_balance(line_items: list[dict[str, object]]) -> tuple[float, int | None, str | None] | None:
    amount_only_pattern = re.compile(r"^(?:HKD\s+)?(-?\d{1,3}(?:,\d{3})*\.\d{2})$", re.IGNORECASE)
    amount_tail_pattern = re.compile(r"(?:HKD\s+)?(-?\d{1,3}(?:,\d{3})*\.\d{2})$", re.IGNORECASE)

    for index, item in enumerate(line_items):
        text = str(item.get("text") or "").strip()
        if "TOTAL OUTSTANDING AMOUNT" not in text.upper():
            continue

        inline_amount_match = amount_tail_pattern.search(text)
        if inline_amount_match is not None:
            return (
                _parse_amount_value(inline_amount_match.group(1)),
                int(item.get("page_no")) if item.get("page_no") is not None else None,
                f"p{item.get('page_no')}_l{item.get('line_no')}" if item.get("page_no") is not None and item.get("line_no") is not None else None,
            )

        page_no = item.get("page_no")
        fallback: tuple[float, int | None, str | None] | None = None
        for candidate in line_items[index + 1 :]:
            candidate_text = str(candidate.get("text") or "").strip()
            if not candidate_text:
                continue
            if page_no is not None and candidate.get("page_no") != page_no:
                break
            if _split_ant_date_prefix(candidate_text) is not None:
                break

            amount_only_match = amount_only_pattern.match(candidate_text)
            if amount_only_match is not None:
                return (
                    _parse_amount_value(amount_only_match.group(1)),
                    int(candidate.get("page_no")) if candidate.get("page_no") is not None else None,
                    f"p{candidate.get('page_no')}_l{candidate.get('line_no')}" if candidate.get("page_no") is not None and candidate.get("line_no") is not None else None,
                )

            amount_tail_match = amount_tail_pattern.search(candidate_text)
            if amount_tail_match is not None and fallback is None:
                fallback = (
                    _parse_amount_value(amount_tail_match.group(1)),
                    int(candidate.get("page_no")) if candidate.get("page_no") is not None else None,
                    f"p{candidate.get('page_no')}_l{candidate.get('line_no')}" if candidate.get("page_no") is not None and candidate.get("line_no") is not None else None,
                )

        if fallback is not None:
            return fallback
    return None


def _build_ant_reference_no(txn_date: str, description_raw: str) -> str:
    upper = description_raw.upper()
    if "INTEREST" in upper:
        return f"ANT-LIBRA-INT-{txn_date}"
    if upper.startswith("TO "):
        return f"ANT-LIBRA-XFER-{txn_date}-001"
    normalized = re.sub(r"[^A-Z0-9]+", "-", upper).strip("-")[:24] or "TXN"
    return f"ANT-LIBRA-{normalized}-{txn_date}"


def _extract_ant_counterparty(description_raw: str) -> str | None:
    match = re.match(r"^To\s+(.+?)(?:\s+\(ending\s+[^)]+\))?$", description_raw.strip(), re.IGNORECASE)
    if not match:
        return None
    return match.group(1).strip() or None


def _normalize_ant_description(description_raw: str) -> str:
    normalized = _trim_ant_payload_suffix(description_raw)
    upper = normalized.upper()
    if "INTEREST" in upper:
        return "Interest 利息"
    english_to = re.search(r"(To\s+.+?\(ending\s+[^)]+\))", normalized, re.IGNORECASE)
    if english_to is not None:
        return english_to.group(1).strip()
    english_from = re.search(r"(From\s+.+?\(endi(?:ng)?\s+[^)]+\))", normalized, re.IGNORECASE)
    if english_from is not None:
        return english_from.group(1).strip()
    return normalized


def _infer_ant_channel(description_raw: str) -> str | None:
    upper = description_raw.upper()
    if "INTEREST" in upper:
        return "INTEREST"
    if upper.startswith("TO ") or upper.startswith("FROM "):
        return "TRANSFER"
    return None


def _build_ant_dedupe_key(
    account_masked: str | None,
    txn_date: str,
    direction: str,
    amount: float,
    description_raw: str,
    reference_no: str,
    running_balance: float,
) -> str:
    payload = "|".join(
        [
            account_masked or "",
            txn_date,
            direction,
            f"{amount:.2f}",
            description_raw,
            reference_no,
            f"{running_balance:.2f}",
        ]
    )
    return sha256_bytes(payload.encode("utf-8"))


def _ensure_ant_seed_records(conn: sqlite3.Connection, account_masked: str | None, created_at: str) -> str:
    account_masked = account_masked or "39375388225873987"
    account_key = _normalize_statement_account_key(account_masked)
    account_id = f"acct_ant_bank_{account_key}_hkd_libra_savings"
    conn.execute(
        """
        INSERT OR IGNORE INTO institutions (
            institution_id, institution_code, institution_name, institution_type,
            country_or_region, default_base_currency, is_active, notes, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, NULL)
        """,
        (
            "ant_bank",
            "ant_bank",
            "Ant Bank (Hong Kong)",
            "bank",
            "Hong Kong",
            "HKD",
            "Seeded by email gateway Ant Bank transaction builder",
            created_at,
        ),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO accounts (
            account_id, institution_id, parent_account_id, account_masked, account_number_hash,
            account_name, account_type, product_type, asset_class, base_currency,
            owner_entity, is_active, opened_at, closed_at, notes, created_at, updated_at
        ) VALUES (?, ?, NULL, ?, NULL, ?, ?, ?, ?, ?, ?, 1, NULL, NULL, ?, ?, NULL)
        """,
        (
            account_id,
            "ant_bank",
            account_masked,
            "Ant Bank Libra Savings HKD",
            "Libra Savings Account",
            "digital_bank_libra_savings",
            "cash",
            "HKD",
            "CHEN GENG",
            "Seeded by email gateway Ant Bank transaction builder",
            created_at,
        ),
    )
    return account_id



def _ensure_hang_seng_seed_records(
    conn: sqlite3.Connection,
    account_masked: str | None,
    created_at: str,
    currency: str = "HKD",
) -> dict[str, str]:
    account_masked = account_masked or "218-765469-888"
    currency = (currency or "HKD").upper()
    account_key = _normalize_statement_account_key(account_masked)
    account_id = f"acct_hang_seng_{account_key}_{currency.lower()}_integrated"
    conn.execute(
        """
        INSERT OR IGNORE INTO institutions (
            institution_id, institution_code, institution_name, institution_type,
            country_or_region, default_base_currency, is_active, notes, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, NULL)
        """,
        (
            "hang_seng",
            "hang_seng",
            "Hang Seng Bank",
            "bank",
            "Hong Kong",
            "HKD",
            "Seeded by email gateway Hang Seng transaction builder",
            created_at,
        ),
    )
    profile = {
        "account_id": account_id,
        "account_masked": account_masked,
        "account_key": account_key,
        "account_name": f"Hang Seng Bank {currency} Integrated Account",
        "account_type": "Integrated Account",
        "product_type": "integrated_account_savings",
        "base_currency": currency,
        "owner_entity": "CHEN GENG",
    }
    conn.execute(
        """
        INSERT OR IGNORE INTO accounts (
            account_id, institution_id, parent_account_id, account_masked, account_number_hash,
            account_name, account_type, product_type, asset_class, base_currency,
            owner_entity, is_active, opened_at, closed_at, notes, created_at, updated_at
        ) VALUES (?, ?, NULL, ?, NULL, ?, ?, ?, ?, ?, ?, 1, NULL, NULL, ?, ?, NULL)
        """,
        (
            profile["account_id"],
            "hang_seng",
            account_masked,
            profile["account_name"],
            profile["account_type"],
            profile["product_type"],
            "cash",
            currency,
            profile["owner_entity"],
            "Seeded by email gateway Hang Seng transaction builder",
            created_at,
        ),
    )
    return profile


def _extract_hang_seng_transaction_section_lines(line_items: list[dict[str, object]]) -> list[dict[str, object]]:
    inside = False
    collected: list[dict[str, object]] = []
    for item in line_items:
        text = str(item["text"]).strip()
        if not text:
            continue
        if not inside:
            if text.upper().startswith("TRANSACTION HISTORY"):
                inside = True
            continue
        if text.upper().startswith("TRANSACTION SUMMARY") or text.upper().startswith("CREDIT INTEREST ACCRUED"):
            break
        collected.append(item)
    return collected


def _parse_hang_seng_account_number(lines: list[dict[str, object]], fallback: str | None) -> str | None:
    for item in lines:
        match = re.search(r"Account Number\s+([0-9]{3}-[0-9]{6}-[0-9]{3})", str(item["text"]), re.IGNORECASE)
        if match is not None:
            return match.group(1)
    return fallback


def _is_hang_seng_section_header(text: str) -> bool:
    upper = text.upper()
    return "INTEGRATED ACCOUNT STATEMENT SAVINGS" in upper or "INTEGRATED ACCOUNT FCY SAVINGS" in upper


def _is_hang_seng_table_header(text: str) -> bool:
    upper = text.upper()
    return upper.startswith("DATE ") or upper in {"CCY", "CCY CNY", "CCY HKD"}


def _extract_hang_seng_currency(text: str, current_currency: str) -> str:
    upper = text.upper()
    match = re.search(r"\b(HKD|CNY|USD|EUR)\b", upper)
    if match is not None:
        return match.group(1)
    if "FCY" in upper:
        return "CNY"
    if "STATEMENT SAVINGS" in upper:
        return "HKD"
    return current_currency


def _parse_hang_seng_amount_columns(payload_lines: list[str]) -> tuple[list[str], float | None, float | None, str | None]:
    tokens: list[str] = []
    for line in payload_lines:
        tokens.extend(line.split())
    amount_positions = [idx for idx, token in enumerate(tokens) if re.fullmatch(r"\d{1,3}(?:,\d{3})*\.\d{2}", token)]
    if not amount_positions:
        return payload_lines, None, None, None
    balance_pos = amount_positions[-1]
    balance = _parse_amount_value(tokens[balance_pos])
    amount = None
    amount_pos = None
    if len(amount_positions) >= 2:
        amount_pos = amount_positions[-2]
        amount = _parse_amount_value(tokens[amount_pos])
    excluded = {balance_pos}
    if amount_pos is not None:
        excluded.add(amount_pos)
    desc_tokens = [token for idx, token in enumerate(tokens) if idx not in excluded]
    description = " ".join(desc_tokens).strip()
    return [description] if description else [], amount, balance, description


def _infer_hang_seng_direction(description_raw: str, previous_balance: float | None, amount: float | None, balance: float | None) -> str | None:
    upper = description_raw.upper()
    if amount is not None and previous_balance is not None and balance is not None:
        if round(previous_balance + amount, 2) == round(balance, 2):
            return "credit"
        if round(previous_balance - amount, 2) == round(balance, 2):
            return "debit"
    if "DEPOSIT" in upper or "CREDIT" in upper or "INTEREST" in upper:
        return "credit"
    if "WITHDRAWAL" in upper or "DEBIT" in upper or "CHARGE" in upper or "FEE" in upper:
        return "debit"
    return None


def _infer_hang_seng_channel(description_raw: str) -> str | None:
    upper = description_raw.upper()
    if "INTEREST" in upper:
        return "interest_credit"
    if "TRANSFER" in upper or "NTRF" in upper:
        return "transfer"
    if "CASH" in upper:
        return "cash"
    return None


def _build_hang_seng_dedupe_key(
    account_masked: str | None,
    txn_date: str,
    currency: str,
    direction: str,
    amount: float,
    description_raw: str,
    balance: float | None,
) -> str:
    payload = "|".join(
        [
            account_masked or "",
            txn_date,
            currency,
            direction,
            f"{amount:.2f}",
            description_raw,
            "" if balance is None else f"{balance:.2f}",
        ]
    )
    return sha256_bytes(payload.encode("utf-8"))

def _ensure_za_seed_records(conn: sqlite3.Connection, account_masked: str | None, created_at: str) -> str:
    account_masked = account_masked or "887027001-210"
    account_key = _normalize_statement_account_key(account_masked)
    account_id = f"acct_za_bank_{account_key}_hkd_savings"
    conn.execute(
        """
        INSERT OR IGNORE INTO institutions (
            institution_id, institution_code, institution_name, institution_type,
            country_or_region, default_base_currency, is_active, notes, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, NULL)
        """,
        (
            "za_bank",
            "za_bank",
            "ZA Bank",
            "bank",
            "Hong Kong",
            "HKD",
            "Seeded by email gateway ZA transaction builder",
            created_at,
        ),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO accounts (
            account_id, institution_id, parent_account_id, account_masked, account_number_hash,
            account_name, account_type, product_type, asset_class, base_currency,
            owner_entity, is_active, opened_at, closed_at, notes, created_at, updated_at
        ) VALUES (?, ?, NULL, ?, NULL, ?, ?, ?, ?, ?, ?, 1, NULL, NULL, ?, ?, NULL)
        """,
        (
            account_id,
            "za_bank",
            account_masked,
            "ZA Bank HKD Savings",
            "HKD Savings",
            "digital_bank_savings",
            "cash",
            "HKD",
            "CHEN GENG",
            "Seeded by email gateway ZA transaction builder",
            created_at,
        ),
    )
    return account_id


def _build_scb_account_profile(account_kind: str, account_masked: str | None) -> dict[str, str]:
    normalized_kind = (account_kind or "savings").strip().lower()
    if normalized_kind == "current":
        account_masked = account_masked or "562-8-582826-0"
        account_label = "Current"
        account_type = "Integrated Deposits Account - Current"
        product_type = "integrated_deposits_current"
        suffix = "current"
    else:
        normalized_kind = "savings"
        account_masked = account_masked or "562-8-582826-1"
        account_label = "Savings"
        account_type = "Integrated Deposits Account - Savings"
        product_type = "integrated_deposits_savings"
        suffix = "savings"

    account_key = _normalize_statement_account_key(account_masked)
    return {
        "account_kind": normalized_kind,
        "account_masked": account_masked,
        "account_key": account_key,
        "account_id": f"acct_scb_hk_{account_key}_hkd_{suffix}",
        "account_label": account_label,
        "account_name": f"Standard Chartered Hong Kong HKD {account_label}",
        "account_type": account_type,
        "product_type": product_type,
        "base_currency": "HKD",
        "owner_entity": "CHEN GENG",
    }



def _parse_scb_account_kind_fragment(text: str) -> str | None:
    match = re.match(
        r"^INTEGRATED\s+(?:DEPOSITS\s+)?ACCOUNT\s*[-–]?\s*(SAVINGS|CURRENT)\b",
        text.strip(),
        re.IGNORECASE,
    )
    if match is None:
        return None
    return match.group(1).lower()


def _parse_scb_account_number_line(text: str) -> str | None:
    match = re.match(r"^:?\s*([0-9]{3}-[0-9]-[0-9]{6}-[0-9])\b", text.strip())
    if match is None:
        return None
    return match.group(1)


def _parse_scb_account_header(text: str) -> dict[str, str] | None:
    account_kind = _parse_scb_account_kind_fragment(text)
    if account_kind is None:
        return None
    account_match = re.search(r"([0-9]{3}-[0-9]-[0-9]{6}-[0-9])\b", text)
    if account_match is None:
        return None
    return _build_scb_account_profile(account_kind, account_match.group(1))



def _ensure_scb_seed_records(
    conn: sqlite3.Connection,
    account_masked: str | None,
    created_at: str,
    account_kind: str = "savings",
) -> dict[str, str]:
    profile = _build_scb_account_profile(account_kind, account_masked)
    conn.execute(
        """
        INSERT OR IGNORE INTO institutions (
            institution_id, institution_code, institution_name, institution_type,
            country_or_region, default_base_currency, is_active, notes, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, NULL)
        """,
        (
            "scb_hk",
            "scb_hk",
            "Standard Chartered Hong Kong",
            "bank",
            "Hong Kong",
            "HKD",
            "Seeded by email gateway SCB transaction builder",
            created_at,
        ),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO accounts (
            account_id, institution_id, parent_account_id, account_masked, account_number_hash,
            account_name, account_type, product_type, asset_class, base_currency,
            owner_entity, is_active, opened_at, closed_at, notes, created_at, updated_at
        ) VALUES (?, ?, NULL, ?, NULL, ?, ?, ?, ?, ?, ?, 1, NULL, NULL, ?, ?, NULL)
        """,
        (
            profile["account_id"],
            "scb_hk",
            profile["account_masked"],
            profile["account_name"],
            profile["account_type"],
            profile["product_type"],
            "cash",
            profile["base_currency"],
            profile["owner_entity"],
            "Seeded by email gateway SCB transaction builder",
            created_at,
        ),
    )
    return profile


def _split_scb_date_prefix(text: str, statement_date: str | None) -> tuple[str, str] | None:
    stripped = text.strip()
    if not stripped or not statement_date:
        return None

    slash_match = re.match(r"^(\d{1,2})/(\d{1,2})(?:\b|\s*)(.*)$", stripped)
    if slash_match is not None:
        month = int(slash_match.group(1))
        day = int(slash_match.group(2))
        remainder = slash_match.group(3).strip()
        year = int(statement_date[:4])
        statement_month = int(statement_date[5:7])
        statement_day = int(statement_date[8:10])
        if month > statement_month or (month == statement_month and day > statement_day):
            year -= 1
        return f"{year:04d}-{month:02d}-{day:02d}", remainder

    month_match = re.match(r"^(\d{1,2})\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)(?:\b|\s*)(.*)$", stripped, re.IGNORECASE)
    if month_match is not None:
        day = int(month_match.group(1))
        month = _MONTH_NAME_TO_NUMBER[month_match.group(2).upper()]
        remainder = month_match.group(3).strip()
        year = int(statement_date[:4])
        statement_month = int(statement_date[5:7])
        statement_day = int(statement_date[8:10])
        if month > statement_month or (month == statement_month and day > statement_day):
            year -= 1
        return f"{year:04d}-{month:02d}-{day:02d}", remainder

    return None


def _parse_scb_partial_date(text: str, statement_date: str | None) -> str | None:
    split = _split_scb_date_prefix(text, statement_date)
    if split is None:
        return None
    return split[0]


def _is_scb_activity_header(text: str) -> bool:
    normalized = _compact_hsbc_marker_text(text)
    return "YOURACCOUNTACTIVITIES" in normalized or "閣下各戶口之進支紀錄" in text


def _is_scb_stop_line(text: str) -> bool:
    normalized = _compact_hsbc_marker_text(text)
    stop_markers = (
        "YOURAVERAGERELATIONSHIPBALANCE",
        "BELOWISASUMMARYOFYOURPASTTHREEMONTHS",
        "AVERAGEDAILYRELATIONSHIPBALANCE",
        "CONGRATULATIONS!YOURAVERAGERELATIONSHIPBALANCE",
        "STATEMENTBACKPAGE",
        "CLICKHERETOVIEWTHEINFORMATIONONTHEBACKPAGEOFTHETSTATEMENT",
        "CLICKHERETOVIEWTHEINFORMATIONONTHEBACKPAGEOFTHESTATEMENT",
    )
    return any(marker in normalized for marker in stop_markers)


def _is_scb_preamble_noise_line(text: str) -> bool:
    compact = _compact_hsbc_marker_text(text)
    if compact in {"CHENGENG", "CHENG***"}:
        return True
    normalized = re.sub(r"\s+", "", text)
    if normalized in {"綜合存款戶口–儲蓄", "綜合存款戶口－儲蓄", "綜合存款戶口–支票", "綜合存款戶口－支票"}:
        return True
    if text and all(char in {"·", "•", "-", "–", "—", "_", " ", "\t"} for char in text):
        return True
    return False


def _extract_scb_currency_heading(text: str) -> str | None:
    normalized = text.strip().upper()
    match = re.match(r"^(HKD|CNY|USD|EUR)(?:\b|\s|[·•])", normalized)
    if match is None:
        return None
    return match.group(1)


def _is_scb_table_header_line(text: str) -> bool:
    stripped = text.strip()
    if stripped in {"(月/日)", "(·/·)"}:
        return True
    normalized = re.sub(r"[^A-Z\u4e00-\u9fff/]", "", text.upper())
    return normalized in {
        "MM/DD",
        "月/日",
        "DATE日期",
        "DATE",
        "DESCRIPTION進支詳列",
        "DESCRIPTION",
        "DEPOSIT存款",
        "DEPOSIT",
        "WITHDRAWAL提款",
        "WITHDRAWAL",
        "BALANCE結餘",
        "BALANCE",
        "HKD港元",
        "CNY人民幣",
    }


def _is_scb_currency_heading_line(text: str) -> bool:
    normalized = text.strip().upper()
    return re.match(r"^(HKD|CNY|USD|EUR)(?:\b|\s|[·•])", normalized) is not None


def _extract_scb_activity_section_lines(line_items: list[dict[str, object]]) -> list[dict[str, object]]:
    inside = False
    collected: list[dict[str, object]] = []
    for item in line_items:
        text = str(item["text"]).strip()
        if not text:
            continue
        if not inside:
            if _is_scb_activity_header(text):
                inside = True
            continue
        if _is_scb_stop_line(text):
            break
        if re.match(r"^(?:頁\s*\d+|Page\b)", text, re.IGNORECASE):
            continue
        collected.append(item)
    return collected


def _normalize_scb_description(description_raw: str) -> str:
    normalized = re.sub(r"[·•]+", " ", description_raw).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    upper = normalized.upper()
    if upper.startswith("CREDIT INTEREST"):
        return "CREDIT INTEREST 利息存入"
    return normalized


def _restore_scb_direction_marker(description_raw: str, direction: str | None) -> str:
    normalized = description_raw.strip()
    if not normalized or direction not in {"credit", "debit"}:
        return normalized
    if "存賬" in normalized or "支賬" in normalized:
        return normalized
    if normalized.upper().startswith("CREDIT INTEREST"):
        return normalized

    parts = normalized.split(" ", 1)
    head = parts[0]
    tail = parts[1] if len(parts) > 1 else ""

    marker: str | None = None
    if direction == "credit" and (head.startswith("BT|") or head.startswith("IBFT|")):
        marker = "存賬"
    elif direction == "debit" and re.match(r"^HK\d", head):
        marker = "支賬"

    if marker is None:
        return normalized
    return f"{head} {marker}" + (f" {tail}" if tail else "")


def _infer_scb_direction(
    description_raw: str,
    amount: float,
    previous_balance: float | None = None,
    balance: float | None = None,
) -> str | None:
    upper = description_raw.upper()
    rounded_amount = round(abs(amount), 2)
    if previous_balance is not None and balance is not None:
        if round(previous_balance + rounded_amount, 2) == round(balance, 2):
            return "credit"
        if round(previous_balance - rounded_amount, 2) == round(balance, 2):
            return "debit"
    credit_markers = (
        "CREDIT INTEREST",
        "TRANSFER IN",
        "FPS IN",
        "DEPOSIT",
        "CREDIT",
        "MISCELLANEOUS CREDIT",
    )
    debit_markers = (
        "LOCAL TRANSFER",
        "TRANSFER WITHDRAWAL",
        "WITHDRAWAL",
        "FPS OUT",
        "PAYMENT",
        "FEE",
        "CHARGE",
        "DEBIT",
    )
    if "存賬" in description_raw:
        return "credit"
    if "支賬" in description_raw:
        return "debit"
    if any(marker in upper for marker in credit_markers):
        return "credit"
    if any(marker in upper for marker in debit_markers):
        return "debit"
    if amount < 0:
        return "debit"
    return None


def _infer_scb_channel(description_raw: str) -> str | None:
    upper = description_raw.upper()
    if "CREDIT INTEREST" in upper:
        return "interest_credit"
    if "TRANSFER" in upper or "FPS" in upper:
        return "transfer"
    return None


def _build_scb_bank_reference(txn_date: str, description_raw: str) -> str | None:
    if "CREDIT INTEREST" in description_raw.upper():
        return f"SCB-INT-{txn_date}"
    return None


def _infer_scb_balance_currency(
    payload_lines: list[str],
    profile: dict[str, str],
    amount: float | None,
) -> str:
    joined = " ".join(payload_lines).upper()
    for candidate in ("HKD", "CNY", "USD", "EUR"):
        if re.search(rf"\b{candidate}\b", joined):
            return candidate
    if profile["account_kind"] == "savings" and amount == 0:
        return "CNY"
    return profile["base_currency"]


def _extract_scb_leading_balance_marker(payload_lines: list[str]) -> dict[str, object] | None:
    cleaned = [line.strip() for line in payload_lines if line.strip()]
    if not cleaned:
        return None

    first_line = cleaned[0]
    upper_first_line = first_line.upper()

    if upper_first_line.startswith("BALANCE FROM PREVIOUS STATEMENT"):
        if len(cleaned) >= 2 and _is_amount_line(cleaned[1]):
            balance = _parse_amount_value(cleaned[1])
            return {
                "marker_role": "opening_balance",
                "explicit_currency": None,
                "balance": balance,
                "remaining_lines": cleaned[2:],
            }
        match = re.match(r"^BALANCE FROM PREVIOUS STATEMENT.*?(-?[\d,]+\.\d{2})$", first_line, re.IGNORECASE)
        if match is not None:
            return {
                "marker_role": "opening_balance",
                "explicit_currency": None,
                "balance": _parse_amount_value(match.group(1)),
                "remaining_lines": cleaned[1:],
            }
        return None

    if upper_first_line.startswith("CLOSING BALANCE"):
        inline_match = re.match(r"^CLOSING BALANCE(?:\s+(HKD|CNY|USD|EUR))?.*?(-?[\d,]+\.\d{2})$", first_line, re.IGNORECASE)
        if inline_match is not None:
            return {
                "marker_role": "closing_balance",
                "explicit_currency": inline_match.group(1),
                "balance": _parse_amount_value(inline_match.group(2)),
                "remaining_lines": cleaned[1:],
            }
        if len(cleaned) >= 2 and _is_amount_line(cleaned[1]):
            currency_match = re.match(r"^CLOSING BALANCE(?:\s+(HKD|CNY|USD|EUR))?", first_line, re.IGNORECASE)
            explicit_currency = currency_match.group(1) if currency_match is not None else None
            return {
                "marker_role": "closing_balance",
                "explicit_currency": explicit_currency,
                "balance": _parse_amount_value(cleaned[1]),
                "remaining_lines": cleaned[2:],
            }
        return None

    return None


def _extract_scb_leading_opening_balance(payload_lines: list[str]) -> tuple[float | None, list[str]]:
    cleaned = [line.strip() for line in payload_lines if line.strip()]
    marker = _extract_scb_leading_balance_marker(cleaned)
    if marker is None or marker["marker_role"] != "opening_balance":
        return None, cleaned
    return float(marker["balance"]), list(marker["remaining_lines"])


def _extract_scb_leading_closing_balance(payload_lines: list[str]) -> tuple[str | None, float | None, list[str]]:
    cleaned = [line.strip() for line in payload_lines if line.strip()]
    marker = _extract_scb_leading_balance_marker(cleaned)
    if marker is None or marker["marker_role"] != "closing_balance":
        return None, None, cleaned
    explicit_currency = marker["explicit_currency"]
    if explicit_currency is not None:
        explicit_currency = str(explicit_currency)
    return explicit_currency, float(marker["balance"]), list(marker["remaining_lines"])


def _parse_scb_transaction_entries(
    payload_lines: list[str],
    previous_balance: float | None,
) -> list[dict[str, object]]:
    cleaned = [line.strip() for line in payload_lines if line.strip()]
    amount_indexes = [index for index, line in enumerate(cleaned) if _is_amount_line(line)]
    if len(amount_indexes) < 2:
        return []

    transactions: list[dict[str, object]] = []
    running_previous_balance = previous_balance
    pair_count = len(amount_indexes) // 2
    for pair_index in range(pair_count):
        amount_index = amount_indexes[pair_index * 2]
        balance_index = amount_indexes[pair_index * 2 + 1]
        desc_start = 0 if pair_index == 0 else amount_indexes[pair_index * 2 - 1] + 1
        desc_lines = [line for line in cleaned[desc_start:amount_index] if not _is_amount_line(line)]
        if pair_index == pair_count - 1:
            trailing_lines = [line for line in cleaned[balance_index + 1 :] if not _is_amount_line(line)]
            if trailing_lines:
                desc_lines.extend(trailing_lines)
        if not desc_lines:
            continue

        amount = _parse_amount_value(cleaned[amount_index])
        balance = _parse_amount_value(cleaned[balance_index])
        description_raw = _normalize_scb_description(" ".join(desc_lines))
        direction = _infer_scb_direction(
            description_raw,
            amount,
            previous_balance=running_previous_balance,
            balance=balance,
        )
        if direction is None:
            continue
        description_raw = _restore_scb_direction_marker(description_raw, direction)
        transactions.append(
            {
                "description_raw": description_raw,
                "amount": amount,
                "balance": balance,
                "direction": direction,
            }
        )
        running_previous_balance = balance
    return transactions


def _parse_scb_transaction_payload(payload: str) -> tuple[str, float, float] | None:
    match = re.match(
        r"^(?P<desc>.+?)\s+(?P<amount>-?\d{1,3}(?:,\d{3})*\.\d{2})\s+(?P<balance>-?\d{1,3}(?:,\d{3})*\.\d{2})$",
        payload.strip(),
    )
    if match is None:
        return None
    amount = _parse_amount_value(match.group("amount"))
    balance = _parse_amount_value(match.group("balance"))
    if amount is None or balance is None:
        return None
    return match.group("desc").strip(), amount, balance


def _build_scb_dedupe_key(
    account_masked: str | None,
    txn_date: str,
    direction: str,
    amount: float,
    description_raw: str,
    bank_reference: str | None,
    balance: float | None,
) -> str:
    payload = "|".join(
        [
            account_masked or "",
            txn_date,
            direction,
            f"{amount:.2f}",
            description_raw,
            bank_reference or "",
            "" if balance is None else f"{balance:.2f}",
        ]
    )
    return sha256_bytes(payload.encode("utf-8"))


def _parse_za_transaction_groups(section_lines: list[dict[str, object]]) -> list[dict[str, object]]:
    groups: list[dict[str, object]] = []
    index = 0
    while index < len(section_lines):
        text = str(section_lines[index]["text"]).strip()
        split = _split_za_date_prefix(text)
        if split is None:
            index += 1
            continue
        txn_date, remainder = split
        items = [section_lines[index]]
        payload_lines: list[str] = []
        if remainder:
            payload_lines.append(remainder)
        index += 1
        while index < len(section_lines):
            next_text = str(section_lines[index]["text"]).strip()
            if _split_za_date_prefix(next_text) is not None:
                break
            payload_lines.append(next_text)
            items.append(section_lines[index])
            index += 1
        groups.append({
            "txn_date": txn_date,
            "items": items,
            "payload_lines": payload_lines,
        })
    return groups


def build_za_transactions_from_ingested_email_documents(
    *,
    db_path: str | Path,
    only_facts_built: bool = True,
) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        query = (
            """
            SELECT d.document_id, d.filename, d.institution_id, d.processing_status,
                   a.email_id, df.document_fact_id, df.account_masked_raw, df.statement_date
            FROM documents d
            JOIN email_attachments a ON a.attachment_id = d.attachment_id
            JOIN document_facts df ON df.document_id = d.document_id
            WHERE d.source_type = 'email_attachment'
              AND d.institution_id = 'za_bank'
            """
        )
        if only_facts_built:
            query += " AND d.processing_status = 'document_facts_built'"
        query += " ORDER BY df.statement_date, d.document_id"
        rows = conn.execute(query).fetchall()

        created_at = now_iso()
        documents_scanned = 0
        transactions_inserted = 0
        balance_markers_inserted = 0
        document_sections_inserted = 0
        duplicate_transactions_skipped = 0

        for row in rows:
            documents_scanned += 1
            document_id = str(row["document_id"])
            account_masked = row["account_masked_raw"]
            statement_date = row["statement_date"]
            document_fact_id = row["document_fact_id"]
            account_id = _ensure_za_seed_records(conn, account_masked, created_at)
            account_key = _normalize_statement_account_key(account_masked)
            activity_section_id = f"sec_email_za_{account_key}_{statement_date}_cash_activity"
            summary_section_id = f"sec_email_za_{account_key}_{statement_date}_balance_summary"

            conn.execute("DELETE FROM transactions WHERE document_id = ?", (document_id,))
            conn.execute("DELETE FROM balance_markers WHERE document_id = ?", (document_id,))
            conn.execute("DELETE FROM document_sections WHERE document_id = ?", (document_id,))

            conn.execute(
                """
                INSERT INTO document_sections (
                    section_id, document_id, parent_section_id, section_type, section_label_raw,
                    account_id, account_number_raw, account_name_raw, account_type_raw,
                    product_type_raw, currency, page_start, page_end, section_order,
                    confidence, notes, created_at
                ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)
                """,
                (
                    activity_section_id,
                    document_id,
                    "cash_account_activity",
                    "ZA Bank HKD Savings Transaction History",
                    account_id,
                    account_masked,
                    "ZA Bank HKD Savings",
                    "HKD Savings",
                    "digital_bank_savings",
                    "HKD",
                    1,
                    1.0,
                    "Auto-built from email gateway ZA transaction extraction",
                    created_at,
                ),
            )
            conn.execute(
                """
                INSERT INTO document_sections (
                    section_id, document_id, parent_section_id, section_type, section_label_raw,
                    account_id, account_number_raw, account_name_raw, account_type_raw,
                    product_type_raw, currency, page_start, page_end, section_order,
                    confidence, notes, created_at
                ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)
                """,
                (
                    summary_section_id,
                    document_id,
                    "cash_balance_summary",
                    "ZA Bank HKD Savings Balance Summary",
                    account_id,
                    account_masked,
                    "ZA Bank HKD Savings",
                    "HKD Savings",
                    "digital_bank_savings",
                    "HKD",
                    2,
                    1.0,
                    "Auto-built from email gateway ZA balance extraction",
                    created_at,
                ),
            )
            document_sections_inserted += 2

            raw_rows = conn.execute(
                "SELECT page_no, line_no, raw_text FROM raw_document_lines WHERE document_id = ? ORDER BY page_no, line_no",
                (document_id,),
            ).fetchall()
            line_items = [
                {
                    "page_no": int(raw_row["page_no"]),
                    "line_no": int(raw_row["line_no"]),
                    "text": _normalize_ocr_line_text(str(raw_row["raw_text"])),
                }
                for raw_row in raw_rows
                if str(raw_row["raw_text"]).strip()
            ]
            section_lines = _extract_za_transaction_section_lines(line_items)
            groups = _parse_za_transaction_groups(section_lines)
            marker_index = 0
            txn_index = 0
            previous_balance: float | None = None
            opening_inserted = False

            for group_index, group in enumerate(groups, start=1):
                txn_date = str(group["txn_date"])
                items = list(group["items"])
                payload_lines = [str(value).strip() for value in group["payload_lines"] if str(value).strip()]
                if not payload_lines:
                    continue

                opening_balance = _parse_za_opening_balance(payload_lines)
                source_page, source_line_ref = _build_hsbc_source_line_ref(items)
                if opening_balance is not None:
                    marker_index += 1
                    conn.execute(
                        """
                        INSERT INTO balance_markers (
                            balance_marker_id, ledger_batch_id, document_id, section_id, institution_id,
                            account_id, account_number_raw, account_name_raw, account_type_raw,
                            product_type_raw, marker_date, currency, balance, balance_hkd_equivalent,
                            marker_role, description_raw, source_file, source_page, source_line_ref,
                            confidence, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"bm_{document_id}_{marker_index}",
                            "email_gateway_za_transactions",
                            document_id,
                            summary_section_id,
                            "za_bank",
                            account_id,
                            account_masked,
                            "ZA Bank HKD Savings",
                            "HKD Savings",
                            "digital_bank_savings",
                            txn_date,
                            "HKD",
                            opening_balance,
                            opening_balance,
                            "opening_balance",
                            payload_lines[0],
                            row["filename"],
                            source_page,
                            source_line_ref,
                            1.0,
                            created_at,
                        ),
                    )
                    balance_markers_inserted += 1
                    previous_balance = opening_balance
                    opening_inserted = True
                    continue

                description_raw: str | None = None
                reference_no: str | None = None
                amount: float | None = None
                running_balance: float | None = None

                entries = _split_za_transaction_payload(payload_lines)
                if not entries:
                    continue

                if len(entries) == 1:
                    description_raw = entries[0]["description_raw"]
                    reference_no = entries[0]["reference_no"]
                    amount = entries[0]["amount"]
                    running_balance = entries[0]["running_balance"]
                else:
                    for entry_index, entry in enumerate(entries, start=1):
                        description_raw = str(entry["description_raw"])
                        reference_no = entry["reference_no"]
                        amount = float(entry["amount"])
                        running_balance = entry["running_balance"]
                        entry_items = items
                        entry_source_page, entry_source_line_ref = source_page, source_line_ref
                        if entry_index < len(entries):
                            entry_source_line_ref = f"{source_page}:{items[0]['line_no'] + (entry_index * 4) - 1}-{items[0]['line_no'] + (entry_index * 4) + 2}"

                        direction = _infer_za_direction(description_raw, amount, running_balance, previous_balance)
                        if direction is None:
                            continue
                        amount_signed = amount if direction == "credit" else -amount

                        if not opening_inserted and running_balance is not None:
                            inferred_opening_balance = round(running_balance - amount_signed, 2)
                            marker_index += 1
                            conn.execute(
                                """
                                INSERT INTO balance_markers (
                                    balance_marker_id, ledger_batch_id, document_id, section_id, institution_id,
                                    account_id, account_number_raw, account_name_raw, account_type_raw,
                                    product_type_raw, marker_date, currency, balance, balance_hkd_equivalent,
                                    marker_role, description_raw, source_file, source_page, source_line_ref,
                                    confidence, created_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    f"bm_{document_id}_{marker_index + 1}",
                                    "email_gateway_za_transactions",
                                    document_id,
                                    summary_section_id,
                                    "za_bank",
                                    account_id,
                                    account_masked,
                                    "ZA Bank HKD Savings",
                                    "HKD Savings",
                                    "digital_bank_savings",
                                    txn_date,
                                    "HKD",
                                    inferred_opening_balance,
                                    inferred_opening_balance,
                                    "opening_balance",
                                    "Opening balance inferred from first transaction and running balance",
                                    row["filename"],
                                    entry_source_page,
                                    entry_source_line_ref,
                                    1.0,
                                    created_at,
                                ),
                            )
                            marker_index += 1
                            balance_markers_inserted += 1
                            previous_balance = inferred_opening_balance
                            opening_inserted = True

                        channel = _infer_za_channel(description_raw)
                        description_clean, counterparty_raw, counterparty_name_raw, counterparty_account_masked, counterparty_phone_raw = _parse_za_counterparty(description_raw)
                        dedupe_key = _build_za_dedupe_key(account_masked, txn_date, direction, amount, description_clean, reference_no, running_balance)
                        existing = conn.execute(
                            "SELECT 1 FROM transactions WHERE dedupe_key = ?",
                            (dedupe_key,),
                        ).fetchone()
                        if existing is not None:
                            duplicate_transactions_skipped += 1
                        else:
                            txn_index += 1
                            conn.execute(
                                """
                                INSERT INTO transactions (
                                    transaction_id, ledger_batch_id, document_id, section_id, document_fact_id,
                                    institution_id, account_id, account_number_raw, account_name_raw,
                                    account_type_raw, product_type_raw, txn_date, posting_date, value_date,
                                    trade_date, settlement_date, effective_date, currency, amount, direction,
                                    amount_signed, base_currency, fx_rate_to_base, amount_in_base_currency,
                                    description_raw, description_clean, reference_no, bank_reference,
                                    external_reference, counterparty_raw, counterparty_clean,
                                    counterparty_name_raw, counterparty_name_clean, counterparty_account_masked,
                                    counterparty_phone_raw, counterparty_bank_name, channel, payment_rail, balance, balance_currency,
                                    balance_hkd_equivalent, balance_source, continuity_check_status, txn_type,
                                    category, tag, business_purpose, accounting_subject, source_file,
                                    source_page, source_line_ref, source_extraction_method, confidence,
                                    needs_review, review_reason, dedupe_key, canonical_hash, record_status,
                                    source_record_type, created_at, updated_at, approved_at, approved_by
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, NULL, ?, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, 0, NULL, ?, ?, 'active', 'statement_txn', ?, NULL, NULL, NULL)
                                """,
                                (
                                    f"txn_{document_id}_{txn_index}",
                                    "email_gateway_za_transactions",
                                    document_id,
                                    activity_section_id,
                                    document_fact_id,
                                    "za_bank",
                                    account_id,
                                    account_masked,
                                    "ZA Bank HKD Savings",
                                    "HKD Savings",
                                    "digital_bank_savings",
                                    txn_date,
                                    txn_date,
                                    txn_date,
                                    "HKD",
                                    amount,
                                    direction,
                                    amount_signed,
                                    "HKD",
                                    amount_signed,
                                    description_raw,
                                    description_clean,
                                    reference_no,
                                    reference_no,
                                    counterparty_raw,
                                    counterparty_raw,
                                    counterparty_name_raw,
                                    counterparty_name_raw,
                                    counterparty_account_masked,
                                    counterparty_phone_raw,
                                    None,
                                    channel,
                                    running_balance,
                                    "HKD" if running_balance is not None else None,
                                    running_balance,
                                    "statement_running_balance" if running_balance is not None else None,
                                    "statement_cash_movement",
                                    row["filename"],
                                    entry_source_page,
                                    entry_source_line_ref,
                                    f"email_gateway_za_{entry_source_page}",
                                    1.0,
                                    dedupe_key,
                                    dedupe_key,
                                    created_at,
                                ),
                            )
                            transactions_inserted += 1

                        if running_balance is not None:
                            marker_index += 1
                            marker_role = "closing_balance" if (group_index == len(groups) and entry_index == len(entries)) else "running_balance"
                            conn.execute(
                                """
                                INSERT INTO balance_markers (
                                    balance_marker_id, ledger_batch_id, document_id, section_id, institution_id,
                                    account_id, account_number_raw, account_name_raw, account_type_raw,
                                    product_type_raw, marker_date, currency, balance, balance_hkd_equivalent,
                                    marker_role, description_raw, source_file, source_page, source_line_ref,
                                    confidence, created_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    f"bm_{document_id}_{marker_index}",
                                    "email_gateway_za_transactions",
                                    document_id,
                                    summary_section_id,
                                    "za_bank",
                                    account_id,
                                    account_masked,
                                    "ZA Bank HKD Savings",
                                    "HKD Savings",
                                    "digital_bank_savings",
                                    txn_date,
                                    "HKD",
                                    running_balance,
                                    running_balance,
                                    marker_role,
                                    description_raw,
                                    row["filename"],
                                    entry_source_page,
                                    entry_source_line_ref,
                                    1.0,
                                    created_at,
                                ),
                            )
                            balance_markers_inserted += 1
                            previous_balance = running_balance
                    continue

                if description_raw is None or amount is None:
                    continue

                direction = _infer_za_direction(description_raw, amount, running_balance, previous_balance)
                if direction is None:
                    continue
                amount_signed = amount if direction == "credit" else -amount

                if not opening_inserted and running_balance is not None:
                    inferred_opening_balance = round(running_balance - amount_signed, 2)
                    marker_index += 1
                    conn.execute(
                        """
                        INSERT INTO balance_markers (
                            balance_marker_id, ledger_batch_id, document_id, section_id, institution_id,
                            account_id, account_number_raw, account_name_raw, account_type_raw,
                            product_type_raw, marker_date, currency, balance, balance_hkd_equivalent,
                            marker_role, description_raw, source_file, source_page, source_line_ref,
                            confidence, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"bm_{document_id}_{marker_index}",
                            "email_gateway_za_transactions",
                            document_id,
                            summary_section_id,
                            "za_bank",
                            account_id,
                            account_masked,
                            "ZA Bank HKD Savings",
                            "HKD Savings",
                            "digital_bank_savings",
                            txn_date,
                            "HKD",
                            inferred_opening_balance,
                            inferred_opening_balance,
                            "opening_balance",
                            "Opening balance inferred from first transaction and running balance",
                            row["filename"],
                            source_page,
                            source_line_ref,
                            1.0,
                            created_at,
                        ),
                    )
                    balance_markers_inserted += 1
                    previous_balance = inferred_opening_balance
                    opening_inserted = True

                channel = _infer_za_channel(description_raw)
                description_clean, counterparty_raw, counterparty_name_raw, counterparty_account_masked, counterparty_phone_raw = _parse_za_counterparty(description_raw)
                dedupe_key = _build_za_dedupe_key(account_masked, txn_date, direction, amount, description_clean, reference_no, running_balance)
                existing = conn.execute(
                    "SELECT 1 FROM transactions WHERE dedupe_key = ?",
                    (dedupe_key,),
                ).fetchone()
                if existing is not None:
                    duplicate_transactions_skipped += 1
                else:
                    txn_index += 1
                    conn.execute(
                        """
                        INSERT INTO transactions (
                            transaction_id, ledger_batch_id, document_id, section_id, document_fact_id,
                            institution_id, account_id, account_number_raw, account_name_raw,
                            account_type_raw, product_type_raw, txn_date, posting_date, value_date,
                            trade_date, settlement_date, effective_date, currency, amount, direction,
                            amount_signed, base_currency, fx_rate_to_base, amount_in_base_currency,
                            description_raw, description_clean, reference_no, bank_reference,
                            external_reference, counterparty_raw, counterparty_clean,
                            counterparty_name_raw, counterparty_name_clean, counterparty_account_masked,
                            counterparty_phone_raw, counterparty_bank_name, channel, payment_rail, balance, balance_currency,
                            balance_hkd_equivalent, balance_source, continuity_check_status, txn_type,
                            category, tag, business_purpose, accounting_subject, source_file,
                            source_page, source_line_ref, source_extraction_method, confidence,
                            needs_review, review_reason, dedupe_key, canonical_hash, record_status,
                            source_record_type, created_at, updated_at, approved_at, approved_by
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, NULL, ?, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, 0, NULL, ?, ?, 'active', 'statement_txn', ?, NULL, NULL, NULL)
                        """,
                        (
                            f"txn_{document_id}_{txn_index}",
                            "email_gateway_za_transactions",
                            document_id,
                            activity_section_id,
                            document_fact_id,
                            "za_bank",
                            account_id,
                            account_masked,
                            "ZA Bank HKD Savings",
                            "HKD Savings",
                            "digital_bank_savings",
                            txn_date,
                            txn_date,
                            txn_date,
                            "HKD",
                            amount,
                            direction,
                            amount_signed,
                            "HKD",
                            amount_signed,
                            description_raw,
                            description_clean,
                            reference_no,
                            reference_no,
                            counterparty_raw,
                            counterparty_raw,
                            counterparty_name_raw,
                            counterparty_name_raw,
                            counterparty_account_masked,
                            counterparty_phone_raw,
                            None,
                            channel,
                            running_balance,
                            "HKD" if running_balance is not None else None,
                            running_balance,
                            "statement_running_balance" if running_balance is not None else None,
                            "statement_cash_movement",
                            row["filename"],
                            source_page,
                            source_line_ref,
                            f"email_gateway_za_{items[0]['page_no']}",
                            1.0,
                            dedupe_key,
                            dedupe_key,
                            created_at,
                        ),
                    )
                    transactions_inserted += 1

                if running_balance is not None:
                    marker_index += 1
                    marker_role = "closing_balance" if group_index == len(groups) else "running_balance"
                    conn.execute(
                        """
                        INSERT INTO balance_markers (
                            balance_marker_id, ledger_batch_id, document_id, section_id, institution_id,
                            account_id, account_number_raw, account_name_raw, account_type_raw,
                            product_type_raw, marker_date, currency, balance, balance_hkd_equivalent,
                            marker_role, description_raw, source_file, source_page, source_line_ref,
                            confidence, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"bm_{document_id}_{marker_index}",
                            "email_gateway_za_transactions",
                            document_id,
                            summary_section_id,
                            "za_bank",
                            account_id,
                            account_masked,
                            "ZA Bank HKD Savings",
                            "HKD Savings",
                            "digital_bank_savings",
                            txn_date,
                            "HKD",
                            running_balance,
                            running_balance,
                            marker_role,
                            description_raw,
                            row["filename"],
                            source_page,
                            source_line_ref,
                            1.0,
                            created_at,
                        ),
                    )
                    balance_markers_inserted += 1
                    previous_balance = running_balance

        conn.commit()
        return {
            "documents_scanned": documents_scanned,
            "transactions_inserted": transactions_inserted,
            "balance_markers_inserted": balance_markers_inserted,
            "document_sections_inserted": document_sections_inserted,
            "duplicate_transactions_skipped": duplicate_transactions_skipped,
        }
    finally:
        conn.close()


def build_hsbc_transactions_from_ingested_email_documents(
    *,
    db_path: str | Path,
    only_facts_built: bool = True,
) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        query = (
            """
            SELECT d.document_id, d.filename, d.institution_id, d.processing_status,
                   a.email_id, a.stored_path,
                   df.document_fact_id, df.account_masked_raw, df.statement_date
            FROM documents d
            JOIN email_attachments a ON a.attachment_id = d.attachment_id
            JOIN document_facts df ON df.document_id = d.document_id
            WHERE d.source_type = 'email_attachment'
              AND d.institution_id = 'hsbc_hk'
            """
        )
        if only_facts_built:
            query += " AND d.processing_status = 'document_facts_built'"
        query += " ORDER BY df.statement_date, d.document_id"
        rows = conn.execute(query).fetchall()

        created_at = now_iso()
        documents_scanned = 0
        transactions_inserted = 0
        balance_markers_inserted = 0
        document_sections_inserted = 0
        duplicate_transactions_skipped = 0

        for row in rows:
            documents_scanned += 1
            document_id = str(row["document_id"])
            account_masked = row["account_masked_raw"]
            statement_date = row["statement_date"]
            document_fact_id = row["document_fact_id"]
            attachment_path = resolve_stored_path(row["stored_path"])
            account_id = _ensure_hsbc_seed_records(conn, account_masked, created_at)
            account_key = _normalize_statement_account_key(account_masked)
            activity_section_id = f"sec_email_hsbc_{account_key}_{statement_date}_{document_id}_cash_activity"
            summary_section_id = f"sec_email_hsbc_{account_key}_{statement_date}_{document_id}_balance_summary"

            conn.execute("DELETE FROM transactions WHERE document_id = ?", (document_id,))
            conn.execute("DELETE FROM balance_markers WHERE document_id = ?", (document_id,))
            conn.execute("DELETE FROM document_sections WHERE document_id = ?", (document_id,))

            conn.execute(
                """
                INSERT INTO document_sections (
                    section_id, document_id, parent_section_id, section_type, section_label_raw,
                    account_id, account_number_raw, account_name_raw, account_type_raw,
                    product_type_raw, currency, page_start, page_end, section_order,
                    confidence, notes, created_at
                ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)
                """,
                (
                    activity_section_id,
                    document_id,
                    "cash_account_activity",
                    "HSBC HKD Savings Transaction History",
                    account_id,
                    account_masked,
                    "HSBC Hong Kong HKD Savings",
                    "HKD Savings",
                    "statement_savings",
                    "HKD",
                    1,
                    1.0,
                    "Auto-built from email gateway HSBC transaction extraction",
                    created_at,
                ),
            )
            conn.execute(
                """
                INSERT INTO document_sections (
                    section_id, document_id, parent_section_id, section_type, section_label_raw,
                    account_id, account_number_raw, account_name_raw, account_type_raw,
                    product_type_raw, currency, page_start, page_end, section_order,
                    confidence, notes, created_at
                ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)
                """,
                (
                    summary_section_id,
                    document_id,
                    "cash_balance_summary",
                    "HSBC HKD Savings Balance Summary",
                    account_id,
                    account_masked,
                    "HSBC Hong Kong HKD Savings",
                    "HKD Savings",
                    "statement_savings",
                    "HKD",
                    2,
                    1.0,
                    "Auto-built from email gateway HSBC balance extraction",
                    created_at,
                ),
            )
            document_sections_inserted += 2

            line_items = _collect_hsbc_line_items(conn, document_id, attachment_path)
            section_lines = _extract_hsbc_transaction_section_lines(line_items, statement_date)
            groups = _iter_hsbc_transaction_groups(section_lines, statement_date)
            balance_index = 0
            txn_index = 0

            for txn_date, items in groups:
                texts = [str(item["text"]).strip() for item in items if str(item["text"]).strip()]
                if not texts:
                    continue

                desc_groups: list[list[str]] = []
                amounts: list[float] = []
                current_desc: list[str] = []
                for text in texts:
                    if _is_amount_line(text):
                        desc_groups.append(current_desc)
                        amounts.append(_parse_amount_value(text))
                        current_desc = []
                    else:
                        current_desc.append(text)
                non_empty_desc_groups = [group for group in desc_groups if group]
                if not non_empty_desc_groups or not amounts:
                    continue

                if len(non_empty_desc_groups) == 1 and len(amounts) == 1 and "CREDITINTEREST" in _compact_hsbc_marker_text(" | ".join(non_empty_desc_groups[0])):
                    if amount := amounts[0]:
                        if amount > 1000:
                            balance_index += 1
                            conn.execute(
                                """
                                INSERT INTO balance_markers (
                                    balance_marker_id, ledger_batch_id, document_id, section_id, institution_id,
                                    account_id, account_number_raw, account_name_raw, account_type_raw,
                                    product_type_raw, marker_date, currency, balance, balance_hkd_equivalent,
                                    marker_role, description_raw, source_file, source_page, source_line_ref,
                                    confidence, created_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    f"bm_{document_id}_{balance_index}",
                                    "email_gateway_hsbc_transactions",
                                    document_id,
                                    summary_section_id,
                                    "hsbc_hk",
                                    account_id,
                                    account_masked,
                                    "HSBC Hong Kong HKD Savings",
                                    "HKD Savings",
                                    "statement_savings",
                                    txn_date,
                                    "HKD",
                                    amount,
                                    amount,
                                    "running_balance",
                                    "CREDIT INTEREST",
                                    row["filename"],
                                    *_build_hsbc_source_line_ref(items),
                                    1.0,
                                    created_at,
                                ),
                            )
                            balance_markers_inserted += 1
                            continue

                trailing_balance = amounts[len(non_empty_desc_groups)] if len(amounts) > len(non_empty_desc_groups) else None
                source_page, source_line_ref = _build_hsbc_source_line_ref(items)

                if non_empty_desc_groups == [["B/F BALANCE"]]:
                    balance_index += 1
                    marker_role = "brought_forward_balance" if balance_index == 1 else "running_balance"
                    opening_balance = amounts[0]
                    conn.execute(
                        """
                        INSERT INTO balance_markers (
                            balance_marker_id, ledger_batch_id, document_id, section_id, institution_id,
                            account_id, account_number_raw, account_name_raw, account_type_raw,
                            product_type_raw, marker_date, currency, balance, balance_hkd_equivalent,
                            marker_role, description_raw, source_file, source_page, source_line_ref,
                            confidence, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"bm_{document_id}_{balance_index}",
                            "email_gateway_hsbc_transactions",
                            document_id,
                            summary_section_id,
                            "hsbc_hk",
                            account_id,
                            account_masked,
                            "HSBC Hong Kong HKD Savings",
                            "HKD Savings",
                            "statement_savings",
                            txn_date,
                            "HKD",
                            opening_balance,
                            opening_balance,
                            marker_role,
                            "B/F BALANCE",
                            row["filename"],
                            source_page,
                            source_line_ref,
                            1.0,
                            created_at,
                        ),
                    )
                    balance_markers_inserted += 1
                    continue

                for index_within_date, desc_lines in enumerate(non_empty_desc_groups, start=1):
                    amount = amounts[index_within_date - 1]
                    balance = trailing_balance if index_within_date == len(non_empty_desc_groups) else None
                    direction = _infer_hsbc_direction(desc_lines)
                    if direction is None:
                        continue

                    description_raw = " | ".join(desc_lines)
                    reference_no = _extract_reference_no(desc_lines)
                    counterparty = _extract_counterparty(desc_lines)
                    channel = _infer_hsbc_channel(desc_lines)
                    dedupe_key = _build_hsbc_dedupe_key(account_masked, txn_date, direction, amount, description_raw, reference_no)
                    existing = conn.execute(
                        "SELECT 1 FROM transactions WHERE dedupe_key = ?",
                        (dedupe_key,),
                    ).fetchone()
                    if existing is not None:
                        duplicate_transactions_skipped += 1
                        if balance is not None:
                            balance_index += 1
                            conn.execute(
                                """
                                INSERT INTO balance_markers (
                                    balance_marker_id, ledger_batch_id, document_id, section_id, institution_id,
                                    account_id, account_number_raw, account_name_raw, account_type_raw,
                                    product_type_raw, marker_date, currency, balance, balance_hkd_equivalent,
                                    marker_role, description_raw, source_file, source_page, source_line_ref,
                                    confidence, created_at
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    f"bm_{document_id}_{balance_index}",
                                    "email_gateway_hsbc_transactions",
                                    document_id,
                                    summary_section_id,
                                    "hsbc_hk",
                                    account_id,
                                    account_masked,
                                    "HSBC Hong Kong HKD Savings",
                                    "HKD Savings",
                                    "statement_savings",
                                    txn_date,
                                    "HKD",
                                    balance,
                                    balance,
                                    "running_balance",
                                    description_raw,
                                    row["filename"],
                                    source_page,
                                    source_line_ref,
                                    1.0,
                                    created_at,
                                ),
                            )
                            balance_markers_inserted += 1
                        continue

                    txn_index += 1
                    amount_signed = amount if direction == "credit" else -amount
                    conn.execute(
                        """
                        INSERT INTO transactions (
                            transaction_id, ledger_batch_id, document_id, section_id, document_fact_id,
                            institution_id, account_id, account_number_raw, account_name_raw,
                            account_type_raw, product_type_raw, txn_date, posting_date, value_date,
                            trade_date, settlement_date, effective_date, currency, amount, direction,
                            amount_signed, base_currency, fx_rate_to_base, amount_in_base_currency,
                            description_raw, description_clean, reference_no, bank_reference,
                            external_reference, counterparty_raw, counterparty_clean,
                            counterparty_name_raw, counterparty_name_clean, counterparty_account_masked,
                            counterparty_bank_name, channel, payment_rail, balance, balance_currency,
                            balance_hkd_equivalent, balance_source, continuity_check_status, txn_type,
                            category, tag, business_purpose, accounting_subject, source_file,
                            source_page, source_line_ref, source_extraction_method, confidence,
                            needs_review, review_reason, dedupe_key, canonical_hash, record_status,
                            source_record_type, created_at, updated_at, approved_at, approved_by
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, 0, NULL, ?, ?, 'active', 'statement_txn', ?, NULL, NULL, NULL)
                        """,
                        (
                            f"txn_{document_id}_{txn_index}",
                            "email_gateway_hsbc_transactions",
                            document_id,
                            activity_section_id,
                            document_fact_id,
                            "hsbc_hk",
                            account_id,
                            account_masked,
                            "HSBC Hong Kong HKD Savings",
                            "HKD Savings",
                            "statement_savings",
                            txn_date,
                            txn_date,
                            txn_date,
                            "HKD",
                            amount,
                            direction,
                            amount_signed,
                            "HKD",
                            amount_signed,
                            description_raw,
                            description_raw,
                            reference_no,
                            reference_no,
                            counterparty,
                            counterparty,
                            counterparty,
                            counterparty,
                            channel,
                            channel,
                            balance,
                            "HKD" if balance is not None else None,
                            balance,
                            "statement_running_balance" if balance is not None else None,
                            "explicit" if balance is not None else None,
                            row["filename"],
                            source_page,
                            source_line_ref,
                            f"email_gateway_hsbc_{items[0]['source']}",
                            1.0,
                            dedupe_key,
                            dedupe_key,
                            created_at,
                        ),
                    )
                    transactions_inserted += 1

                    if balance is not None:
                        balance_index += 1
                        conn.execute(
                            """
                            INSERT INTO balance_markers (
                                balance_marker_id, ledger_batch_id, document_id, section_id, institution_id,
                                account_id, account_number_raw, account_name_raw, account_type_raw,
                                product_type_raw, marker_date, currency, balance, balance_hkd_equivalent,
                                marker_role, description_raw, source_file, source_page, source_line_ref,
                                confidence, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                f"bm_{document_id}_{balance_index}",
                                "email_gateway_hsbc_transactions",
                                document_id,
                                summary_section_id,
                                "hsbc_hk",
                                account_id,
                                account_masked,
                                "HSBC Hong Kong HKD Savings",
                                "HKD Savings",
                                "statement_savings",
                                txn_date,
                                "HKD",
                                balance,
                                balance,
                                "running_balance",
                                description_raw,
                                row["filename"],
                                source_page,
                                source_line_ref,
                                1.0,
                                created_at,
                            ),
                        )
                        balance_markers_inserted += 1

        conn.commit()
        return {
            "documents_scanned": documents_scanned,
            "transactions_inserted": transactions_inserted,
            "balance_markers_inserted": balance_markers_inserted,
            "document_sections_inserted": document_sections_inserted,
            "duplicate_transactions_skipped": duplicate_transactions_skipped,
        }
    finally:
        conn.close()





def build_hang_seng_transactions_from_ingested_email_documents(
    *,
    db_path: str | Path,
    only_facts_built: bool = True,
) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        query = (
            """
            SELECT d.document_id, d.filename, d.processing_status,
                   a.email_id, a.stored_path,
                   df.document_fact_id, df.account_masked_raw, df.statement_date
            FROM documents d
            JOIN email_attachments a ON a.attachment_id = d.attachment_id
            JOIN document_facts df ON df.document_id = d.document_id
            WHERE d.source_type = 'email_attachment'
              AND d.institution_id = 'hang_seng'
            """
        )
        if only_facts_built:
            query += " AND d.processing_status = 'document_facts_built'"
        query += " ORDER BY df.statement_date, d.document_id"
        rows = conn.execute(query).fetchall()

        created_at = now_iso()
        documents_scanned = 0
        transactions_inserted = 0
        balance_markers_inserted = 0
        document_sections_inserted = 0
        duplicate_transactions_skipped = 0

        for row in rows:
            documents_scanned += 1
            document_id = str(row["document_id"])
            statement_date = row["statement_date"]
            document_fact_id = row["document_fact_id"]
            raw_rows = conn.execute(
                "SELECT page_no, line_no, raw_text FROM raw_document_lines WHERE document_id = ? ORDER BY page_no, line_no",
                (document_id,),
            ).fetchall()
            line_items = [
                {
                    "page_no": int(raw_row["page_no"]),
                    "line_no": int(raw_row["line_no"]),
                    "text": _normalize_ocr_line_text(str(raw_row["raw_text"])),
                    "source": "raw",
                }
                for raw_row in raw_rows
                if str(raw_row["raw_text"]).strip()
            ]
            account_masked = _parse_hang_seng_account_number(line_items, row["account_masked_raw"])

            conn.execute("DELETE FROM transactions WHERE document_id = ?", (document_id,))
            conn.execute("DELETE FROM balance_markers WHERE document_id = ?", (document_id,))
            conn.execute("DELETE FROM document_sections WHERE document_id = ?", (document_id,))

            current_currency = "HKD"
            current_profile = _ensure_hang_seng_seed_records(conn, account_masked, created_at, current_currency)
            current_activity_section_id: str | None = None
            current_summary_section_id: str | None = None
            inserted_activity_sections: set[str] = set()
            inserted_summary_sections: set[str] = set()
            next_activity_order = 1
            next_summary_order = 100
            marker_index = 0
            txn_index = 0
            previous_balance_by_currency: dict[str, float] = {}

            def ensure_sections(profile: dict[str, str], section_label: str) -> tuple[str, str]:
                nonlocal document_sections_inserted, next_activity_order, next_summary_order
                section_key = f"{profile['account_key']}_{profile['base_currency'].lower()}"
                activity_section_id = f"sec_email_hang_seng_{section_key}_{statement_date}_{document_id}_cash_activity"
                summary_section_id = f"sec_email_hang_seng_{section_key}_{statement_date}_{document_id}_balance_summary"
                if activity_section_id not in inserted_activity_sections:
                    conn.execute(
                        """
                        INSERT INTO document_sections (
                            section_id, document_id, parent_section_id, section_type, section_label_raw,
                            account_id, account_number_raw, account_name_raw, account_type_raw,
                            product_type_raw, currency, page_start, page_end, section_order,
                            confidence, notes, created_at
                        ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)
                        """,
                        (
                            activity_section_id,
                            document_id,
                            "cash_account_activity",
                            f"Hang Seng {profile['base_currency']} {section_label} Transaction History",
                            profile["account_id"],
                            profile["account_masked"],
                            profile["account_name"],
                            profile["account_type"],
                            profile["product_type"],
                            profile["base_currency"],
                            next_activity_order,
                            1.0,
                            "Auto-built from email gateway Hang Seng transaction extraction",
                            created_at,
                        ),
                    )
                    inserted_activity_sections.add(activity_section_id)
                    next_activity_order += 1
                    document_sections_inserted += 1
                if summary_section_id not in inserted_summary_sections:
                    conn.execute(
                        """
                        INSERT INTO document_sections (
                            section_id, document_id, parent_section_id, section_type, section_label_raw,
                            account_id, account_number_raw, account_name_raw, account_type_raw,
                            product_type_raw, currency, page_start, page_end, section_order,
                            confidence, notes, created_at
                        ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)
                        """,
                        (
                            summary_section_id,
                            document_id,
                            "cash_balance_summary",
                            f"Hang Seng {profile['base_currency']} {section_label} Balance Summary",
                            profile["account_id"],
                            profile["account_masked"],
                            profile["account_name"],
                            profile["account_type"],
                            profile["product_type"],
                            profile["base_currency"],
                            next_summary_order,
                            1.0,
                            "Auto-built from email gateway Hang Seng balance extraction",
                            created_at,
                        ),
                    )
                    inserted_summary_sections.add(summary_section_id)
                    next_summary_order += 1
                    document_sections_inserted += 1
                return activity_section_id, summary_section_id

            def insert_balance_marker(
                *,
                profile: dict[str, str],
                section_id: str,
                marker_date: str,
                balance: float,
                marker_role: str,
                description_raw: str,
                source_page: int | None,
                source_line_ref: str | None,
            ) -> None:
                nonlocal marker_index, balance_markers_inserted
                marker_index += 1
                balance_hkd_equivalent = balance if profile["base_currency"] == "HKD" else None
                conn.execute(
                    """
                    INSERT INTO balance_markers (
                        balance_marker_id, ledger_batch_id, document_id, section_id, institution_id,
                        account_id, account_number_raw, account_name_raw, account_type_raw,
                        product_type_raw, marker_date, currency, balance, balance_hkd_equivalent,
                        marker_role, description_raw, source_file, source_page, source_line_ref,
                        confidence, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"bm_{document_id}_{marker_index}",
                        "email_gateway_hang_seng_transactions",
                        document_id,
                        section_id,
                        "hang_seng",
                        profile["account_id"],
                        profile["account_masked"],
                        profile["account_name"],
                        profile["account_type"],
                        profile["product_type"],
                        marker_date,
                        profile["base_currency"],
                        balance,
                        balance_hkd_equivalent,
                        marker_role,
                        description_raw,
                        row["filename"],
                        source_page,
                        source_line_ref,
                        1.0,
                        created_at,
                    ),
                )
                balance_markers_inserted += 1
                previous_balance_by_currency[profile["base_currency"]] = balance

            section_lines = _extract_hang_seng_transaction_section_lines(line_items)
            pending_items: list[dict[str, object]] = []
            pending_date: str | None = None
            pending_remainder: str | None = None
            section_label = "Statement Savings"

            def flush_pending() -> None:
                nonlocal pending_items, pending_date, pending_remainder, txn_index, transactions_inserted, duplicate_transactions_skipped
                if not pending_items or pending_date is None or pending_remainder is None or current_activity_section_id is None or current_summary_section_id is None:
                    pending_items = []
                    pending_date = None
                    pending_remainder = None
                    return
                payload_lines = [pending_remainder] + [str(item["text"]).strip() for item in pending_items[1:]]
                source_page, source_line_ref = _build_hsbc_source_line_ref(pending_items)
                desc_lines, amount, balance, description_raw = _parse_hang_seng_amount_columns(payload_lines)
                amount_tokens = re.findall(r"\d{1,3}(?:,\d{3})*\.\d{2}", " ".join(payload_lines))
                if description_raw:
                    upper_desc = description_raw.upper()
                else:
                    upper_desc = " ".join(payload_lines).upper()
                if upper_desc.startswith("BALANCE B/F") or upper_desc.startswith("BALANCE C/F"):
                    marker_balance = balance if balance is not None else (_parse_amount_value(amount_tokens[-1]) if amount_tokens else None)
                    if marker_balance is None:
                        pending_items = []
                        pending_date = None
                        pending_remainder = None
                        return
                    marker_description = description_raw or re.sub(r"\s*\d{1,3}(?:,\d{3})*\.\d{2}\s*$", "", " ".join(payload_lines)).strip()
                    role = "brought_forward_balance" if upper_desc.startswith("BALANCE B/F") else "closing_balance"
                    insert_balance_marker(
                        profile=current_profile,
                        section_id=current_summary_section_id,
                        marker_date=pending_date,
                        balance=marker_balance,
                        marker_role=role,
                        description_raw=marker_description,
                        source_page=source_page,
                        source_line_ref=source_line_ref,
                    )
                    pending_items = []
                    pending_date = None
                    pending_remainder = None
                    return
                if amount is None or balance is None or not description_raw:
                    pending_items = []
                    pending_date = None
                    pending_remainder = None
                    return
                if current_profile["base_currency"] != "HKD":
                    pending_items = []
                    pending_date = None
                    pending_remainder = None
                    return
                previous_balance = previous_balance_by_currency.get(current_profile["base_currency"])
                direction = _infer_hang_seng_direction(description_raw, previous_balance, amount, balance)
                if direction is None:
                    pending_items = []
                    pending_date = None
                    pending_remainder = None
                    return
                channel = _infer_hang_seng_channel(description_raw)
                dedupe_key = _build_hang_seng_dedupe_key(
                    current_profile["account_masked"],
                    pending_date,
                    current_profile["base_currency"],
                    direction,
                    amount,
                    description_raw,
                    balance,
                )
                existing = conn.execute("SELECT 1 FROM transactions WHERE dedupe_key = ?", (dedupe_key,)).fetchone()
                if existing is not None:
                    duplicate_transactions_skipped += 1
                    insert_balance_marker(
                        profile=current_profile,
                        section_id=current_summary_section_id,
                        marker_date=pending_date,
                        balance=balance,
                        marker_role="running_balance",
                        description_raw=description_raw,
                        source_page=source_page,
                        source_line_ref=source_line_ref,
                    )
                    pending_items = []
                    pending_date = None
                    pending_remainder = None
                    return
                txn_index += 1
                amount_signed = amount if direction == "credit" else -amount
                amount_in_base_currency = amount_signed if current_profile["base_currency"] == "HKD" else None
                conn.execute(
                    """
                    INSERT INTO transactions (
                        transaction_id, ledger_batch_id, document_id, section_id, document_fact_id,
                        institution_id, account_id, account_number_raw, account_name_raw,
                        account_type_raw, product_type_raw, txn_date, posting_date, value_date,
                        trade_date, settlement_date, effective_date, currency, amount, direction,
                        amount_signed, base_currency, fx_rate_to_base, amount_in_base_currency,
                        description_raw, description_clean, reference_no, bank_reference,
                        external_reference, counterparty_raw, counterparty_clean,
                        counterparty_name_raw, counterparty_name_clean, counterparty_account_masked,
                        counterparty_bank_name, channel, payment_rail, balance, balance_currency,
                        balance_hkd_equivalent, balance_source, continuity_check_status, txn_type,
                        category, tag, business_purpose, accounting_subject, source_file,
                        source_page, source_line_ref, source_extraction_method, confidence,
                        needs_review, review_reason, dedupe_key, canonical_hash, record_status,
                        source_record_type, created_at, updated_at, approved_at, approved_by
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, 0, NULL, ?, ?, 'active', 'statement_txn', ?, NULL, NULL, NULL)
                    """,
                    (
                        f"txn_{document_id}_{txn_index}",
                        "email_gateway_hang_seng_transactions",
                        document_id,
                        current_activity_section_id,
                        document_fact_id,
                        "hang_seng",
                        current_profile["account_id"],
                        current_profile["account_masked"],
                        current_profile["account_name"],
                        current_profile["account_type"],
                        current_profile["product_type"],
                        pending_date,
                        pending_date,
                        pending_date,
                        current_profile["base_currency"],
                        amount,
                        direction,
                        amount_signed,
                        "HKD",
                        amount_in_base_currency,
                        description_raw,
                        description_raw,
                        channel,
                        channel,
                        balance,
                        current_profile["base_currency"],
                        balance if current_profile["base_currency"] == "HKD" else None,
                        "statement_running_balance",
                        "explicit",
                        row["filename"],
                        source_page,
                        source_line_ref,
                        f"email_gateway_hang_seng_{pending_items[0].get('source', 'raw')}",
                        1.0,
                        dedupe_key,
                        dedupe_key,
                        created_at,
                    ),
                )
                transactions_inserted += 1
                previous_balance_by_currency[current_profile["base_currency"]] = balance
                pending_items = []
                pending_date = None
                pending_remainder = None

            for item in section_lines:
                text = str(item["text"]).strip()
                if not text:
                    continue
                if _is_hang_seng_section_header(text):
                    flush_pending()
                    current_currency = _extract_hang_seng_currency(text, current_currency)
                    current_profile = _ensure_hang_seng_seed_records(conn, account_masked, created_at, current_currency)
                    section_label = text
                    current_activity_section_id, current_summary_section_id = ensure_sections(current_profile, section_label)
                    continue
                if _is_hang_seng_table_header(text):
                    if text.upper().startswith("CCY"):
                        current_currency = _extract_hang_seng_currency(text, current_currency)
                        current_profile = _ensure_hang_seng_seed_records(conn, account_masked, created_at, current_currency)
                        current_activity_section_id, current_summary_section_id = ensure_sections(current_profile, section_label)
                    continue
                split = _split_scb_date_prefix(text, statement_date)
                if split is not None:
                    flush_pending()
                    pending_date, pending_remainder = split
                    pending_items = [item]
                    if current_activity_section_id is None or current_summary_section_id is None:
                        current_activity_section_id, current_summary_section_id = ensure_sections(current_profile, section_label)
                    continue
                if pending_items:
                    pending_items.append(item)
            flush_pending()

        conn.commit()
        return {
            "documents_scanned": documents_scanned,
            "transactions_inserted": transactions_inserted,
            "balance_markers_inserted": balance_markers_inserted,
            "document_sections_inserted": document_sections_inserted,
            "duplicate_transactions_skipped": duplicate_transactions_skipped,
        }
    finally:
        conn.close()


def build_scb_transactions_from_ingested_email_documents(
    *,
    db_path: str | Path,
    only_facts_built: bool = True,
) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        query = (
            """
            SELECT d.document_id, d.filename, d.processing_status,
                   a.email_id,
                   df.document_fact_id, df.account_masked_raw, df.statement_date
            FROM documents d
            JOIN email_attachments a ON a.attachment_id = d.attachment_id
            JOIN document_facts df ON df.document_id = d.document_id
            WHERE d.source_type = 'email_attachment'
              AND d.institution_id = 'scb_hk'
            """
        )
        if only_facts_built:
            query += " AND d.processing_status = 'document_facts_built'"
        query += " ORDER BY df.statement_date, d.document_id"
        rows = conn.execute(query).fetchall()

        created_at = now_iso()
        documents_scanned = 0
        transactions_inserted = 0
        balance_markers_inserted = 0
        document_sections_inserted = 0
        duplicate_transactions_skipped = 0

        for row in rows:
            documents_scanned += 1
            document_id = str(row["document_id"])
            account_masked = row["account_masked_raw"] or "562-8-582826-1"
            statement_date = row["statement_date"]
            document_fact_id = row["document_fact_id"]

            conn.execute("DELETE FROM transactions WHERE document_id = ?", (document_id,))
            conn.execute("DELETE FROM balance_markers WHERE document_id = ?", (document_id,))
            conn.execute("DELETE FROM document_sections WHERE document_id = ?", (document_id,))

            default_profile = dict(_ensure_scb_seed_records(conn, account_masked, created_at))
            raw_rows = conn.execute(
                "SELECT page_no, line_no, raw_text FROM raw_document_lines WHERE document_id = ? ORDER BY page_no, line_no",
                (document_id,),
            ).fetchall()
            line_items = [
                {
                    "page_no": int(raw_row["page_no"]),
                    "line_no": int(raw_row["line_no"]),
                    "text": _normalize_ocr_line_text(str(raw_row["raw_text"])),
                }
                for raw_row in raw_rows
                if str(raw_row["raw_text"]).strip()
            ]

            activity_lines = _extract_scb_activity_section_lines(line_items)
            current_profile: dict[str, str] = dict(default_profile)
            inserted_activity_sections: set[str] = set()
            inserted_summary_sections: set[str] = set()
            next_activity_order = 1
            next_summary_order = 100
            txn_index = 0
            marker_index = 0
            current_opening_balance: float | None = None

            def ensure_activity_section(profile: dict[str, str]) -> str:
                nonlocal document_sections_inserted, next_activity_order
                section_id = f"sec_email_scb_{profile['account_key']}_{statement_date}_{profile['account_kind']}_cash_activity"
                if section_id in inserted_activity_sections:
                    return section_id
                conn.execute(
                    """
                    INSERT INTO document_sections (
                        section_id, document_id, parent_section_id, section_type, section_label_raw,
                        account_id, account_number_raw, account_name_raw, account_type_raw,
                        product_type_raw, currency, page_start, page_end, section_order,
                        confidence, notes, created_at
                    ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)
                    """,
                    (
                        section_id,
                        document_id,
                        "cash_account_activity",
                        f"SCB {profile['account_label']} Cash Activity",
                        profile["account_id"],
                        profile["account_masked"],
                        profile["account_name"],
                        profile["account_type"],
                        profile["product_type"],
                        profile["base_currency"],
                        next_activity_order,
                        1.0,
                        "Auto-built from email gateway SCB transaction extraction",
                        created_at,
                    ),
                )
                inserted_activity_sections.add(section_id)
                document_sections_inserted += 1
                next_activity_order += 1
                return section_id

            def ensure_summary_section(profile: dict[str, str]) -> str:
                nonlocal document_sections_inserted, next_summary_order
                section_id = f"sec_email_scb_{profile['account_key']}_{statement_date}_{profile['account_kind']}_balance_summary"
                if section_id in inserted_summary_sections:
                    return section_id
                section_currency = "multi" if profile["account_kind"] == "savings" else profile["base_currency"]
                conn.execute(
                    """
                    INSERT INTO document_sections (
                        section_id, document_id, parent_section_id, section_type, section_label_raw,
                        account_id, account_number_raw, account_name_raw, account_type_raw,
                        product_type_raw, currency, page_start, page_end, section_order,
                        confidence, notes, created_at
                    ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)
                    """,
                    (
                        section_id,
                        document_id,
                        "cash_balance_summary",
                        f"SCB {profile['account_label']} Balance Summary",
                        profile["account_id"],
                        profile["account_masked"],
                        profile["account_name"],
                        profile["account_type"],
                        profile["product_type"],
                        section_currency,
                        next_summary_order,
                        1.0,
                        "Auto-built from email gateway SCB balance extraction",
                        created_at,
                    ),
                )
                inserted_summary_sections.add(section_id)
                document_sections_inserted += 1
                next_summary_order += 1
                return section_id

            dated_items: list[dict[str, object]] = []
            current_group: dict[str, object] | None = None
            pending_currency_heading: str | None = None
            pending_prefix_lines: list[str] = []
            pending_prefix_page_no: int | None = None
            pending_prefix_line_no: int | None = None
            for item in activity_lines:
                text = str(item["text"]).strip()
                header = _parse_scb_account_header(text)
                if header is not None:
                    if current_group is not None:
                        dated_items.append(current_group)
                        current_group = None
                    current_profile = dict(
                        _ensure_scb_seed_records(
                            conn,
                            header["account_masked"],
                            created_at,
                            account_kind=header["account_kind"],
                        )
                    )
                    current_opening_balance = None
                    pending_currency_heading = None
                    pending_prefix_lines = []
                    pending_prefix_page_no = None
                    pending_prefix_line_no = None
                    continue

                account_kind = _parse_scb_account_kind_fragment(text)
                if account_kind is not None:
                    if current_group is not None:
                        dated_items.append(current_group)
                        current_group = None
                    pending_profile = dict(_build_scb_account_profile(account_kind, current_profile.get("account_masked")))
                    account_number = _parse_scb_account_number_line(text)
                    if account_number is not None:
                        current_profile = dict(
                            _ensure_scb_seed_records(
                                conn,
                                account_number,
                                created_at,
                                account_kind=account_kind,
                            )
                        )
                    else:
                        current_profile = pending_profile
                    current_opening_balance = None
                    pending_currency_heading = None
                    pending_prefix_lines = []
                    pending_prefix_page_no = None
                    pending_prefix_line_no = None
                    continue

                account_number = _parse_scb_account_number_line(text)
                if account_number is not None:
                    current_profile = dict(
                        _ensure_scb_seed_records(
                            conn,
                            account_number,
                            created_at,
                            account_kind=current_profile["account_kind"],
                        )
                    )
                    current_opening_balance = None
                    pending_currency_heading = None
                    pending_prefix_lines = []
                    pending_prefix_page_no = None
                    pending_prefix_line_no = None
                    continue

                if _is_scb_table_header_line(text):
                    continue
                if current_group is None and _is_scb_preamble_noise_line(text):
                    continue

                currency_heading = _extract_scb_currency_heading(text)
                if current_group is None and currency_heading is not None:
                    pending_currency_heading = currency_heading
                    if pending_prefix_page_no is None:
                        pending_prefix_page_no = int(item["page_no"])
                        pending_prefix_line_no = int(item["line_no"])
                    continue

                split = _split_scb_date_prefix(text, statement_date)
                if split is not None:
                    if current_group is not None:
                        dated_items.append(current_group)
                    txn_date, remainder = split
                    payload_lines = [remainder] if remainder else []
                    current_group = {
                        "page_no": pending_prefix_page_no if pending_prefix_page_no is not None else item["page_no"],
                        "line_no": pending_prefix_line_no if pending_prefix_line_no is not None else item["line_no"],
                        "txn_date": txn_date,
                        "payload_lines": [*pending_prefix_lines, *payload_lines],
                        "profile": dict(current_profile),
                    }
                    if pending_currency_heading is not None:
                        current_group["currency_hint"] = pending_currency_heading
                        pending_currency_heading = None
                    pending_prefix_lines = []
                    pending_prefix_page_no = None
                    pending_prefix_line_no = None
                    continue

                if current_group is not None:
                    payload_lines = current_group.setdefault("payload_lines", [])
                    assert isinstance(payload_lines, list)
                    if (
                        payload_lines
                        and str(payload_lines[0]).strip().upper().startswith("CLOSING BALANCE")
                        and _is_scb_currency_heading_line(text)
                        and any(_is_amount_line(str(value)) for value in payload_lines)
                    ):
                        continue
                    payload_lines.append(text)
                else:
                    if pending_prefix_page_no is None:
                        pending_prefix_page_no = int(item["page_no"])
                        pending_prefix_line_no = int(item["line_no"])
                    pending_prefix_lines.append(text)

            if current_group is not None:
                dated_items.append(current_group)

            for item in dated_items:
                txn_date = str(item["txn_date"])
                payload_lines = [str(value).strip() for value in (item.get("payload_lines") or []) if str(value).strip()]
                payload = " ".join(payload_lines)
                profile = dict(item["profile"])
                currency_hint = item.get("currency_hint")
                source_page = int(item["page_no"])
                source_line_ref = f"{source_page}:{item['line_no']}"

                leading_opening_balance, txn_payload_lines = _extract_scb_leading_opening_balance(payload_lines)
                if leading_opening_balance is not None:
                    current_opening_balance = leading_opening_balance
                    summary_section_id = ensure_summary_section(profile)
                    opening_currency = (str(currency_hint).upper() if currency_hint else _infer_scb_balance_currency(payload_lines, profile, leading_opening_balance))
                    marker_index += 1
                    conn.execute(
                        """
                        INSERT INTO balance_markers (
                            balance_marker_id, ledger_batch_id, document_id, section_id, institution_id,
                            account_id, account_number_raw, account_name_raw, account_type_raw,
                            product_type_raw, marker_date, currency, balance, balance_hkd_equivalent,
                            marker_role, description_raw, source_file, source_page, source_line_ref,
                            confidence, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"bm_{document_id}_{marker_index}",
                            "email_gateway_scb_transactions",
                            document_id,
                            summary_section_id,
                            "scb_hk",
                            profile["account_id"],
                            profile["account_masked"],
                            profile["account_name"],
                            profile["account_type"],
                            profile["product_type"],
                            txn_date,
                            opening_currency,
                            leading_opening_balance,
                            leading_opening_balance if opening_currency == "HKD" else None,
                            "opening_balance",
                            f"{profile['account_label']} {opening_currency} brought forward balance",
                            row["filename"],
                            source_page,
                            source_line_ref,
                            1.0,
                            created_at,
                        ),
                    )
                    balance_markers_inserted += 1
                    payload_lines = txn_payload_lines
                    payload = " ".join(payload_lines)

                explicit_closing_currency, leading_closing_balance, txn_payload_lines = _extract_scb_leading_closing_balance(payload_lines)
                if leading_closing_balance is not None and not txn_payload_lines:
                    if explicit_closing_currency:
                        closing_currency = explicit_closing_currency.upper()
                    elif currency_hint:
                        closing_currency = str(currency_hint).upper()
                    else:
                        closing_currency = _infer_scb_balance_currency(payload_lines, profile, None).upper()
                    summary_section_id = ensure_summary_section(profile)
                    marker_index += 1
                    conn.execute(
                        """
                        INSERT INTO balance_markers (
                            balance_marker_id, ledger_batch_id, document_id, section_id, institution_id,
                            account_id, account_number_raw, account_name_raw, account_type_raw,
                            product_type_raw, marker_date, currency, balance, balance_hkd_equivalent,
                            marker_role, description_raw, source_file, source_page, source_line_ref,
                            confidence, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"bm_{document_id}_{marker_index}",
                            "email_gateway_scb_transactions",
                            document_id,
                            summary_section_id,
                            "scb_hk",
                            profile["account_id"],
                            profile["account_masked"],
                            profile["account_name"],
                            profile["account_type"],
                            profile["product_type"],
                            txn_date,
                            closing_currency,
                            leading_closing_balance,
                            leading_closing_balance if closing_currency == "HKD" else None,
                            "closing_balance",
                            f"{profile['account_label']} {closing_currency} closing balance",
                            row["filename"],
                            source_page,
                            source_line_ref,
                            1.0,
                            created_at,
                        ),
                    )
                    balance_markers_inserted += 1
                    current_opening_balance = leading_closing_balance
                    continue

                if not payload_lines:
                    continue

                opening_match = re.match(r"^BALANCE FROM PREVIOUS STATEMENT.*?(-?[\d,]+\.\d{2})$", payload, re.IGNORECASE)
                if opening_match:
                    opening_balance = _parse_amount_value(opening_match.group(1))
                    current_opening_balance = opening_balance
                    summary_section_id = ensure_summary_section(profile)
                    currency = (str(currency_hint).upper() if currency_hint else _infer_scb_balance_currency(payload_lines, profile, opening_balance))
                    marker_index += 1
                    conn.execute(
                        """
                        INSERT INTO balance_markers (
                            balance_marker_id, ledger_batch_id, document_id, section_id, institution_id,
                            account_id, account_number_raw, account_name_raw, account_type_raw,
                            product_type_raw, marker_date, currency, balance, balance_hkd_equivalent,
                            marker_role, description_raw, source_file, source_page, source_line_ref,
                            confidence, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"bm_{document_id}_{marker_index}",
                            "email_gateway_scb_transactions",
                            document_id,
                            summary_section_id,
                            "scb_hk",
                            profile["account_id"],
                            profile["account_masked"],
                            profile["account_name"],
                            profile["account_type"],
                            profile["product_type"],
                            txn_date,
                            currency,
                            opening_balance,
                            opening_balance if currency == "HKD" else None,
                            "opening_balance",
                            f"{profile['account_label']} {currency} brought forward balance",
                            row["filename"],
                            source_page,
                            source_line_ref,
                            1.0,
                            created_at,
                        ),
                    )
                    balance_markers_inserted += 1
                    continue

                closing_match = re.match(r"^CLOSING BALANCE(?:\s+(HKD|CNY|USD|EUR))?.*?(-?[\d,]+\.\d{2})$", payload, re.IGNORECASE)
                if closing_match:
                    explicit_closing_currency = closing_match.group(1)
                    if explicit_closing_currency:
                        closing_currency = explicit_closing_currency.upper()
                    elif currency_hint:
                        closing_currency = str(currency_hint).upper()
                    else:
                        closing_currency = _infer_scb_balance_currency(payload_lines, profile, None).upper()
                    closing_balance = _parse_amount_value(closing_match.group(2))
                    summary_section_id = ensure_summary_section(profile)
                    marker_index += 1
                    conn.execute(
                        """
                        INSERT INTO balance_markers (
                            balance_marker_id, ledger_batch_id, document_id, section_id, institution_id,
                            account_id, account_number_raw, account_name_raw, account_type_raw,
                            product_type_raw, marker_date, currency, balance, balance_hkd_equivalent,
                            marker_role, description_raw, source_file, source_page, source_line_ref,
                            confidence, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"bm_{document_id}_{marker_index}",
                            "email_gateway_scb_transactions",
                            document_id,
                            summary_section_id,
                            "scb_hk",
                            profile["account_id"],
                            profile["account_masked"],
                            profile["account_name"],
                            profile["account_type"],
                            profile["product_type"],
                            txn_date,
                            closing_currency,
                            closing_balance,
                            closing_balance if closing_currency == "HKD" else None,
                            "closing_balance",
                            f"{profile['account_label']} {closing_currency} closing balance",
                            row["filename"],
                            source_page,
                            source_line_ref,
                            1.0,
                            created_at,
                        ),
                    )
                    balance_markers_inserted += 1
                    continue

                txn_entries = _parse_scb_transaction_entries(payload_lines, current_opening_balance)
                if not txn_entries and payload:
                    txn_payload = _parse_scb_transaction_payload(payload)
                    if txn_payload is not None:
                        description_raw = _normalize_scb_description(txn_payload[0])
                        amount = txn_payload[1]
                        balance = txn_payload[2]
                        direction = _infer_scb_direction(
                            description_raw,
                            amount,
                            previous_balance=current_opening_balance,
                            balance=balance,
                        )
                        if direction is not None:
                            description_raw = _restore_scb_direction_marker(description_raw, direction)
                            txn_entries = [
                                {
                                    "description_raw": description_raw,
                                    "amount": amount,
                                    "balance": balance,
                                    "direction": direction,
                                }
                            ]
                if not txn_entries:
                    continue

                for entry in txn_entries:
                    description_raw = str(entry["description_raw"])
                    amount = abs(float(entry["amount"]))
                    balance = float(entry["balance"])
                    direction = str(entry["direction"])
                    amount_signed = amount if direction == "credit" else -amount
                    channel = _infer_scb_channel(description_raw)
                    bank_reference = _build_scb_bank_reference(txn_date, description_raw)
                    dedupe_key = _build_scb_dedupe_key(
                        profile["account_masked"],
                        txn_date,
                        direction,
                        amount,
                        description_raw,
                        bank_reference,
                        balance,
                    )
                    existing = conn.execute(
                        "SELECT 1 FROM transactions WHERE dedupe_key = ?",
                        (dedupe_key,),
                    ).fetchone()
                    if existing is not None:
                        duplicate_transactions_skipped += 1
                        continue

                    activity_section_id = ensure_activity_section(profile)
                    txn_index += 1
                    txn_type = "interest_credit" if description_raw.upper().startswith("CREDIT INTEREST") else None
                    currency = profile["base_currency"]
                    conn.execute(
                        """
                        INSERT INTO transactions (
                            transaction_id,
                            ledger_batch_id,
                            document_id,
                            section_id,
                            document_fact_id,
                            institution_id,
                            account_id,
                            account_number_raw,
                            account_name_raw,
                            account_type_raw,
                            product_type_raw,
                            txn_date,
                            posting_date,
                            effective_date,
                            currency,
                            amount,
                            direction,
                            amount_signed,
                            base_currency,
                            amount_in_base_currency,
                            description_raw,
                            description_clean,
                            reference_no,
                            bank_reference,
                            channel,
                            payment_rail,
                            balance,
                            balance_currency,
                            balance_hkd_equivalent,
                            balance_source,
                            continuity_check_status,
                            txn_type,
                            source_file,
                            source_page,
                            source_line_ref,
                            source_extraction_method,
                            confidence,
                            dedupe_key,
                            canonical_hash,
                            created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"txn_{document_id}_{txn_index}",
                            "email_gateway_scb_transactions",
                            document_id,
                            activity_section_id,
                            document_fact_id,
                            "scb_hk",
                            profile["account_id"],
                            profile["account_masked"],
                            profile["account_name"],
                            profile["account_type"],
                            profile["product_type"],
                            txn_date,
                            txn_date,
                            txn_date,
                            currency,
                            amount,
                            direction,
                            amount_signed,
                            currency,
                            amount_signed if currency == "HKD" else None,
                            description_raw,
                            description_raw,
                            bank_reference,
                            bank_reference,
                            channel,
                            channel,
                            balance,
                            currency,
                            balance if currency == "HKD" else None,
                            "statement_running_balance" if balance is not None else None,
                            "explicit" if balance is not None else None,
                            txn_type,
                            row["filename"],
                            source_page,
                            source_line_ref,
                            "email_gateway_raw_text",
                            1.0,
                            dedupe_key,
                            dedupe_key,
                            created_at,
                        ),
                    )
                    transactions_inserted += 1
                    current_opening_balance = balance

        conn.commit()
        return {
            "documents_scanned": documents_scanned,
            "transactions_inserted": transactions_inserted,
            "balance_markers_inserted": balance_markers_inserted,
            "document_sections_inserted": document_sections_inserted,
            "duplicate_transactions_skipped": duplicate_transactions_skipped,
        }
    finally:
        conn.close()

def build_ant_bank_transactions_from_ingested_email_documents(
    *,
    db_path: str | Path,
    only_facts_built: bool = True,
) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        query = (
            """
            SELECT d.document_id, d.filename, d.processing_status,
                   a.email_id,
                   df.document_fact_id, df.account_masked_raw, df.statement_date
            FROM documents d
            JOIN email_attachments a ON a.attachment_id = d.attachment_id
            JOIN document_facts df ON df.document_id = d.document_id
            WHERE d.source_type = 'email_attachment'
              AND d.institution_id = 'ant_bank'
            """
        )
        if only_facts_built:
            query += " AND d.processing_status = 'document_facts_built'"
        query += " ORDER BY df.statement_date, d.document_id"
        rows = conn.execute(query).fetchall()

        created_at = now_iso()
        documents_scanned = 0
        transactions_inserted = 0
        balance_markers_inserted = 0
        document_sections_inserted = 0
        duplicate_transactions_skipped = 0

        for row in rows:
            documents_scanned += 1
            document_id = str(row["document_id"])
            account_bundle = row["account_masked_raw"]
            account_masked = _parse_ant_primary_account(account_bundle)
            statement_date = row["statement_date"]
            document_fact_id = row["document_fact_id"]
            account_id = _ensure_ant_seed_records(conn, account_masked, created_at)
            account_key = _normalize_statement_account_key(account_masked)
            activity_section_id = f"sec_email_ant_{account_key}_{statement_date}_cash_activity"
            summary_section_id = f"sec_email_ant_{account_key}_{statement_date}_balance_summary"

            conn.execute("DELETE FROM transactions WHERE document_id = ?", (document_id,))
            conn.execute("DELETE FROM balance_markers WHERE document_id = ?", (document_id,))
            conn.execute("DELETE FROM document_sections WHERE document_id = ?", (document_id,))

            conn.execute(
                """
                INSERT INTO document_sections (
                    section_id, document_id, parent_section_id, section_type, section_label_raw,
                    account_id, account_number_raw, account_name_raw, account_type_raw,
                    product_type_raw, currency, page_start, page_end, section_order,
                    confidence, notes, created_at
                ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)
                """,
                (
                    activity_section_id,
                    document_id,
                    "cash_account_activity",
                    "Ant Bank Libra Savings Cash Activity",
                    account_id,
                    account_masked,
                    "Ant Bank Libra Savings HKD",
                    "Libra Savings Account",
                    "digital_bank_libra_savings",
                    "HKD",
                    1,
                    1.0,
                    "Auto-built from email gateway Ant Bank transaction extraction",
                    created_at,
                ),
            )
            conn.execute(
                """
                INSERT INTO document_sections (
                    section_id, document_id, parent_section_id, section_type, section_label_raw,
                    account_id, account_number_raw, account_name_raw, account_type_raw,
                    product_type_raw, currency, page_start, page_end, section_order,
                    confidence, notes, created_at
                ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)
                """,
                (
                    summary_section_id,
                    document_id,
                    "cash_balance_summary",
                    "Ant Bank Libra Savings Balance Summary",
                    account_id,
                    account_masked,
                    "Ant Bank Libra Savings HKD",
                    "Libra Savings Account",
                    "digital_bank_libra_savings",
                    "HKD",
                    2,
                    1.0,
                    "Auto-built from email gateway Ant Bank balance extraction",
                    created_at,
                ),
            )
            document_sections_inserted += 2

            raw_rows = conn.execute(
                "SELECT page_no, line_no, raw_text FROM raw_document_lines WHERE document_id = ? ORDER BY page_no, line_no",
                (document_id,),
            ).fetchall()
            line_items = [
                {
                    "page_no": int(raw_row["page_no"]),
                    "line_no": int(raw_row["line_no"]),
                    "text": _normalize_ocr_line_text(str(raw_row["raw_text"])),
                }
                for raw_row in raw_rows
                if str(raw_row["raw_text"]).strip()
            ]
            ant_groups = _group_ant_statement_lines(line_items)
            document_closing_summary = _extract_ant_document_closing_balance(line_items)

            opening_balance_inserted = False
            closing_balance_inserted = False
            document_closing_balance_inserted = False
            txn_index = 0
            marker_index = 0

            if document_closing_summary is not None:
                closing_balance, source_page, source_line_ref = document_closing_summary
                marker_index += 1
                conn.execute(
                    """
                    INSERT INTO balance_markers (
                        balance_marker_id, ledger_batch_id, document_id, section_id, institution_id,
                        account_id, account_number_raw, account_name_raw, account_type_raw,
                        product_type_raw, marker_date, currency, balance, balance_hkd_equivalent,
                        marker_role, description_raw, source_file, source_page, source_line_ref,
                        confidence, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"bm_{document_id}_{marker_index}",
                        "email_gateway_ant_bank_transactions",
                        document_id,
                        summary_section_id,
                        "ant_bank",
                        account_id,
                        account_masked,
                        "Ant Bank Libra Savings HKD",
                        "Libra Savings Account",
                        "digital_bank_libra_savings",
                        statement_date,
                        "HKD",
                        closing_balance,
                        closing_balance,
                        "closing_balance",
                        "Document-level HKD equivalent closing balance",
                        row["filename"],
                        source_page,
                        source_line_ref,
                        1.0,
                        created_at,
                    ),
                )
                balance_markers_inserted += 1
                document_closing_balance_inserted = True

            for group in ant_groups:
                txn_date = group.get("txn_date")
                payload = str(group.get("payload") or "").strip()
                items = list(group.get("items") or [])
                if not payload:
                    continue

                if txn_date is None:
                    continue

                source_page, source_line_ref = _build_hsbc_source_line_ref(items)
                upper_payload = _compact_ant_text(payload)

                if upper_payload.startswith("BALANCEBROUGHTFORWARD"):
                    parsed_tail = _parse_ant_amount_tail(payload)
                    if parsed_tail is None:
                        continue
                    _, opening_balance = parsed_tail
                    marker_index += 1
                    conn.execute(
                        """
                        INSERT INTO balance_markers (
                            balance_marker_id, ledger_batch_id, document_id, section_id, institution_id,
                            account_id, account_number_raw, account_name_raw, account_type_raw,
                            product_type_raw, marker_date, currency, balance, balance_hkd_equivalent,
                            marker_role, description_raw, source_file, source_page, source_line_ref,
                            confidence, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"bm_{document_id}_{marker_index}",
                            "email_gateway_ant_bank_transactions",
                            document_id,
                            summary_section_id,
                            "ant_bank",
                            account_id,
                            account_masked,
                            "Ant Bank Libra Savings HKD",
                            "Libra Savings Account",
                            "digital_bank_libra_savings",
                            txn_date,
                            "HKD",
                            opening_balance,
                            opening_balance,
                            "opening_balance",
                            "Libra Savings opening balance",
                            row["filename"],
                            source_page,
                            source_line_ref,
                            1.0,
                            created_at,
                        ),
                    )
                    balance_markers_inserted += 1
                    opening_balance_inserted = True
                    continue

                if upper_payload.startswith("CLOSINGBALANCE"):
                    parsed_tail = _parse_ant_amount_tail(payload)
                    if parsed_tail is None:
                        continue
                    _, closing_balance = parsed_tail
                    marker_index += 1
                    conn.execute(
                        """
                        INSERT INTO balance_markers (
                            balance_marker_id, ledger_batch_id, document_id, section_id, institution_id,
                            account_id, account_number_raw, account_name_raw, account_type_raw,
                            product_type_raw, marker_date, currency, balance, balance_hkd_equivalent,
                            marker_role, description_raw, source_file, source_page, source_line_ref,
                            confidence, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            f"bm_{document_id}_{marker_index}",
                            "email_gateway_ant_bank_transactions",
                            document_id,
                            summary_section_id,
                            "ant_bank",
                            account_id,
                            account_masked,
                            "Ant Bank Libra Savings HKD",
                            "Libra Savings Account",
                            "digital_bank_libra_savings",
                            txn_date,
                            "HKD",
                            closing_balance,
                            closing_balance,
                            "closing_balance",
                            "Libra Savings closing balance",
                            row["filename"],
                            source_page,
                            source_line_ref,
                            1.0,
                            created_at,
                        ),
                    )
                    balance_markers_inserted += 1
                    closing_balance_inserted = True
                    continue

                parsed_txn = _parse_ant_transaction_payload(payload)
                if parsed_txn is None:
                    continue
                description_raw, amount_signed, running_balance = parsed_txn
                description_raw = _normalize_ant_description(description_raw)
                direction = "credit" if amount_signed >= 0 else "debit"
                amount = abs(amount_signed)
                reference_no = _build_ant_reference_no(txn_date, description_raw)
                counterparty = _extract_ant_counterparty(description_raw)
                channel = _infer_ant_channel(description_raw)
                dedupe_key = _build_ant_dedupe_key(
                    account_masked,
                    txn_date,
                    direction,
                    amount,
                    description_raw,
                    reference_no,
                    running_balance,
                )
                existing = conn.execute(
                    "SELECT 1 FROM transactions WHERE dedupe_key = ?",
                    (dedupe_key,),
                ).fetchone()
                if existing is not None:
                    duplicate_transactions_skipped += 1
                    continue

                txn_index += 1
                conn.execute(
                    """
                    INSERT INTO transactions (
                        transaction_id, ledger_batch_id, document_id, section_id, document_fact_id,
                        institution_id, account_id, account_number_raw, account_name_raw,
                        account_type_raw, product_type_raw, txn_date, posting_date, value_date,
                        trade_date, settlement_date, effective_date, currency, amount, direction,
                        amount_signed, base_currency, fx_rate_to_base, amount_in_base_currency,
                        description_raw, description_clean, reference_no, bank_reference,
                        external_reference, counterparty_raw, counterparty_clean,
                        counterparty_name_raw, counterparty_name_clean, counterparty_account_masked,
                        counterparty_phone_raw, counterparty_bank_name, channel, payment_rail, balance, balance_currency,
                        balance_hkd_equivalent, balance_source, continuity_check_status, txn_type,
                        category, tag, business_purpose, accounting_subject, source_file,
                        source_page, source_line_ref, source_extraction_method, confidence,
                        needs_review, review_reason, dedupe_key, canonical_hash, record_status,
                        source_record_type, created_at, updated_at, approved_at, approved_by
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, NULL, ?, NULL, NULL, NULL, NULL, ?, ?, ?, ?, ?, 0, NULL, ?, ?, 'active', 'statement_txn', ?, NULL, NULL, NULL)
                    """,
                    (
                        f"txn_{document_id}_{txn_index}",
                        "email_gateway_ant_bank_transactions",
                        document_id,
                        activity_section_id,
                        document_fact_id,
                        "ant_bank",
                        account_id,
                        account_masked,
                        "Ant Bank Libra Savings HKD",
                        "Libra Savings Account",
                        "digital_bank_libra_savings",
                        txn_date,
                        txn_date,
                        txn_date,
                        "HKD",
                        amount,
                        direction,
                        amount_signed,
                        "HKD",
                        amount_signed,
                        description_raw,
                        description_raw,
                        reference_no,
                        reference_no,
                        counterparty,
                        counterparty,
                        counterparty,
                        counterparty,
                        None,
                        None,
                        counterparty,
                        channel,
                        running_balance,
                        "HKD",
                        running_balance,
                        "statement_running_balance",
                        "statement_cash_movement",
                        row["filename"],
                        source_page,
                        source_line_ref,
                        f"email_gateway_ant_bank_{source_page}",
                        1.0,
                        dedupe_key,
                        dedupe_key,
                        created_at,
                    ),
                )
                transactions_inserted += 1

            if not opening_balance_inserted or not closing_balance_inserted or not document_closing_balance_inserted:
                # keep builder deterministic even when some summary rows are absent in future fixtures
                pass

        conn.commit()
        return {
            "documents_scanned": documents_scanned,
            "transactions_inserted": transactions_inserted,
            "balance_markers_inserted": balance_markers_inserted,
            "document_sections_inserted": document_sections_inserted,
            "duplicate_transactions_skipped": duplicate_transactions_skipped,
        }
    finally:
        conn.close()


def ingest_email_file(
    *,
    email_path: str | Path,
    db_path: str | Path,
    schema_path: str | Path,
    attachments_dir: str | Path,
    raw_email_dir: str | Path,
    source_channel: str = "email_gateway",
    overwrite: bool = False,
) -> dict[str, object]:

    message = BytesParser(policy=policy.default).parse(Path(email_path).open("rb"))

    return ingest_email_message(
        message=message,
        db_path=db_path,
        schema_path=schema_path,
        attachments_dir=attachments_dir,
        raw_email_dir=raw_email_dir,
        source_channel=source_channel,
        overwrite=overwrite,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Register inbound emails, attachments, and raw documents into schema v2.")
    parser.add_argument("email_path", help="Path to a raw .eml file to ingest.")
    parser.add_argument("--db-path", required=True, help="Target SQLite database path.")
    parser.add_argument("--schema", default=str(Path(__file__).resolve().parents[1] / "schema-v2-draft.sql"), help="Path to schema v2 SQL.")
    parser.add_argument("--attachments-dir", required=True, help="Directory where decoded attachments are stored.")
    parser.add_argument("--raw-email-dir", required=True, help="Directory where raw email copies are stored.")
    parser.add_argument("--source-channel", default="email_gateway", help="Source channel label stored in inbound_emails.")
    parser.add_argument("--overwrite", action="store_true", help="Replace an already ingested email with the same message-id.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary = ingest_email_file(
        email_path=args.email_path,
        db_path=args.db_path,
        schema_path=args.schema,
        attachments_dir=args.attachments_dir,
        raw_email_dir=args.raw_email_dir,
        source_channel=args.source_channel,
        overwrite=args.overwrite,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
