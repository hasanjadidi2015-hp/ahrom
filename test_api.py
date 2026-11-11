import requests

# آدرس دیده بان بازار مشتقات (آپشن) - پایدارترین آدرس فعلی
url = "https://cdn.tsetmc.com/api/ClosingPrice/GetMarketWatch?market=3&paperTypes[0]=7"

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fa-IR,fa;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Referer": "https://web.tsetmc.com/",
    "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
}

def test_market_watch():
    print("--- در حال تلاش برای عبور از فایروال بورس و دریافت قیمت‌های زنده ---")
    try:
        # استفاده از Session برای مدیریت کوکی‌ها (شبیه‌سازی بهتر مرورگر)
        session = requests.Session()
        response = session.get(url, headers=headers, timeout=20)
        
        print(f"کد پاسخ سرور: {response.status_code}")
        
        if response.status_code == 200:
            content_type = response.headers.get('Content-Type', '')
            if 'application/json' in content_type:
                data = response.json()
                # در این API، لیست اصلی در 'marketwatch' است
                items = data.get("marketwatch", [])
                
                if items:
                    # پیدا کردن آپشن‌های اهرم (نمادهایی که با 'ض' شروع می‌شوند و مربوط به اهرم هستند)
                    # برای تست، فقط 5 مورد اول را نشان می‌دهیم
                    print(f"✅ موفقیت! تعداد کل نمادهای آپشن دریافت شده: {len(items)}")
                    print("-" * 50)
                    print("نمونه دیتای دریافت شده برای پردازش در داشبورد:")
                    print(items[0]) # نمایش اولین آیتم به صورت خام
                else:
                    print("⚠️ لیست مارکت‌واچ خالی است.")
            else:
                print("❌ پاسخ سرور JSON نیست. متن پاسخ:")
                print(response.text[:200]) # نمایش ابتدای متن خطا
        else:
            print(f"❌ خطا. کد وضعیت: {response.status_code}")
            if response.status_code == 403:
                print("آی‌پی شما توسط بورس بلاک شده است. لطفا اینترنت را قطع و وصل کنید یا DNS را تغییر دهید.")

    except Exception as e:
        print(f"❌ خطای غیرمنتظره: {e}")

if __name__ == "__main__":
    test_market_watch()