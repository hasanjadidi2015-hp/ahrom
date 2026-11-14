import collections
import json

import engine as e

opts = e.fetch_market_watch(e.MARKET_OPTION, e.OPTION_PAPER_TYPE)

total_opt = 0
total_err = 0
counts = collections.Counter()
sample = {}

for rec in opts:
    if not e.is_option_record(rec):
        continue
    total_opt += 1
    code = e.normalize_text(rec.get("lva"))
    name = e.normalize_text(rec.get("lvc"))
    try:
        e.parse_option_name(name, code)
    except e.ParseError as exc:
        total_err += 1
        key = " ".join(str(exc).split()[:4])
        counts[key] += 1
        sample.setdefault(key, f"{code} | {name}")

print("اختیار:", total_opt, "| خطای تفسیر کامل:", total_err)
for key, n in counts.most_common(5):
    print(n, "|", key, "|", sample[key][:90])

for rec in opts:
    if e.normalize_text(rec.get("lva")) == "ضهرم8024":
        print("blDs:", json.dumps(rec.get("blDs"), ensure_ascii=True))
        print("pmd:", rec.get("pmd"), "pmo:", rec.get("pmo"),
              "qtc:", rec.get("qtc"), "buyOP:", rec.get("buyOP"))
        break