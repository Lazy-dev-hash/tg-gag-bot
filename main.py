import logging
import asyncio
import os
import yt_dlp
from datetime import datetime, timedelta
import pytz
import httpx
from flask import Flask, render_template_string, request, session, redirect, url_for
from threading import Thread

from telegram import Update, Bot, User
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# --- WEB SERVER & DASHBOARD SETUP ---
app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'default-secret-key-for-local-dev')
ADMIN_USER = os.environ.get('ADMIN_USER', 'admin')
ADMIN_PASS = os.environ.get('ADMIN_PASS', 'password')

# --- CONFIGURATION ---
TOKEN = os.environ.get('TELEGRAM_TOKEN')
API_STOCK_URL = "https://gagstock.gleeze.com/grow-a-garden"
API_WEATHER_URL = "https://growagardenstock.com/api/stock/weather"
TRACKING_INTERVAL_SECONDS = 45
MULTOMUSIC_URL = "https://www.youtube.com/watch?v=sPma_hV4_sU"
PRIZED_ITEMS = ["master sprinkler", "beanstalk", "advanced sprinkler", "godly sprinkler", "ember lily"]

# --- GLOBAL STATE MANAGEMENT ---
ACTIVE_TRACKERS = {}
LAST_SENT_DATA = {}
USER_ACTIVITY = []

# --- LOGGING SETUP ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- NEW: Decoupled Async Logger (The Fix) ---
async def log_user_activity(user: User, command: str, bot: Bot):
    """Safely fetches user data and logs it for the dashboard."""
    if not user: return
    
    avatar_url = "https://i.imgur.com/jpfrJd3.png" # Default avatar
    try:
        profile_photos = await bot.get_user_profile_photos(user.id, limit=1)
        if profile_photos and profile_photos.photos:
            avatar_file = await profile_photos.photos[0][0].get_file()
            avatar_url = avatar_file.file_path
    except Exception as e:
        logger.warning(f"Could not fetch avatar for {user.id}: {e}")

    activity_log = {
        "user_id": user.id, "first_name": user.first_name, "username": user.username or "N/A",
        "command": command, "timestamp": datetime.now(pytz.utc), "avatar_url": avatar_url
    }
    USER_ACTIVITY.insert(0, activity_log)
    del USER_ACTIVITY[50:]
    logger.info(f"Logged activity for {user.first_name}: {command}")


# --- HELPER & FORMATTING FUNCTIONS (Unchanged) ---
PHT = pytz.timezone('Asia/Manila')
def get_ph_time() -> datetime: return datetime.now(PHT)
def get_countdown(target: datetime) -> str:
    now = get_ph_time(); time_left = target - now
    if time_left.total_seconds() <= 0: return "Restocked!"
    total_seconds = int(time_left.total_seconds())
    h, m, s = total_seconds // 3600, (total_seconds % 3600) // 60, total_seconds % 60
    return f"{h:02}h {m:02}m {s:02}s"
