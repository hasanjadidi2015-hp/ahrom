"""
engine.py  (نسخه ۴)
موتور داده و محاسبات دستیار اسکلپ اختیار معامله (بورس ایران)

تفاوت‌های مهم با نسخه ۳:
- قیمت خرید (Bid) و فروش (Ask) فقط از دفتر سفارش (blDs) خوانده می‌شود.
  فیلدهای سطح بالای pmd و pmo فقط برای ممیزی ثبت می‌شوند و هرگز قیمت محسوب نمی‌شوند.
- خارج از پیش‌گشایش و معاملات هیچ قیمتی نمایش داده یا استفاده نمی‌شود.
- تشخیص داده ایستا فقط در ساعت معامله و بر اساس تغییر قیمت یا دفتر سفارش انجام می‌شود.
  حجم معاملات (zTotTran) و dEven برای این کار استفاده نمی‌شوند.
- دستورهای خط فرمان:
    python engine.py check
    python engine.py dump <نام یا کد نماد>

هیچ داده ساختگی، قیمت پیش‌فرض یا نماد جایگزین در این فایل وجود ندارد.
"""

import json
import logging
import math
import os
import re
import sqlite3
import sys
import threading
import time
import unicodedata
import zlib
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, time as dtime
from logging.handlers import RotatingFileHandler
from urllib import parse as urlparse
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

import jdatetime


# ---------------------------------------------------------------------------
# ثابت‌ها و مسیرها
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
LOG_FILE = os.path.join(LOG_DIR, "app.log")
DB_PATH = os.path.join(BASE_DIR, "market.db")

IRAN_TZ = timezone(timedelta(hours=3, minutes=30))

TSETMC_MARKETWATCH_URL = "https://cdn.tsetmc.com/api/ClosingPrice/GetMarketWatch"
MARKET_STOCK = 1
MARKET_OPTION = 3
OPTION_PAPER_TYPE = 7
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://www.tsetmc.com/",
    "Accept": "application/json, text/plain, */*",
}
HTTP_TIMEOUT_SEC = 10
HTTP_RETRIES = 2                    # تعداد تلاش مجدد پس از خطای اول

CONTRACT_SIZE = 1000                # تعداد سهم در هر قرارداد اختیار
RIAL_PER_TOMAN = 10
QTC_TO_MIL_RIAL = 1_000_000         # فرض: qtc بر حسب ریال است (تأیید نشده)

# weekday(): دوشنبه=0 ... جمعه=4، شنبه=5، یکشنبه=6
TRADING_WEEKDAYS = {5, 6, 0, 1, 2}
PREOPEN_START = dtime(8, 45)
OPEN_START = dtime(9, 0)
CLOSE_TIME = dtime(12, 30)
ENTRY_STATUSES = ("PREOPEN", "OPEN")
HOLIDAYS = {"2026-12-23", "2027-01-06"}     # تاریخ میلادی YYYY-MM-DD

STALE_SEC = 180                     # بعد از این مدت بدون تغییر در ساعت معامله، داده ایستا است
DELTA_CENTER = 0.55
DELTA_HALF_WIDTH = 0.20
SCENARIO_MOVES_PCT = (-2.0, -1.0, 0.0, 1.0, 2.0, 5.0)
ALERT_COOLDOWN_MIN = 10
LOG_RETENTION_DAYS = 14
EVENT_RETENTION_DAYS = 30
MAX_LIST_ITEMS = 200
AUDIT_LIMIT = 300

CALL_PREFIX = "ض"
PUT_PREFIX = "ط"

NAME_RE = re.compile(
    r"^اختیار\s*([خف])\s+(.+?)\s*-\s*(\d{1,7})\s*-\s*(\d{4})/(\d{1,2})/(\d{1,2})$"
)


# ---------------------------------------------------------------------------
# خطاها
# ---------------------------------------------------------------------------

class EngineError(Exception):
    """خطای پایه موتور"""


class DataSourceError(EngineError):
    """خطا در دریافت داده از TSETMC"""


class DataEmptyError(DataSourceError):
    """پاسخ TSETMC خالی است"""


class ParseError(EngineError):
    """داده یک قرارداد قابل تفسیر نیست"""


class ConfigError(EngineError):
    """تنظیمات نامعتبر است"""


# ---------------------------------------------------------------------------
# زمان ایران
# ---------------------------------------------------------------------------

def iran_now():
    """زمان فعلی ایران (بدون منطقه زمانی، برای مقایسه با ساعت بازار)"""
    return datetime.now(timezone.utc).astimezone(IRAN_TZ).replace(tzinfo=None)


def _now_iso():
    return iran_now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# لاگ فایلی
# ---------------------------------------------------------------------------

