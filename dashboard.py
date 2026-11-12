import streamlit as st
import requests
import pandas as pd
import datetime
import math
import json
import os
import streamlit.components.v1 as components

# تنظیمات اصلی Streamlit
st.set_page_config(
    page_title="سیستم پیشرفته اسکلپ اختیار معامله - بورس ایران",
    page_icon="⚡",
    layout="wide"
)

st.markdown("""
<style>
    .block-container { padding-top: 1rem; padding-bottom: 1rem; padding-left: 1rem; padding-right: 1rem; }
    header {visibility: hidden;}
    .stTabs [data-baseweb="tab-list"] { gap: 10px; }
    .stTabs [data-baseweb="tab"] {
        background-color: #1e293b;
        border-radius: 6px;
        color: #94a3b8;
        padding: 8px 16px;
        font-weight: bold;
    }
    .stTabs [aria-selected="true"] {
        background-color: #38bdf8 !important;
        color: #0f172a !important;
    }
</style>
""", unsafe_allow_html=True)

TARGET_BASE_SYMBOLS = ["اهرم", "خودرو", "فملی", "شستا", "وبملت"]

# --- تابع ارسال پیام به تلگرام ---
def send_telegram_alert(bot_token, chat_id, message):
    if not bot_token or not chat_id: return False
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "parse_mode": "Markdown"}
    try:
        res = requests.post(url, data=payload, timeout=5)
        return res.status_code == 200
    except Exception: return False

# --- ریاضیات بلک-شولز ---
def normal_cdf(x):
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

def calculate_delta_and_iv(S, K, days_to_maturity, market_price, r=0.28, is_call=True):
    if S <= 0 or K <= 0 or days_to_maturity <= 0 or market_price <= 0:
        return 0.5 if is_call else -0.5, 40.0
    
    T = days_to_maturity / 365.0
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

def get_days_left(maturity_str):
    try:
        parts = maturity_str.split('/')
        if len(parts) == 3:
            jy, jm, jd = int(parts[0]), int(parts[1]), int(parts[2])
            diff = (jy - 1403) * 365 + (jm - 8) * 30 + (jd - 20)
            return max(diff, 1)
    except Exception: pass
    return 30

# --- دریافت داده‌های زنده ---
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
        delta, iv = calculate_delta_and_iv(underlying_p, strike, days_left, entry_price, is_call=(opt_type == "خرید"))
        
        if trade_val_toman >= 300_000_000 and spread_pct <= 3.0: liquidity_grade = "عالی 🟢"
        elif trade_val_toman >= 50_000_000 and spread_pct <= 7.0: liquidity_grade = "خوب 🟡"
        else: liquidity_grade = "ضعیف/ریسک بالا 🔴"
        
        if spread_pct <= 4.0 and abs(delta) >= 0.35 and trade_val_toman >= 50_000_000: entry_status = "آماده ورود 🚀"
        elif spread_pct <= 8.0: entry_status = "در انتظار تایید ⏳"
        else: entry_status = "عدم ورود (اسپرد بالا) ❌"
        
        if iv > 65: iv_risk = "بالا (حباب شدید) 🔴"
        elif iv >= 40: iv_risk = "متوسط 🟡"
        else: iv_risk = "کم (منطقی) 🟢"
        
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
            'scen_up_1': scen_up_1, 'scen_up_2': scen_up_2, 'scen_down_1': scen_down_1,
            'underlying_price': underlying_p
        })
    return pd.DataFrame(processed)

