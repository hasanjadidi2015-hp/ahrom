"""
engine.py  (نسخه ۳)
موتور داده و محاسبات دستیار اسکلپ اختیار معامله (بورس ایران)

- دریافت داده زنده از TSETMC (هیچ داده ساختگی تولید نمی‌شود)
- ساعت کاری: شنبه تا چهارشنبه، پیش‌گشایش ۰۸:۴۵ و معاملات ۰۹:۰۰ تا ۱۲:۳۰
- ورود فقط در پیش‌گشایش و معاملات مجاز است
- تشخیص داده ایستا (قیمت بدون تغییر در ساعت معامله)
- محاسبه IV و دلتا با Black-Scholes
- مدیریت سرمایه، سناریو و سود/زیان
- لاگ در فایل و پایگاه داده
- ارسال هشدار تلگرام با فاصله زمانی
"""

import json
import logging
import math
import os
import re
import sqlite3
import threading
import time
import zlib
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta, time as dtime
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

CONTRACT_SIZE = 1000            # تعداد سهم در هر قرارداد اختیار
QTC_TO_MIL_RIAL = 1_000_000     # تبدیل ارزش معامله (ریال) به میلیون ریال (فرض: qtc بر حسب ریال است)
SCENARIO_MOVES = [-2.0, -1.0, 0.0, 1.0, 2.0, 5.0]   # درصد تغییر قیمت پایه

TSETMC_MARKETWATCH = "https://cdn.tsetmc.com/api/ClosingPrice/GetMarketWatch"
OPTION_URL = TSETMC_MARKETWATCH + "?market=3&paperTypes%5B0%5D=7"
STOCK_URL = TSETMC_MARKETWATCH + "?market=1"
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://www.tsetmc.com/",
    "Accept": "application/json, text/plain, */*",
}
HTTP_TIMEOUT = 10

SRC_ORDER_BOOK = "دفتر سفارش"
SRC_TOP_LEVEL = "قیمت سطح بالا"

# ساعت کاری بورس
# weekday() در پایتون: دوشنبه=0 ... پنجشنبه=3، جمعه=4، شنبه=5، یکشنبه=6
TRADING_WEEKDAYS = {5, 6, 0, 1, 2}      # شنبه، یکشنبه، دوشنبه، سه‌شنبه، چهارشنبه
PREOPEN_START = dtime(8, 45)
OPEN_START = dtime(9, 0)
CLOSE_TIME = dtime(12, 30)
ENTRY_STATUSES = ("PREOPEN", "OPEN")    # فقط در این وضعیت‌ها ورود مجاز است

# تعطیلات رسمی بورس به‌صورت میلادی "YYYY-MM-DD". فعلاً خالی است و بعداً پر می‌شود.
HOLIDAYS = {"2026-12-23", "2027-01-06"}

# اگر قیمت یا دفتر سفارش در ساعت معامله بیش از این مدت (ثانیه) بدون تغییر بماند، ایستا حساب می‌شود
STATIC_AFTER_SEC = 180


# ---------------------------------------------------------------------------
# خطاها
# ---------------------------------------------------------------------------

class DataSourceError(Exception):
    """خطا در دریافت داده از TSETMC"""


class ParseError(Exception):
    """داده یک قرارداد قابل تفسیر نیست"""


class ConfigError(Exception):
    """تنظیمات نامعتبر است"""


# ---------------------------------------------------------------------------
# لاگ فایلی
# ---------------------------------------------------------------------------

logger = logging.getLogger("ahrom")


def setup_logging():
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
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


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


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
                n_options INTEGER,
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
        conn.commit()


def cleanup_old_rows():
    now = datetime.now()
    fetch_cut = (now - timedelta(days=14)).isoformat(timespec="seconds")
    event_cut = (now - timedelta(days=30)).isoformat(timespec="seconds")
    try:
        with closing(_connect()) as conn:
            conn.execute("DELETE FROM fetch_log WHERE ts < ?", (fetch_cut,))
            conn.execute("DELETE FROM events WHERE ts < ?", (event_cut,))
            conn.commit()
    except sqlite3.Error as exc:
        logger.error("پاکسازی پایگاه داده ناموفق: %s", exc)


def log_event(level, component, message, details=None):
    lvl = getattr(logging, str(level).upper(), logging.INFO)
    logger.log(lvl, "[%s] %s", component, message)
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
    except (sqlite3.Error, TypeError, ValueError) as exc:
        logger.error("ثبت رویداد در پایگاه داده ناموفق: %s", exc)


