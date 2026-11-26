"""Weighbridge ticketing system.

A terminal program for weighing vehicles in and out, capturing deductions,
submitting tickets and printing weighbridge notes. Data is kept in CSV files
in the data folder (see README.md).
"""

from __future__ import annotations

import argparse
import csv
import getpass
import logging
import os
import signal
import subprocess
import sys
import textwrap
import threading
import zipfile
from contextlib import contextmanager
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from logging.handlers import RotatingFileHandler
from pathlib import Path

__version__ = "1.0.0"

BASE_DIR = Path(__file__).resolve().parent

MAX_WEIGHT_KG = 100_000
TARE_WARN_KG = 500
AVG_TARE_LOADS = 10
BACKUP_KEEP = 60
DATE_FMT = "%Y-%m-%d"
STAMP_FMT = "%Y-%m-%d %H:%M:%S"

CUSTOMER_DELIVERED = "Customer Delivered"
COMPANY_COLLECTION = "Company Collection"
LOADING = "Loading"
OFFLOADING = "Offloading"
ACTIVE_STATUSES = ("OPEN", "REVIEW")
STATUSES = ("OPEN", "REVIEW", "COMPLETE", "VOID")
RECEIPT_WIDTH = 52
RECEIPT_LABEL_WIDTH = 22

TICKET_FIELDS = [
    "wb_number", "status", "reg_no", "account", "order_no", "delivery_note",
    "depot", "bin_number", "container", "delivery_type", "action", "operator",
    "multi_weigh",
    "multi_products", "print_quality_slip", "comments", "transporter",
    "driver", "driver_id", "vehicle_description", "product", "cust_weight",
    "date", "time_in", "time_out", "total_time", "first_weight",
    "second_weight", "product_weight", "moisture_pct", "moisture_kg",
    "deduction_value", "deduction_type", "deduction_auto", "deduction_reason",
    "deduction_kg", "payable_weight", "scale", "void_reason",
]

SCHEMAS = {
    "tickets": TICKET_FIELDS,
    "weighings": ["wb_number", "seq", "weight_kg", "product", "scale", "timestamp"],
    "ticket_lines": ["wb_number", "product", "net_kg", "payable_kg"],
    "customers": ["name", "contact", "phone"],
    "products": ["name", "default_deduction_pct"],
    "vehicles": ["reg_no", "description", "transporter"],
    "vehicle_types": ["name"],
    "transporters": ["name", "contact", "phone"],
    "drivers": ["name", "id_number", "phone"],
    "depots": ["name"],
    "bins": ["name"],
    "settings": ["key", "value"],
    "audit": ["timestamp", "user", "action", "wb_number", "detail"],
}

SETTING_DEFAULTS = {
    "next_wb_number": "1",
    "current_scale": "Manual",
}

COMPANY_SETTINGS = [
    ("company_name", "Company name"), ("address_1", "Address line 1"),
    ("address_2", "Address line 2"), ("address_3", "Address line 3"),
    ("address_4", "Address line 4"), ("address_5", "Address line 5"),
    ("tel", "Tel"), ("fax", "Fax"), ("vat_no", "VAT number"),
    ("operator_name", "Operator"),
]

SEED_DATA = {
    "settings": [{"key": key, "value": value} for key, value in SETTING_DEFAULTS.items()],
}


class Cancelled(Exception):
    """The operator cancelled the current action."""


class InputClosed(Cancelled):
    """The terminal input ended, no more answers can be read."""


log = logging.getLogger("weighbridge")
log.addHandler(logging.NullHandler())

DATA_DIR = EXPORT_DIR = BACKUP_DIR = LOG_DIR = RECEIPT_DIR = Path()


def configure(data_dir):
    """Point the program at a data folder."""
    global DATA_DIR, EXPORT_DIR, BACKUP_DIR, LOG_DIR, RECEIPT_DIR
    DATA_DIR = Path(data_dir).resolve()
    EXPORT_DIR = DATA_DIR / "exports"
    BACKUP_DIR = DATA_DIR / "backups"
    LOG_DIR = DATA_DIR / "logs"
    RECEIPT_DIR = DATA_DIR / "receipts"


configure(os.environ.get("WEIGHBRIDGE_DATA") or BASE_DIR / "data")


def log_path():
    return LOG_DIR / "weighbridge.log"