logger = logging.getLogger("ahrom")
_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def setup_logging():
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        handler = RotatingFileHandler(
            LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        logger.addHandler(handler)
    except OSError as exc:
        print(f"ساخت فایل لاگ ممکن نشد: {exc}")


# ---------------------------------------------------------------------------
# پایگاه داده SQLite
# ---------------------------------------------------------------------------

def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(_connect()) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                level TEXT NOT NULL,
                component TEXT NOT NULL,
                message TEXT NOT NULL,
                details TEXT
            );
            CREATE TABLE IF NOT EXISTS fetch_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                ok INTEGER NOT NULL,
                latency_ms INTEGER,
                n_records INTEGER,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                payload BLOB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS alerts_sent (
                symbol TEXT PRIMARY KEY,
                sent_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_snap_date ON snapshots(trade_date);
            """
        )
        # سازگاری با جدول نسخه ۳ (ستون n_options به n_records تغییر نام می‌یابد)
        cols = [row[1] for row in conn.execute("PRAGMA table_info(fetch_log)").fetchall()]
        if "n_options" in cols and "n_records" not in cols:
            conn.execute("ALTER TABLE fetch_log RENAME COLUMN n_options TO n_records")
        conn.commit()


def cleanup_old_rows():
    now = iran_now()
    fetch_cut = (now - timedelta(days=LOG_RETENTION_DAYS)).isoformat(timespec="seconds")
    event_cut = (now - timedelta(days=EVENT_RETENTION_DAYS)).isoformat(timespec="seconds")
    try:
        with closing(_connect()) as conn:
            conn.execute("DELETE FROM fetch_log WHERE ts < ?", (fetch_cut,))
            conn.execute("DELETE FROM events WHERE ts < ?", (event_cut,))
            conn.commit()
    except sqlite3.Error as exc:
        logger.error("پاکسازی پایگاه داده ناموفق: %s", exc)


def log_event(level, component, message, details=None):
    """هرگز خطا پرتاب نمی‌کند. همهٔ رویدادها در فایل و جدول events ثبت می‌شوند."""
    try:
        lvl = _LEVELS.get(str(level).upper(), logging.INFO)
        logger.log(lvl, "[%s] %s", component, message)
    except Exception:
        pass
    try:
        details_text = None
        if details is not None:
            details_text = json.dumps(details, ensure_ascii=False, default=str)
        with closing(_connect()) as conn:
            conn.execute(
                "INSERT INTO events (ts, level, component, message, details) VALUES (?,?,?,?,?)",
                (_now_iso(), str(level).upper(), component, str(message), details_text),
            )
            conn.commit()
    except Exception as exc:
        try:
            logger.error("ثبت رویداد در پایگاه داده ناموفق: %s", exc)
        except Exception:
            pass


def _log_fetch(ok, latency_ms, n_records, error):
    try:
        with closing(_connect()) as conn:
            conn.execute(
                "INSERT INTO fetch_log (ts, ok, latency_ms, n_records, error) VALUES (?,?,?,?,?)",
                (_now_iso(), 1 if ok else 0, latency_ms, n_records, error),
            )
            conn.commit()
    except sqlite3.Error as exc:
        logger.error("ثبت fetch_log ناموفق: %s", exc)


# ---------------------------------------------------------------------------
# متن و عدد
# ---------------------------------------------------------------------------

def _build_trans():
    table = {}
    for i in range(10):
        table[0x06F0 + i] = str(i)      # ارقام فارسی
        table[0x0660 + i] = str(i)      # ارقام عربی
    table[0x064A] = "\u06cc"            # ي  -> ی
    table[0x0643] = "\u06a9"            # ك  -> ک
    table[0x0629] = "\u0647"            # ة  -> ه
    table[0x066B] = "."                 # جداکنندهٔ اعشار عربی
    table[0x066C] = None                # جداکنندهٔ هزارگان عربی (حذف)
    table[0x00A0] = " "
    for code in (0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2212, 0xFE63, 0xFF0D):
        table[code] = "-"
    for code in (0x200B, 0x200C, 0x200D, 0x200E, 0x200F,
                 0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0xFEFF):
        table[code] = None
    return table


_TRANS = _build_trans()


def normalize_text(text):
    if text is None:
        return ""
    try:
        s = str(text).translate(_TRANS)
        s = unicodedata.normalize("NFKC", s)
        return re.sub(r"\s+", " ", s).strip()
    except Exception:
        return ""


def to_float(value):
    """عدد یا None. مقدار خالی، غیرعددی یا غیرمتناهی به None تبدیل می‌شود."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            x = float(value)
        else:
            s = normalize_text(value).replace(",", "")
            if s == "":
                return None
            x = float(s)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x):
        return None
    return x


def to_int(value):
    x = to_float(value)
    if x is None:
        return None
    return int(round(x))


def valid_pair(bid, ask):
    return bid is not None and ask is not None and bid > 0 and ask > 0 and ask >= bid


# ---------------------------------------------------------------------------
# دفتر سفارش و ممیزی
# ---------------------------------------------------------------------------

def extract_book(rec):
    """
    بهترین خرید = بالاترین pmd در سطوح دارای قیمت و حجم مثبت
    بهترین فروش = پایین‌ترین pmo در سطوح دارای قیمت و حجم مثبت
    حجم و تعداد صف از qmd/zmd (خرید) و qmo/zmo (فروش) خوانده می‌شود.
    """
    out = {
        "bid": None, "ask": None,
        "bid_vol": None, "ask_vol": None,
        "bid_count": None, "ask_count": None,
        "levels": 0,
    }
    levels = rec.get("blDs")
    if not isinstance(levels, list):
        return out
    rows = [x for x in levels if isinstance(x, dict)]
    out["levels"] = len(rows)

    best_bid = None
    best_ask = None
    for row in rows:
        bp = to_float(row.get("pmd"))
        bq = to_float(row.get("qmd"))
        if bp is not None and bq is not None and bp > 0 and bq > 0:
            if best_bid is None or bp > best_bid[0]:
                best_bid = (bp, bq, to_float(row.get("zmd")))

        ap = to_float(row.get("pmo"))
        aq = to_float(row.get("qmo"))
        if ap is not None and aq is not None and ap > 0 and aq > 0:
            if best_ask is None or ap < best_ask[0]:
                best_ask = (ap, aq, to_float(row.get("zmo")))

    if best_bid is not None:
        out["bid"], out["bid_vol"], out["bid_count"] = best_bid
    if best_ask is not None:
        out["ask"], out["ask_vol"], out["ask_count"] = best_ask
    return out


_AUDITED = set()
_AUDIT_LOCK = threading.Lock()


