import streamlit as st
import requests
import pandas as pd
import datetime
import math
import json
import streamlit.components.v1 as components

# تنظیمات اصلی Streamlit
st.set_page_config(
    page_title="سیستم دستیار اسکلپ اختیار معامله - بورس ایران",
    page_icon="🤖",
    layout="wide"
)

st.markdown("""
<style>
    .block-container { padding-top: 1rem; padding-bottom: 1rem; padding-left: 1rem; padding-right: 1rem; }
    header {visibility: hidden;}
    .stRadio > label { font-weight: bold; color: #38bdf8; }
</style>
""", unsafe_allow_html=True)

TARGET_BASE_SYMBOLS = ["اهرم", "خودرو", "فملی", "شستا", "وبملت"]

# --- ۱. ریاضیات بلک-شولز و یونانی‌ها (Black-Scholes & Greeks) ---
def normal_cdf(x):
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

def calculate_delta_and_iv(S, K, days_to_maturity, market_price, r=0.28, is_call=True):
    if S <= 0 or K <= 0 or days_to_maturity <= 0 or market_price <= 0:
        return 0.5 if is_call else -0.5, 40.0
    
    T = days_to_maturity / 365.0
    
    # محاسبه عددی نوسان‌پذیری ضمنی (IV)
    low_vol, high_vol = 0.05, 2.5
    for _ in range(15):
        mid_vol = (low_vol + high_vol) / 2.0
        d1 = (math.log(S / K) + (r + 0.5 * mid_vol ** 2) * T) / (mid_vol * math.sqrt(T))
        d2 = d1 - mid_vol * math.sqrt(T)
        price = S * normal_cdf(d1) - K * math.exp(-r * T) * normal_cdf(d2) if is_call else K * math.exp(-r * T) * normal_cdf(-d2) - S * normal_cdf(-d1)
        if price > market_price: high_vol = mid_vol
        else: low_vol = mid_vol
            
    iv_pct = round((low_vol + high_vol) / 2.0 * 100, 1)
    d1 = (math.log(S / K) + (r + 0.5 * (iv_pct/100) ** 2) * T) / ((iv_pct/100) * math.sqrt(T))
    delta = round(normal_cdf(d1) if is_call else normal_cdf(d1) - 1.0, 2)
    return delta, iv_pct

def clean_persian(text):
    if not text: return ""
    return str(text).replace('ي', 'ی').replace('ك', 'ک').strip()

def parse_option_info(lvc, lva):
    lvc_clean = clean_persian(lvc)
    lva_clean = clean_persian(lva)
    opt_type = "خرید"
    if "اختيارف" in lvc_clean or "اخف" in lvc_clean or lva_clean.startswith('ط') or lva_clean.startswith('ت'):
        opt_type = "فروش"
    parts = lvc_clean.split('-')
    if len(parts) >= 3:
        maturity = parts[-1].strip()
        try: strike = int(parts[-2].strip())
        except ValueError: strike = 0
        first_part = parts[0].replace("اختيارخ", "").replace("اختيارف", "").strip()
        return opt_type, first_part, strike, maturity
    return opt_type, "", 0, ""