# --- رندر کارت‌ها و جدول ---
def render_decision_matrix_cards(df_top):
    if df_top.empty: return
    st.markdown("### 📊 ۴ موقعیت برتر اسکلپینگ")
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
                    <div>• ریسک حباب (IV): <b>{row['iv_risk']}</b> ({row['iv']}%)</div>
                </div>
                <div style="font-size: 10px; background: #1a2234; padding: 6px; border-radius: 4px; border-right: 3px solid #38bdf8;">
                    <div>سهم +۱٪ 👈 قیمت: <b style="color:#4ade80;">{row['scen_up_1']:,}</b></div>
                    <div>سهم +۲٪ 👈 قیمت: <b style="color:#22c55e;">{row['scen_up_2']:,}</b></div>
                    <div>سهم -۱٪ 👈 قیمت: <b style="color:#f87171;">{row['scen_down_1']:,}</b></div>
                </div>
            </div>
            """, unsafe_allow_html=True)

def render_rtl_table(df):
    if df.empty: return "<div style='text-align:center; padding:20px; color:#aaa;'>هیچ قراردادی یافت نشد.</div>"
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

# --- رابط کاربری اصلی ---
st.title("⚡ سیستم پیشرفته اسکلپ اختیار معامله")

st.sidebar.header("⚙️ تنظیمات اصلی")
mode = st.sidebar.radio("منبع داده:", ["🔴 بازار زنده (Live TSETMC)", "⏳ ماشین زمان (Time Machine)"])

st.sidebar.markdown("---")
st.sidebar.subheader("📡 سیستم هشدار تلگرام")
enable_tg = st.sidebar.checkbox("فعال‌سازی ارسال هشدار", value=False)
bot_token = st.sidebar.text_input("Bot Token:", type="password") if enable_tg else ""
chat_id = st.sidebar.text_input("Chat ID:") if enable_tg else ""

st.sidebar.markdown("---")
capital_toman = st.sidebar.number_input("سرمایه کل شما (تومان):", min_value=1_000_000, value=100_000_000, step=5_000_000)
risk_per_trade_pct = st.sidebar.slider("حداکثر ریسک هر معامله (%):", 1.0, 10.0, 3.0)

raw_options, stock_prices = [], {"اهرم": 21000, "خودرو": 2800, "فملی": 7200, "شستا": 1200, "وبملت": 2400}

if "Live" in mode:
    components.html("<script>setTimeout(function(){window.parent.postMessage({type:'streamlit:rerun'},'*');},5000);</script>", height=0, width=0)
    raw_options, stock_prices, error_msg = fetch_live_market_data()
    fetch_time = datetime.datetime.now().strftime('%H:%M:%S')
    if error_msg: st.sidebar.error(f"🔴 {error_msg}")
    else: st.sidebar.success(f"🟢 اتصال زنده ({fetch_time})")
else:
    st.sidebar.info("⏳ حالت شبیه‌ساز ماشین زمان فعال است.")
    hist_folder = "history_data"
    if os.path.exists(hist_folder) and os.listdir(hist_folder):
        files = sorted(os.listdir(hist_folder), reverse=True)
        selected_file = st.sidebar.selectbox("انتخاب فایل تاریخچه:", files)
        file_p = os.path.join(hist_folder, selected_file)
        with open(file_p, "r", encoding="utf-8") as f: snapshots = json.load(f)
        if snapshots:
            snap_idx = st.sidebar.slider("انتخاب زمان:", 0, len(snapshots)-1, len(snapshots)-1)
            selected_snap = snapshots[snap_idx]
            st.sidebar.caption(f"⏰ زمان ثبت: {selected_snap.get('timestamp')}")
            raw_options = selected_snap.get('data', [])
    else:
        raw_options = [
            {'lva': 'ضهرم8024', 'lvc': 'اختيارخ اهرم-30000-1405/08/27', 'pmd': 63005, 'pcl': 62997, 'qtc': 10583506000, 'buyOP': 5819, 'blDs': [{'qmd': 1000, 'pmd': 62500, 'qmo': 1200, 'pmo': 63000}]},
            {'lva': 'ضهرم8025', 'lvc': 'اختيارخ اهرم-34000-1405/08/27', 'pmd': 47600, 'pcl': 47600, 'qtc': 8500000000, 'buyOP': 3400, 'blDs': [{'qmd': 500, 'pmd': 47000, 'qmo': 800, 'pmo': 47600}]}
        ]

if not raw_options:
    st.warning("⚠️ دیتایی دریافت نشد.")
    st.stop()

df_all = process_data(raw_options, stock_prices)

# تعریف ۴ تب اصی داشبورد
tab_main, tab_chain, tab_compare, tab_predict = st.tabs([
    "📊 دیده‌بان و سیگنال‌های اسکلپ",
    "🔗 زنجیره قراردادها (Option Chain)",
    "⚖️ مقایسه هوشمند (Comparison)",
    "🧮 پیش‌بین سود و زیان (Profit Predictor)"
])

# --- تب ۱: دیده‌بان اصلی ---
with tab_main:
    col_f1, col_f2, col_f3 = st.columns(3)
    with col_f1:
        selected_base = st.selectbox("نماد پایه:", ["همه"] + TARGET_BASE_SYMBOLS, key="t1_base")
    with col_f2:
        opt_type_filter = st.radio("نوع اختیار:", ["همه", "اختیار خرید 📈", "اختیار فروش 📉"], horizontal=True, key="t1_type")
    with col_f3:
        max_spread = st.slider("حداکثر اسپرد (%):", 0.5, 50.0, 20.0, step=0.5, key="t1_spread")

    df_filtered = df_all.copy()
    if selected_base != "همه": df_filtered = df_filtered[df_filtered['base_symbol'] == selected_base]
    if "خرید" in opt_type_filter: df_filtered = df_filtered[df_filtered['opt_type'] == "خرید"]
    elif "فروش" in opt_type_filter: df_filtered = df_filtered[df_filtered['opt_type'] == "فروش"]
    df_filtered = df_filtered[df_filtered['spread_pct'] <= max_spread]

    df_top_4 = df_filtered.sort_values(by='score', ascending=False).head(4)
    render_decision_matrix_cards(df_top_4)
    st.markdown("<br>", unsafe_allow_html=True)

    if not df_top_4.empty:
        top_pick = df_top_4.iloc[0]
        max_risk_amount = capital_toman * (risk_per_trade_pct / 100.0)
        risk_per_contract = top_pick['entry_price'] * 0.15 * 1000
        recommended_contracts = max(1, int(max_risk_amount / risk_per_contract)) if risk_per_contract > 0 else 1
        st.info(f"💡 **مدیریت ریسک ({top_pick['symbol']}):** حداکثر مجاز به خرید **{recommended_contracts:,} قرارداد** هستید (سقف زیان با حدضرر ۱۵٪: {int(recommended_contracts * risk_per_contract):,} تومان).")

    df_filtered = df_filtered.sort_values(by='trade_val_toman', ascending=False)
    html_table = render_rtl_table(df_filtered)
    components.html(html_table, height=550, scrolling=True)

# --- تب ۲: زنجیره قراردادها (Option Chain) ---
with tab_chain:
    st.subheader("🔗 زنجیره کامل قراردادها (Call | Strike | Put)")
    chain_base = st.selectbox("انتخاب نماد پایه جهت نمایش زنجیره:", TARGET_BASE_SYMBOLS, index=0)
    df_chain_base = df_all[df_all['base_symbol'] == chain_base]
    
    maturities = sorted(list(df_chain_base['maturity'].unique()))
    if maturities:
        selected_m = st.selectbox("انتخاب سررسید:", maturities)
        df_m = df_chain_base[df_chain_base['maturity'] == selected_m]
        
        # دسته‌بندی Call و Put بر اساس قیمت اعمال
        strikes = sorted(list(df_m['strike'].unique()))
        chain_rows = []
        for strk in strikes:
            call_row = df_m[(df_m['strike'] == strk) & (df_m['opt_type'] == "خرید")]
            put_row = df_m[(df_m['strike'] == strk) & (df_m['opt_type'] == "فروش")]
            
            c_sym = call_row.iloc[0]['symbol'] if not call_row.empty else "-"
            c_ask = f"{call_row.iloc[0]['entry_price']:,}" if not call_row.empty else "-"
            c_oi = f"{call_row.iloc[0]['open_interest']:,}" if not call_row.empty else "-"
            c_delta = f"{call_row.iloc[0]['delta']}" if not call_row.empty else "-"
            
            p_sym = put_row.iloc[0]['symbol'] if not put_row.empty else "-"
            p_ask = f"{put_row.iloc[0]['entry_price']:,}" if not put_row.empty else "-"
            p_oi = f"{put_row.iloc[0]['open_interest']:,}" if not put_row.empty else "-"
            p_delta = f"{put_row.iloc[0]['delta']}" if not put_row.empty else "-"
            
            chain_rows.append({
                'اختیار خرید (Call)': c_sym, 'ورود Call': c_ask, 'OI خرید': c_oi, 'دلتا Call': c_delta,
                'قیمت اعمال (Strike)': f"{strk:,}",
                'دلتا Put': p_delta, 'OI فروش': p_oi, 'ورود Put': p_ask, 'اختیار فروش (Put)': p_sym
            })
            
        st.dataframe(pd.DataFrame(chain_rows), use_container_width=True)
    else:
        st.info("هیچ سررسیدی یافت نشد.")

# --- تب ۳: مقایسه هوشمند (Contract Comparison) ---
with tab_compare:
    st.subheader("⚖️ مقایسه شانه به شانه قراردادها")
    all_syms = list(df_all['symbol'].unique())
    col_c1, col_c2 = st.columns(2)
    with col_c1:
        opt1 = st.selectbox("انتخاب قرارداد اول:", all_syms, index=0 if len(all_syms)>0 else 0)
    with col_c2:
        opt2 = st.selectbox("انتخاب قرارداد دوم:", all_syms, index=1 if len(all_syms)>1 else 0)
        
    if opt1 and opt2:
        r1 = df_all[df_all['symbol'] == opt1].iloc[0]
        r2 = df_all[df_all['symbol'] == opt2].iloc[0]
        
        cmp_df = pd.DataFrame({
            'ویژگی / فیلد': ['نماد پایه', 'نوع اختیار', 'قیمت اعمال', 'قیمت ورود (Ask1)', 'هدف اول (+20%)', 'حد ضرر (-15%)', 'دلتا (Δ)', 'نوسان‌پذیری (IV)', 'فاصله اسپرد', 'موقعیت باز (OI)', 'کیفیت نقدشوندگی'],
            f'قرارداد ۱ ({opt1})': [r1['base_symbol'], r1['opt_type'], f"{r1['strike']:,}", f"{r1['entry_price']:,}", f"{r1['target1']:,}", f"{r1['stop_loss']:,}", r1['delta'], f"{r1['iv']}%", f"{r1['spread_pct']}%", f"{r1['open_interest']:,}", r1['liquidity_grade']],
            f'قرارداد ۲ ({opt2})': [r2['base_symbol'], r2['opt_type'], f"{r2['strike']:,}", f"{r2['entry_price']:,}", f"{r2['target2']:,}", f"{r2['stop_loss']:,}", r2['delta'], f"{r2['iv']}%", f"{r2['spread_pct']}%", f"{r2['open_interest']:,}", r2['liquidity_grade']]
        })
        st.table(cmp_df)

# --- تب ۴: پیش‌بین سود و زیان (Profit Predictor) ---
with tab_predict:
    st.subheader("🧮 پیش‌بین هوشمند سود و زیان معامله")
    sel_opt_pred = st.selectbox("انتخاب قرارداد جهت شبیه‌سازی سود:", list(df_all['symbol'].unique()))
    if sel_opt_pred:
        r_pred = df_all[df_all['symbol'] == sel_opt_pred].iloc[0]
        st.markdown(f"**مشخصات:** ورود بر اساس Ask1: **{r_pred['entry_price']:,} ریال** | دلتا: **{r_pred['delta']}** | قیمت پایه: **{int(r_pred['underlying_price']):,} ریال**")
        
        col_p1, col_p2 = st.columns(2)
        with col_p1:
            stock_move_pct = st.slider("پیش‌بینی تغییر قیمت سهم پایه (%):", -5.0, 10.0, 2.0, step=0.5)
        with col_p2:
            num_contracts = st.number_input("تعداد قرارداد انتخابی جهت خرید:", min_value=1, value=10, step=1)
            
        # محاسبات پیش‌بین
        expected_opt_price = round(r_pred['entry_price'] * (1 + (stock_move_pct/100.0 * r_pred['delta'] * (r_pred['underlying_price'] / r_pred['entry_price']))))
        total_investment_toman = (r_pred['entry_price'] * 1000 * num_contracts) / 10.0
        expected_return_toman = ((expected_opt_price - r_pred['entry_price']) * 1000 * num_contracts) / 10.0
        opt_return_pct = round(((expected_opt_price - r_pred['entry_price']) / r_pred['entry_price']) * 100, 1)
        
        st.markdown("---")
        mcol1, mcol2, mcol3 = st.columns(3)
        with mcol1:
            st.metric("ارزش کل سرمایه‌گذاری", f"{int(total_investment_toman):,} تومان")
        with mcol2:
            st.metric("قیمت تخمینی آپشن", f"{expected_opt_price:,} ریال", delta=f"{opt_return_pct}%")
        with mcol3:
            st.metric("سود / زیان خالص تخمینی", f"{int(expected_return_toman):,} تومان", delta=f"{int(expected_return_toman):,} Toman")