def setup_logging():
    for handler in list(log.handlers):
        if isinstance(handler, RotatingFileHandler):
            log.removeHandler(handler)
            handler.close()
    handler = RotatingFileHandler(log_path(), maxBytes=1_000_000, backupCount=5,
                                  encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def current_user():
    try:
        return getpass.getuser()
    except Exception:  # getuser can fail when no user variables are set
        return "unknown"


# output

USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ


def paint(text, code):
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else str(text)


def heading(text):
    print("\n" + paint("=" * 66, "36"))
    print(paint(f" {text}", "1;36"))
    print(paint("=" * 66, "36"))


def info(text):
    print(paint(f"  {text}", "32"))


def warn(text):
    print(paint(f"  WARNING: {text}", "33"))


def error(text):
    print(paint(f"  ERROR: {text}", "31"))


def fmt_kg(value):
    return f"{int(value or 0):,} kg".replace(",", " ")


def print_table(headers, rows):
    if not rows:
        print("  (nothing to show)")
        return
    text_rows = [[str(cell) for cell in row] for row in rows]
    widths = [
        max(len(headers[i]), *(len(row[i]) for row in text_rows))
        for i in range(len(headers))
    ]
    print("  " + paint("  ".join(h.ljust(w) for h, w in zip(headers, widths)), "1"))
    print("  " + "  ".join("-" * w for w in widths))
    for row in text_rows:
        print("  " + "  ".join(c.ljust(w) for c, w in zip(row, widths)))


# CSV storage

def table_path(name):
    return DATA_DIR / f"{name}.csv"


def init_storage():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for folder in (EXPORT_DIR, BACKUP_DIR, LOG_DIR, RECEIPT_DIR):
        folder.mkdir(exist_ok=True)
    missing = {
        name: SEED_DATA.get(name, [])
        for name in SCHEMAS if not table_path(name).exists()
    }
    if missing:
        write_tables(missing)


def read_table(name):
    fields = SCHEMAS[name]
    with open(table_path(name), newline="", encoding="utf-8-sig") as handle:
        return [
            {field: (row.get(field) or "") for field in fields}
            for row in csv.DictReader(handle)
        ]


def write_tables(changes):
    temp_files = {}
    try:
        for name, rows in changes.items():
            path = table_path(name)
            if path.exists():
                with open(path, "r+b"):
                    pass
            temp = path.with_name(path.name + ".tmp")
            temp_files[path] = temp
            with open(temp, "w", newline="", encoding="utf-8-sig") as handle:
                writer = csv.DictWriter(handle, SCHEMAS[name], extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)
                handle.flush()
                os.fsync(handle.fileno())
        # Ctrl+C half way through would leave some tables saved and others not.
        with interrupts_deferred():
            for path, temp in temp_files.items():
                os.replace(temp, path)
    finally:
        for temp in temp_files.values():
            temp.unlink(missing_ok=True)


@contextmanager
def interrupts_deferred():
    """Hold back Ctrl+C until the block has finished."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    received = []
    previous = signal.signal(signal.SIGINT, lambda *_: received.append(True))
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)
    if received:
        raise KeyboardInterrupt


def commit(changes):
    while True:
        try:
            write_tables(changes)
            return
        except PermissionError as exc:
            name = Path(exc.filename).name if exc.filename else "a data file"
            log.warning("Save blocked: %s", exc)
            error(f"Cannot save {name}. It is probably open in Excel.")
            ask("Close it, then press Enter to retry (/c to cancel)")


def with_audit(changes, action, wb_number, detail=""):
    audit = read_table("audit")
    audit.append({
        "timestamp": now_stamp(), "user": current_user(), "action": action,
        "wb_number": wb_number, "detail": detail,
    })
    changes["audit"] = audit
    log.info("%s ticket %s: %s", action, wb_number, detail)
    return changes


# single instance lock

def acquire_lock():
    """Lock the data folder so a second copy of the program cannot use it.

    Two copies would each overwrite the other's changes. Returns the open
    lock file (keep it open while running) or None if already locked.
    """
    handle = open(DATA_DIR / ".lock", "a+")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


# backups

def backup_data(keep=BACKUP_KEEP):
    """Zip the data files once a day. Returns the new backup path, or None."""
    target = BACKUP_DIR / f"backup_{date.today().isoformat()}.zip"
    if target.exists():
        return None
    BACKUP_DIR.mkdir(exist_ok=True)
    temp = target.with_name(target.name + ".tmp")
    try:
        with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as archive:
            for name in SCHEMAS:
                path = table_path(name)
                if path.exists():
                    archive.write(path, path.name)
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    for old in sorted(BACKUP_DIR.glob("backup_*.zip"))[:-keep]:
        old.unlink(missing_ok=True)
    return target


_backup_warned_on = None


def run_daily_backup():
    global _backup_warned_on
    try:
        made = backup_data()
        if made:
            log.info("Backup written to %s", made)
    except OSError as exc:
        log.warning("Backup failed: %s", exc)
        if _backup_warned_on != date.today():
            _backup_warned_on = date.today()
            warn(f"Today's backup could not be made: {exc}")


# data checks

TICKET_INT_FIELDS = ("wb_number", "first_weight", "second_weight", "product_weight",
                     "moisture_kg", "deduction_kg", "payable_weight", "cust_weight")
OTHER_INT_FIELDS = {"weighings": ("seq", "weight_kg"), "ticket_lines": ("net_kg", "payable_kg")}


def is_whole(value):
    return value.isascii() and value.isdigit()


def check_data():
    """Look for damaged rows, for example after a file was edited by hand."""
    problems = []

    def bad(table, line, text):
        problems.append(f"{table}.csv line {line}: {text}")

    seen = set()
    for line, t in enumerate(read_table("tickets"), 2):
        for field in TICKET_INT_FIELDS:
            if t[field] and not is_whole(t[field]):
                bad("tickets", line, f"{field} is not a whole number ({t[field]!r})")
        if not t["wb_number"]:
            bad("tickets", line, "wb_number is missing")
        elif t["wb_number"] in seen:
            bad("tickets", line, f"WB number {t['wb_number']} is used more than once")
        seen.add(t["wb_number"])
        if t["status"] not in STATUSES:
            bad("tickets", line, f"unknown status {t['status']!r}")
        if not t["first_weight"]:
            bad("tickets", line, "first_weight is missing")
        if t["status"] in ("REVIEW", "COMPLETE") and not (t["second_weight"] and t["payable_weight"]):
            bad("tickets", line, f"{t['status']} ticket has no second or total weight")
        if not t["time_in"]:
            bad("tickets", line, "time_in is missing")
        for field in ("time_in", "time_out"):
            if t[field]:
                try:
                    datetime.strptime(t[field], STAMP_FMT)
                except ValueError:
                    bad("tickets", line, f"{field} is not a YYYY-MM-DD HH:MM:SS time ({t[field]!r})")
    for table, fields in OTHER_INT_FIELDS.items():
        for line, row in enumerate(read_table(table), 2):
            for field in fields:
                if not is_whole(row[field]):
                    bad(table, line, f"{field} is not a whole number ({row[field]!r})")
    next_wb = get_setting("next_wb_number", "1")
    if next_wb and not is_whole(next_wb):
        problems.append(f"settings.csv: next_wb_number is not a whole number ({next_wb!r})")
    return problems


def get_setting(key, default=None):
    for row in read_table("settings"):
        if row["key"] == key:
            return row["value"]
    return SETTING_DEFAULTS.get(key, "") if default is None else default


def settings_with(key, value):
    rows = read_table("settings")
    for row in rows:
        if row["key"] == key:
            row["value"] = str(value)
            return rows
    rows.append({"key": key, "value": str(value)})
    return rows


def next_wb_number(tickets):
    highest = max((int(t["wb_number"]) for t in tickets), default=0)
    return max(int(get_setting("next_wb_number", "1") or 1), highest + 1)


# input helpers

def ask(prompt, default="", required=False):
    suffix = f" [{default}]" if default not in ("", None) else ""
    while True:
        try:
            raw = input(f"  {prompt}{suffix}: ").strip()
        except EOFError:
            raise InputClosed from None
        if raw.lower() == "/c":
            raise Cancelled
        value = raw or ("" if default is None else str(default))
        if value or not required:
            return value
        error("This field is required.")


def confirm(prompt, default=True):
    hint = "Y/n" if default else "y/N"
    while True:
        answer = ask(f"{prompt} ({hint})").lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        error("Please answer y or n.")


def ask_int(prompt, default=None, minimum=0, maximum=MAX_WEIGHT_KG):
    while True:
        raw = ask(prompt, "" if default is None else default, required=True)
        cleaned = raw.replace(" ", "").replace(",", "")
        if cleaned.isascii() and cleaned.isdigit():
            value = int(cleaned)
            if minimum <= value <= maximum:
                return value
        error(f"Enter a whole number from {minimum} to {maximum}.")


def ask_weight(prompt):
    return ask_int(f"{prompt} (kg)", minimum=1)


def ask_decimal(prompt, default="0", minimum=Decimal(0), maximum=None):
    while True:
        raw = ask(prompt, default, required=True).replace(",", ".")
        try:
            value = Decimal(raw)
        except InvalidOperation:
            error("Enter a number.")
            continue
        if not value.is_finite() or value < minimum or (maximum is not None and value > maximum):
            limit = f" to {maximum}" if maximum is not None else " or more"
            error(f"Enter a number from {minimum}{limit}.")
            continue
        return value


def ask_date(prompt, default):
    while True:
        raw = ask(f"{prompt} (YYYY-MM-DD)", default, required=True)
        try:
            return datetime.strptime(raw, DATE_FMT).date().isoformat()
        except ValueError:
            error("Use a real date, for example 2026-11-23.")


def clean_reg(value):
    return "".join(value.upper().split()).replace("-", "")


def pick(table, label, default="", required=False, allowed=()):
    key = SCHEMAS[table][0]
    shown = []
    listed_for = None  # the text whose partial matches are on screen
    hint = "? = list" if required else "? = list, - = clear"
    while True:
        raw = ask(f"{label} ({hint})", default, required)
        if not raw or (raw == "-" and not required):
            return ""
        rows = read_table(table)
        names = [row[key] for row in rows]
        if raw == "?":
            shown, listed_for = names, None
            show_choices(shown)
            continue
        value = " ".join(raw.upper().split())
        again = value == listed_for  # typed the same text again: they want it as new
        if shown and not again and raw.isdigit() and 1 <= int(raw) <= len(shown):
            return shown[int(raw) - 1]
        if value in names or value in allowed:
            return value
        matches = [name for name in names if value in name]
        if len(matches) == 1 and confirm(f"Use {matches[0]}?"):
            return matches[0]
        if len(matches) > 1 and not again:
            shown, listed_for = matches, value
            show_choices(shown)
            info(f"Type a number to choose, or type {value} again to add it as new.")
            continue
        if (not matches or again) and confirm(f"{value} is new. Add it?"):
            rows.append({key: value})
            commit({table: rows})
            return value


def show_choices(names):
    if not names:
        print("  (list is empty, type a name to add it)")
    for number, name in enumerate(names, 1):
        print(f"  {number:>3}. {name}")


# calculations

def round_kg(value):
    return int(Decimal(value).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def calculate(first, second, moisture_pct, deduction_value, deduction_type):
    net = abs(first - second)
    moisture = round_kg(Decimal(net) * Decimal(moisture_pct or 0) / 100)
    value = Decimal(deduction_value or 0)
    deduction = round_kg(Decimal(net) * value / 100 if deduction_type == "%" else value)
    return net, moisture, deduction, net - moisture - deduction


def allocate_payable(lines, payable):
    total = sum(int(line["net_kg"]) for line in lines)
    running = previous = 0
    for line in lines:
        running += int(line["net_kg"])
        share = round_kg(Decimal(payable) * running / total) if total else 0
        line["payable_kg"] = share - previous
        previous = share


def average_tare(reg_no, tickets, exclude=""):
    tares = [
        min(int(t["first_weight"]), int(t["second_weight"]))
        for t in tickets
        if t["reg_no"] == reg_no and t["status"] == "COMPLETE" and t["wb_number"] != exclude
    ]
    recent = tares[-AVG_TARE_LOADS:]
    return round_kg(Decimal(sum(recent)) / len(recent)) if recent else 0


def duration(start, end):
    seconds = int((end - start).total_seconds())
    hours, remainder = divmod(max(seconds, 0), 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def now_stamp():
    return datetime.now().strftime(STAMP_FMT)


def mask_id(value):
    return "*" * (len(value) - 4) + value[-4:] if len(value) > 4 else value


# ticket display and receipts

def yes_no(value):
    return "Yes" if value == "Y" else "No"


def ticket_lines_for(wb_number):
    return [l for l in read_table("ticket_lines") if l["wb_number"] == wb_number]


def weighings_for(wb_number, weighings):
    return sorted((w for w in weighings if w["wb_number"] == wb_number),
                  key=lambda w: int(w["seq"]))


def show_ticket(t):
    pairs = [
        ("WB Number", t["wb_number"]), ("Status", t["status"]),
        ("Reg No", t["reg_no"]), ("Account", t["account"]),
        ("Order / PO", t["order_no"]), ("Delivery Note", t["delivery_note"]),
        ("Type", t["delivery_type"]), ("Action", t["action"]),
        ("Vehicle", t["vehicle_description"]), ("Transporter", t["transporter"]),
        ("Driver", t["driver"]), ("ID Number", mask_id(t["driver_id"])),
        ("Product", t["product"]), ("Depot", t["depot"]),
        ("Bin", t["bin_number"]), ("Container", yes_no(t["container"])),
        ("Scale", t["scale"]), ("Multi Weigh", yes_no(t["multi_weigh"])),
        ("Multi Products", yes_no(t["multi_products"])),
        ("Cust Weight", fmt_kg(t["cust_weight"])),
        ("Time In", t["time_in"]), ("Time Out", t["time_out"] or "-"),
        ("Total Time", t["total_time"] or "-"), ("Operator", t["operator"] or "-"),
    ]
    print()
    for i in range(0, len(pairs), 2):
        left = f"{pairs[i][0]:<14}: {str(pairs[i][1])[:19]:<20}"
        right = f"{pairs[i + 1][0]:<14}: {str(pairs[i + 1][1])[:24]}"
        print(f"  {left} {right}")
    print("  " + "-" * 62)
    print(f"  First Weight    : {fmt_kg(t['first_weight'])}")
    if t["second_weight"]:
        print(f"  Second Weight   : {fmt_kg(t['second_weight'])}")
        print(f"  Product Weight  : {fmt_kg(t['product_weight'])}")
        print(f"  Moisture        : {fmt_kg(t['moisture_kg'])} ({t['moisture_pct'] or 0}%)")
        print(f"  Deduction       : {fmt_kg(t['deduction_kg'])} {t['deduction_reason']}")
        print(paint(f"  Total Weight    : {fmt_kg(t['payable_weight'])}", "1"))
        lines = ticket_lines_for(t["wb_number"])
        if len(lines) > 1:
            print_table(
                ["Product", "Net", "Total"],
                [[l["product"], fmt_kg(l["net_kg"]), fmt_kg(l["payable_kg"])] for l in lines],
            )
    if t["comments"]:
        print(f"  Comments        : {t['comments']}")
    if t["void_reason"]:
        print(f"  Void Reason     : {t['void_reason']}")


def receipt_time(stamp):
    if not stamp:
        return ""
    moment = datetime.strptime(stamp, STAMP_FMT)
    return f"{moment:%Y/%m/%d %I:%M:%S} {moment.strftime('%p').lower()}"


def receipt_total_time(t):
    if not t["time_in"] or not t["time_out"]:
        return ""
    seconds = int((datetime.strptime(t["time_out"], STAMP_FMT)
                   - datetime.strptime(t["time_in"], STAMP_FMT)).total_seconds())
    days, remainder = divmod(max(seconds, 0), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"Days: {days} Time: {hours:02}:{minutes:02}:{seconds:02}"


def receipt_kg(value):
    return f"{int(value)} Kg" if value != "" else ""


def receipt_vehicle(value):
    return " ".join("and" if word == "&" else word.capitalize() for word in value.split())


def render_receipt(t):
    rule = "-" * RECEIPT_WIDTH
    letterhead = [get_setting(key) for key in ("company_name", "address_1", "address_2",
                                                "address_3", "address_4", "address_5")]
    out = [line for line in letterhead if line]
    contacts = [f"{label}: {get_setting(key)}"
                for label, key in (("Tel", "tel"), ("Fax", "fax"), ("Vat No.", "vat_no"))
                if get_setting(key)]
    if out and contacts:
        out.append("")
    out += contacts
    if out:
        out.append("")
    out += ["WEIGHBRIDGE NOTE", rule]
    banner = {"OPEN": "*** FIRST WEIGH ONLY ***", "REVIEW": "*** DRAFT - NOT SUBMITTED ***",
              "VOID": "*** VOID - NOT VALID ***"}.get(t["status"])
    if banner:
        out += [banner.center(RECEIPT_WIDTH), rule]

    def row(label, value=""):
        parts = textwrap.wrap(str(value), RECEIPT_WIDTH - RECEIPT_LABEL_WIDTH) or [""]
        out.append(f"{label:<{RECEIPT_LABEL_WIDTH}}{parts[0]}".rstrip())
        out.extend(" " * RECEIPT_LABEL_WIDTH + part for part in parts[1:])

    transport = "Collection" if t["delivery_type"] == COMPANY_COLLECTION else "Delivery"
    row("Date:", t["date"].replace("-", "/"))
    row("WB Number:", t["wb_number"])
    row("Account Name:", t["account"])
    row("Depot:", t["depot"])
    row("Vehicle Descrip:", receipt_vehicle(t["vehicle_description"]))
    row("Registration Number:", t["reg_no"])
    row("Transport Type:", transport)
    row("Transporter:", t["transporter"])
    row("Driver:", t["driver"])
    row("Operator:", t["operator"] or get_setting("operator_name"))
    row("Product Name:", t["product"])
    row("Time In:", receipt_time(t["time_in"]))
    row("Bin No:", t["bin_number"])
    row("Action:", t["action"])
    row("Time Out:", receipt_time(t["time_out"]))
    row("First Weight:", receipt_kg(t["first_weight"]))
    row("Total Time:", receipt_total_time(t))
    row("Second Weight:", receipt_kg(t["second_weight"]))
    row("Nett Weight:", receipt_kg(t["product_weight"]))
    if int(t["moisture_kg"] or 0):
        row("Moisture Qty:", f"{t['moisture_kg']} Kg ({t['moisture_pct']}%)")
    row("Deduction Reason:", t["deduction_reason"])
    row("Deduction Qty:", receipt_kg(t["deduction_kg"]))
    row("Comments:", t["comments"])
    if t["void_reason"]:
        row("Void Reason:", t["void_reason"])
    out.append(rule)
    row("Total Weight:", receipt_kg(t["payable_weight"]))
    out.append(rule)
    lines = ticket_lines_for(t["wb_number"])
    if len(lines) > 1:
        out.append("Product breakdown:")
        for line in lines:
            row(f"  {line['product'][:RECEIPT_LABEL_WIDTH - 3]}", receipt_kg(line["payable_kg"]))
        out.append(rule)
    if t["print_quality_slip"] == "Y":
        out += ["QUALITY / LOAD SLIP", "Quality: " + "_" * 30,
                "Checked by: " + "_" * 27, "Signature: " + "_" * 28, rule]
    return "\n".join(out)


def show_receipt(t):
    print()
    for line in render_receipt(t).splitlines():
        strong = line.startswith("Total Weight:") or line == "WEIGHBRIDGE NOTE"
        print("  " + (paint(line, "1") if strong else line))


def receipt_path(t):
    return RECEIPT_DIR / f"WB{t['wb_number']:0>6}.txt"


def save_receipt(t):
    """Write the receipt to the receipts folder. Returns the path, or None."""
    path = receipt_path(t)
    try:
        RECEIPT_DIR.mkdir(exist_ok=True)
        path.write_text(render_receipt(t) + "\n", encoding="utf-8")
    except OSError as exc:
        log.warning("Could not save receipt %s: %s", path, exc)
        error(f"Could not save the receipt file: {exc}")
        return None
    return path


def print_receipt(path):
    try:
        if os.name == "nt":
            os.startfile(str(path), "print")
        else:
            subprocess.run(["lp", str(path)], check=True, capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("Printing %s failed: %s", path, exc)
        error(f"Could not print: {exc}")
        info(f"The receipt is saved at {path}")
        return
    info("Sent to the printer.")


def offer_print(t, saved=None):
    if confirm("Print the receipt?", default=False):
        path = saved or save_receipt(t)
        if path:
            print_receipt(path)


# weigh in

def default_action(delivery_type):
    return LOADING if delivery_type == COMPANY_COLLECTION else OFFLOADING


def weight_label(action, position):
    full_first = action == OFFLOADING
    state = "full" if (position == "First") == full_first else "empty"
    return f"{position} weight - {state}"


def ask_delivery_type(current):
    default = "2" if current == COMPANY_COLLECTION else "1"
    while True:
        choice = ask("1 = Customer Delivered, 2 = Company Collection", default)
        if choice in ("1", "2"):
            return CUSTOMER_DELIVERED if choice == "1" else COMPANY_COLLECTION
        error("Enter 1 or 2.")


def ask_action(current):
    default = "1" if current == LOADING else "2"
    while True:
        choice = ask("Action: 1 = Loading, 2 = Offloading", default)
        if choice in ("1", "2"):
            return LOADING if choice == "1" else OFFLOADING
        error("Enter 1 or 2.")


def ask_driver(default_name, default_id):
    name = pick("drivers", "Contract driver", default_name)
    if not name:
        return "", ""
    drivers = read_table("drivers")
    record = next((d for d in drivers if d["name"] == name), None)
    known_id = record["id_number"] if record and record["id_number"] else default_id
    id_number = ask("ID number", known_id)
    if record and id_number != record["id_number"]:
        record["id_number"] = id_number
        commit({"drivers": drivers})
    return name, id_number


def check_first_weight(weight, avg_tare, action):
    if not avg_tare:
        return True
    gap = abs(weight - avg_tare)
    if action == OFFLOADING and gap <= TARE_WARN_KG:
        warn(f"{fmt_kg(weight)} is close to this vehicle's average tare of "
             f"{fmt_kg(avg_tare)}, so it may be empty.")
        return confirm("Use this as the full weight?", default=False)
    if action == LOADING and gap > TARE_WARN_KG:
        warn(f"{fmt_kg(weight)} differs from this vehicle's average tare of "
             f"{fmt_kg(avg_tare)} by {fmt_kg(gap)}, so it may not be empty.")
        return confirm("Use this as the empty weight?", default=False)
    return True


def weigh_in():
    while True:
        heading("WEIGH IN - NEW TICKET   (/c to cancel)")
        tickets = read_table("tickets")
        reg = clean_reg(ask("Reg No", required=True))
        existing = next((t for t in tickets
                         if t["reg_no"] == reg and t["status"] in ACTIVE_STATUSES), None)
        if existing:
            if existing["status"] == "OPEN":
                error(f"{reg} is already on site on ticket {existing['wb_number']}.")
            else:
                error(f"{reg} has unsubmitted ticket {existing['wb_number']}. Submit it first.")
            return

        vehicles = read_table("vehicles")
        vehicle = next((v for v in vehicles if v["reg_no"] == reg), None)
        last = next((t for t in reversed(tickets) if t["reg_no"] == reg and t["status"] != "VOID"), None)
        previous = last or {field: "" for field in TICKET_FIELDS}
        avg_tare = average_tare(reg, tickets)
        info(f"Avg tare: {fmt_kg(avg_tare)}" if avg_tare else "New vehicle, no average tare yet.")

        wb_number = next_wb_number(tickets)
        t = {field: "" for field in TICKET_FIELDS}
        t.update(wb_number=str(wb_number), status="OPEN", reg_no=reg)
        t["account"] = pick("customers", "Account name (or CASUAL)", previous["account"],
                            required=True, allowed=("CASUAL",))
        t["order_no"] = ask("Order No / PO")
        t["delivery_note"] = ask("Delivery Note No")
        t["delivery_type"] = ask_delivery_type(previous["delivery_type"])
        t["action"] = ask_action(default_action(t["delivery_type"]))
        t["transporter"] = pick("transporters", "Transporter",
                                (vehicle or {}).get("transporter") or previous["transporter"])
        t["driver"], t["driver_id"] = ask_driver(previous["driver"], previous["driver_id"])
        t["vehicle_description"] = pick("vehicle_types", "Vehicle description",
                                        (vehicle or {}).get("description") or previous["vehicle_description"])
        t["product"] = pick("products", "Product name", previous["product"], required=True)
        t["cust_weight"] = str(ask_int("Customer weight (kg)", default=0))

        for field in ("container", "multi_weigh", "multi_products", "print_quality_slip"):
            t[field] = "N"
        if confirm("Add depot, bin, multi-weigh or other options?", default=False):
            t["depot"] = pick("depots", "Depot")
            t["bin_number"] = pick("bins", "Bin number")
            t["container"] = "Y" if confirm("Container?", default=False) else "N"
            t["multi_weigh"] = "Y" if confirm("Multi weigh?", default=False) else "N"
            if t["multi_weigh"] == "N":
                t["multi_products"] = "Y" if confirm("Multi products?", default=False) else "N"
            else:
                t["multi_products"] = "Y"
            t["print_quality_slip"] = "Y" if confirm("Add quality/load section?", default=False) else "N"
            t["comments"] = ask("Comments")

        while True:
            first = ask_weight(weight_label(t["action"], "First"))
            if check_first_weight(first, avg_tare, t["action"]):
                break
        stamp = now_stamp()
        t.update(first_weight=str(first), date=stamp[:10], time_in=stamp,
                 scale=get_setting("current_scale"))

        show_ticket(t)
        if not confirm("Save this ticket?"):
            raise Cancelled

        tickets.append(t)
        weighings = read_table("weighings")
        weighings.append({"wb_number": t["wb_number"], "seq": 1, "weight_kg": first,
                          "product": t["product"], "scale": t["scale"], "timestamp": stamp})
        if vehicle:
            vehicle["description"] = t["vehicle_description"]
            vehicle["transporter"] = t["transporter"]
        else:
            vehicles.append({"reg_no": reg, "description": t["vehicle_description"],
                             "transporter": t["transporter"]})
        commit(with_audit({
            "tickets": tickets, "weighings": weighings, "vehicles": vehicles,
            "settings": settings_with("next_wb_number", wb_number + 1),
        }, "WEIGH_IN", t["wb_number"], f"{reg} first weight {first}"))
        info(f"Ticket {t['wb_number']} saved. {reg} is now on site.")
        if not confirm("Start another weigh in?", default=False):
            return


# weigh out

def open_tickets(tickets):
    return [t for t in tickets if t["status"] == "OPEN"]


def review_tickets(tickets):
    return [t for t in tickets if t["status"] == "REVIEW"]


def show_on_site(tickets):
    now = datetime.now()
    rows = []
    for number, t in enumerate(open_tickets(tickets), 1):
        since = datetime.strptime(t["time_in"], STAMP_FMT)
        rows.append([number, t["wb_number"], t["reg_no"], t["action"], t["product"][:24],
                     t["account"][:20], fmt_kg(t["first_weight"]), duration(since, now)])
    print_table(["#", "WB No", "Reg No", "Action", "Product", "Account", "First", "On site"], rows)


def select_open_ticket(tickets):
    on_site = open_tickets(tickets)
    if not on_site:
        info("No vehicles are on site.")
        return None
    show_on_site(tickets)
    while True:
        raw = ask("Select # or Reg No", required=True)
        if raw.isdigit() and 1 <= int(raw) <= len(on_site):
            return on_site[int(raw) - 1]
        reg = clean_reg(raw)
        found = next((t for t in on_site if t["reg_no"] == reg), None)
        if found:
            return found
        error("That vehicle is not on site.")


def split_products(t, net):
    """Split one net weight across several products."""
    lines, remaining = {}, net
    default = t["product"]
    while remaining > 0:
        product = pick("products", "Product", default, required=True)
        kg = ask_int(f"Kg of {product}", default=remaining, minimum=1, maximum=remaining)
        lines[product] = lines.get(product, 0) + kg
        remaining -= kg
        default = ""
        if remaining:
            info(f"{fmt_kg(remaining)} still to allocate.")
    return lines or {t["product"]: 0}


def lines_from_weighings(mine):
    lines = {}
    for before, after in zip(mine, mine[1:]):
        drop = abs(int(after["weight_kg"]) - int(before["weight_kg"]))
        lines[after["product"]] = lines.get(after["product"], 0) + drop
    return lines


def capture_multi_weighs(t, weighings):
    """Capture weights one at a time. Returns final weight and product lines."""
    mine = weighings_for(t["wb_number"], weighings)
    direction = 1 if t["action"] == LOADING else -1
    trend = "heavier" if direction > 0 else "lighter"
    while True:
        previous = int(mine[-1]["weight_kg"])
        print_table(["#", "Weight", "Product", "Time"],
                    [[w["seq"], fmt_kg(w["weight_kg"]), w["product"] if i else "(first)", w["timestamp"]]
                     for i, w in enumerate(mine)])
        weight = ask_weight(f"Weight {len(mine) + 1}")
        if (weight - previous) * direction <= 0:
            error(f"A {t['action']} ticket must get {trend} at each weighing.")
            continue
        product = pick("products", f"Product for this {fmt_kg(abs(weight - previous))}",
                       t["product"], required=True)
        record = {"wb_number": t["wb_number"], "seq": len(mine) + 1, "weight_kg": weight,
                  "product": product, "scale": t["scale"], "timestamp": now_stamp()}
        final = confirm("Is this the last weight for this vehicle?", default=False)
        weighings.append(record)
        mine.append(record)
        if final:
            return weight, lines_from_weighings(mine)
        commit(with_audit({"weighings": weighings}, "MULTI_WEIGH", t["wb_number"],
                          f"weighing {record['seq']}: {weight}"))
        if not confirm("Capture the next weight now?"):
            info("Saved. The vehicle stays on site.")
            return None


def ask_deductions(t, net):
    products = read_table("products")
    record = next((p for p in products if p["name"] == t["product"]), None)
    auto_pct = Decimal(record["default_deduction_pct"] or 0) if record else Decimal(0)
    while True:
        moisture = ask_decimal("Moisture %", t["moisture_pct"] or "0", maximum=Decimal(100))
        auto = auto_pct > 0 and confirm(f"Apply auto deduction of {auto_pct}%?")
        if auto:
            value, kind = auto_pct, "%"
        else:
            current = "P" if t["deduction_type"] == "%" else "K"
            while True:
                kind = ask("Deduction in K = kg or P = %", current).upper()
                if kind in ("K", "P"):
                    break
                error("Enter K or P.")
            kind = "Kg" if kind == "K" else "%"
            value = ask_decimal(f"Deduction ({kind})", t["deduction_value"] or "0",
                                maximum=Decimal(100) if kind == "%" else None)
        reason = ask("Deduction reason", t["deduction_reason"], required=True) if value > 0 else ""
        _, moisture_kg, deduction_kg, payable = calculate(net, 0, moisture, value, kind)
        if payable < 0:
            error(f"Deductions of {fmt_kg(moisture_kg + deduction_kg)} are more than "
                  f"the product weight of {fmt_kg(net)}.")
            continue
        return {
            "moisture_pct": str(moisture), "deduction_value": str(value),
            "deduction_type": kind, "deduction_auto": "Y" if auto else "N",
            "deduction_reason": reason,
        }


def check_second_weight(t, second, tickets):
    """Check the second weight. May switch the ticket's action if confirmed."""
    first = int(t["first_weight"])
    action = t["action"]
    if second == first:
        warn("Second weight equals first weight, so the product weight is 0.")
        if not confirm("Continue?", default=False):
            return False
    elif (second > first) != (action == LOADING):
        other = OFFLOADING if action == LOADING else LOADING
        trend = "heavier" if second > first else "lighter"
        warn(f"This ticket is {action}, but the second weight is {trend} than the first.")
        if not confirm(f"Change the action to {other}?", default=False):
            return False
        action = other
    avg = average_tare(t["reg_no"], tickets, exclude=t["wb_number"])
    tare = min(first, second)
    if avg and abs(tare - avg) > TARE_WARN_KG:
        warn(f"Tare of {fmt_kg(tare)} differs from the average of {fmt_kg(avg)} "
             f"by {fmt_kg(abs(tare - avg))}.")
        if not confirm("Continue?", default=False):
            return False
    t["action"] = action
    return True


def apply_calculation(t):
    net, moisture, deduction, payable = calculate(
        int(t["first_weight"]), int(t["second_weight"]), t["moisture_pct"],
        t["deduction_value"], t["deduction_type"])
    t.update(product_weight=str(net), moisture_kg=str(moisture),
             deduction_kg=str(deduction), payable_weight=str(payable))


def finalise_lines(t, product_kg=None):
    """Recalculate the ticket and return the full product line table."""
    apply_calculation(t)
    all_lines = read_table("ticket_lines")
    others = [l for l in all_lines if l["wb_number"] != t["wb_number"]]
    mine = [l for l in all_lines if l["wb_number"] == t["wb_number"]]
    if product_kg is not None:
        mine = [{"wb_number": t["wb_number"], "product": product, "net_kg": kg, "payable_kg": 0}
                for product, kg in product_kg.items()]
    elif len(mine) == 1:
        mine[0]["net_kg"] = t["product_weight"]
    allocate_payable(mine, int(t["payable_weight"]))
    return others + mine


def weigh_out():
    heading("WEIGH OUT - SECOND WEIGHT   (/c to cancel)")
    tickets = read_table("tickets")
    t = select_open_ticket(tickets)
    if t is None:
        return
    show_ticket(t)
    first = int(t["first_weight"])
    weighings = read_table("weighings")

    if t["multi_weigh"] == "Y":
        result = capture_multi_weighs(t, weighings)
        if result is None:
            return
        second, product_kg = result
    else:
        while True:
            second = ask_weight(weight_label(t["action"], "Second"))
            if check_second_weight(t, second, tickets):
                break
        net = abs(first - second)
        product_kg = split_products(t, net) if t["multi_products"] == "Y" else {t["product"]: net}
        weighings.append({"wb_number": t["wb_number"], "seq": 2, "weight_kg": second,
                          "product": t["product"], "scale": t["scale"], "timestamp": now_stamp()})

    t["second_weight"] = str(second)
    t.update(ask_deductions(t, abs(first - second)))
    stamp = now_stamp()
    t.update(status="REVIEW", time_out=stamp,
             total_time=duration(datetime.strptime(t["time_in"], STAMP_FMT),
                                 datetime.strptime(stamp, STAMP_FMT)))
    lines = finalise_lines(t, product_kg)
    commit(with_audit({"tickets": tickets, "weighings": weighings, "ticket_lines": lines},
                      "WEIGH_OUT", t["wb_number"],
                      f"second {second}, total {t['payable_weight']}"))
    customer_kg = int(t["cust_weight"] or 0)
    if customer_kg:
        info(f"Difference from customer weight: {int(t['product_weight']) - customer_kg:+} kg")
    review_ticket(t["wb_number"])



# review and submit

def load_ticket(wb_number):
    tickets = read_table("tickets")
    return tickets, next(t for t in tickets if t["wb_number"] == wb_number)


def review_ticket(wb_number):
    while True:
        tickets, t = load_ticket(wb_number)
        heading(f"REVIEW TICKET {wb_number}")
        show_ticket(t)
        print()
        choice = ask("S = submit, E = edit an entry, L = leave for later", required=True).upper()
        if choice == "S":
            submit_ticket(tickets, t)
            return
        if choice == "E":
            edit_entries(wb_number)
        elif choice == "L":
            info(f"Ticket {wb_number} is saved and waiting to be submitted.")
            return
        else:
            error("Enter S, E or L.")


def submit_ticket(tickets, t):
    t.update(status="COMPLETE", operator=get_setting("operator_name"))
    commit(with_audit({"tickets": tickets}, "SUBMIT", t["wb_number"],
                      f"total {t['payable_weight']}"))
    heading(f"TICKET {t['wb_number']} SUBMITTED")
    show_receipt(t)
    saved = save_receipt(t)
    if saved:
        info(f"Receipt saved to {saved}")
    offer_print(t, saved)


def review_menu():
    heading("REVIEW AND SUBMIT")
    tickets = read_table("tickets")
    pending = review_tickets(tickets)
    if not pending:
        info("No tickets are waiting to be submitted.")
        return
    print_table(["#", "WB No", "Reg No", "Action", "Product", "Account", "Total"],
                [[i, t["wb_number"], t["reg_no"], t["action"], t["product"][:24], t["account"][:20],
                  fmt_kg(t["payable_weight"])] for i, t in enumerate(pending, 1)])
    while True:
        raw = ask("Select #, Reg No or WB No", required=True)
        if raw.isdigit() and 1 <= int(raw) <= len(pending):
            found = pending[int(raw) - 1]
        else:
            reg = clean_reg(raw)
            found = next((t for t in pending if raw == t["wb_number"] or reg == t["reg_no"]), None)
        if found:
            review_ticket(found["wb_number"])
            return
        error("That ticket is not waiting to be submitted.")


# ticket maintenance

ENTRY_OPTIONS = [
    ("reg_no", "Reg No"), ("account", "Account"), ("order_no", "Order No / PO"),
    ("delivery_note", "Delivery Note"), ("delivery_type", "Transport Type"),
    ("action", "Action"), ("transporter", "Transporter"), ("driver", "Driver and ID"),
    ("vehicle_description", "Vehicle"), ("product", "Product"), ("depot", "Depot"),
    ("bin_number", "Bin"), ("container", "Container"), ("cust_weight", "Cust Weight"),
    ("comments", "Comments"), ("time_in", "Time In"), ("time_out", "Time Out"),
    ("weights", "Weights"), ("deductions", "Moisture / Deduction"),
]
PICK_TABLES = {"transporter": "transporters", "vehicle_description": "vehicle_types",
               "depot": "depots", "bin_number": "bins"}


def find_ticket(tickets):
    number = ask("WB Number", required=True).strip()
    if is_whole(number):
        number = number.lstrip("0") or "0"
    found = next((t for t in tickets if t["wb_number"] == number), None)
    if found is None:
        error(f"Ticket {number} was not found.")
    return found


def view_ticket():
    heading("VIEW TICKET")
    t = find_ticket(read_table("tickets"))
    if t:
        show_ticket(t)


def view_receipt():
    heading("VIEW RECEIPT")
    t = find_ticket(read_table("tickets"))
    if t:
        show_receipt(t)
        offer_print(t)


def void_ticket():
    heading("DELETE (VOID) TICKET")
    tickets = read_table("tickets")
    t = find_ticket(tickets)
    if t is None:
        return
    if t["status"] == "VOID":
        error("This ticket is already void.")
        return
    show_ticket(t)
    reason = ask("Reason for voiding", required=True)
    if not confirm(f"Void ticket {t['wb_number']}? It stays on record but leaves all totals", default=False):
        raise Cancelled
    t.update(status="VOID", void_reason=reason)
    commit(with_audit({"tickets": tickets}, "VOID", t["wb_number"], reason))
    if receipt_path(t).exists():
        save_receipt(t)  # so the archived copy shows it is void
    info(f"Ticket {t['wb_number']} is now void.")


def save_edit(tickets, t, field, old, new, lines=None, weighings=None):
    """Save one change. Submitted tickets need a reason for the audit log."""
    detail = f"{field}: {old} -> {new}"
    if t["status"] == "COMPLETE":
        detail += f" ({ask('Reason for changing a submitted ticket', required=True)})"
    changes = {"tickets": tickets}
    if lines is not None:
        changes["ticket_lines"] = lines
    if weighings is not None:
        changes["weighings"] = weighings
    commit(with_audit(changes, "EDIT", t["wb_number"], detail))
    info("Saved.")


def edit_weights(tickets, t):
    weighings = read_table("weighings")
    mine = weighings_for(t["wb_number"], weighings)
    if not mine:
        error("No weighings are recorded for this ticket.")
        return
    print_table(["#", "Weight", "Time"],
                [[i, fmt_kg(w["weight_kg"]), w["timestamp"]] for i, w in enumerate(mine, 1)])
    index = ask_int("Weight # to change", minimum=1, maximum=len(mine)) - 1
    new = ask_weight("New weight")
    old = mine[index]["weight_kg"]
    values = [int(w["weight_kg"]) for w in mine]
    values[index] = new
    if len(values) > 1:
        rising = all(b > a for a, b in zip(values, values[1:]))
        falling = all(b < a for a, b in zip(values, values[1:]))
        if not rising and not falling:
            if len(values) > 2:
                error("Weights must keep rising or keep falling on a multi-weigh ticket.")
                return
            warn("Both weights are the same, so the product weight is 0.")
            if not confirm("Continue?", default=False):
                return
        elif rising != (t["action"] == LOADING):
            other = LOADING if rising else OFFLOADING
            warn(f"These weights mean the ticket is {other}, not {t['action']}.")
            if not confirm(f"Change the action to {other}?", default=False):
                return
            t["action"] = other
    previous_net = int(t["product_weight"] or 0)
    mine[index]["weight_kg"] = str(new)
    t["first_weight"] = str(values[0])
    lines = None
    if t["second_weight"]:
        t["second_weight"] = str(values[-1])
        *_, payable = calculate(values[0], values[-1], t["moisture_pct"],
                                t["deduction_value"], t["deduction_type"])
        if payable < 0:
            error("That weight would make the total weight negative.")
            return
        net = abs(values[0] - values[-1])
        product_kg = None
        if t["multi_weigh"] == "Y":
            product_kg = lines_from_weighings(mine)
        elif len(ticket_lines_for(t["wb_number"])) > 1 and net != previous_net:
            info(f"The product weight is now {fmt_kg(net)}. Split it across the products again.")
            product_kg = split_products(t, net)
        lines = finalise_lines(t, product_kg)
    save_edit(tickets, t, "weight", old, new, lines, weighings)


def edit_deductions(tickets, t):
    if not t["second_weight"]:
        error("Moisture and deductions are captured at weigh out.")
        return
    old = f"moisture {t['moisture_pct']}%, deduction {t['deduction_value']}{t['deduction_type']}"
    t.update(ask_deductions(t, int(t["product_weight"])))
    lines = finalise_lines(t)
    new = f"moisture {t['moisture_pct']}%, deduction {t['deduction_value']}{t['deduction_type']}"
    save_edit(tickets, t, "deductions", old, new, lines)


def edit_time(tickets, t, field, label):
    if not t[field]:
        error("This ticket has no time out yet.")
        return
    while True:
        raw = ask(f"{label} (YYYY-MM-DD HH:MM:SS)", t[field], required=True)
        try:
            new = datetime.strptime(raw, STAMP_FMT).strftime(STAMP_FMT)
            break
        except ValueError:
            error("Use a real date and time, for example 2026-11-21 12:54:50.")
    old = t[field]
    t[field] = new
    if t["time_out"]:
        start = datetime.strptime(t["time_in"], STAMP_FMT)
        end = datetime.strptime(t["time_out"], STAMP_FMT)
        if end < start:
            error("Time out cannot be before time in.")
            return
        t["total_time"] = duration(start, end)
    t["date"] = t["time_in"][:10]
    if new != old:
        save_edit(tickets, t, field, old, new)


def edit_product(tickets, t, label):
    if t["second_weight"] and t["multi_weigh"] == "Y":
        weighings = read_table("weighings")
        mine = weighings_for(t["wb_number"], weighings)
        for before, after in zip(mine, mine[1:]):
            drop = abs(int(after["weight_kg"]) - int(before["weight_kg"]))
            after["product"] = pick("products", f"Product for {fmt_kg(drop)}",
                                    after["product"], required=True)
        lines = finalise_lines(t, lines_from_weighings(mine))
        save_edit(tickets, t, "product split", "", "updated", lines, weighings)
    elif t["second_weight"] and t["multi_products"] == "Y":
        lines = finalise_lines(t, split_products(t, int(t["product_weight"])))
        save_edit(tickets, t, "product split", "", "updated", lines)
    else:
        old = t["product"]
        new = pick("products", label, old, required=True)
        if new == old:
            return
        t["product"] = new
        lines = None
        if t["second_weight"]:
            lines = finalise_lines(t, {new: int(t["product_weight"])})
        save_edit(tickets, t, "product", old, new, lines)


def edit_entry(tickets, t, field, label):
    if field == "weights":
        edit_weights(tickets, t)
        return
    if field == "deductions":
        edit_deductions(tickets, t)
        return
    if field in ("time_in", "time_out"):
        edit_time(tickets, t, field, label)
        return
    if field == "product":
        edit_product(tickets, t, label)
        return
    if field == "driver":
        old = f"{t['driver']} / {mask_id(t['driver_id'])}"
        t["driver"], t["driver_id"] = ask_driver(t["driver"], t["driver_id"])
        new = f"{t['driver']} / {mask_id(t['driver_id'])}"
        if new != old:
            save_edit(tickets, t, field, old, new)
        return

    old = t[field]
    if field == "reg_no":
        new = clean_reg(ask(label, old, required=True))
        if new != old and any(o["reg_no"] == new and o["status"] in ACTIVE_STATUSES for o in tickets):
            error(f"{new} already has an active ticket.")
            return
    elif field == "account":
        new = pick("customers", label, old, required=True, allowed=("CASUAL",))
    elif field == "delivery_type":
        new = ask_delivery_type(old)
    elif field == "action":
        if len(weighings_for(t["wb_number"], read_table("weighings"))) > 1:
            error("The weights decide the action now. Edit the weights to change it.")
            return
        new = ask_action(old)
    elif field in PICK_TABLES:
        new = pick(PICK_TABLES[field], label, old)
    elif field == "container":
        new = "Y" if confirm("Container?", default=old == "Y") else "N"
    elif field == "cust_weight":
        new = str(ask_int(label, default=old or 0))
    else:
        new = ask(label, old)
    if new != old:
        t[field] = new
        save_edit(tickets, t, field, old, new)


def edit_entries(wb_number):
    """Change entries one at a time until the operator chooses Done."""
    while True:
        tickets, t = load_ticket(wb_number)
        heading(f"EDIT TICKET {wb_number}   (/c cancels one change)")
        show_ticket(t)
        print()
        for number, (_, label) in enumerate(ENTRY_OPTIONS, 1):
            end = "\n" if number % 2 == 0 else ""
            print(f"  {number:>2}. {label:<26}", end=end)
        print("\n   0. Done" if len(ENTRY_OPTIONS) % 2 else "   0. Done")
        try:
            choice = ask_int("Entry to change", default=0, minimum=0, maximum=len(ENTRY_OPTIONS))
            if choice == 0:
                return
            field, label = ENTRY_OPTIONS[choice - 1]
            edit_entry(tickets, t, field, label)
        except InputClosed:
            raise
        except Cancelled:
            info("Change cancelled.")


def edit_ticket():
    heading("EDIT TICKET   (/c to go back)")
    t = find_ticket(read_table("tickets"))
    if t is None:
        return
    if t["status"] == "VOID":
        error("Void tickets cannot be edited.")
        return
    if t["status"] == "REVIEW":
        review_ticket(t["wb_number"])
        return
    edit_entries(t["wb_number"])
    _, updated = load_ticket(t["wb_number"])
    if updated["status"] == "COMPLETE" and updated != t:
        heading(f"UPDATED RECEIPT - TICKET {t['wb_number']}")
        show_receipt(updated)
        saved = save_receipt(updated)
        offer_print(updated, saved)



# search and reports

def ticket_row(t):
    return [t["wb_number"], t["date"], t["status"], t["reg_no"], t["account"][:20],
            t["product"][:20], fmt_kg(t["payable_weight"]) if t["payable_weight"] else "-"]


def search_tickets():
    heading("SEARCH TICKETS")
    term = ask("Search reg, account, product, PO, note, driver or WB No", required=True).upper()
    fields = ("wb_number", "reg_no", "account", "product", "order_no", "delivery_note",
              "transporter", "driver")
    hits = [t for t in read_table("tickets") if any(term in t[f].upper() for f in fields)]
    print_table(["WB No", "Date", "Status", "Reg No", "Account", "Product", "Payable"],
                [ticket_row(t) for t in hits[-30:][::-1]])
    if len(hits) > 30:
        info(f"Showing the latest 30 of {len(hits)} matches.")


def summarise(items):
    """items are (group, wb_number, net, payable) tuples."""
    groups = {}
    for group, wb_number, net, payable in items:
        entry = groups.setdefault(group or "(none)", {"loads": set(), "net": 0, "payable": 0})
        entry["loads"].add(wb_number)
        entry["net"] += net
        entry["payable"] += payable
    return [[name, len(e["loads"]), fmt_kg(e["net"]), fmt_kg(e["payable"])]
            for name, e in sorted(groups.items())]


def reports():
    heading("REPORT")
    today = date.today().isoformat()
    start = ask_date("From", today)
    end = ask_date("To", start)
    if end < start:
        error("The end date is before the start date.")
        return
    tickets = [t for t in read_table("tickets")
               if t["status"] == "COMPLETE" and start <= t["date"] <= end]
    if not tickets:
        info("No completed tickets in that period.")
        return
    wanted = {t["wb_number"] for t in tickets}
    heads = ["Name", "Loads", "Net", "Payable"]
    print("\n  BY PRODUCT")
    print_table(heads, summarise(
        (l["product"], l["wb_number"], int(l["net_kg"]), int(l["payable_kg"]))
        for l in read_table("ticket_lines") if l["wb_number"] in wanted))
    for title, field in (("BY ACCOUNT", "account"), ("BY TRANSPORTER", "transporter")):
        print(f"\n  {title}")
        print_table(heads, summarise(
            (t[field], t["wb_number"], int(t["product_weight"]), int(t["payable_weight"]))
            for t in tickets))
    total_net = sum(int(t["product_weight"]) for t in tickets)
    total_payable = sum(int(t["payable_weight"]) for t in tickets)
    print(paint(f"\n  TOTAL: {len(tickets)} loads, net {fmt_kg(total_net)}, "
                f"payable {fmt_kg(total_payable)}", "1"))
    if confirm("Export these tickets to CSV?", default=False):
        fields = [f for f in TICKET_FIELDS if f != "driver_id"]
        path = EXPORT_DIR / f"tickets_{start}_to_{end}.csv"
        with open(path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows({k: spreadsheet_safe(v) for k, v in t.items()} for t in tickets)
        info(f"Exported to {path}")


def spreadsheet_safe(value):
    """Stop Excel running typed text such as =HYPERLINK(...) as a formula."""
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


# master data and settings

def ask_master_field(field, default=""):
    if field.endswith("_pct"):
        return str(ask_decimal(field.replace("_", " ").capitalize(), default or "0",
                               maximum=Decimal(100)))
    value = ask(field.replace("_", " ").capitalize(), default)
    if field == "reg_no":
        return clean_reg(value)
    return value if field in ("id_number", "phone") else " ".join(value.upper().split())


def display_master(field, value):
    return mask_id(value) if field == "id_number" else value


def manage_table(name, title):
    fields = SCHEMAS[name]
    key = fields[0]
    while True:
        heading(f"{title.upper()}   (/c to go back)")
        rows = read_table(name)
        print_table(["#"] + [f.replace("_", " ").title() for f in fields],
                    [[i] + [display_master(f, r[f]) for f in fields] for i, r in enumerate(rows, 1)])
        action = ask("A = add, E = edit, D = delete, B = back", "B").upper()
        if action == "B":
            return
        if action == "A":
            record = {field: ask_master_field(field) for field in fields}
            if not record[key]:
                error("A name is required.")
            elif any(r[key] == record[key] for r in rows):
                error(f"{record[key]} already exists.")
            else:
                commit({name: rows + [record]})
        elif action in ("E", "D"):
            if not rows:
                continue
            row = rows[ask_int("Row #", minimum=1, maximum=len(rows)) - 1]
            if action == "D":
                if confirm(f"Delete {row[key]}? Past tickets keep their copy", default=False):
                    rows.remove(row)
                    commit({name: rows})
            else:
                updated = {field: ask_master_field(field, row[field]) for field in fields}
                if not updated[key] or any(r is not row and r[key] == updated[key] for r in rows):
                    error("That name is empty or already exists.")
                else:
                    row.update(updated)
                    commit({name: rows})
        else:
            error("Enter A, E, D or B.")


def master_menu():
    sections = [("customers", "Customers"), ("products", "Products"), ("vehicles", "Vehicles"),
                ("vehicle_types", "Vehicle descriptions"), ("transporters", "Transporters"),
                ("drivers", "Contract drivers"), ("depots", "Depots"), ("bins", "Bins")]
    while True:
        heading("MASTER DATA")
        for number, (_, title) in enumerate(sections, 1):
            print(f"  {number}. {title}")
        print("  0. Back")
        choice = ask_int("Choice", default=0, minimum=0, maximum=len(sections))
        if choice == 0:
            return
        try:
            manage_table(*sections[choice - 1])
        except Cancelled:
            pass


def settings_menu():
    while True:
        tickets = read_table("tickets")
        heading("SETTINGS")
        for number, (key, label) in enumerate(COMPANY_SETTINGS, 1):
            print(f"  {number:>2}. {label:<16}: {get_setting(key)}")
        wb_choice = len(COMPANY_SETTINGS) + 1
        scale_choice = wb_choice + 1
        print(f"  {wb_choice:>2}. {'Next WB number':<16}: {next_wb_number(tickets)}")
        print(f"  {scale_choice:>2}. {'Current scale':<16}: {get_setting('current_scale')}")
        print("   0. Back")
        choice = ask_int("Choice", default=0, minimum=0, maximum=scale_choice)
        if choice == 0:
            return
        if choice < wb_choice:
            key, label = COMPANY_SETTINGS[choice - 1]
            value = ask(f"{label} (- to clear)", get_setting(key))
            commit({"settings": settings_with(key, "" if value == "-" else value)})
        elif choice == wb_choice:
            lowest = max((int(t["wb_number"]) for t in tickets), default=0) + 1
            value = ask_int("Next WB number", default=next_wb_number(tickets),
                            minimum=lowest, maximum=999_999_999)
            commit({"settings": settings_with("next_wb_number", value)})
        else:
            value = {"1": "Scale 1", "2": "Scale 2", "M": "Manual"}.get(
                ask("1 = Scale 1, 2 = Scale 2, M = Manual", "M").upper())
            if value:
                commit({"settings": settings_with("current_scale", value)})
            else:
                error("Enter 1, 2 or M.")


def on_site_screen():
    heading("VEHICLES ON SITE")
    show_on_site(read_table("tickets"))


# main menu

MENU = [
    ("1", "Weigh in (new ticket)", weigh_in),
    ("2", "Weigh out (second weight)", weigh_out),
    ("3", "Review and submit tickets", review_menu),
    ("4", "Vehicles on site", on_site_screen),
    ("5", "View ticket", view_ticket),
    ("6", "Edit ticket", edit_ticket),
    ("7", "View receipt", view_receipt),
    ("8", "Delete (void) ticket", void_ticket),
    ("9", "Search tickets", search_tickets),
    ("10", "Reports and export", reports),
    ("11", "Master data", master_menu),
    ("12", "Settings", settings_menu),
]


def parse_args(argv=None):
    parser = argparse.ArgumentParser(prog="weighbridge", description="Weighbridge ticketing system.")
    parser.add_argument("--data", type=Path, metavar="FOLDER",
                        help="data folder (default: WEIGHBRIDGE_DATA or ./data next to this file)")
    parser.add_argument("--check", action="store_true",
                        help="check the data files for damaged rows and exit")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser.parse_args(argv)


def show_menu_header():
    try:
        tickets = read_table("tickets")
        status = (f"On site: {len(open_tickets(tickets))}   "
                  f"To submit: {len(review_tickets(tickets))}   Next WB: {next_wb_number(tickets)}")
        title = get_setting("company_name")
        missing_company = not title
    except Exception:
        log.exception("Could not read data for the main menu")
        title, status = "", paint("Could not read the data files, see the log", "31")
        missing_company = False
    heading(f"{title} - WEIGHBRIDGE SYSTEM" if title else "WEIGHBRIDGE SYSTEM")
    try:
        scale = get_setting("current_scale")
    except Exception:
        scale = "?"
    print(f"  {date.today().isoformat()}   Scale: {scale}   {status}\n")
    if missing_company:
        warn("Company details are not set, so receipts have no letterhead. Add them under Settings.\n")


def run_menu():
    actions = {key: (label, action) for key, label, action in MENU}
    while True:
        run_daily_backup()
        show_menu_header()
        for key, label, _ in MENU:
            print(f"  {key:>2}. {label}")
        print("   0. Exit")
        try:
            choice = input("\n  Choice: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if choice == "0":
            return
        if choice not in actions:
            error("Choose a number from the menu.")
            continue
        label, action = actions[choice]
        try:
            action()
        except (Cancelled, KeyboardInterrupt):
            print()
            info("Cancelled. Nothing further was saved.")
        except OSError as exc:
            log.exception("File problem during %s", label)
            error(f"File problem: {exc}")
        except Exception as exc:
            log.exception("Unexpected error during %s", label)
            error(f"Something went wrong ({type(exc).__name__}: {exc}). "
                  f"The action was stopped. Details are in {log_path()}")
            info("If a data file was edited by hand, run the program with --check.")


def main(argv=None):
    args = parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    if USE_COLOR and os.name == "nt":
        os.system("")  # Turns on colour codes in the Windows console.
    if args.data:
        configure(args.data)
    try:
        init_storage()
        setup_logging()
    except OSError as exc:
        error(f"Cannot use the data folder {DATA_DIR}: {exc}")
        return 1

    if args.check:
        problems = check_data()
        for problem in problems:
            print(f"  {problem}")
        info("No problems found." if not problems else f"{len(problems)} problem(s) found.")
        return 1 if problems else 0

    lock = acquire_lock()
    if lock is None:
        error(f"The weighbridge program is already running on {DATA_DIR}.")
        info("Use the window that is already open. Two copies would overwrite each other's tickets.")
        return 1
    log.info("Started version %s by %s, data %s", __version__, current_user(), DATA_DIR)
    try:
        try:
            problems = check_data()
        except Exception as exc:
            log.exception("Data check failed")
            problems = [f"The data files could not be checked: {exc}"]
        if problems:
            log.warning("Data check found %d problem(s): %s", len(problems), "; ".join(problems))
            heading("DATA PROBLEMS FOUND")
            for problem in problems[:20]:
                warn(problem)
            if len(problems) > 20:
                warn(f"... and {len(problems) - 20} more (see {log_path()})")
            info("Close the program to fix these files by hand, or restore a backup.")
            try:
                input("  Press Enter to continue anyway: ")
            except (EOFError, KeyboardInterrupt):
                print()
                return 1
        run_menu()
    finally:
        log.info("Stopped")
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
