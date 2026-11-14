import engine as e


def fmt(x):
    return "-" if x is None else f"{x:,.0f}"


def bestlimits(ins):
    data = e._http_get_json(f"https://cdn.tsetmc.com/api/BestLimits/{ins}")
    rows = data.get("bestLimits") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)]


def lv(r, p, q):
    return f"{fmt(e.to_float(r.get(p)))}({fmt(e.to_float(r.get(q)))})"


def main():
    now = e.iran_now()
    print("زمان:", now.isoformat(timespec="seconds"), "| وضعیت:", e.market_status(now))

    opts = e.fetch_market_watch(e.MARKET_OPTION, e.OPTION_PAPER_TYPE)
    stocks = e.fetch_market_watch(e.MARKET_STOCK)

    und = next((s for s in stocks if e.normalize_text(s.get("lva")) == "اهرم"), None)
    und_mid = None
    if und is not None:
        ub = e.extract_book(und)
        print(f"اهرم: خرید={fmt(ub['bid'])} فروش={fmt(ub['ask'])}")
        if ub["bid"] and ub["ask"]:
            und_mid = (ub["bid"] + ub["ask"]) / 2

    cands = []
    for r in opts:
        if not e.is_option_record(r):
            continue
        code = e.normalize_text(r.get("lva"))
        try:
            info = e.parse_option_name(r.get("lvc"), code)
        except e.ParseError:
            continue
        if info["base"] == "اهرم":
            cands.append((info["strike"], code, r))

    if und_mid:
        cands.sort(key=lambda t: abs(t[0] - und_mid))
    by_code = {code: (k, r) for k, code, r in cands}

    picks = [code for _, code, _ in cands[:2]]
    if "ضهرم8024" in by_code and "ضهرم8024" not in picks:
        picks.append("ضهرم8024")

    for code in picks:
        strike, rec = by_code[code]
        book = e.extract_book(rec)
        print(
            f"{code} | K={fmt(strike)} | blDs خرید={fmt(book['bid'])} فروش={fmt(book['ask'])} | "
            f"بالا pmd={rec.get('pmd')} pmo={rec.get('pmo')} | hEven={rec.get('hEven')}"
        )
        try:
            rows = bestlimits(e.normalize_text(rec.get("insCode")))
        except e.EngineError as exc:
            print("   BestLimits خطا:", exc)
            continue
        if not rows:
            print("   BestLimits خالی")
            continue
        top = rows[:3]
        print("   BL خرید:", " | ".join(lv(r, "pMeDem", "qTitMeDem") for r in top))
        print("   BL فروش:", " | ".join(lv(r, "pMeOf", "qTitMeOf") for r in top))


if __name__ == "__main__":
    try:
        main()
    except e.EngineError as exc:
        print("خطا:", exc)