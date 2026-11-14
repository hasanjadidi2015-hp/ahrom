import collections
import json
import re

import engine as e

SYMS = set(e.cfg_symbols(e.EngineConfig()))

opts = e.fetch_market_watch(e.MARKET_OPTION, e.OPTION_PAPER_TYPE)

shapes = collections.Counter()
shape_ex = {}
fail_by_base = collections.Counter()
ok_in_sym = 0
book_pos_in_sym = 0
top_pos_in_sym = 0

for rec in opts:
    if not e.is_option_record(rec):
        continue
    name = e.normalize_text(rec.get("lvc"))
    code = e.normalize_text(rec.get("lva"))
    bm = re.match(r"^اختیار\s*[خف]\s+(.+?)\s*-", name)
    base = bm.group(1).strip() if bm else "?"
    try:
        e.parse_option_name(name, code)
    except e.ParseError:
        tail = name.split("-")[-1].strip()
        shape = re.sub(r"\d", "9", tail)
        shapes[shape] += 1
        shape_ex.setdefault(shape, f"{code} | {name}")
        if base in SYMS:
            fail_by_base[base] += 1
        continue
    if base in SYMS:
        ok_in_sym += 1
        b = e.extract_book(rec)
        if b["bid"] is not None or b["ask"] is not None:
            book_pos_in_sym += 1
        if e.to_float(rec.get("pmo")):
            top_pos_in_sym += 1

print("الگوی تاریخ در نام‌های شکست‌خورده:")
for shape, n in shapes.most_common(5):
    print(f"  {shape}: {n} | مثال: {shape_ex[shape][:80]}")
print("شکست تفسیر در نمادهای ما:", dict(fail_by_base))
print("قرارداد قابل تفسیر در نمادهای ما:", ok_in_sym)
print("از این، دفتر سفارش مثبت:", book_pos_in_sym, "| pmo مثبت:", top_pos_in_sym)

rec = next((r for r in opts if e.normalize_text(r.get("lva")) == "ضهرم8024"), None)
if rec is None:
    print("ضهرم8024 پیدا نشد")
else:
    ins = e.normalize_text(rec.get("insCode"))
    print("insCode:", ins)
    url = f"https://cdn.tsetmc.com/api/BestLimits/{ins}"
    try:
        data = e._http_get_json(url)
        print("BestLimits:", json.dumps(data, ensure_ascii=False)[:300])
    except e.EngineError as exc:
        print("BestLimits خطا:", exc)