def jalali_to_gregorian(jy, jm, jd):
    """Convert a Jalali date to Gregorian without relying on a third-party package."""
    if jm < 1 or jm > 12 or jd < 1 or jd > 31:
        raise ValueError("Invalid Jalali date")

    jy += 1595
    days = -355668 + 365 * jy + (jy // 33) * 8 + ((jy % 33 + 3) // 4) + jd
    if jm < 7:
        days += (jm - 1) * 31
    else:
        days += (jm - 7) * 30 + 186

    gy = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        gy += 100 * ((days - 1) // 36524)
        days = (days - 1) % 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365

    gd = days + 1
    month_lengths = [0, 31, 29 if (gy % 4 == 0 and (gy % 100 != 0 or gy % 400 == 0)) else 28,
                     31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    gm = 1
    while gm <= 12 and gd > month_lengths[gm]:
        gd -= month_lengths[gm]
        gm += 1
    if gm > 12:
        raise ValueError("Jalali date is out of supported range")
    return datetime.date(gy, gm, gd)


def get_days_left(maturity_str):
    try:
        jy, jm, jd = [int(part.strip()) for part in maturity_str.split('/')]
        if jm < 1 or jm > 12 or jd < 1 or jd > (31 if jm <= 6 else 30):
            return None
        expiry_date = jalali_to_gregorian(jy, jm, jd)
        # Tehran local date (UTC+03:30); avoids using the computer's local timezone.
        tehran_today = (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=3, minutes=30)
        ).date()
        return (expiry_date - tehran_today).days
    except (TypeError, ValueError, OverflowError):
        return None

# --- ۲. ماژول Live Data Engine ---
@st.cache_data(ttl=4)
def fetch_live_market_data():
    url_options = "https://cdn.tsetmc.com/api/ClosingPrice/GetMarketWatch?market=3&paperTypes[0]=7"
    url_stocks = "https://cdn.tsetmc.com/api/ClosingPrice/GetMarketWatch?market=1"
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json", "Referer": "https://web.tsetmc.com/"}
    session = requests.Session()
    try:
        res_opt = session.get(url_options, headers=headers, timeout=10)
        if res_opt.status_code != 200: return [], {}, f"خطای سرور بورس ({res_opt.status_code})"
        raw_options = res_opt.json().get("marketwatch", [])
        
        res_stock = session.get(url_stocks, headers=headers, timeout=10)
        stock_prices = {}
        if res_stock.status_code == 200:
            for s in res_stock.json().get("marketwatch", []):
                p = float(s.get('pmd', 0) or s.get('pcl', 0) or 0)
                if p > 0: stock_prices[clean_persian(s.get('lva', ''))] = p
        
        defaults = {"اهرم": 21000, "خودرو": 2800, "فملی": 7200, "شستا": 1200, "وبملت": 2400}
        for k, v in defaults.items():
            if k not in stock_prices: stock_prices[k] = v
            
        return raw_options, stock_prices, None
    except Exception as e:
        return [], {}, f"قطع ارتباط با TSETMC: {str(e)}"

# --- ۳. پردازش ارزیابی و تحلیل کیفی (Decision Matrix) ---
def process_data(raw_options, stock_prices):
    processed = []
    for item in raw_options:
        lva = clean_persian(item.get('lva', ''))
        lvc = clean_persian(item.get('lvc', ''))
        opt_type, ua_symbol, strike, maturity = parse_option_info(lvc, lva)
        
        matched_base = next((b for b in TARGET_BASE_SYMBOLS if b in ua_symbol), None)
        if not matched_base: continue
        
        underlying_p = stock_prices.get(matched_base, 20000)
        blds = item.get('blDs', [])
        bid1, ask1 = 0.0, 0.0
        bid_vol1, ask_vol1 = 0, 0
        if blds:
            bid1 = float(blds[0].get('pmd', 0) or 0)
            bid_vol1 = int(blds[0].get('qmd', 0) or 0)
            ask1 = float(blds[0].get('pmo', 0) or 0)
            ask_vol1 = int(blds[0].get('qmo', 0) or 0)
            
        last_price = float(item.get('pmd', 0) or 0)
        entry_price = ask1 if ask1 > 0 else last_price
        if entry_price <= 0: continue
        
        target1 = round(entry_price * 1.20)
        target2 = round(entry_price * 1.40)
        stop_loss = round(entry_price * 0.85)
        
        spread_pct = round(((ask1 - bid1) / bid1) * 100, 1) if (ask1 > 0 and bid1 > 0) else 0.0
        trade_val_toman = float(item.get('qtc', 0) or 0) / 10.0
        open_interest = int(item.get('buyOP', 0) or 0)
        
        days_left = get_days_left(maturity)
        # Invalid or expired contracts must not receive fabricated Greeks.
        if days_left is None or days_left <= 0:
            continue
        delta, iv = calculate_delta_and_iv(underlying_p, strike, days_left, entry_price, is_call=(opt_type == "خرید"))
        
        # --- ماتریس تحلیل کیفی سیستم (بر اساس پیشنهادات ارشد) ---
        # ۱. کیفیت نقدشوندگی
        if trade_val_toman >= 300_000_000 and spread_pct <= 3.0: liquidity_grade = "عالی 🟢"
        elif trade_val_toman >= 50_000_000 and spread_pct <= 7.0: liquidity_grade = "خوب 🟡"
        else: liquidity_grade = "ضعیف/ریسک بالا 🔴"
        
        # ۲. وضعیت ورود
        if spread_pct <= 4.0 and abs(delta) >= 0.35 and trade_val_toman >= 50_000_000: entry_status = "آماده ورود 🚀"
        elif spread_pct <= 8.0: entry_status = "در انتظار تایید ⏳"
        else: entry_status = "عدم ورود (اسپرد بالا) ❌"
        
        # ۳. ریسک IV
        if iv > 65: iv_risk = "بالا (حباب شدید) 🔴"
        elif iv >= 40: iv_risk = "متوسط 🟡"
        else: iv_risk = "کم (منظقی) 🟢"
        
        # ۴. تحلیل سناریوهای حساسیت سهم پایه
        scen_up_1 = round(entry_price * (1 + (0.01 * delta * (underlying_p / entry_price))))
        scen_up_2 = round(entry_price * (1 + (0.02 * delta * (underlying_p / entry_price))))
        scen_down_1 = round(entry_price * (1 + (-0.01 * delta * (underlying_p / entry_price))))
        
        score = (trade_val_toman / 1_000_000) * (abs(delta) + 0.1) / (spread_pct + 0.5)
        
        processed.append({
            'symbol': lva, 'name': lvc, 'base_symbol': matched_base, 'opt_type': opt_type,
            'strike': strike, 'maturity': maturity, 'entry_price': int(entry_price),
            'bid1': int(bid1), 'ask1': int(ask1), 'bid_vol': bid_vol1, 'ask_vol': ask_vol1,
            'target1': int(target1), 'target2': int(target2), 'stop_loss': int(stop_loss),
            'spread_pct': spread_pct, 'trade_val_toman': trade_val_toman, 'open_interest': open_interest,
            'delta': delta, 'iv': iv, 'days_left': days_left, 'score': score,
            'liquidity_grade': liquidity_grade, 'entry_status': entry_status, 'iv_risk': iv_risk,
            'scen_up_1': scen_up_1, 'scen_up_2': scen_up_2, 'scen_down_1': scen_down_1
        })
    return pd.DataFrame(processed)

# --- ۴. رندر ۴ کارت تصمیم‌گیری تخصصی (Qualitative Decision Cards) ---
def render_decision_matrix_cards(df_top):
    if df_top.empty: return
    st.markdown("### 📊 کارت تصمیم‌گیری و ماتریس سناریوهای ۴ موقعیت برتر")
    cols = st.columns(min(4, len(df_top)))
    
    for idx, (_, row) in enumerate(df_top.iterrows()):
        if idx >= 4: break
        with cols[idx]:
            badge_c = "#10b981" if row['opt_type'] == "خرید" else "#ef4444"
            st.markdown(f"""
            <div style="background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 10px; font-family: Tahoma; direction: rtl; text-align: right;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                    <b style="font-size: 14px; color: #f8fafc;">{row['symbol']}</b>
                    <span style="background: {badge_c}22; color: {badge_c}; padding: 2px 6px; border-radius: 4px; font-size: 10px;">اخ {row['opt_type']} ({row['base_symbol']})</span>
                </div>
                <div style="font-size: 11px; color: #94a3b8; margin-bottom: 4px;">ورود (Ask): <b style="color:#38bdf8; font-size:13px;">{row['entry_price']:,}</b></div>
                <div style="font-size: 10px; background: #0f172a; padding: 6px; border-radius: 4px; margin-bottom: 6px; line-height: 1.6;">
                    <div>• نقدشوندگی: <b>{row['liquidity_grade']}</b></div>
                    <div>• وضعیت ورود: <b style="color:#4ade80;">{row['entry_status']}</b></div>
                    <div>• ریسک حباب (IV): <b>{row['iv_risk']}</b> (IV: {row['iv']}%)</div>
                </div>
                <div style="font-size: 10px; background: #1a2234; padding: 6px; border-radius: 4px; border-right: 3px solid #38bdf8;">
                    <div style="color:#94a3b8; font-weight:bold; margin-bottom:2px;">📈 سناریوی سهم پایه:</div>
                    <div>سهم +۱٪ 👈 قیمت آپشن: <b style="color:#4ade80;">{row['scen_up_1']:,}</b></div>
                    <div>سهم +۲٪ 👈 قیمت آپشن: <b style="color:#22c55e;">{row['scen_up_2']:,}</b></div>
                    <div>سهم -۱٪ 👈 قیمت آپشن: <b style="color:#f87171;">{row['scen_down_1']:,}</b></div>
                </div>
            </div>
            """, unsafe_allow_html=True)

# --- ۵. رندر جدول کامل ---
def render_rtl_table(df):
    if df.empty: return "<div style='text-align:center; padding:20px; color:#aaa;'>هیچ قراردادی با این مشخصات یافت نشد.</div>"
    rows_html = ""
    for idx, row in df.iterrows():
        t_badge = "<span style='color:#10b981; font-weight:bold;'>خرید</span>" if row['opt_type'] == "خرید" else "<span style='color:#ef4444; font-weight:bold;'>فروش</span>"
        rows_html += f"""
        <tr>
            <td style='font-weight:bold; color:#f8fafc;'>{row['symbol']}</td>
            <td>{row['base_symbol']}</td>
            <td>{t_badge}</td>
            <td>{row['strike']:,}</td>
            <td style='font-size:10px;'>{row['maturity']}</td>
            <td style='font-weight:bold; color:#38bdf8;'>{row['entry_price']:,}</td>
            <td style='color:#4ade80; font-weight:bold;'>{row['target1']:,}</td>
            <td style='color:#22c55e; font-weight:bold;'>{row['target2']:,}</td>
            <td style='color:#f87171; font-weight:bold;'>{row['stop_loss']:,}</td>
            <td style='color:#eab308; font-weight:bold;'>{row['delta']}</td>
            <td>{row['ask1']:,}</td>
            <td>{row['bid1']:,}</td>
            <td style='color:{ "#ef4444" if row["spread_pct"] > 5 else "#a855f7" };'>{row['spread_pct']}%</td>
            <td style='color:#38bdf8;'>{row['open_interest']:,}</td>
            <td>{int(row['trade_val_toman'] / 1_000_000):,}</td>
        </tr>
        """
    return f"""
    <!DOCTYPE html><html dir="rtl"><head><style>
        body {{ font-family: Tahoma, sans-serif; background-color: #0f172a; color: #f8fafc; margin: 0; }}
        .table-container {{ width: 100%; border: 1px solid #1e293b; border-radius: 8px; }}
        table {{ width: 100%; border-collapse: collapse; text-align: center; font-size: 11px; }}
        th {{ background-color: #1e293b; color: #94a3b8; padding: 8px 3px; font-weight: 600; border-bottom: 2px solid #334155; white-space: nowrap; }}
        td {{ padding: 5px 2px; border-bottom: 1px solid #1e293b; white-space: nowrap; }}
        tr:hover {{ background-color: #1e293b88; }}
    </style></head><body><div class="table-container"><table>
        <thead><tr>
            <th>نماد</th><th>پایه</th><th>نوع</th><th>اعمال</th><th>سررسید</th>
            <th>ورود (Ask)</th><th>سود ۲۰٪</th><th>سود ۴۰٪</th><th>حدضررب۱۵٪</th>
            <th>دلتا (Δ)</th><th>فروش (Ask)</th><th>خرید (Bid)</th><th>اسپرد</th>
            <th>موقعیت باز (OI)</th><th>ارزش(م.ت)</th>
        </tr></thead><tbody>{rows_html}</tbody>
    </table></div></body></html>
    """

# --- رابط کاربری اصلی (Streamlit Dashboard) ---
st.title("🤖 دستیار اسکلپ روزانه اختیار معامله (بورس ایران)")

# نوار کناری: انتخاب حالت (زنده / ماشین زمان Option Time Machine)
st.sidebar.header("⚙️ تنظیمات و حالت اجرا")
mode = st.sidebar.radio("انتخاب منبع داده:", ["🔴 بازار زنده (Live TSETMC)", "⏳ ماشین زمان / شبیه‌ساز آفلاین (Time Machine)"])

capital_toman = st.sidebar.number_input("سرمایه کل شما (تومان):", min_value=1_000_000, value=100_000_000, step=5_000_000)
risk_per_trade_pct = st.sidebar.slider("حداکثر ریسک در هر معامله (%):", 1.0, 10.0, 3.0)

if "Live" in mode:
    # خودکار رفرش ۵ ثانیه‌ای
    components.html("<script>setTimeout(function(){window.parent.postMessage({type:'streamlit:rerun'},'*');},5000);</script>", height=0, width=0)
    raw_options, stock_prices, error_msg = fetch_live_market_data()
    fetch_time = datetime.datetime.now().strftime('%H:%M:%S')
    if error_msg:
        st.sidebar.error(f"🔴 {error_msg}")
    else:
        st.sidebar.success(f"🟢 اتصال زنده TSETMC ({fetch_time})")
else:
    st.sidebar.info("⏳ حالت شبیه‌ساز ماشین زمان فعال است (تست آفلاین بدون نیاز به بورس).")
    # نمونه داده آفلاین معتبر جهت تست ماشین زمان (Time Machine Replay Data)
    raw_options = [
        {'lva': 'ضهرم8024', 'lvc': 'اختيارخ اهرم-30000-1405/08/27', 'pmd': 63005, 'pcl': 62997, 'qtc': 10583506000, 'buyOP': 5819, 'blDs': [{'qmd': 1000, 'pmd': 62500, 'qmo': 1200, 'pmo': 63000}]},
        {'lva': 'ضهرم8025', 'lvc': 'اختيارخ اهرم-34000-1405/08/27', 'pmd': 47600, 'pcl': 47600, 'qtc': 8500000000, 'buyOP': 3400, 'blDs': [{'qmd': 500, 'pmd': 47000, 'qmo': 800, 'pmo': 47600}]},
        {'lva': 'ضخود8012', 'lvc': 'اختيارخ خودرو-2800-1404/02/15', 'pmd': 450, 'pcl': 440, 'qtc': 4200000000, 'buyOP': 12000, 'blDs': [{'qmd': 10000, 'pmd': 445, 'qmo': 15000, 'pmo': 450}]},
        {'lva': 'ضملی6001', 'lvc': 'اختيارخ فملی-7000-1404/01/20', 'pmd': 820, 'pcl': 810, 'qtc': 3100000000, 'buyOP': 8900, 'blDs': [{'qmd': 3000, 'pmd': 810, 'qmo': 4000, 'pmo': 820}]}
    ]
    stock_prices = {"اهرم": 21000, "خودرو": 2800, "فملی": 7200, "شستا": 1200, "وبملت": 2400}

if not raw_options:
    st.warning("⚠️ دیتایی دریافت نشد.")
    st.stop()

df_all = process_data(raw_options, stock_prices)

# فیلترهای نوار کناری
selected_base = st.sidebar.selectbox("انتخاب نماد پایه:", ["همه"] + TARGET_BASE_SYMBOLS)
if selected_base != "همه": df_filtered = df_all[df_all['base_symbol'] == selected_base]
else: df_filtered = df_all.copy()

opt_type_filter = st.sidebar.radio("نوع اختیار:", ["همه", "اختیار خرید 📈", "اختیار فروش 📉"])
if "خرید" in opt_type_filter: df_filtered = df_filtered[df_filtered['opt_type'] == "خرید"]
elif "فروش" in opt_type_filter: df_filtered = df_filtered[df_filtered['opt_type'] == "فروش"]

min_val = st.sidebar.slider("حداقل ارزش معاملات (میلیون تومان):", 0, 1000, 0, step=10)
df_filtered = df_filtered[df_filtered['trade_val_toman'] >= (min_val * 1_000_000)]

max_spread = st.sidebar.slider("حداکثر فاصله اسپرد (%):", 0.5, 50.0, 20.0, step=0.5)
df_filtered = df_filtered[df_filtered['spread_pct'] <= max_spread]

# ۱. نمایش کارت‌های تصمیم‌گیری و سناریو
df_top_4 = df_filtered.sort_values(by='score', ascending=False).head(4)
render_decision_matrix_cards(df_top_4)

st.markdown("<br>", unsafe_allow_html=True)

# ۲. ماژول مدیریت ریسک و سرمایه (Risk Manager)
if not df_top_4.empty:
    top_pick = df_top_4.iloc[0]
    max_risk_amount = capital_toman * (risk_per_trade_pct / 100.0)
    risk_per_contract = top_pick['entry_price'] * 0.15 * 1000 # هر قرارداد ۱۰۰۰ تایی
    recommended_contracts = max(1, int(max_risk_amount / risk_per_contract)) if risk_per_contract > 0 else 1
    
    st.info(f"💡 **پیشنهاد مدیریت ریسک سیستم برای برترین نماد ({top_pick['symbol']}):** با سرمایه {capital_toman:,} تومان و ریسک {risk_per_trade_pct}٪، حداکثر مجاز به خرید **{recommended_contracts:,} قرارداد** هستید (حداکثر زیان در صورت حدضرر ۱۵٪: {int(recommended_contracts * risk_per_contract):,} تومان).")

# ۳. نمایش جدول اصلی
df_filtered = df_filtered.sort_values(by='trade_val_toman', ascending=False)
html_table = render_rtl_table(df_filtered)
components.html(html_table, height=600, scrolling=True)