def _log_fetch(ok, latency_ms, n_options, error):
    try:
        with closing(_connect()) as conn:
            conn.execute(
                "INSERT INTO fetch_log (ts, ok, latency_ms, n_options, error) VALUES (?,?,?,?,?)",
                (_now_iso(), 1 if ok else 0, latency_ms, n_options, error),
            )
            conn.commit()
    except sqlite3.Error as exc:
        logger.error("ثبت fetch_log ناموفق: %s", exc)


# ---------------------------------------------------------------------------
# وضعیت بازار
# ---------------------------------------------------------------------------

def market_status(now=None):
    """
    وضعیت بازار در لحظه داده‌شده:
    CLOSED_HOLIDAY   تعطیل رسمی (از فهرست HOLIDAYS)
    CLOSED_DAY_OFF   پنجشنبه یا جمعه
    PREOPEN          ۰۸:۴۵ تا ۰۹:۰۰
    OPEN             ۰۹:۰۰ تا ۱۲:۳۰
    CLOSED           خارج از ساعت بالا در روز کاری
    """
    now = now or datetime.now()
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

_FRESH_LOCK = threading.Lock()
_FRESH_STATE = {}   # key -> (signature, first_seen_time)


def _touch(key, sig, now, seen):
    """
    ثبت می‌کند که امضای داده این کلید در این لحظه چیست.
    خروجی: چند ثانیه است که این امضا بدون تغییر مانده است.
    """
    seen.add(key)
    with _FRESH_LOCK:
        prev = _FRESH_STATE.get(key)
        if prev is None or prev[0] != sig:
            _FRESH_STATE[key] = (sig, now)
            return 0.0
        return (now - prev[1]).total_seconds()


def _prune_fresh(seen):
    """کلیدهایی که در این دور دیده نشده‌اند را پاک می‌کند (مثلاً بعد از بسته شدن بازار)"""
    with _FRESH_LOCK:
        for k in list(_FRESH_STATE):
            if k not in seen:
                del _FRESH_STATE[k]


# ---------------------------------------------------------------------------
# ابزارهای متن و عدد
# ---------------------------------------------------------------------------

_NORMALIZE_MAP = str.maketrans({
    "ي": "ی",
    "ك": "ک",
    "ة": "ه",
    "\u200c": "",
    "\u200f": "",
    "\u200e": "",
})

NAME_RE = re.compile(
    r"^اختیار\s*([خف])\s+(.+?)\s*-\s*(\d+)\s*-\s*(\d{4}/\d{1,2}/\d{1,2})$"
)


def normalize_text(text):
    if text is None:
        return ""
    return str(text).translate(_NORMALIZE_MAP).strip()


def _num(value, default=None):
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _best_level(levels):
    if not isinstance(levels, list) or not levels:
        return None
    valid = [x for x in levels if isinstance(x, dict)]
    if not valid:
        return None
    valid.sort(key=lambda x: _num(x.get("n"), 99.0))
    return valid[0]


def _valid_pair(bid, ask):
    return bid > 0 and ask > 0 and ask >= bid


def is_option_record(rec):
    return normalize_text(rec.get("lvc")).startswith("اختیار")


# ---------------------------------------------------------------------------
# تفسیر نام و کد قرارداد
# ---------------------------------------------------------------------------

