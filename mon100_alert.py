import yfinance as yf
import requests
import time
from datetime import datetime, time as dtime
import pytz
import sys

# ========= CONFIG =========
BOT_TOKEN = "8858808191:AAGVkrMCCkEqfXBLhax9DPAGASMoLLco_6Q"
CHAT_ID = "8909300366"

CHECK_INTERVAL = 300        # 5 minutes
DROP_FROM_HIGH = 1.0        # percent
# ==========================

IST = pytz.timezone("Asia/Kolkata")

def now_ist():
    return datetime.now(IST)

def nasdaq_open_time():
    return dtime(19, 0)   # 7:00 PM IST

def nasdaq_close_time():
    return dtime(1, 30)   # 1:30 AM IST

def is_nasdaq_open(now):
    t = now.time()
    return t >= nasdaq_open_time() or t <= nasdaq_close_time()

def send_alert(msg):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": msg})

def sleep_until_open():
    while True:
        now = now_ist()
        if is_nasdaq_open(now):
            return
        time.sleep(300)

# ---- STARTUP ----
send_alert("✅ MON100 alert system started (waiting for NASDAQ open)")
sleep_until_open()
send_alert("📈 NASDAQ market is OPEN – monitoring started")

# ---- MAIN LOOP ----
while True:
    now = now_ist()
    t = now.time()

    # ⛔ Stop at 1:30 AM IST
    if t > nasdaq_close_time() and t < nasdaq_open_time():
        send_alert("⏹ NASDAQ closed – MON100 monitor stopped for the day")
        sys.exit(0)

    try:
        qqq = yf.Ticker("QQQ")
        data = qqq.history(period="1d", interval="5m")

        high = data["High"].max()
        last = data["Close"].iloc[-1]
        drop_pct = ((high - last) / high) * 100

        print(f"High={high:.2f} Last={last:.2f} Drop={drop_pct:.2f}%")

        if drop_pct >= DROP_FROM_HIGH:
            send_alert(
                f"📉 MON100 BUY ALERT\n"
                f"QQQ down {drop_pct:.2f}% from day high\n"
                f"Consider buying MON100 next NSE session"
            )
            time.sleep(1800)  # anti-spam (30 min)

        time.sleep(CHECK_INTERVAL)

    except Exception as e:
        print("Error:", e)
        time.sleep(300)
