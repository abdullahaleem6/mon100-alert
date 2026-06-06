import yfinance as yf
import requests
import time
import logging
import os
from datetime import datetime, time as dtime, date
from pathlib import Path
import pytz

# ================= CONFIG =================
BOT_TOKEN = os.environ.get("MON100_BOT_TOKEN", "")
CHAT_ID = os.environ.get("MON100_CHAT_ID", "")

CHECK_INTERVAL = 300            # 5 minutes
DROP_FROM_HIGH = 2.0            # QQQ % drop from day high (was 1.0 — too sensitive)
FUTURES_CONFIRM = -0.8          # NASDAQ futures % change threshold
MON100_GAP_BUY = -1.5           # % gap-down for definite buy (was -0.7)
VIX_THRESHOLD = 18.0            # Only buy when VIX > this (filters out noise dips)
# ==========================================

# ================= LOGGING ================
log_dir = Path(__file__).parent / "logs"
log_dir.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_dir / "mon100.log"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("mon100")
# ==========================================

ist = pytz.timezone("Asia/Kolkata")
et = pytz.timezone("US/Eastern")
alert_sent_today = False
definite_buy_sent = False
last_reset_date = None
us_alert_time = None  # Timestamp when US alert fired — used to bind India logic to correct session


def send_alert(msg):
    if not BOT_TOKEN or not CHAT_ID:
        log.error("BOT_TOKEN or CHAT_ID not set in environment")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(url, json={"chat_id": CHAT_ID, "text": msg}, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as e:
        log.error(f"Telegram send failed: {e}")


def now_ist():
    return datetime.now(ist)


def is_us_market_hours():
    """Check if US market is open using Eastern Time (handles DST automatically)."""
    now_et = datetime.now(et)
    t = now_et.time()
    # US market: 9:30 AM - 4:00 PM Eastern (auto-adjusts for DST)
    return dtime(9, 30) <= t <= dtime(16, 0)


def is_weekday():
    return now_ist().weekday() < 5  # Mon=0, Fri=4


def is_after_india_open():
    t = now_ist().time()
    return dtime(9, 15) <= t <= dtime(9, 45)  # Only check in first 30 min window


def get_sleep_seconds(current_ist, has_pending_india_check):
    """Sleep longer outside the narrow windows where the bot can act."""
    current_time = current_ist.time()

    if current_ist.weekday() >= 5:
        return 3600

    if dtime(9, 10) <= current_time <= dtime(9, 45):
        return CHECK_INTERVAL

    if has_pending_india_check and dtime(8, 45) <= current_time < dtime(9, 10):
        return 60

    if is_us_market_hours():
        return CHECK_INTERVAL

    if dtime(16, 0) <= current_time < dtime(18, 45):
        return 1800

    return 3600


def safe_history(ticker, period="1d", interval="5m", retries=3):
    """Fetch ticker history with retry logic for cloud resilience."""
    for attempt in range(retries):
        try:
            data = yf.Ticker(ticker).history(period=period, interval=interval)
            if data is not None and len(data) > 0:
                return data
            log.warning(f"{ticker} returned empty data (attempt {attempt + 1}/{retries})")
        except Exception as e:
            log.warning(f"{ticker} fetch failed (attempt {attempt + 1}/{retries}): {e}")
        if attempt < retries - 1:
            time.sleep(5)
    return None


def get_vix():
    """Get current VIX level (real-time aligned with QQQ)."""
    vix_data = safe_history("^VIX", period="1d", interval="5m")
    if vix_data is not None and len(vix_data) >= 1:
        return vix_data["Close"].iloc[-1]
    return None


def get_mon100_gap():
    """Calculate MON100 gap using daily candles for accuracy."""
    # Use daily data to get correct previous close
    daily = safe_history("MON100.NS", period="5d", interval="1d")

    if daily is None or len(daily) < 2:
        log.warning("Not enough MON100 daily data")
        return None

    prev_close = daily["Close"].iloc[-2]

    # Get today's intraday data for actual open price
    intraday = safe_history("MON100.NS", period="1d", interval="5m")
    if intraday is None or len(intraday) < 1:
        log.warning("No MON100 intraday data yet")
        return None

    today_open = intraday["Open"].dropna().iloc[0]
    gap_pct = ((today_open - prev_close) / prev_close) * 100
    log.info(f"MON100 prev_close={prev_close:.2f} today_open={today_open:.2f} gap={gap_pct:.2f}%")
    return gap_pct


if not BOT_TOKEN or not CHAT_ID:
    log.error("Set MON100_BOT_TOKEN and MON100_CHAT_ID environment variables")
    exit(1)

send_alert("✅ MON100 alert system started")
log.info("System started")


while True:
    try:
        current_ist = now_ist()

        # Skip weekends
        if current_ist.weekday() >= 5:
            time.sleep(get_sleep_seconds(current_ist, False))
            continue

        # ===================== DAILY RESET =====================
        # Reset at 16:00 IST on weekdays only — after India close (15:30) but before US open (~19:00 IST)
        # This preserves alert_sent_today across the overnight boundary so India logic can act on it.
        # Skipping weekend resets ensures Friday US alert persists until Monday India open.
        today = current_ist.date()
        if (
            last_reset_date != today
            and current_ist.time() >= dtime(16, 0)
            and current_ist.weekday() < 5  # Only reset on Mon–Fri
        ):
            alert_sent_today = False
            definite_buy_sent = False
            us_alert_time = None
            last_reset_date = today
            log.info("Daily state reset")

        # ===================== US SESSION LOGIC =====================
        if is_us_market_hours() and not alert_sent_today:

            qqq_data = safe_history("QQQ")

            if qqq_data is None or len(qqq_data) < 2:
                time.sleep(CHECK_INTERVAL)
                continue

            day_high = qqq_data["High"].max()
            last_price = qqq_data["Close"].iloc[-1]
            drop_pct = ((day_high - last_price) / day_high) * 100

            # NASDAQ FUTURES
            nq_data = safe_history("NQ=F")

            if nq_data is not None and len(nq_data) >= 2:
                nq_open = nq_data["Open"].iloc[0]
                nq_last = nq_data["Close"].iloc[-1]
                nq_pct = ((nq_last - nq_open) / nq_open) * 100
            else:
                nq_pct = 0

            # VIX check
            vix_level = get_vix()
            vix_ok = vix_level is not None and vix_level >= VIX_THRESHOLD

            log.info(f"QQQ High={day_high:.2f} Last={last_price:.2f} Drop={drop_pct:.2f}% | NQ={nq_pct:.2f}% | VIX={vix_level}")

            if drop_pct >= DROP_FROM_HIGH and nq_pct <= FUTURES_CONFIRM and vix_ok:
                send_alert(
                    f"🔔 MON100 BUY SETUP\n"
                    f"QQQ drop from high: {drop_pct:.2f}%\n"
                    f"NASDAQ futures: {nq_pct:.2f}%\n"
                    f"VIX: {vix_level:.1f}\n"
                    f"⏰ {now_ist().strftime('%d %b %H:%M IST')}\n"
                    f"📌 Watch MON100 gap at 9:15 AM"
                )
                alert_sent_today = True
                us_alert_time = now_ist()
            else:
                # Log why conditions weren't met
                reasons = []
                if drop_pct < DROP_FROM_HIGH:
                    reasons.append(f"QQQ drop {drop_pct:.2f}% < {DROP_FROM_HIGH}%")
                if nq_pct > FUTURES_CONFIRM:
                    reasons.append(f"NQ {nq_pct:.2f}% > {FUTURES_CONFIRM}%")
                if not vix_ok:
                    reasons.append(f"VIX {vix_level} < {VIX_THRESHOLD}")
                if reasons:
                    log.debug(f"No alert: {', '.join(reasons)}")

        # ===================== INDIA OPEN LOGIC =====================
        # Only fire if US session triggered an alert recently (within last 18 hours)
        us_alert_fresh = (
            us_alert_time is not None
            and (current_ist - us_alert_time).total_seconds() < 18 * 3600
        )
        if dtime(9, 15) <= current_ist.time() <= dtime(9, 45) and alert_sent_today and us_alert_fresh and not definite_buy_sent:

            gap_pct = get_mon100_gap()

            if gap_pct is not None:
                if gap_pct <= MON100_GAP_BUY:
                    send_alert(
                        f"✅ DEFINITE MON100 BUY\n"
                        f"Gap-down: {gap_pct:.2f}%\n"
                        f"⏰ {now_ist().strftime('%d %b %H:%M IST')}\n"
                        f"💡 Buy in tranches if possible"
                    )
                    definite_buy_sent = True
                else:
                    log.info(f"MON100 gap {gap_pct:.2f}% not enough (need <= {MON100_GAP_BUY}%)")
                    # Allow retry until 9:45 in case Yahoo data was delayed

        # Stop rechecking after India window closes
        if alert_sent_today and not definite_buy_sent and current_ist.time() > dtime(9, 45):
            definite_buy_sent = True
            log.info("India window closed, no valid gap found")

        # Heartbeat (every cycle) — helps confirm process is alive on Railway
        log.debug(f"Heartbeat | alert={alert_sent_today} buy={definite_buy_sent} time={current_ist.strftime('%H:%M')}")

        pending_india_check = alert_sent_today and us_alert_fresh and not definite_buy_sent
        time.sleep(get_sleep_seconds(current_ist, pending_india_check))

    except Exception as e:
        log.error(f"Error: {e}", exc_info=True)
        time.sleep(300)