def parse_option_name(lvc, lva):
    name = normalize_text(lvc)
    m = NAME_RE.match(name)
    if not m:
        raise ParseError(f"نام قرارداد قابل تشخیص نیست: {name}")

    side, base, strike_s, exp_s = m.groups()
    base = base.strip()
    code = normalize_text(lva)
    if not code:
        raise ParseError("کد قرارداد خالی است")

    is_call = side == "خ"
    expected_prefix = "ض" if is_call else "ط"
    if not code.startswith(expected_prefix):
        raise ParseError(f"کد {code} با نوع قرارداد {side} سازگار نیست")

    strike = int(strike_s)
    if strike <= 0:
        raise ParseError("قیمت اعمال نامعتبر است")

    try:
        jy, jm, jd = (int(x) for x in exp_s.split("/"))
        expiry_greg = jdatetime.date(jy, jm, jd).togregorian()
    except (ValueError, TypeError) as exc:
        raise ParseError(f"تاریخ سررسید نامعتبر: {exp_s}") from exc

    return {
        "base": base,
        "strike": strike,
        "is_call": is_call,
        "expiry_jalali": exp_s,
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
        return max(S - K, 0.0) if is_call else max(K - S, 0.0)
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
    if price <= 0 or S <= 0 or K <= 0 or T <= 0:
        return None
    disc_k = K * math.exp(-r * T)
    lower = max(S - disc_k, 0.0) if is_call else max(disc_k - S, 0.0)
    upper = S if is_call else disc_k
    if price <= lower or price >= upper:
        return None

    lo, hi = 1e-4, 5.0
    if bs_price(S, K, T, r, hi, is_call) < price:
        return None

    tol = 1e-6 * max(1.0, price)
    for _ in range(100):
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

@dataclass
class ScalpConfig:
    risk_free_rate: float = 0.30        # اعشاری: 0.30 یعنی ۳۰ درصد
    target1_pct: float = 20.0
    target2_pct: float = 40.0
    stop_pct: float = 15.0
    min_trade_val_mil: float = 50.0     # میلیون ریال
    max_spread_pct: float = 5.0
    delta_min: float = 0.35
    delta_max: float = 0.75
    capital_toman: float = 100_000_000
    risk_pct: float = 3.0
    fee_pct: float = 0.0
    stale_after_sec: int = 30

    def validate(self):
        checks = [
            (0 <= self.risk_free_rate <= 1, "نرخ بدون ریسک باید بین ۰ و ۱۰۰ درصد باشد"),
            (self.target1_pct > 0, "هدف ۱ باید مثبت باشد"),
            (self.target2_pct > self.target1_pct, "هدف ۲ باید بیشتر از هدف ۱ باشد"),
            (0 < self.stop_pct < 100, "حد ضرر نامعتبر است"),
            (self.max_spread_pct > 0, "حداکثر اسپرد باید مثبت باشد"),
            (0 <= self.delta_min < self.delta_max <= 1, "بازه دلتا نامعتبر است"),
            (self.capital_toman > 0, "سرمایه باید مثبت باشد"),
            (0 < self.risk_pct <= 100, "درصد ریسک نامعتبر است"),
            (0 <= self.fee_pct < 10, "کارمزد نامعتبر است"),
            (self.min_trade_val_mil >= 0, "حداقل ارزش معامله نامعتبر است"),
        ]
        for ok, msg in checks:
            if not ok:
                raise ConfigError(msg)


def contract_levels(entry, cfg):
    return {
        "target1": int(round(entry * (1 + cfg.target1_pct / 100.0))),
        "target2": int(round(entry * (1 + cfg.target2_pct / 100.0))),
        "stop": int(round(entry * (1 - cfg.stop_pct / 100.0))),
    }


def position_size(entry, cfg):
    """تعداد قرارداد مجاز بر اساس سرمایه و ریسک هر معامله. واحد پول: تومان"""
    if entry <= 0:
        return 0
    cost_per = entry * CONTRACT_SIZE / 10.0
    loss_per = entry * (cfg.stop_pct / 100.0) * CONTRACT_SIZE / 10.0
    if cost_per <= 0 or loss_per <= 0:
        return 0
    budget = cfg.capital_toman * cfg.risk_pct / 100.0
    n_risk = int(budget // loss_per)
    n_cash = int(cfg.capital_toman // cost_per)
    return max(0, min(n_risk, n_cash))


def pnl_toman(entry, exit_price, n, fee_pct=0.0):
    gross = (exit_price - entry) * CONTRACT_SIZE * n / 10.0
    fees = (fee_pct / 100.0) * (entry + exit_price) * CONTRACT_SIZE * n / 10.0
    return gross - fees


def scenario_table(c, cfg):
    """سناریوی قیمت بر اساس تغییر قیمت پایه، IV و زمان"""
    rows = []
    if c.get("iv") is None or c.get("underlying") is None:
        return rows
    S = c["underlying"]
    K = c["strike"]
    T = max(c["days"], 1) / 365.0
    T_less = max(T - 1 / 365.0, 1 / 365.0)
    r = cfg.risk_free_rate
    sigma = c["iv"]
    sigma_down = max(sigma - 0.05, 0.01)
    is_call = c["is_call"]
    entry = c["entry"]

    for m in SCENARIO_MOVES:
        s2 = S * (1 + m / 100.0)
        base = bs_price(s2, K, T, r, sigma, is_call)
        iv_down = bs_price(s2, K, T, r, sigma_down, is_call)
        time_less = bs_price(s2, K, T_less, r, sigma, is_call)
        rows.append({
            "move_pct": m,
            "underlying": int(round(s2)),
            "price_base": int(round(base)),
            "price_iv_minus5": int(round(iv_down)),
            "price_one_day_less": int(round(time_less)),
            "pnl_toman_per_contract": int(round(pnl_toman(entry, base, 1, cfg.fee_pct))),
            "pnl_pct": round((base / entry - 1) * 100, 2) if entry > 0 else None,
        })
    return rows


def required_move_for_price(c, target_price, cfg):
    """درصد تغییر لازم قیمت پایه برای رسیدن اختیار به قیمت هدف (با IV و زمان فعلی)"""
    if c.get("iv") is None or c.get("underlying") is None or target_price <= 0:
        return None
    S = c["underlying"]
    K = c["strike"]
    T = max(c["days"], 1) / 365.0
    r = cfg.risk_free_rate
    sigma = c["iv"]
    is_call = c["is_call"]

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
            return mid
        if flo * fm < 0:
            hi, fhi = mid, fm
        else:
            lo, flo = mid, fm
    return (lo + hi) / 2.0


def _liquidity_grade(trade_val_mil, spread_pct, cfg):
    if spread_pct is None:
        return "ضعیف"
    if trade_val_mil >= 5 * cfg.min_trade_val_mil and spread_pct <= cfg.max_spread_pct / 2:
        return "عالی"
    if trade_val_mil >= cfg.min_trade_val_mil and spread_pct <= cfg.max_spread_pct:
        return "خوب"
    return "ضعیف"


def _score(trade_val_mil, spread_pct, delta, cfg):
    liq = min(1.0, trade_val_mil / (10 * cfg.min_trade_val_mil)) if cfg.min_trade_val_mil > 0 else 1.0
    spr = max(0.0, 1 - spread_pct / cfg.max_spread_pct) if cfg.max_spread_pct > 0 else 0.0
    dfit = max(0.0, 1 - abs(abs(delta) - 0.55) / 0.20)
    return round(100 * (0.40 * liq + 0.35 * spr + 0.25 * dfit), 1)


# ---------------------------------------------------------------------------
# تحلیل داده بازار
# ---------------------------------------------------------------------------

def build_underlying_map(stocks, now=None, seen=None, fresh_open=False):
    """
    قیمت پایه هر نماد را پیدا می‌کند. اولویت با دفتر سفارش است.
    اگر دفتر سفارش خالی یا صفر بود، از pmd/pmo سطح بالای همان رکورد استفاده می‌شود
    و منبع در فیلد source ثبت می‌شود. اگر هیچ‌کدام معتبر نبود، نماد وارد نمی‌شود.
    فقط در ساعت معامله (fresh_open=True) تشخیص داده ایستا انجام می‌شود.
    """
    result = {}
    for st in stocks:
        name = normalize_text(st.get("lva"))
        if not name or name in result:
            continue

        bid = ask = None
        source = None

        lvl = _best_level(st.get("blDs"))
        if lvl is not None:
            b = _num(lvl.get("pmd"), 0.0)
            a = _num(lvl.get("pmo"), 0.0)
            if _valid_pair(b, a):
                bid, ask, source = b, a, SRC_ORDER_BOOK

        if source is None:
            b = _num(st.get("pmd"), 0.0)
            a = _num(st.get("pmo"), 0.0)
            if _valid_pair(b, a):
                bid, ask, source = b, a, SRC_TOP_LEVEL

        if source is None:
            continue

        age = None
        if fresh_open and seen is not None and now is not None:
            sig = (bid, ask, st.get("hEven"), st.get("zTotTran"))
            age = _touch(f"u:{name}", sig, now, seen)

        result[name] = {
            "bid": bid,
            "ask": ask,
            "mid": (bid + ask) / 2.0,
            "source": source,
            "age_sec": round(age, 1) if age is not None else None,
            "stale": age is not None and age > STATIC_AFTER_SEC,
        }
    return result


def analyze_contract(rec, underlying_map, cfg, today, gate_open=True, opt_age=None):
    info = parse_option_name(rec.get("lvc"), rec.get("lva"))
    code = normalize_text(rec.get("lva"))
    base = info["base"]
    strike = info["strike"]
    is_call = info["is_call"]
    days = (info["expiry_greg"] - today).days

    lvl = _best_level(rec.get("blDs"))
    if lvl is None:
        raise ParseError("دفتر سفارش خالی است")

    bid = _num(lvl.get("pmd"), 0.0)
    ask = _num(lvl.get("pmo"), 0.0)
    bid_vol = _num(lvl.get("qmd"), 0.0)
    ask_vol = _num(lvl.get("qmo"), 0.0)
    trade_val_mil = _num(rec.get("qtc"), 0.0) / QTC_TO_MIL_RIAL
    oi = int(_num(rec.get("buyOP"), 0.0))

    mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else None

    reasons = []
    if not gate_open:
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

    und = underlying_map.get(base)
    if und is None:
        S = None
        reasons.append("قیمت پایه معتبر نیست")
    elif und["stale"]:
        S = None
        reasons.append("قیمت پایه ایستا است (احتمالاً قدیمی)")
    else:
        S = und["mid"]

    if opt_age is not None and opt_age > STATIC_AFTER_SEC:
        reasons.append("داده قرارداد ایستا است (احتمالاً قدیمی)")

    spread_pct = (ask - bid) / mid * 100.0 if mid else None
    if spread_pct is not None and spread_pct > cfg.max_spread_pct:
        reasons.append("اسپرد بالاتر از حد مجاز است")
    if trade_val_mil < cfg.min_trade_val_mil:
        reasons.append("ارزش معامله کم است")
    if bid_vol <= 0 or ask_vol <= 0:
        reasons.append("صف خرید یا فروش خالی است")

    iv = None
    delta = None
    if mid is not None and S is not None and days >= 0:
        T = max(days, 1) / 365.0
        iv = implied_vol(mid, S, strike, T, cfg.risk_free_rate, is_call)
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
    levels = contract_levels(ask, cfg) if ask > 0 else {"target1": None, "target2": None, "stop": None}

    return {
        "symbol": code,
        "base": base,
        "is_call": is_call,
        "type": "اختیار خرید" if is_call else "اختیار فروش",
        "strike": strike,
        "expiry": info["expiry_jalali"],
        "expiry_greg": info["expiry_greg"].isoformat(),
        "days": days,
        "bid": bid,
        "ask": ask,
        "entry": ask,
        "mid": mid,
        "bid_vol": bid_vol,
        "ask_vol": ask_vol,
        "spread_pct": round(spread_pct, 2) if spread_pct is not None else None,
        "delta": round(delta, 3) if delta is not None else None,
        "iv": round(iv, 4) if iv is not None else None,
        "oi": oi,
        "trade_val_mil": round(trade_val_mil, 2),
        "underlying": S,
        "underlying_source": und["source"] if und else None,
        "underlying_age_sec": und["age_sec"] if und else None,
        "option_age_sec": round(opt_age, 1) if opt_age is not None else None,
        "target1": levels["target1"],
        "target2": levels["target2"],
        "stop": levels["stop"],
        "liquidity": liquidity,
        "score": score,
        "ready": ready,
        "reasons": reasons,
        "raw": rec,
    }


def analyze_snapshot(options, stocks, cfg):
    cfg.validate()
    now = datetime.now()
    today = now.date()
    status = market_status(now)
    gate_open = status in ENTRY_STATUSES
    fresh_open = status == "OPEN"

    seen = set()
    und = build_underlying_map(stocks, now, seen, fresh_open)

    option_recs = []
    skipped_non_option = 0
    for rec in options:
        if is_option_record(rec):
            option_recs.append(rec)
        else:
            skipped_non_option += 1

    contracts = []
    rejected = []

    for rec in option_recs:
        try:
            opt_age = None
            if fresh_open:
                lvl = _best_level(rec.get("blDs"))
                if lvl is not None:
                    code = normalize_text(rec.get("lva"))
                    sig = (
                        lvl.get("pmd"), lvl.get("pmo"),
                        lvl.get("qmd"), lvl.get("qmo"),
                        rec.get("hEven"), rec.get("zTotTran"),
                    )
                    opt_age = _touch(f"o:{code}", sig, now, seen)
            contracts.append(analyze_contract(rec, und, cfg, today, gate_open, opt_age))
        except ParseError as exc:
            rejected.append({
                "code": normalize_text(rec.get("lva")),
                "name": normalize_text(rec.get("lvc")),
                "reason": str(exc),
            })
        except Exception as exc:
            logger.exception("خطای غیرمنتظره در تحلیل قرارداد")
            rejected.append({
                "code": normalize_text(rec.get("lva")),
                "name": normalize_text(rec.get("lvc")),
                "reason": f"خطای داخلی: {exc}",
            })

    _prune_fresh(seen)

    contracts.sort(key=lambda c: (not c["ready"], -(c["score"] or 0), c["days"]))

    return {
        "ts": _now_iso(),
        "status": status,
        "market_open": gate_open,
        "underlying": und,
        "stale_underlyings": [k for k, v in und.items() if v["stale"]],
        "contracts": contracts,
        "rejected": rejected,
        "option_count": len(option_recs),
        "skipped_non_option": skipped_non_option,
        "stock_count": len(stocks),
    }


# ---------------------------------------------------------------------------
# دریافت داده از TSETMC
# ---------------------------------------------------------------------------

def _http_json(url):
    req = urlrequest.Request(url, headers=HTTP_HEADERS)
    try:
        with urlrequest.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read()
    except HTTPError as exc:
        raise DataSourceError(f"سرور TSETMC خطای HTTP {exc.code} داد") from exc
    except (URLError, OSError) as exc:
        raise DataSourceError(f"اتصال به TSETMC برقرار نشد: {exc}") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataSourceError("پاسخ TSETMC JSON معتبر نیست (احتمالاً صفحه مسدودسازی شبکه)") from exc


def _extract_list(payload, label):
    if not isinstance(payload, dict) or "marketwatch" not in payload:
        raise DataSourceError(f"کلید marketwatch در پاسخ {label} وجود ندارد")
    items = payload["marketwatch"]
    if not isinstance(items, list) or not items:
        raise DataSourceError(f"لیست {label} خالی است")
    return [x for x in items if isinstance(x, dict)]


def fetch_market_data():
    t0 = time.perf_counter()
    try:
        options = _extract_list(_http_json(OPTION_URL), "اختیار")
        stocks = _extract_list(_http_json(STOCK_URL), "سهام")
    except DataSourceError as exc:
        latency = int((time.perf_counter() - t0) * 1000)
        _log_fetch(False, latency, 0, str(exc))
        raise
    latency = int((time.perf_counter() - t0) * 1000)
    n_opt = sum(1 for x in options if is_option_record(x))
    _log_fetch(True, latency, n_opt, None)
    return options, stocks


def fetch_snapshot(cfg):
    options, stocks = fetch_market_data()
    snap = analyze_snapshot(options, stocks, cfg)
    snap["fetch_ok"] = True
    return snap


# ---------------------------------------------------------------------------
# تلگرام با فاصله زمانی
# ---------------------------------------------------------------------------

def send_telegram(token, chat_id, text):
    token = (token or "").strip()
    chat_id = str(chat_id or "").strip()
    if not token or not chat_id:
        return False, "توکن یا Chat ID خالی است"

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urlparse.urlencode({"chat_id": chat_id, "text": str(text)[:4000]}).encode("utf-8")
    req = urlrequest.Request(url, data=body, method="POST")
    try:
        with urlrequest.urlopen(req, timeout=8) as resp:
            resp.read()
    except HTTPError as exc:
        msg = f"تلگرام خطای HTTP {exc.code} داد"
        logger.warning("ارسال تلگرام ناموفق: %s", msg)
        return False, msg
    except (URLError, OSError) as exc:
        msg = f"ارسال تلگرام ناموفق: {exc}"
        logger.warning(msg)
        return False, msg

    logger.info("پیام تلگرام ارسال شد")
    return True, "ارسال شد"


def can_alert(symbol, cooldown_min=10):
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
    return datetime.now() - last >= timedelta(minutes=cooldown_min)


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
    except (sqlite3.Error, TypeError, ValueError) as exc:
        logger.error("ذخیره اسنپ‌شات ناموفق: %s", exc)
        return False


def list_replay_dates():
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT DISTINCT trade_date FROM snapshots ORDER BY trade_date DESC"
        ).fetchall()
    return [r["trade_date"] for r in rows]


def list_replay_snapshots(trade_date):
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT id, ts FROM snapshots WHERE trade_date = ? ORDER BY ts", (trade_date,)
        ).fetchall()
    return [(r["id"], r["ts"]) for r in rows]


def load_snapshot(snap_id):
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT payload FROM snapshots WHERE id = ?", (snap_id,)
        ).fetchone()
    if row is None:
        return None
    return json.loads(zlib.decompress(row["payload"]).decode("utf-8"))


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