def _audit_book_vs_top(code, book, rec):
    """اگر قیمت سطح بالا با دفتر سفارش فرق دارد، برای هر نماد یک بار ثبت می‌شود (فقط ممیزی)."""
    try:
        top_bid = to_float(rec.get("pmd"))
        top_ask = to_float(rec.get("pmo"))
        if top_bid == book["bid"] and top_ask == book["ask"]:
            return
        with _AUDIT_LOCK:
            if code in _AUDITED or len(_AUDITED) >= AUDIT_LIMIT:
                return
            _AUDITED.add(code)
        log_event(
            "INFO", "audit",
            f"قیمت سطح بالا با دفتر سفارش متفاوت است: {code}",
            {
                "code": code,
                "top_pmd": top_bid,
                "top_pmo": top_ask,
                "book_bid": book["bid"],
                "book_ask": book["ask"],
            },
        )
    except Exception as exc:
        logger.error("ممیزی قیمت ناموفق برای %s: %s", code, exc)


# ---------------------------------------------------------------------------
# وضعیت بازار
# ---------------------------------------------------------------------------

def market_status(now=None):
    """
    CLOSED_HOLIDAY   تعطیل رسمی
    CLOSED_DAY_OFF   پنجشنبه یا جمعه
    PREOPEN          ۰۸:۴۵ تا ۰۹:۰۰
    OPEN             ۰۹:۰۰ تا ۱۲:۳۰
    CLOSED           خارج از ساعت بالا در روز کاری
    """
    now = now or iran_now()
    if now.strftime("%Y-%m-%d") in HOLIDAYS:
        return "CLOSED_HOLIDAY"
    if now.weekday() not in TRADING_WEEKDAYS:
        return "CLOSED_DAY_OFF"
    t = now.time()
    if PREOPEN_START <= t < OPEN_START:
        return "PREOPEN"
    if OPEN_START <= t < CLOSE_TIME:
        return "OPEN"
    return "CLOSED"


# ---------------------------------------------------------------------------
# تشخیص داده ایستا
# ---------------------------------------------------------------------------

class StaleTracker:
    """برای هر کلید، امضای داده و زمان آخرین تغییر را نگه می‌دارد."""

    def __init__(self):
        self._lock = threading.Lock()
        self._state = {}

    def age(self, key, signature, now):
        """ثانیه‌هایی که امضا بدون تغییر مانده است. اولین بار صفر برمی‌گرداند."""
        with self._lock:
            prev = self._state.get(key)
            if prev is None or prev[0] != signature:
                self._state[key] = (signature, now)
                return 0.0
            return max(0.0, (now - prev[1]).total_seconds())

    def prune(self, seen):
        with self._lock:
            for k in [k for k in self._state if k not in seen]:
                del self._state[k]


TRACKER = StaleTracker()


def is_stale(age, status, stale_sec):
    """فقط در ساعت معامله ایستا تشخیص داده می‌شود."""
    if status != "OPEN":
        return False
    if age is None:
        return True
    return age >= stale_sec


# ---------------------------------------------------------------------------
# تفسیر نام و کد قرارداد
# ---------------------------------------------------------------------------

def is_option_record(rec):
    return normalize_text(rec.get("lvc")).startswith("اختیار")


def parse_option_name(lvc, lva):
    name = normalize_text(lvc)
    m = NAME_RE.match(name)
    if not m:
        raise ParseError(f"نام قرارداد قابل تشخیص نیست: {name}")

    side, base, strike_s, jy, jm, jd = m.groups()
    code = normalize_text(lva)
    if not code:
        raise ParseError("کد قرارداد خالی است")

    is_call = side == "خ"
    prefix = CALL_PREFIX if is_call else PUT_PREFIX
    if not code.startswith(prefix):
        raise ParseError(f"کد {code} با نوع قرارداد {side} سازگار نیست")

    strike = int(strike_s)
    if strike <= 0:
        raise ParseError("قیمت اعمال نامعتبر است")

    try:
        expiry_greg = jdatetime.date(int(jy), int(jm), int(jd)).togregorian()
    except (ValueError, TypeError, OverflowError) as exc:
        raise ParseError(f"تاریخ سررسید نامعتبر: {jy}/{jm}/{jd}") from exc

    return {
        "base": base.strip(),
        "strike": strike,
        "is_call": is_call,
        "expiry_jalali": f"{int(jy):04d}/{int(jm):02d}/{int(jd):02d}",
        "expiry_greg": expiry_greg,
    }


# ---------------------------------------------------------------------------
# Black-Scholes: قیمت، دلتا، IV
# ---------------------------------------------------------------------------

def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _d1(S, K, T, r, sigma):
    return (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))


def bs_price(S, K, T, r, sigma, is_call):
    if S <= 0 or K <= 0:
        raise ValueError("قیمت پایه و قیمت اعمال باید مثبت باشند")
    if T <= 0 or sigma <= 0:
        intrinsic = S - K if is_call else K - S
        return max(intrinsic, 0.0)
    d1 = _d1(S, K, T, r, sigma)
    d2 = d1 - sigma * math.sqrt(T)
    disc = math.exp(-r * T)
    if is_call:
        return S * _norm_cdf(d1) - K * disc * _norm_cdf(d2)
    return K * disc * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def bs_delta(S, K, T, r, sigma, is_call):
    if S <= 0 or K <= 0 or T <= 0 or sigma <= 0:
        return None
    d1 = _d1(S, K, T, r, sigma)
    return _norm_cdf(d1) if is_call else _norm_cdf(d1) - 1.0


