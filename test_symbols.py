import engine as e
import jdatetime
from datetime import date

options, stocks = e.fetch_market_data()
names = ['اهرم', 'خودرو', 'شستا', 'فملی', 'وبملت', 'شپنا']

today = date.today()
print('امروز میلادی:', today.isoformat(), '| yyyymmdd:', today.strftime('%Y%m%d'))
print('امروز شمسی:', jdatetime.date.today())

for st in stocks:
    name = e.normalize_text(st.get('lva'))
    if name in names:
        print(name, '| dEven', st.get('dEven'), '| hEven', st.get('hEven'), '| zTotTran', st.get('zTotTran'))

n = 0
for rec in options:
    if e.is_option_record(rec) and n < 2:
        n += 1
        print('اختیار', e.normalize_text(rec.get('lva')), '| dEven', rec.get('dEven'), '| hEven', rec.get('hEven'), '| zTotTran', rec.get('zTotTran'))