def get_all_restock_timers() -> dict:
    now = get_ph_time(); timers = {}
    next_egg = now.replace(second=0, microsecond=0)
    if now.minute < 30: next_egg = next_egg.replace(minute=30)
    else: next_egg = (next_egg + timedelta(hours=1)).replace(minute=0)
    timers['Egg'] = get_countdown(next_egg)
    next_5 = now.replace(second=0, microsecond=0)
    next_m = (now.minute // 5 + 1) * 5
    if next_m >= 60: next_5 = (next_5 + timedelta(hours=1)).replace(minute=0)
    else: next_5 = next_5.replace(minute=next_m)
    timers['Gear'] = timers['Seed'] = get_countdown(next_5)
    next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    timers['Honey'] = get_countdown(next_hour)
    next_7 = now.replace(minute=0, second=0, microsecond=0)
    next_7h = (now.hour // 7 + 1) * 7
    if next_7h >= 24: next_7 = (next_7 + timedelta(days=1)).replace(hour=next_7h % 24)
    else: next_7 = next_7.replace(hour=next_7h)
    timers['Cosmetics'] = get_countdown(next_7)
    return timers
def format_value(val: int) -> str:
    if val >= 1_000_000: return f"x{val / 1_000_000:.1f}M"
    if val >= 1_000: return f"x{val / 1_000:.1f}K"
    return f"x{val}"
def add_emoji(name: str) -> str:
    emojis = {"Common Egg": "🥚", "Uncommon Egg": "🐣", "Rare Egg": "🍳", "Legendary Egg": "🪺", "Mythical Egg": "🥚", "Bug Egg": "🪲", "Watering Can": "🚿", "Trowel": "🛠️", "Recall Wrench": "🔧", "Basic Sprinkler": "💧", "Advanced Sprinkler": "💦", "Godly Sprinkler": "⛲", "Lightning Rod": "⚡", "Master Sprinkler": "🌊", "Favorite Tool": "❤️", "Harvest Tool": "🌾", "Carrot": "🥕", "Strawberry": "🍓", "Blueberry": "🫐", "Orange Tulip": "🌷", "Tomato": "🍅", "Corn": "🌽", "Daffodil": "🌼", "Watermelon": "🍉", "Pumpkin": "🎃", "Apple": "🍎", "Bamboo": "🎍", "Coconut": "🥥", "Cactus": "🌵", "Dragon Fruit": "🍈", "Mango": "🥭", "Grape": "🍇", "Mushroom": "🍄", "Pepper": "🌶️", "Cacao": "🍫", "Beanstalk": "🌱", "Ember Lily": "🔥"}
    return f"{emojis.get(name, '❔')} {name}"
def format_category_message(category_name: str, items: list, restock_timer: str, weather_info: str) -> str:
    header_emojis = {"Gear": "🛠️", "Seed": "🌱", "Egg": "🥚", "Cosmetics": "🎨", "Honey": "🍯"}
    header = f"{header_emojis.get(category_name, '📦')} <b>Grow A Garden — {category_name} Stock</b>"
    item_list = "\n".join([f"• {add_emoji(i['name'])}: {format_value(i['value'])}" for i in items]) if items else "<i>No items currently in stock.</i>"
    return f"{header}\n\n{item_list}\n\n⏳ Restock in: {restock_timer}\n{weather_info}"
async def send_music_vm(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    logger.info(f"Preparing to send music to {chat_id}")
    try:
        ydl_opts = {'format': 'bestaudio/best', 'outtmpl': f'{chat_id}_%(title)s.%(ext)s', 'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}], 'quiet': True}
        loop = asyncio.get_running_loop()
        with yt_dlp.YoutubeDL(ydl_opts) as ydl: info = await loop.run_in_executor(None, lambda: ydl.extract_info(MULTOMUSIC_URL, download=True)); filename = ydl.prepare_filename(info).replace('.webm', '.mp3').replace('.m4a', '.mp3')
        await context.bot.send_audio(chat_id=chat_id, audio=open(filename, 'rb'), title="Multo", performer="Cup of Joe"); os.remove(filename)
    except Exception as e: logger.error(f"Failed to send music to {chat_id}: {e}")

# --- CORE BOT LOGIC (Unchanged) ---
async def fetch_all_data() -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            stock_res, weather_res = await asyncio.gather(client.get(API_STOCK_URL), client.get(API_WEATHER_URL))
            stock_res.raise_for_status(); weather_res.raise_for_status()
            stock_data_raw, weather_data = stock_res.json()['data'], weather_res.json()
            all_data = {"stock": {}, "weather": weather_data}
            for cat, details in stock_data_raw.items():
                if 'items' in details: all_data["stock"][cat.capitalize()] = [{'name': item['name'], 'value': int(item['quantity'])} for item in details.get('items', [])]
            return all_data
    except Exception as e: logger.error(f"Error fetching all data: {e}"); return None
async def tracking_loop(chat_id: int, bot: Bot, context: ContextTypes.DEFAULT_TYPE, filters: list[str]):
    logger.info(f"Starting tracking for chat_id: {chat_id}")
    try:
        while True:
            await asyncio.sleep(TRACKING_INTERVAL_SECONDS)
            tracker_info = ACTIVE_TRACKERS.get(chat_id); is_muted = tracker_info.get('is_muted', False) if tracker_info else True
            new_data = await fetch_all_data()
            if not new_data: continue
            old_data = LAST_SENT_DATA.get(chat_id, {"stock": {}})
            old_prized = {item['name'] for cat in old_data['stock'].values() for item in cat if item['name'].lower() in PRIZED_ITEMS}
            new_prized = {item['name'] for cat in new_data['stock'].values() for item in cat if item['name'].lower() in PRIZED_ITEMS}
            just_appeared_prized = new_prized - old_prized
            if just_appeared_prized and not is_muted:
                item_details = [item for cat in new_data['stock'].values() for item in cat if item['name'] in just_appeared_prized]
                alert_list = "\n".join([f"› {add_emoji(i['name'])}: {format_value(i['value'])}" for i in item_details])
                alert_message = f"🚨 <b>PRIZED ITEM ALERT!</b> 🚨\n\n{alert_list}"
                try: await bot.send_message(chat_id, text=alert_message, parse_mode=ParseMode.HTML); await send_music_vm(context, chat_id)
                except Exception as e: logger.error(f"Failed prized alert to {chat_id}: {e}")
            for category_name, new_items in new_data["stock"].items():
                old_items_set = {frozenset(item.items()) for item in old_data["stock"].get(category_name, [])}; new_items_set = {frozenset(item.items()) for item in new_items}
                if old_items_set != new_items_set:
                    if len(new_items_set - old_items_set) == 1 and any(item['name'] in just_appeared_prized for item in new_items): continue
                    if not is_muted:
                        items_to_show = [item for item in new_items if not filters or any(f in item['name'].lower() for f in filters)]
                        if items_to_show:
                            restock_timers = get_all_restock_timers(); weather = new_data['weather']; weather_info = f"🌤️ Weather: {weather.get('icon', '')} {weather.get('currentWeather', 'N/A')}"
                            category_message = format_category_message(category_name, items_to_show, restock_timers.get(category_name, "N/A"), weather_info)
                            alert_message = f"🔄 <b>{category_name} has been updated!</b>"
                            try: await bot.send_message(chat_id, text=alert_message, parse_mode=ParseMode.HTML); await bot.send_message(chat_id, text=category_message, parse_mode=ParseMode.HTML)
                            except Exception as e: logger.error(f"Failed category alert to {chat_id}: {e}")
            LAST_SENT_DATA[chat_id] = new_data
    except asyncio.CancelledError: logger.info(f"Tracking loop for {chat_id} cancelled.")
    finally:
        if chat_id in ACTIVE_TRACKERS: del ACTIVE_TRACKERS[chat_id]
        if chat_id in LAST_SENT_DATA: del LAST_SENT_DATA[chat_id]

# --- NEW: Enhanced HTML Templates ---
DASHBOARD_HTML = """
<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Bot Dashboard</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
<style>
:root { --bg: #0d1117; --primary: #bb86fc; --secondary: #03dac6; --surface: #161b22; --on-surface: #c9d1d9; --border: #30363d; --red: #f85149; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; background-color: var(--bg); color: var(--on-surface); margin: 0; padding: 2rem; }
.container { max-width: 1000px; margin: auto; }
.header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); padding-bottom: 1rem; margin-bottom: 2rem; }
h1 { color: white; margin: 0; }
.logout-btn { color: var(--red); text-decoration: none; background-color: rgba(248, 81, 73, 0.1); padding: 10px 15px; border-radius: 6px; border: 1px solid var(--red); font-weight: 500; transition: background-color 0.2s; }
.logout-btn:hover { background-color: rgba(248, 81, 73, 0.2); }
.stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1.5rem; margin-bottom: 2.5rem; }
.stat-card { background-color: var(--surface); padding: 1.5rem; border-radius: 8px; border: 1px solid var(--border); display: flex; align-items: center; gap: 1.5rem; }
.stat-card .icon { font-size: 2rem; color: var(--primary); width: 50px; text-align: center; }
.stat-card .value { font-size: 2.2rem; font-weight: 600; color: white; }
.stat-card .label { font-size: 0.9rem; color: var(--on-surface); }
h2 { color: white; border-bottom: 1px solid var(--border); padding-bottom: 10px; margin: 2.5rem 0 1.5rem 0; }
.activity-log { background-color: var(--surface); border-radius: 8px; border: 1px solid var(--border); overflow: hidden; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 14px 18px; }
th { background-color: rgba(0,0,0,0.2); color: var(--primary); font-weight: 500; }
tbody tr { border-bottom: 1px solid var(--border); transition: background-color 0.2s; }
tbody tr:last-child { border-bottom: none; }
tbody tr:hover { background-color: rgba(187, 134, 252, 0.05); }
.user-cell { display: flex; align-items: center; gap: 12px; }
.user-cell img { width: 40px; height: 40px; border-radius: 50%; border: 2px solid var(--border); }
.user-cell .name { font-weight: 500; color: white; }
.user-cell .username { color: #8b949e; font-size: 0.9em; }
code { background-color: #2b2b2b; color: var(--secondary); padding: 3px 6px; border-radius: 4px; font-family: "SF Mono", "Fira Code", monospace; }
</style></head><body><div class="container">
<div class="header"><h1><i class="fa-solid fa-robot"></i> GAG Bot Dashboard</h1><a href="/logout" class="logout-btn">Logout</a></div>
<div class="stats-grid"><div class="stat-card"><i class="fa-solid fa-satellite-dish icon"></i><div><div class="value">{{ stats.active_trackers }}</div><div class="label">Active Trackers</div></div></div><div class="stat-card"><i class="fa-solid fa-users icon"></i><div><div class="value">{{ stats.unique_users }}</div><div class="label">Recent Unique Users</div></div></div></div>
<h2><i class="fa-solid fa-chart-line"></i> Recent Activity</h2>
<div class="activity-log"><table><thead><tr><th>User</th><th>Command</th><th>Time</th></tr></thead><tbody>
{% for log in activity %}
<tr><td><div class="user-cell"><img src="{{ log.avatar_url }}" alt="Avatar"><div><div class="name">{{ log.first_name }}</div><div class="username">@{{ log.username }}</div></div></div></td><td><code>{{ log.command }}</code></td><td>{{ log.time_ago }} ago</td></tr>
{% endfor %}
</tbody></table></div></div></body></html>
"""
LOGIN_HTML = """
<!DOCTYPE html><html><head><title>Admin Login</title><style>
:root { --bg: #0d1117; --primary: #bb86fc; --surface: #161b22; --border: #30363d; --red: #f85149; }
body { display:flex; justify-content:center; align-items:center; height:100vh; background-color:var(--bg); color:white; font-family: -apple-system, sans-serif; }
.login-box { background-color: var(--surface); padding: 40px; border-radius: 8px; border: 1px solid var(--border); text-align: center; width: 320px; box-shadow: 0 10px 30px rgba(0,0,0,0.2); }
h2 { color: var(--primary); margin-top: 0; margin-bottom: 25px; font-weight: 500; }
input { width: 100%; box-sizing: border-box; padding: 12px; margin-bottom: 15px; border-radius: 6px; border: 1px solid var(--border); background: var(--bg); color: white; font-size: 1rem; }
input:focus { border-color: var(--primary); outline: none; }
button { width: 100%; padding: 12px; background-color: var(--primary); color: black; border: none; border-radius: 6px; cursor: pointer; font-weight: bold; font-size: 1rem; transition: background-color 0.2s; }
button:hover { background-color: #a872e2; }
.error { color: var(--red); margin-top: 15px; }
</style></head><body><div class="login-box"><form method="post"><h2>Bot Dashboard Login</h2><input type="text" name="username" placeholder="Username" required><input type="password" name="password" placeholder="Password" required><button type="submit">Login</button>{% if error %}<p class="error">{{ error }}</p>{% endif %}</form></div></body></html>
"""

# --- FLASK WEB ROUTES (FIXED & STABLE) ---
@app.route('/')
def home_route(): return "Bot is alive. Admin dashboard is at /login."
@app.route('/login', methods=['GET', 'POST'])
def login_route():
    error = None
    if request.method == 'POST':
        if request.form['username'] == ADMIN_USER and request.form['password'] == ADMIN_PASS:
            session['logged_in'] = True; return redirect(url_for('dashboard_route'))
        else: error = 'Invalid Credentials. Please try again.'
    return render_template_string(LOGIN_HTML, error=error)
@app.route('/dashboard')
def dashboard_route():
    if not session.get('logged_in'): return redirect(url_for('login_route'))
    
    # Dashboard is now fully sync and just reads pre-processed data
    display_activity = []
    for log in USER_ACTIVITY:
        time_diff = datetime.now(pytz.utc) - log['timestamp']
        if time_diff.total_seconds() < 60: time_ago = f"{int(time_diff.total_seconds())}s"
        elif time_diff.total_seconds() < 3600: time_ago = f"{int(time_diff.total_seconds() / 60)}m"
        else: time_ago = f"{int(time_diff.total_seconds() / 3600)}h"
        display_activity.append({**log, "time_ago": time_ago})
    
    stats = {"active_trackers": len(ACTIVE_TRACKERS), "unique_users": len(set(log['user_id'] for log in USER_ACTIVITY))}
    return render_template_string(DASHBOARD_HTML, activity=display_activity, stats=stats)
@app.route('/logout')
def logout_route(): session.pop('logged_in', None); return redirect(url_for('login_route'))

# --- TELEGRAM COMMAND HANDLERS (MODIFIED TO USE NEW LOGGER) ---
async def send_full_stock_report(update: Update, context: ContextTypes.DEFAULT_TYPE, filters: list[str]):
    loader_message = await update.message.reply_text("⏳ Fetching all stock categories...")
    data = await fetch_all_data()
    if not data: await loader_message.edit_text("⚠️ Could not fetch data."); return None
    restock_timers = get_all_restock_timers(); weather_info = f"🌤️ Weather: {data['weather'].get('icon', '')} {data['weather'].get('currentWeather', 'N/A')}"
    sent_anything = False
    for category_name, items in data["stock"].items():
        items_to_show = [item for item in items if not filters or any(f in item['name'].lower() for f in filters)]
        if items_to_show:
            sent_anything = True
            category_message = format_category_message(category_name, items_to_show, restock_timers.get(category_name, "N/A"), weather_info)
            await context.bot.send_message(update.effective_chat.id, text=category_message, parse_mode=ParseMode.HTML)
    if not sent_anything and filters: await context.bot.send_message(update.effective_chat.id, text="Your filter didn't match any items.")
    await loader_message.delete()
    if sent_anything: await send_music_vm(context, update.effective_chat.id)
    return data
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await log_user_activity(update.effective_user, "/start", context.bot)
    chat_id = update.effective_chat.id
    if chat_id in ACTIVE_TRACKERS: await update.message.reply_text("📡 Already tracking! Use /stop or /refresh."); return
    filters = [f.strip().lower() for f in " ".join(context.args).split('|') if f.strip()]
    initial_data = await send_full_stock_report(update, context, filters)
    if initial_data:
        LAST_SENT_DATA[chat_id] = initial_data; task = asyncio.create_task(tracking_loop(chat_id, context.bot, context, filters))
        ACTIVE_TRACKERS[chat_id] = {'task': task, 'filters': filters, 'is_muted': False}
        await context.bot.send_message(chat_id, text=f"✅ <b>Tracking started!</b>\nNotifications are <b>ON</b>. Use /mute to silence.\n(Filters: <code>{', '.join(filters) or 'None'}</code>)", parse_mode=ParseMode.HTML)
async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await log_user_activity(update.effective_user, "/stop", context.bot); chat_id = update.effective_chat.id
    if chat_id in ACTIVE_TRACKERS: ACTIVE_TRACKERS[chat_id]['task'].cancel(); await update.message.reply_text("🛑 Tracking stopped.")
    else: await update.message.reply_text("⚠️ Not tracking anything.")
async def refresh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await log_user_activity(update.effective_user, "/refresh", context.bot); filters = ACTIVE_TRACKERS.get(update.effective_chat.id, {}).get('filters', [])
    await send_full_stock_report(update, context, filters)
async def mute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await log_user_activity(update.effective_user, "/mute", context.bot); chat_id = update.effective_chat.id; tracker_info = ACTIVE_TRACKERS.get(chat_id)
    if not tracker_info: await update.message.reply_text("⚠️ Not tracking. Use /start first."); return
    if tracker_info.get('is_muted'): await update.message.reply_text("Notifications already muted.")
    else: tracker_info['is_muted'] = True; await update.message.reply_text("🔇 Notifications muted. Use /unmute to resume.")
async def unmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await log_user_activity(update.effective_user, "/unmute", context.bot); chat_id = update.effective_chat.id; tracker_info = ACTIVE_TRACKERS.get(chat_id)
    if not tracker_info: await update.message.reply_text("⚠️ Not tracking. Use /start first."); return
    if not tracker_info.get('is_muted'): await update.message.reply_text("Notifications already on.")
    else: tracker_info['is_muted'] = False; await update.message.reply_text("🔊 Notifications resumed!")
async def dashboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await log_user_activity(update.effective_user, "/dashboard", context.bot); base_url = os.environ.get('RENDER_EXTERNAL_URL', f'http://localhost:{os.environ.get("PORT", 8080)}')
    dashboard_url = f"{base_url}/login"
    await update.message.reply_text(f"🔒 Your admin dashboard is ready.\n\nPlease log in here: {dashboard_url}", disable_web_page_preview=True)
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await log_user_activity(update.effective_user, "/help", context.bot)
    help_text = "<b>Welcome to the GAG Prized Stock Alerter!</b>\n\n▶️  <b>/start</b> - Starts tracking stock & sends alerts.\n🔄  <b>/refresh</b> - Manually shows current stock.\n🔇  <b>/mute</b> - Silence all notifications.\n🔊  <b>/unmute</b> - Resume notifications.\n⏹️  <b>/stop</b> - Stops the tracker completely.\n🔒  <b>/dashboard</b> - Get the link to the admin dashboard."
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)

def main():
    if not TOKEN: logger.critical("TELEGRAM_TOKEN not set!"); return
    Thread(target=app.run, kwargs={'host': '0.0.0.0', 'port': int(os.environ.get('PORT', 8080))}, daemon=True).start()
    
    global application # Make application instance globally accessible for the dashboard
    application = Application.builder().token(TOKEN).build()
    
    application.add_handler(CommandHandler("start", start_cmd)); application.add_handler(CommandHandler("stop", stop_cmd))
    application.add_handler(CommandHandler("refresh", refresh_cmd)); application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("mute", mute_cmd)); application.add_handler(CommandHandler("unmute", unmute_cmd))
    application.add_handler(CommandHandler("dashboard", dashboard_cmd))
    
    logger.info("Bot and a STABLE Dashboard are running...")
    application.run_polling()

if __name__ == '__main__':
    main()