def implied_vol(price, S, K, T, r, is_call):
    """IV به روش دوتقسیمی. اگر قیمت خارج از کران قابل قبول باشد None برمی‌گرداند."""
    if price <= 0 or S <= 0 or K <= 0 or T <= 0:
        return None
    disc_k = K * math.exp(-r * T)
    lower = max(S - disc_k, 0.0) if is_call else max(disc_k - S, 0.0)
    upper = S if is_call else disc_k
    if not (lower < price < upper):
        return None

    lo, hi = 1e-4, 5.0
    if bs_price(S, K, T, r, lo, is_call) > price:
        return None
    if bs_price(S, K, T, r, hi, is_call) < price:
        return None

    tol = 1e-6 * max(1.0, price)
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        p = bs_price(S, K, T, r, mid, is_call)
        if abs(p - price) < tol:
            return mid
        if p < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# ---------------------------------------------------------------------------
# تنظیمات و محاسبات معاملاتی
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EngineConfig:
    symbols: tuple = ("اهرم", "خودرو", "شستا", "فملی", "وبملت", "شپنا")
    risk_free_rate: float = 0.30        # اعشاری: 0.30 یعنی ۳۰ درصد
    min_trade_value_mil: float = 50.0   # میلیون ریال
    max_spread_pct: float = 5.0
    delta_min: float = 0.35
    delta_max: float = 0.75
    capital: float = 100_000_000.0      # تومان
    risk_pct: float = 3.0
    fee_pct: float = 0.0
    stale_sec: int = STALE_SEC
    target1_pct: float = 20.0
    target2_pct: float = 40.0
    stop_pct: float = 15.0

    def validate(self):
        if not self.symbols or any(not isinstance(s, str) or not s.strip() for s in self.symbols):
            raise ConfigError("فهرست نمادها نامعتبر است")
        checks = [
            (0.0 <= self.risk_free_rate <= 1.0, "نرخ بدون ریسک باید بین ۰ و ۱۰۰ درصد باشد"),
            (self.target1_pct > 0, "هدف ۱ باید مثبت باشد"),
            (self.target2_pct > self.target1_pct, "هدف ۲ باید بیشتر از هدف ۱ باشد"),
            (0 < self.stop_pct < 100, "حد ضرر نامعتبر است"),
            (self.max_spread_pct > 0, "حداکثر اسپرد باید مثبت باشد"),
            (0.0 <= self.delta_min < self.delta_max <= 1.0, "بازه دلتا نامعتبر است"),
            (self.capital > 0, "سرمایه باید مثبت باشد"),
            (0 < self.risk_pct <= 100, "درصد ریسک نامعتبر است"),
            (0.0 <= self.fee_pct < 10, "کارمزد نامعتبر است"),
            (self.min_trade_value_mil >= 0, "حداقل ارزش معامله نامعتبر است"),
            (self.stale_sec > 0, "مدت تشخیص داده ایستا نامعتبر است"),
        ]
        for ok, msg in checks:
            if not ok:
                raise ConfigError(msg)


def cfg_symbols(cfg):
    return [normalize_text(s) for s in cfg.symbols]


def contract_levels(entry, cfg):
    return {
        "target1": int(round(entry * (1 + cfg.target1_pct / 100.0))),
        "target2": int(round(entry * (1 + cfg.target2_pct / 100.0))),
        "stop": int(round(entry * (1 - cfg.stop_pct / 100.0))),
    }


