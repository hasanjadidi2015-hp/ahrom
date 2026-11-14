"""
capture.py  (ضبط داده خام برای آزمون)

- هر ۱۰ ثانیه در پیش‌گشایش و معاملات ذخیره می‌کند:
  ۱) رکورد نمادهای پایه از فهرست سهام
  ۲) چند قرارداد نزدیک‌ترین قیمت اعمال به هر نماد پایه، به‌همراه ضهرم۸۰۲۴
  ۳) دفتر سفارش این قراردادها از endpoint BestLimits
- خروجی: logs/raw_YYYYMMDD.jsonl (هر خط یک چرخه)
- engine.py را تغییر نمی‌دهد. خطای شبکه ثبت می‌شود و برنامه متوقف نمی‌شود.

دستورها:
    python capture.py         حلقه‌ی ضبط (توقف با Ctrl+C)
    python capture.py once    یک چرخه، بدون توجه به ساعت بازار (برای تست)
"""

import json
import os
import sys
import time

import engine as e

POLL_SEC = 10
IDLE_SEC = 60
FAIL_BACKOFF_SEC = 30
MAX_FAILS_BEFORE_BACKOFF = 6
NEAREST_PER_BASE = 3
ALWAYS_WATCH = ("ضهرم8024",)
BESTLIMITS_URL = "https://cdn.tsetmc.com/api/BestLimits/{}"


def raw_path(now):
    os.makedirs(e.LOG_DIR, exist_ok=True)
    return os.path.join(e.LOG_DIR, f"raw_{now:%Y%m%d}.jsonl")


def append_line(path, obj):
    line = json.dumps(obj, ensure_ascii=False, default=str)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def index_stocks(stocks):
    out = {}
    for st in stocks:
        nm = e.normalize_text(st.get("lva"))
        if nm and nm not in out:
            out[nm] = st
    return out


def pick_watch(options, stocks, cfg):
    """
    خروجی: (دیکشنری کد -> رکورد اختیار, دیکشنری نماد پایه -> رکورد سهام)
    برای هر نماد پایه، نزدیک‌ترین قیمت‌های اعمال به قیمت میانی دفتر سفارش انتخاب می‌شوند.
    اگر قیمت پایه در دسترس نباشد، برای آن نماد قراردادی انتخاب نمی‌شود.
    """
    by_name = index_stocks(stocks)
    watch = {}
    und = {}

    for code in ALWAYS_WATCH:
        for rec in options:
            if e.normalize_text(rec.get("lva")) == code:
                watch[code] = rec
                break

    for sym in e.cfg_symbols(cfg):
        st = by_name.get(sym)
        if st is None:
            continue
        und[sym] = st
        book = e.extract_book(st)
        if not (book["bid"] and book["ask"]):
            continue
        mid = (book["bid"] + book["ask"]) / 2.0

        cands = []
        for rec in options:
            if not e.is_option_record(rec):
                continue
            code = e.normalize_text(rec.get("lva"))
            try:
                info = e.parse_option_name(rec.get("lvc"), code)
            except e.ParseError:
                continue
            if info["base"] == sym:
                cands.append((abs(info["strike"] - mid), code, rec))
        cands.sort(key=lambda t: t[0])
        for _, code, rec in cands[:NEAREST_PER_BASE]:
            watch.setdefault(code, rec)

    return watch, und


def fetch_bestlimits(ins):
    data = e._http_get_json(BESTLIMITS_URL.format(ins))
    rows = data.get("bestLimits") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise e.DataSourceError("ساختار BestLimits نامعتبر است")
    return [r for r in rows if isinstance(r, dict)]


def capture_once(cfg, now):
    t0 = time.perf_counter()
    status = e.market_status(now)
    path = raw_path(now)
    ts = now.isoformat(timespec="seconds")

    try:
        options = e.fetch_market_watch(e.MARKET_OPTION, e.OPTION_PAPER_TYPE)
        stocks = e.fetch_market_watch(e.MARKET_STOCK)
    except e.EngineError as exc:
        append_line(path, {"ts": ts, "status": status, "ok": False, "error": str(exc)})
        raise

    watch, und = pick_watch(options, stocks, cfg)

    contracts = []
    for code, rec in watch.items():
        item = {"code": code, "record": rec}
        try:
            item["bestlimits"] = fetch_bestlimits(e.normalize_text(rec.get("insCode")))
        except e.EngineError as exc:
            item["bestlimits_error"] = str(exc)
        contracts.append(item)

    elapsed = time.perf_counter() - t0
    append_line(path, {
        "ts": ts,
        "status": status,
        "ok": True,
        "elapsed_sec": round(elapsed, 2),
        "option_count": len(options),
        "stock_count": len(stocks),
        "underlying": und,
        "contracts": contracts,
    })

    return {
        "status": status,
        "watch": len(watch),
        "bl_ok": sum(1 for c in contracts if "bestlimits" in c),
        "elapsed": round(elapsed, 1),
    }


def describe(now, summary):
    return (
        f"{now:%H:%M:%S} | {summary['status']} | مورد رصد: {summary['watch']} | "
        f"BestLimits موفق: {summary['bl_ok']} | {summary['elapsed']} ثانیه"
    )


def run_loop(cfg):
    fails = 0
    print("ضبط داده شروع شد. برای توقف Ctrl+C را بزنید.")
    while True:
        now = e.iran_now()
        status = e.market_status(now)
        if status not in e.ENTRY_STATUSES:
            time.sleep(IDLE_SEC)
            continue

        t0 = time.perf_counter()
        try:
            summary = capture_once(cfg, now)
            fails = 0
            print(describe(now, summary))
        except e.EngineError as exc:
            fails += 1
            print(f"{now:%H:%M:%S} | خطا ({fails}): {exc}")
            e.log_event("WARNING", "capture", str(exc), {"fails": fails})
        except Exception as exc:
            fails += 1
            print(f"{now:%H:%M:%S} | خطای غیرمنتظره ({fails}): {exc}")
            e.logger.exception("خطای غیرمنتظره در ضبط")
            e.log_event("ERROR", "capture", "خطای غیرمنتظره در ضبط", {"error": str(exc)})

        wait = FAIL_BACKOFF_SEC if fails >= MAX_FAILS_BEFORE_BACKOFF else POLL_SEC
        time.sleep(max(0.0, wait - (time.perf_counter() - t0)))


def main(argv):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    cfg = e.EngineConfig()
    cfg.validate()

    if argv and argv[0] == "once":
        now = e.iran_now()
        try:
            summary = capture_once(cfg, now)
        except e.EngineError as exc:
            print(f"خطا: {exc}")
            return 1
        print(describe(now, summary))
        print(f"ذخیره شد در: {raw_path(now)}")
        return 0

    try:
        return run_loop(cfg)
    except KeyboardInterrupt:
        print("ضبط متوقف شد.")
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))