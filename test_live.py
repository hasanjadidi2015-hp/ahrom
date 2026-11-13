import time
from datetime import datetime
import engine as e

NAMES = ['اهرم', 'خودرو', 'شستا', 'فملی', 'وبملت', 'شپنا']
OPTION_CODE = 'ضهرم8024'
ROUNDS = 200
WAIT_SEC = 30
cfg = e.ScalpConfig()

try:
    for i in range(ROUNDS):
        now = datetime.now().strftime('%H:%M:%S')
        try:
            options, stocks = e.fetch_market_data()
            snap = e.analyze_snapshot(options, stocks, cfg)
        except e.DataSourceError as exc:
            print(now, 'خطای اتصال:', exc)
        except Exception as exc:
            print(now, 'خطای غیرمنتظره:', exc)
        else:
            parts = []
            for st in stocks:
                name = e.normalize_text(st.get('lva'))
                if name in NAMES:
                    parts.append(f"{name} {st.get('pmd')}/{st.get('pmo')} z{st.get('zTotTran')} h{st.get('hEven')}")
            opt = next((r for r in options if e.normalize_text(r.get('lva')) == OPTION_CODE), None)
            if opt is not None:
                lvl = e._best_level(opt.get('blDs')) or {}
                parts.append(f"{OPTION_CODE} {lvl.get('pmd')}/{lvl.get('pmo')} z{opt.get('zTotTran')} h{opt.get('hEven')}")
            print(now, snap['status'], '| ایستا:', snap['stale_underlyings'], '|', ' | '.join(parts))
        time.sleep(WAIT_SEC)
    print('پایان تست')
except KeyboardInterrupt:
    print('متوقف شد')