def position_size(entry, cfg):
    """تعداد قرارداد مجاز بر اساس سرمایه و ریسک هر معامله (واحد پول: تومان)."""
    if entry is None or entry <= 0:
        return 0
    cost_per = entry * CONTRACT_SIZE / RIAL_PER_TOMAN
    loss_per = entry * (cfg.stop_pct / 100.0) * CONTRACT_SIZE / RIAL_PER_TOMAN
    if cost_per <= 0 or loss_per <= 0:
        return 0
    budget = cfg.capital * cfg.risk_pct / 100.0
    n_risk = int(budget // loss_per)
    n_cash = int(cfg.capital // cost_per)
    return max(0, min(n_risk, n_cash))


def pnl_toman(entry, exit_price, qty, fee_pct=0.0):
    """سود/زیان خالص به تومان. کارمزد روی مجموع ارزش ورود و خروج محاسبه می‌شود."""
    gross = (exit_price - entry) * CONTRACT_SIZE * qty / RIAL_PER_TOMAN
    fees = (fee_pct / 100.0) * (entry + exit_price) * CONTRACT_SIZE * qty / RIAL_PER_TOMAN
    return gross - fees


def scenario_table(S, K, days, r, sigma, is_call, entry, fee_pct):
    """سناریوی مدل: قیمت با IV فعلی، با IV کمتر ۵ واحد، و با یک روز کمتر تا سررسید."""
    T = max(days, 1) / 365.0
    T_less = max(T - 1 / 365.0, 1 / 365.0)
    sigma_down = max(sigma - 0.05, 0.01)
    rows = []
    for m in SCENARIO_MOVES_PCT:
        s2 = S * (1 + m / 100.0)
        base_p = bs_price(s2, K, T, r, sigma, is_call)
        iv_p = bs_price(s2, K, T, r, sigma_down, is_call)
        time_p = bs_price(s2, K, T_less, r, sigma, is_call)
        rows.append({
            "move_pct": m,
            "underlying": int(round(s2)),
            "price": int(round(base_p)),
            "price_iv_minus5": int(round(iv_p)),
            "price_one_day_less": int(round(time_p)),
            "pnl_toman_per_contract": int(round(pnl_toman(entry, base_p, 1, fee_pct))),
            "pnl_pct": round((base_p / entry - 1) * 100, 2) if entry > 0 else None,
        })
    return rows


def required_move_pct(S, K, days, r, sigma, is_call, target_price):
    """درصد تغییر لازم قیمت پایه برای رسیدن اختیار به قیمت هدف (با IV و زمان فعلی)."""
    if target_price is None or target_price <= 0 or S <= 0:
        return None
    T = max(days, 1) / 365.0

    def f(m):
        return bs_price(S * (1 + m / 100.0), K, T, r, sigma, is_call) - target_price

    lo, hi = -60.0, 150.0
    flo, fhi = f(lo), f(hi)
    if flo == 0:
        return lo
    if fhi == 0:
        return hi
    if flo * fhi > 0:
        return None

    tol = 1e-6 * max(1.0, target_price)
    for _ in range(100):
        mid = (lo + hi) / 2.0
        fm = f(mid)
        if abs(fm) < tol:
            return round(mid, 2)
        if flo * fm < 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return round((lo + hi) / 2.0, 2)


def _liquidity_grade(trade_val_mil, spread_pct, cfg):
    if trade_val_mil is None:
        return "نامشخص"
    if spread_pct is None:
        return "ضعیف"
    if trade_val_mil >= 5 * cfg.min_trade_value_mil and spread_pct <= cfg.max_spread_pct / 2:
        return "عالی"
    if trade_val_mil >= cfg.min_trade_value_mil and spread_pct <= cfg.max_spread_pct:
        return "خوب"
    return "ضعیف"


def _score(trade_val_mil, spread_pct, delta, cfg):
    liq = min(1.0, trade_val_mil / (10 * cfg.min_trade_value_mil)) if cfg.min_trade_value_mil > 0 else 1.0
    spr = max(0.0, 1 - spread_pct / cfg.max_spread_pct) if cfg.max_spread_pct > 0 else 0.0
    dfit = max(0.0, 1 - abs(abs(delta) - DELTA_CENTER) / DELTA_HALF_WIDTH)
    return round(100 * (0.40 * liq + 0.35 * spr + 0.25 * dfit), 1)


# ---------------------------------------------------------------------------
# تحلیل داده بازار
# ---------------------------------------------------------------------------

def _empty_underlying(reason):
    return {
        "found": False, "usable": False, "reason": reason,
        "bid": None, "ask": None, "mid": None, "source": None,
        "age_sec": None, "stale": False, "h_even": None,
    }


def build_underlying(stocks, cfg, status, now, seen):
    """قیمت نمادهای پایه از دفتر سفارش. فقط نمادهای تنظیمات بررسی می‌شوند."""
    by_name = {}
    for st in stocks:
        nm = normalize_text(st.get("lva"))
        if nm and nm not in by_name:
            by_name[nm] = st

    result = {}
    for sym in cfg_symbols(cfg):
        rec = by_name.get(sym)
        if rec is None:
            result[sym] = _empty_underlying("در فهرست سهام TSETMC پیدا نشد")
            continue

        book = extract_book(rec)
        _audit_book_vs_top(sym, book, rec)
        bid, ask = book["bid"], book["ask"]
        pair_ok = valid_pair(bid, ask)

        key = "u:" + sym
        seen.add(key)
        sig = (bid, ask, rec.get("hEven"))
        age = TRACKER.age(key, sig, now)
        stale = is_stale(age, status, cfg.stale_sec)

        reason = None
        if status not in ENTRY_STATUSES:
            reason = "بازار باز نیست"
        elif not pair_ok:
            reason = "قیمت معتبر در دفتر سفارش نیست"
        elif stale:
            reason = "قیمت پایه ایستا است (احتمالاً قدیمی)"

        usable = reason is None
        result[sym] = {
            "found": True,
            "usable": usable,
            "reason": reason,
            "bid": bid if usable else None,
            "ask": ask if usable else None,
            "mid": (bid + ask) / 2.0 if usable else None,
            "source": "دفتر سفارش" if usable else None,
            "age_sec": round(age, 1) if age is not None else None,
            "stale": stale,
            "h_even": rec.get("hEven"),
        }
    return result


def analyze_contract(rec, info, und_map, cfg, today, status, opt_age):
    gate = status in ENTRY_STATUSES
    code = normalize_text(rec.get("lva"))
    name = normalize_text(rec.get("lvc"))
    base = info["base"]
    strike = info["strike"]
    is_call = info["is_call"]
    days = (info["expiry_greg"] - today).days

    book = extract_book(rec)
    bid = book["bid"] or 0.0
    ask = book["ask"] or 0.0
    bid_vol = book["bid_vol"] or 0.0
    ask_vol = book["ask_vol"] or 0.0

    qtc = to_float(rec.get("qtc"))
    trade_val_mil = qtc / QTC_TO_MIL_RIAL if (qtc is not None and qtc >= 0) else None
    oi = to_int(rec.get("buyOP"))
    if oi is not None and oi < 0:
        oi = None

    reasons = []
    if not gate:
        reasons.append("خارج از ساعت معامله: ورود مجاز نیست")
    if ask <= 0:
        reasons.append("ورود ممکن نیست: Ask صفر است")
    if bid <= 0:
        reasons.append("Bid صفر است")
    if bid > 0 and ask > 0 and ask < bid:
        reasons.append("Ask کمتر از Bid است (داده نامعتبر)")
    if days < 0:
        reasons.append("سررسید گذشته است")
    elif days == 0:
        reasons.append("امروز سررسید است")

    und = und_map.get(base)
    S = None
    if und is None:
        reasons.append("قیمت پایه معتبر نیست")
    elif not und["usable"]:
        if gate:
            reasons.append("قیمت پایه معتبر نیست: " + und["reason"])
    else:
        S = und["mid"]

    if is_stale(opt_age, status, cfg.stale_sec):
        reasons.append("داده قرارداد ایستا است (احتمالاً قدیمی)")

    mid_opt = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else None
    spread_pct = (ask - bid) / mid_opt * 100.0 if mid_opt else None
    if spread_pct is not None and spread_pct > cfg.max_spread_pct:
        reasons.append("اسپرد بالاتر از حد مجاز است")
    if trade_val_mil is None:
        reasons.append("ارزش معامله نامشخص است")
    elif trade_val_mil < cfg.min_trade_value_mil:
        reasons.append("ارزش معامله کم است")
    if bid_vol <= 0 or ask_vol <= 0:
        reasons.append("صف خرید یا فروش خالی است")

    iv = None
    delta = None
    if mid_opt is not None and S is not None and days >= 0:
        T = max(days, 1) / 365.0
        iv = implied_vol(mid_opt, S, strike, T, cfg.risk_free_rate, is_call)
        if iv is None:
            reasons.append("IV محاسبه نشد")
        else:
            delta = bs_delta(S, strike, T, cfg.risk_free_rate, iv, is_call)
    if delta is None:
        reasons.append("دلتا محاسبه نشد")
    elif not (cfg.delta_min <= abs(delta) <= cfg.delta_max):
        reasons.append("دلتا خارج از بازه مجاز است")

    ready = not reasons
    liquidity = _liquidity_grade(trade_val_mil, spread_pct, cfg)
    score = _score(trade_val_mil, spread_pct, delta, cfg) if ready else None

    entry = ask if (gate and ask > 0) else None
    levels = contract_levels(entry, cfg) if entry else {"target1": None, "target2": None, "stop": None}
    qty = position_size(entry, cfg) if entry else 0

    scenario, req1, req2 = [], None, None
    if iv is not None and S is not None and entry:
        scenario = scenario_table(S, strike, days, cfg.risk_free_rate, iv, is_call, entry, cfg.fee_pct)
        req1 = required_move_pct(S, strike, days, cfg.risk_free_rate, iv, is_call, levels["target1"])
        req2 = required_move_pct(S, strike, days, cfg.risk_free_rate, iv, is_call, levels["target2"])

    return {
        "symbol": code,
        "name": name,
        "base": base,
        "is_call": is_call,
        "type": "اختیار خرید" if is_call else "اختیار فروش",
        "strike": strike,
        "expiry": info["expiry_jalali"],
        "expiry_greg": info["expiry_greg"].isoformat(),
        "days": days,
        "bid": bid if (gate and bid > 0) else None,
        "ask": ask if (gate and ask > 0) else None,
        "entry": entry,
        "mid": mid_opt if gate else None,
        "bid_vol": book["bid_vol"],
        "ask_vol": book["ask_vol"],
        "bid_count": book["bid_count"],
        "ask_count": book["ask_count"],
        "spread_pct": round(spread_pct, 2) if (spread_pct is not None and gate) else None,
        "delta": round(delta, 3) if delta is not None else None,
        "iv": round(iv, 4) if iv is not None else None,
        "oi": oi,
        "trade_val_mil": round(trade_val_mil, 2) if trade_val_mil is not None else None,
        "underlying": S,
        "underlying_source": und.get("source") if und else None,
        "underlying_age_sec": und.get("age_sec") if und else None,
        "option_age_sec": round(opt_age, 1) if opt_age is not None else None,
        "target1": levels["target1"],
        "target2": levels["target2"],
        "stop": levels["stop"],
        "qty": qty,
        "liquidity": liquidity,
        "score": score,
        "ready": ready,
        "reasons": reasons,
        "scenario": scenario,
        "req_move_t1": req1,
        "req_move_t2": req2,
        "raw": rec,
    }


def analyze_snapshot(options, stocks, cfg, now=None):
    cfg.validate()
    now = now or iran_now()
    today = now.date()
    status = market_status(now)
    symbols = set(cfg_symbols(cfg))

    seen = set()
    und = build_underlying(stocks, cfg, status, now, seen)

    contracts = []
    parse_errors = []
    option_count = 0
    skipped_non_option = 0
    skipped_other_base = 0
    book_empty = 0

    for rec in options:
        code = normalize_text(rec.get("lva"))
        name = normalize_text(rec.get("lvc"))
        try:
            if not is_option_record(rec):
                skipped_non_option += 1
                continue
            option_count += 1

            info = parse_option_name(name, code)
            if info["base"] not in symbols:
                skipped_other_base += 1
                continue

            book = extract_book(rec)
            if book["bid"] is None and book["ask"] is None:
                book_empty += 1
            _audit_book_vs_top(code, book, rec)

            key = "o:" + code
            seen.add(key)
            sig = (book["bid"], book["ask"], book["bid_vol"], book["ask_vol"], rec.get("hEven"))
            opt_age = TRACKER.age(key, sig, now)

            contracts.append(analyze_contract(rec, info, und, cfg, today, status, opt_age))
        except ParseError as exc:
            parse_errors.append({"code": code, "name": name, "reason": str(exc)})
        except Exception as exc:
            logger.exception("خطای غیرمنتظره در تحلیل قرارداد %s", code)
            parse_errors.append({"code": code, "name": name, "reason": f"خطای داخلی: {exc}"})

    TRACKER.prune(seen)

    contracts.sort(key=lambda c: (not c["ready"], -(c["score"] or 0), c["days"]))

    rejected = [
        {"code": c["symbol"], "name": c["name"], "reasons": c["reasons"]}
        for c in contracts if not c["ready"]
    ][:MAX_LIST_ITEMS]

    return {
        "ts": now.isoformat(timespec="seconds"),
        "status": status,
        "market_open": status in ENTRY_STATUSES,
        "underlying": und,
        "stale_underlyings": [s for s, u in und.items() if u.get("stale")],
        "contracts": contracts,
        "ready_count": sum(1 for c in contracts if c["ready"]),
        "rejected": rejected,
        "parse_errors": parse_errors[:MAX_LIST_ITEMS],
        "option_count": option_count,
        "skipped_non_option": skipped_non_option,
        "skipped_other_base": skipped_other_base,
        "book_empty_count": book_empty,
        "stock_count": len(stocks),
    }


# ---------------------------------------------------------------------------
# دریافت داده از TSETMC
# ---------------------------------------------------------------------------

def _http_get_json(url):
    req = urlrequest.Request(url, headers=HTTP_HEADERS)
    try:
        with urlrequest.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as resp:
            raw = resp.read()
    except HTTPError as exc:
        raise DataSourceError(f"سرور TSETMC خطای HTTP {exc.code} داد") from exc
    except (URLError, OSError) as exc:
        raise DataSourceError(f"اتصال به TSETMC برقرار نشد: {exc}") from exc
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataSourceError("پاسخ TSETMC JSON معتبر نیست (احتمالاً صفحه مسدودسازی شبکه)") from exc


def _extract_records(payload):
    if not isinstance(payload, dict) or "marketwatch" not in payload:
        raise DataSourceError("کلید marketwatch در پاسخ TSETMC وجود ندارد")
    items = payload.get("marketwatch")
    if not isinstance(items, list):
        raise DataSourceError("ساختار marketwatch نامعتبر است")
    records = [x for x in items if isinstance(x, dict)]
    if not records:
        raise DataEmptyError("پاسخ TSETMC خالی است")
    return records


def fetch_market_watch(market, paper_type=None):
    url = f"{TSETMC_MARKETWATCH_URL}?market={market}"
    if paper_type is not None:
        url += f"&paperTypes%5B0%5D={paper_type}"

    t0 = time.perf_counter()
    records = None
    last_error = None
    for attempt in range(HTTP_RETRIES + 1):
        try:
            records = _extract_records(_http_get_json(url))
            break
        except DataSourceError as exc:
            last_error = exc
            if attempt < HTTP_RETRIES:
                time.sleep(0.5 * (attempt + 1))

    latency_ms = int((time.perf_counter() - t0) * 1000)
    if records is None:
        msg = str(last_error)
        _log_fetch(False, latency_ms, 0, msg)
        log_event("ERROR", "fetch", f"دریافت از TSETMC ناموفق (بازار {market})", {"error": msg})
        raise last_error

    _log_fetch(True, latency_ms, len(records), None)
    return records


def fetch_snapshot(cfg):
    options = fetch_market_watch(MARKET_OPTION, OPTION_PAPER_TYPE)
    stocks = fetch_market_watch(MARKET_STOCK)
    snap = analyze_snapshot(options, stocks, cfg)
    snap["fetch_ok"] = True
    return snap


def try_fetch_snapshot(cfg):
    """
    خروجی: (snapshot, None) یا (None, پیام خطا).
    هرگز خطا پرتاب نمی‌کند تا داشبورد قطع نشود.
    """
    try:
        return fetch_snapshot(cfg), None
    except EngineError as exc:
        return None, str(exc)
    except Exception as exc:
        logger.exception("خطای غیرمنتظره در دریافت یا تحلیل")
        log_event("ERROR", "engine", "خطای غیرمنتظره در دریافت یا تحلیل", {"error": str(exc)})
        return None, f"خطای داخلی: {exc}"


# ---------------------------------------------------------------------------
# تلگرام با فاصله زمانی
# ---------------------------------------------------------------------------

def _mask(text, token):
    if token:
        return str(text).replace(token, "***")
    return str(text)


def send_telegram(token, chat_id, text, timeout=8):
    token = (token or "").strip()
    chat_id = str(chat_id or "").strip()
    if not token or not chat_id:
        return False, "توکن یا Chat ID خالی است"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urlparse.urlencode({"chat_id": chat_id, "text": str(text)[:4000]}).encode("utf-8")
    req = urlrequest.Request(url, data=body, method="POST")
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            resp.read()
    except HTTPError as exc:
        msg = f"تلگرام خطای HTTP {exc.code} داد"
        log_event("WARNING", "telegram", msg)
        return False, msg
    except (URLError, OSError) as exc:
        msg = _mask(f"ارسال تلگرام ناموفق: {exc}", token)
        log_event("WARNING", "telegram", msg)
        return False, msg

    log_event("INFO", "telegram", "پیام تلگرام ارسال شد")
    return True, "ارسال شد"


def can_alert(symbol, cooldown_min=ALERT_COOLDOWN_MIN):
    try:
        with closing(_connect()) as conn:
            row = conn.execute(
                "SELECT sent_at FROM alerts_sent WHERE symbol = ?", (symbol,)
            ).fetchone()
    except sqlite3.Error as exc:
        logger.error("بررسی فاصله هشدار ناموفق: %s", exc)
        return False
    if row is None:
        return True
    try:
        last = datetime.fromisoformat(row["sent_at"])
    except ValueError:
        return True
    return iran_now() - last >= timedelta(minutes=cooldown_min)


def mark_alert(symbol):
    try:
        with closing(_connect()) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO alerts_sent (symbol, sent_at) VALUES (?,?)",
                (symbol, _now_iso()),
            )
            conn.commit()
    except sqlite3.Error as exc:
        logger.error("ثبت زمان هشدار ناموفق: %s", exc)


# ---------------------------------------------------------------------------
# ذخیره و بازپخش اسنپ‌شات‌ها
# ---------------------------------------------------------------------------

def _slim_for_storage(snap):
    slim = dict(snap)
    slim["contracts"] = [
        {k: v for k, v in c.items() if k != "raw"} for c in snap["contracts"]
    ]
    return slim


def store_snapshot(snap):
    try:
        slim = _slim_for_storage(snap)
        payload = zlib.compress(
            json.dumps(slim, ensure_ascii=False, default=str).encode("utf-8"), 6
        )
        with closing(_connect()) as conn:
            conn.execute(
                "INSERT INTO snapshots (ts, trade_date, payload) VALUES (?,?,?)",
                (snap["ts"], snap["ts"][:10], sqlite3.Binary(payload)),
            )
            conn.commit()
        return True
    except (sqlite3.Error, TypeError, ValueError, zlib.error) as exc:
        logger.error("ذخیره اسنپ‌شات ناموفق: %s", exc)
        return False


def list_replay_dates():
    try:
        with closing(_connect()) as conn:
            rows = conn.execute(
                "SELECT DISTINCT trade_date FROM snapshots ORDER BY trade_date DESC"
            ).fetchall()
        return [r["trade_date"] for r in rows]
    except sqlite3.Error as exc:
        logger.error("خواندن فهرست روزها ناموفق: %s", exc)
        return []


def list_replay_snapshots(trade_date):
    try:
        with closing(_connect()) as conn:
            rows = conn.execute(
                "SELECT id, ts FROM snapshots WHERE trade_date = ? ORDER BY ts", (trade_date,)
            ).fetchall()
        return [(r["id"], r["ts"]) for r in rows]
    except sqlite3.Error as exc:
        logger.error("خواندن اسنپ‌شات‌های روز ناموفق: %s", exc)
        return []


def load_snapshot(snap_id):
    try:
        with closing(_connect()) as conn:
            row = conn.execute(
                "SELECT payload FROM snapshots WHERE id = ?", (snap_id,)
            ).fetchone()
        if row is None:
            return None
        return json.loads(zlib.decompress(row["payload"]).decode("utf-8"))
    except (sqlite3.Error, zlib.error, json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.error("بارگذاری اسنپ‌شات %s ناموفق: %s", snap_id, exc)
        return None


# ---------------------------------------------------------------------------
# دستورهای خط فرمان
# ---------------------------------------------------------------------------

def _fmt_num(x):
    if x is None:
        return "None"
    return f"{x:,.0f}"


def cmd_check(cfg):
    snap, err = try_fetch_snapshot(cfg)
    if err:
        print(f"خطا: {err}")
        return 1

    print(f"وضعیت: {snap['status']} | زمان: {snap['ts']}")
    print(
        f"اختیار: {snap['option_count']} | سهام: {snap['stock_count']} | "
        f"غیراختیار: {snap['skipped_non_option']} | پایه دیگر: {snap['skipped_other_base']} | "
        f"دفتر خالی: {snap['book_empty_count']} | خطای تحلیل: {len(snap['parse_errors'])}"
    )
    for sym in cfg_symbols(cfg):
        u = snap["underlying"].get(sym) or {}
        line = (
            f"{sym}: خرید={_fmt_num(u.get('bid'))} فروش={_fmt_num(u.get('ask'))} "
            f"منبع={u.get('source') or '-'}"
        )
        if not u.get("usable") and u.get("reason"):
            line += f" | {u['reason']}"
        print(line)
    print(f"قرارداد آماده: {snap['ready_count']} | ایستا: {snap['stale_underlyings']}")
    return 0


def cmd_dump(query):
    q = normalize_text(query)
    if not q:
        print("نام یا کد نماد را وارد کنید. مثال: python engine.py dump ضهرم8024")
        return 1
    try:
        options = fetch_market_watch(MARKET_OPTION, OPTION_PAPER_TYPE)
        stocks = fetch_market_watch(MARKET_STOCK)
    except EngineError as exc:
        print(f"خطا: {exc}")
        return 1

    matches = [
        r for r in options + stocks
        if normalize_text(r.get("lva")) == q or q in normalize_text(r.get("lvc"))
    ]
    if not matches:
        print("رکوردی پیدا نشد")
        return 1

    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        path = os.path.join(LOG_DIR, f"dump_{iran_now():%Y%m%d_%H%M%S}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(matches, f, ensure_ascii=False, indent=2, default=str)
    except (OSError, TypeError, ValueError) as exc:
        print(f"ذخیره فایل dump ناموفق: {exc}")
        path = "-"

    for r in matches[:30]:
        book = extract_book(r)
        print(
            f"{normalize_text(r.get('lva'))} | {normalize_text(r.get('lvc'))} | "
            f"pmd={r.get('pmd')} pmo={r.get('pmo')} | "
            f"خرید={_fmt_num(book['bid'])} فروش={_fmt_num(book['ask'])} | "
            f"سطح={book['levels']} | hEven={r.get('hEven')}"
        )
    print(f"تعداد: {len(matches)} | فایل: {path}")
    return 0


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    args = sys.argv[1:]
    cfg = EngineConfig()
    try:
        cfg.validate()
    except ConfigError as exc:
        print(f"تنظیمات نامعتبر: {exc}")
        return 1

    if args and args[0] == "check":
        return cmd_check(cfg)
    if len(args) >= 2 and args[0] == "dump":
        return cmd_dump(" ".join(args[1:]))

    print("استفاده:")
    print("  python engine.py check")
    print("  python engine.py dump <نام یا کد نماد>")
    return 1


# ---------------------------------------------------------------------------
# راه‌اندازی
# ---------------------------------------------------------------------------

def bootstrap():
    setup_logging()
    init_db()
    cleanup_old_rows()


try:
    bootstrap()
except Exception as exc:
    print(f"خطا در راه‌اندازی موتور: {exc}")


if __name__ == "__main__":
    sys.exit(main())