import logging
import asyncio
import os
import yt_dlp
import random
import string
from datetime import datetime, timedelta
import pytz
import httpx
from flask import Flask, render_template_string, request, session, redirect, url_for
from threading import Thread

from telegram import Update, Bot, User
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# --- FLASK, CONFIG, & STATE MANAGEMENT ---
app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'default-secret-key-for-local-dev')
ADMIN_USER = os.environ.get('ADMIN_USER', 'admin')
ADMIN_PASS = os.environ.get('ADMIN_PASS', 'password')

TOKEN = os.environ.get('TELEGRAM_TOKEN')
BOT_OWNER_ID = int(os.environ.get('BOT_OWNER_ID', 0))
BOT_VERSION = os.environ.get('BOT_VERSION', '1.0.0') # For update notifications

API_STOCK_URL = "https://gagstock.gleeze.com/grow-a-garden"
API_WEATHER_URL = "https://growagardenstock.com/api/stock/weather"
TRACKING_INTERVAL_SECONDS = 45
MULTOMUSIC_URL = "https://www.youtube.com/watch?v=sPma_hV4_sU"
PRIZED_ITEMS = ["master sprinkler", "beanstalk", "advanced sprinkler", "godly sprinkler", "ember lily"]
UPDATE_GIF_URL = "https://i.pinimg.com/originals/e5/22/07/e52207b837755b763b65b6302409feda.gif"

ACTIVE_TRACKERS, LAST_SENT_DATA, USER_ACTIVITY = {}, {}, []
AUTHORIZED_USERS, ADMIN_USERS = set(), set()
LAST_KNOWN_VERSION = "" # For update detection

# --- LOGGING SETUP ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- PERSISTENT USER & ADMIN STORAGE ---
def load_from_file(filename):
    if not os.path.exists(filename): return set()
    with open(filename, 'r') as f: return {int(line.strip()) for line in f if line.strip().isdigit()}

def save_to_file(filename, user_set):
    with open(filename, 'w') as f:
        for user_id in user_set: f.write(f"{user_id}\n")

def load_all_users():
    global AUTHORIZED_USERS, ADMIN_USERS, LAST_KNOWN_VERSION
    AUTHORIZED_USERS = load_from_file("authorized_users.txt")
    ADMIN_USERS = load_from_file("admins.txt")
    if BOT_OWNER_ID: AUTHORIZED_USERS.add(BOT_OWNER_ID); ADMIN_USERS.add(BOT_OWNER_ID)
    if os.path.exists("version.txt"):
        with open("version.txt", 'r') as f: LAST_KNOWN_VERSION = f.read().strip()
    logger.info(f"Loaded {len(AUTHORIZED_USERS)} users, {len(ADMIN_USERS)} admins. Previous version: {LAST_KNOWN_VERSION or 'N/A'}")

# --- DECOUPLED ACTIVITY LOGGER ---
async def log_user_activity(user: User, command: str, bot: Bot):
    if not user: return
    avatar_url = "https://i.imgur.com/jpfrJd3.png"
    try:
        if user:
            profile_photos = await bot.get_user_profile_photos(user.id, limit=1)
            if profile_photos and profile_photos.photos and profile_photos.photos[0]:
                avatar_file = await profile_photos.photos[0][0].get_file()
                avatar_url = f"https://api.telegram.org/file/bot{TOKEN}/{avatar_file.file_path}"
    except Exception as e: logger.warning(f"Could not fetch avatar for {user.id}. Error: {e}")
    activity_log = {"user_id": user.id, "first_name": user.first_name, "username": user.username or "N/A", "command": command, "timestamp": datetime.now(pytz.utc), "avatar_url": avatar_url}
    USER_ACTIVITY.insert(0, activity_log); del USER_ACTIVITY[50:]
    logger.info(f"Logged activity for {user.first_name}: {command}")

# --- HELPER & CORE BOT FUNCTIONS (Unchanged) ---
PHT = pytz.timezone('Asia/Manila')
def get_ph_time()->datetime: return datetime.now(PHT)
def get_countdown(target: datetime) -> str:
    now = get_ph_time(); time_left = target - now
    if time_left.total_seconds() <= 0: return "Restocked!"
    total_seconds = int(time_left.total_seconds()); h, m, s = total_seconds // 3600, (total_seconds % 3600) // 60, total_seconds % 60
    return f"{h:02}h {m:02}m {s:02}s"
def get_all_restock_timers() -> dict:
    now = get_ph_time(); timers = {}
    next_egg = now.replace(second=0, microsecond=0)
    if now.minute < 30: next_egg = next_egg.replace(minute=30)
    else: next_egg = (next_egg + timedelta(hours=1)).replace(minute=0)
    timers['Egg'] = get_countdown(next_egg)
    next_5 = now.replace(second=0, microsecond=0); next_m = (now.minute // 5 + 1) * 5
    if next_m >= 60: next_5 = (next_5 + timedelta(hours=1)).replace(minute=0)
    else: next_5 = next_5.replace(minute=next_m)
    timers['Gear'] = timers['Seed'] = get_countdown(next_5)
    next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0); timers['Honey'] = get_countdown(next_hour)
    next_7 = now.replace(minute=0, second=0, microsecond=0); next_7h = (now.hour // 7 + 1) * 7
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
    try:
        ydl_opts = {'format': 'bestaudio/best', 'outtmpl': f'{chat_id}_%(title)s.%(ext)s', 'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}], 'quiet': True}
        loop = asyncio.get_running_loop()
        with yt_dlp.YoutubeDL(ydl_opts) as ydl: info = await loop.run_in_executor(None, lambda: ydl.extract_info(MULTOMUSIC_URL, download=True)); filename = ydl.prepare_filename(info).replace('.webm', '.mp3').replace('.m4a', '.mp3')
        await context.bot.send_audio(chat_id=chat_id, audio=open(filename, 'rb'), title="Multo", performer="Cup of Joe"); os.remove(filename)
    except Exception as e: logger.error(f"Failed to send music to {chat_id}: {e}")
async def fetch_all_data() -> dict | None: #... same as before
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            stock_res, weather_res = await asyncio.gather(client.get(API_STOCK_URL), client.get(API_WEATHER_URL))
            stock_res.raise_for_status(); weather_res.raise_for_status(); stock_data_raw, weather_data = stock_res.json()['data'], weather_res.json()
            all_data = {"stock": {}, "weather": weather_data}
            for cat, details in stock_data_raw.items():
                if 'items' in details: all_data["stock"][cat.capitalize()] = [{'name': item['name'], 'value': int(item['quantity'])} for item in details.get('items', [])]
            return all_data
    except Exception as e: logger.error(f"Error fetching all data: {e}"); return None
async def tracking_loop(chat_id: int, bot: Bot, context: ContextTypes.DEFAULT_TYPE, filters: list[str]): #... same as before
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

# --- AESTHETIC HTML TEMPLATES (Unchanged) ---
DASHBOARD_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Bot Dashboard</title><script src="https://cdn.jsdelivr.net/npm/tsparticles-slim@2.12.0/tsparticles.slim.bundle.min.js"></script><style>:root{--bg:#0d1117;--primary:#c9a4ff;--secondary:#58a6ff;--surface:#161b22;--on-surface:#e6edf3;--border:#30363d;--red:#f85149;}body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background-color:var(--bg);color:var(--on-surface);margin:0;padding:1.5rem;overflow-x:hidden;}#tsparticles{position:fixed;top:0;left:0;width:100%;height:100%;z-index:-1;}.container{max-width:1200px;margin:auto;animation:fadeIn 0.8s ease-out;}.header{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;border-bottom:1px solid var(--border);padding-bottom:1rem;margin-bottom:2rem;}h1, h2{font-weight:600;color:white;letter-spacing:-1px;}h1{margin:0;font-size:1.8rem;} h2{border-bottom:1px solid var(--border);padding-bottom:10px;margin:2.5rem 0 1.5rem 0;}h2 i{margin-right:0.5rem;color:var(--primary);}.logout-btn{color:var(--red);text-decoration:none;background-color:rgba(248,81,73,0.1);padding:10px 15px;border-radius:6px;border:1px solid var(--red);font-weight:500;transition:all 0.2s;}.logout-btn:hover{background-color:rgba(248,81,73,0.2);transform:translateY(-2px);}.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:1.5rem;margin-bottom:2.5rem;}.stat-card{background:linear-gradient(145deg,rgba(255,255,255,0.05),rgba(255,255,255,0));backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);padding:1.5rem;border-radius:12px;border:1px solid var(--border);display:flex;align-items:center;gap:1.5rem;transition:all 0.3s ease;}.stat-card:hover{transform:translateY(-5px);box-shadow:0 10px 20px rgba(0,0,0,0.2);}.stat-card .icon{font-size:1.8rem;color:var(--primary);background:linear-gradient(145deg,rgba(201,164,255,0.1),rgba(201,164,255,0.2));width:60px;height:60px;border-radius:50%;display:grid;place-items:center;}.stat-card .value{font-size:2.8rem;font-weight:700;color:white;} .stat-card .label{font-size:1rem;color:#8b949e;}.user-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:1.5rem;}.user-card{background-color:var(--surface);border-radius:12px;border:1px solid var(--border);padding:1.5rem;display:flex;align-items:center;gap:1rem;transition:all 0.3s ease;}.user-card:hover{transform:translateY(-5px);box-shadow:0 10px 20px rgba(0,0,0,0.2);}.user-card img{width:50px;height:50px;border-radius:50%;border:2px solid var(--border);}.user-card .name{font-weight:600;color:white;} .user-card .username{color:#8b949e;font-size:0.9em;}.user-card .status{margin-left:auto;padding:5px 10px;border-radius:20px;font-size:0.8rem;font-weight:600;}.status.muted{background-color:rgba(248,81,73,0.1);color:var(--red);} .status.active{background-color:rgba(46,160,67,0.15);color:#3fb950;}.activity-log{background-color:var(--surface);border-radius:12px;border:1px solid var(--border);overflow:hidden;box-shadow:0 5px 15px rgba(0,0,0,0.1);}table{width:100%;border-collapse:collapse;}th,td{text-align:left;padding:16px 20px;}th{background-color:rgba(187,134,252,0.05);color:var(--primary);font-weight:600;text-transform:uppercase;font-size:0.8rem;letter-spacing:0.5px;}tbody tr{border-bottom:1px solid var(--border);transition:background-color 0.2s;}tbody tr:last-child{border-bottom:none;}tbody tr:hover{background-color:rgba(88,166,255,0.08);}.user-cell{display:flex;align-items:center;gap:15px;}.user-cell img{width:45px;height:45px;border-radius:50%;border:2px solid var(--border);}.user-cell .name{font-weight:600;color:white;}.user-cell .username{color:#8b949e;font-size:0.9em;}code{background-color:#2b2b2b;color:var(--secondary);padding:4px 8px;border-radius:4px;font-family:"SF Mono","Fira Code",monospace;}@keyframes fadeIn{from{opacity:0;transform:translateY(20px);}to{opacity:1;transform:translateY(0);}}@media(max-width:768px){body{padding:1rem;}.header,h1{flex-direction:column;gap:1rem;text-align:center;}.stats-grid,.user-grid{grid-template-columns:1fr;}h1{font-size:1.5rem;}.stat-card .value{font-size:2.2rem;}}</style><link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css"></head><body><div id="tsparticles"></div><div class="container"><div class="header"><h1><i class="fa-solid fa-shield-halved"></i> GAG Bot Dashboard</h1><a href="/logout" class="logout-btn"><i class="fa-solid fa-arrow-right-from-bracket"></i> Logout</a></div><div class="stats-grid"><div class="stat-card"><div class="icon"><i class="fa-solid fa-users"></i></div><div><div class="value" data-target="{{ stats.authorized_users }}">0</div><div class="label">Total Authorized Users</div></div></div><div class="stat-card"><div class="icon"><i class="fa-solid fa-user-shield"></i></div><div><div class="value" data-target="{{ stats.admins }}">0</div><div class="label">Admins</div></div></div></div><h2><i class="fa-solid fa-satellite-dish"></i> Active Trackers ({{ stats.active_trackers }})</h2><div class="user-grid">{% for user in active_users %}<div class="user-card"><img src="{{ user.avatar_url }}" alt="Avatar"><div><div class="name">{{ user.first_name }}</div><div class="username">@{{ user.username }}</div></div><div class="status {{ 'muted' if user.is_muted else 'active' }}">{{ 'MUTED' if user.is_muted else 'ACTIVE' }}</div></div>{% else %} <p>No users are currently tracking.</p> {% endfor %}</div><h2><i class="fa-solid fa-chart-line"></i> Recent Activity</h2><div class="activity-log"><table><thead><tr><th>User</th><th>Command</th><th>Time</th></tr></thead><tbody>{% for log in activity %}<tr><td><div class="user-cell"><img src="{{ log.avatar_url }}" alt="Avatar"><div><div class="name">{{ log.first_name }}</div><div class="username">@{{ log.username }}</div></div></div></td><td><code>{{ log.command }}</code></td><td>{{ log.time_ago }} ago</td></tr>{% endfor %}</tbody></table></div></div><script>document.addEventListener("DOMContentLoaded",function(){tsParticles.load("tsparticles",{preset:"stars",background:{color:{value:"#0d1117"}},particles:{color:{value:"#ffffff"},links:{color:"#ffffff",distance:150,enable:!0,opacity:.1,width:1},move:{enable:!0,speed:.5},number:{density:{enable:!0,area:800},value:40}}});document.querySelectorAll(".value").forEach(e=>{const t=+e.getAttribute("data-target"),o=()=>{const a=+e.innerText;if(a<t){e.innerText=`${Math.ceil(a+t/100)}`;setTimeout(o,20)}else{e.innerText=t}};o()})});</script></body></html>"""
LOGIN_HTML = """<!DOCTYPE html><html><head><title>Admin Login</title><style>:root{--bg:#0d1117;--primary:#c9a4ff;--surface:#161b22;--border:#21262d;--red:#f85149;}body{display:flex;justify-content:center;align-items:center;height:100vh;background-color:var(--bg);color:white;font-family:-apple-system,sans-serif;}.login-box{background-color:var(--surface);padding:40px;border-radius:12px;border:1px solid var(--border);text-align:center;width:340px;box-shadow:0 10px 30px rgba(0,0,0,0.2);animation:fadeIn 0.5s ease-out;}h2{color:var(--primary);margin-top:0;margin-bottom:25px;font-weight:600;letter-spacing:-0.5px;}input{width:100%;box-sizing:border-box;padding:14px;margin-bottom:15px;border-radius:8px;border:1px solid var(--border);background:var(--bg);color:white;font-size:1rem;transition:border-color 0.2s;}input:focus{border-color:var(--primary);outline:none;}button{width:100%;padding:14px;background:linear-gradient(90deg,var(--primary),#9a66e2);color:black;border:none;border-radius:8px;cursor:pointer;font-weight:bold;font-size:1rem;transition:all 0.2s;}button:hover{transform:translateY(-2px);box-shadow:0 4px 15px rgba(201,164,255,0.2);}.error{color:var(--red);background-color:rgba(248,81,73,0.1);padding:10px;border-radius:6px;margin-top:15px;border:1px solid var(--red);}@keyframes fadeIn{from{opacity:0;transform:scale(0.95);}to{opacity:1;transform:scale(1);}}</style></head><body><div class="login-box"><form method="post"><h2>Bot Dashboard Login</h2><input type="text" name="username" placeholder="Username" required><input type="password" name="password" placeholder="Password" required><button type="submit">Login</button>{% if error %}<p class="error">{{ error }}</p>{% endif %}</form></div></body></html>"""

# --- FLASK WEB ROUTES ---
@app.route('/')
def home_route(): return "Bot is alive. Admin dashboard is at /login."
@app.route('/login', methods=['GET', 'POST'])
def login_route():
    error = None
    if request.method == 'POST':
        if request.form['username'] == ADMIN_USER and request.form['password'] == ADMIN_PASS: session['logged_in'] = True; return redirect(url_for('dashboard_route'))
        else: error = 'Invalid Credentials.'
    return render_template_string(LOGIN_HTML, error=error)
@app.route('/dashboard')
def dashboard_route():
    if not session.get('logged_in'): return redirect(url_for('login_route'))
    display_activity, active_users = [], []
    user_info_map = {log['user_id']: log for log in reversed(USER_ACTIVITY)}.values()
    for user_id, tracker_data in ACTIVE_TRACKERS.items():
        user_info = next((u for u in user_info_map if u['user_id'] == user_id), None)
        if user_info: active_users.append({**user_info, 'is_muted': tracker_data['is_muted']})
    for log in USER_ACTIVITY:
        time_diff = datetime.now(pytz.utc) - log['timestamp']
        if time_diff.total_seconds() < 60: time_ago = f"{int(time_diff.total_seconds())}s"
        elif time_diff.total_seconds() < 3600: time_ago = f"{int(time_diff.total_seconds() / 60)}m"
        else: time_ago = f"{int(time_diff.total_seconds() / 3600)}h"
        display_activity.append({**log, "time_ago": time_ago})
    stats = {"active_trackers": len(ACTIVE_TRACKERS), "authorized_users": len(AUTHORIZED_USERS), "admins": len(ADMIN_USERS)}
    return render_template_string(DASHBOARD_HTML, activity=display_activity, stats=stats, active_users=active_users)
@app.route('/logout')
def logout_route(): session.pop('logged_in', None); return redirect(url_for('login_route'))

# --- TELEGRAM COMMAND HANDLERS ---
async def send_full_stock_report(update: Update, context: ContextTypes.DEFAULT_TYPE, filters: list[str]):
    loader_message = await update.message.reply_text("⏳ Fetching all stock categories...")
    data = await fetch_all_data()
    if not data: await loader_message.edit_text("⚠️ Could not fetch data."); return None
    restock_timers = get_all_restock_timers(); weather_info = f"🌤️ Weather: {data['weather'].get('icon', '')} {data['weather'].get('currentWeather', 'N/A')}"
    sent_anything = False
    for category_name, items in data["stock"].items():
        items_to_show = [item for item in items if not filters or any(f in item['name'].lower() for f in filters)]
        if items_to_show: sent_anything = True; category_message = format_category_message(category_name, items_to_show, restock_timers.get(category_name, "N/A"), weather_info); await context.bot.send_message(update.effective_chat.id, text=category_message, parse_mode=ParseMode.HTML)
    if not sent_anything and filters: await context.bot.send_message(update.effective_chat.id, text="Your filter didn't match any items.")
    await loader_message.delete()
    if sent_anything: await send_music_vm(context, update.effective_chat.id)
    return data

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await log_user_activity(user, "/start", context.bot)
    if user.id not in AUTHORIZED_USERS:
        code = "GAG-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=3)) + '-' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=3))
        user_msg = f"👋 <b>Welcome! This is a private bot.</b>\n\nTo get access, send the following code to the bot admin for approval:\n\n🔑 Approval Code: <code>{code}</code>"
        admin_msg = f"👤 <b>New User Request</b>\n\n<b>Name:</b> {user.first_name}\n<b>Username:</b> @{user.username or 'N/A'}\n<b>User ID:</b> <code>{user.id}</code>\n<b>Approval Code:</b> <code>{code}</code>\n\nTo approve, use: <code>/approve {user.id}</code>"
        await update.message.reply_html(user_msg)
        for admin_id in ADMIN_USERS:
            try: await context.bot.send_message(chat_id=admin_id, text=admin_msg, parse_mode=ParseMode.HTML)
            except Exception as e: logger.error(f"Failed to send approval notice to admin {admin_id}: {e}")
        return
    chat_id = user.id
    if chat_id in ACTIVE_TRACKERS: await update.message.reply_text("📡 Already tracking! Use /stop or /refresh."); return
    filters = [f.strip().lower() for f in " ".join(context.args).split('|') if f.strip()]
    initial_data = await send_full_stock_report(update, context, filters)
    if initial_data:
        LAST_SENT_DATA[chat_id] = initial_data; task = asyncio.create_task(tracking_loop(chat_id, context.bot, context, filters))
        ACTIVE_TRACKERS[chat_id] = {'task': task, 'filters': filters, 'is_muted': False}
        await context.bot.send_message(chat_id, text=f"✅ <b>Tracking started!</b>\nNotifications are <b>ON</b>. Use /mute to silence.\n(Filters: <code>{', '.join(filters) or 'None'}</code>)", parse_mode=ParseMode.HTML)

async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_USERS: return
    await log_user_activity(admin, f"/approve {' '.join(context.args)}", context.bot)
    try:
        target_id = int(context.args[0])
        if target_id in AUTHORIZED_USERS: await update.message.reply_text("This user is already authorized."); return
        AUTHORIZED_USERS.add(target_id); save_to_file("authorized_users.txt", AUTHORIZED_USERS)
        await update.message.reply_text(f"✅ User <code>{target_id}</code> has been authorized!", parse_mode=ParseMode.HTML)
        await context.bot.send_message(chat_id=target_id, text="🎉 <b>You have been approved!</b>\n\nYou can now use /start to begin tracking.")
    except (IndexError, ValueError): await update.message.reply_text("⚠️ Usage: <code>/approve [user_id]</code>", parse_mode=ParseMode.HTML)
    except Exception as e: await update.message.reply_text(f"❌ Error approving user: {e}")

async def add_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_USERS: return
    await log_user_activity(admin, f"/addadmin {' '.join(context.args)}", context.bot)
    try:
        target_id = int(context.args[0])
        if target_id in ADMIN_USERS: await update.message.reply_text("This user is already an admin."); return
        ADMIN_USERS.add(target_id); save_to_file("admins.txt", ADMIN_USERS)
        if target_id not in AUTHORIZED_USERS: AUTHORIZED_USERS.add(target_id); save_to_file("authorized_users.txt", AUTHORIZED_USERS)
        await update.message.reply_text(f"👑 User <code>{target_id}</code> is now an admin!", parse_mode=ParseMode.HTML)
        await context.bot.send_message(chat_id=target_id, text="🛡️ <b>You have been promoted to an Admin!</b>")
    except (IndexError, ValueError): await update.message.reply_text("Usage: <code>/addadmin [user_id]</code>", parse_mode=ParseMode.HTML)

async def recent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in AUTHORIZED_USERS: return
    await log_user_activity(user, "/recent", context.bot)
    
    chat_data = LAST_SENT_DATA.get(user.id)
    if not chat_data or not chat_data.get("stock"):
        await update.message.reply_text("I don't have any recent stock data for you yet. Please run /start or /refresh first.")
        return
        
    recent_items = []
    for category, items in chat_data["stock"].items():
        if items:
            # Get the first item as the most "recent" representation of the category
            recent_items.append(items[0])
    
    if not recent_items:
        await update.message.reply_text("It seems the stock is completely empty right now.")
        return
    
    message = "<b>📈 Most Recent Stock Items</b>\n\n"
    message += "\n".join([f"• {add_emoji(i['name'])}: {format_value(i['value'])}" for i in recent_items])
    await update.message.reply_html(message)

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in AUTHORIZED_USERS: return
    await log_user_activity(update.effective_user, "/stop", context.bot); chat_id = update.effective_chat.id
    if chat_id in ACTIVE_TRACKERS: ACTIVE_TRACKERS[chat_id]['task'].cancel(); await update.message.reply_text("🛑 Tracking stopped.")
    else: await update.message.reply_text("⚠️ Not tracking anything.")
async def refresh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in AUTHORIZED_USERS: return
    await log_user_activity(update.effective_user, "/refresh", context.bot); filters = ACTIVE_TRACKERS.get(update.effective_chat.id, {}).get('filters', [])
    await send_full_stock_report(update, context, filters)
async def mute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in AUTHORIZED_USERS: return
    await log_user_activity(update.effective_user, "/mute", context.bot); chat_id = update.effective_chat.id; tracker_info = ACTIVE_TRACKERS.get(chat_id)
    if not tracker_info: await update.message.reply_text("⚠️ Not tracking. Use /start first."); return
    if tracker_info.get('is_muted'): await update.message.reply_text("Notifications already muted.")
    else: tracker_info['is_muted'] = True; await update.message.reply_text("🔇 Notifications muted. Use /unmute to resume.")
async def unmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in AUTHORIZED_USERS: return
    await log_user_activity(update.effective_user, "/unmute", context.bot); chat_id = update.effective_chat.id; tracker_info = ACTIVE_TRACKERS.get(chat_id)
    if not tracker_info: await update.message.reply_text("⚠️ Not tracking. Use /start first."); return
    if not tracker_info.get('is_muted'): await update.message.reply_text("Notifications already on.")
    else: tracker_info['is_muted'] = False; await update.message.reply_text("🔊 Notifications resumed!")
async def dashboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USERS: return
    await log_user_activity(update.effective_user, "/dashboard", context.bot); base_url = os.environ.get('RENDER_EXTERNAL_URL', f'http://localhost:{os.environ.get("PORT", 8080)}')
    dashboard_url = f"{base_url}/login"
    await update.message.reply_text(f"🔒 Your admin dashboard is ready.\n\nPlease log in here: {dashboard_url}", disable_web_page_preview=True)
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in AUTHORIZED_USERS: return
    await log_user_activity(user, "/help", context.bot)
    help_text = "<b>Welcome to the GAG Prized Stock Alerter!</b>\n\n▶️  <b>/start</b> - Shows stock & starts the tracker.\n🔄  <b>/refresh</b> - Manually shows current stock.\n📈  <b>/recent</b> - Shows the most recently stocked items.\n🔇  <b>/mute</b> - Silence all notifications.\n🔊  <b>/unmute</b> - Resume notifications.\n⏹️  <b>/stop</b> - Stops the tracker completely.\n\n"
    if user.id in ADMIN_USERS:
        help_text += "<b>Admin Commands:</b>\n"
        help_text += "🔒  <b>/dashboard</b> - Get the admin dashboard link.\n"
        help_text += "✅  <b>/approve [user_id]</b> - Authorize a new user."
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)

# --- NEW: Update Notification Check ---
async def check_for_updates(application: Application):
    global LAST_KNOWN_VERSION
    if BOT_VERSION != LAST_KNOWN_VERSION:
        logger.info(f"Version change detected! New: {BOT_VERSION}, Old: {LAST_KNOWN_VERSION}")
        if LAST_KNOWN_VERSION != "": # Don't send on first ever startup
            update_message = f"✨ <b>Bot has been updated to v{BOT_VERSION}!</b> ✨\n\nNew features and improvements have been deployed. Restarting the bot is not required."
            # Send to all currently active users
            for chat_id in list(ACTIVE_TRACKERS.keys()):
                try:
                    await application.bot.send_animation(chat_id=chat_id, animation=UPDATE_GIF_URL, caption=update_message, parse_mode=ParseMode.HTML)
                except Exception as e:
                    logger.error(f"Failed to send update notice to {chat_id}: {e}")
        # Update the version file
        with open("version.txt", "w") as f:
            f.write(BOT_VERSION)
        LAST_KNOWN_VERSION = BOT_VERSION


def main():
    if not TOKEN: logger.critical("TELEGRAM_TOKEN not set!"); return
    if not BOT_OWNER_ID: logger.critical("BOT_OWNER_ID not set!"); return
    load_all_users()
    Thread(target=app.run, kwargs={'host': '0.0.0.0', 'port': int(os.environ.get('PORT', 8080))}, daemon=True).start()
    
    global application
    application = Application.builder().token(TOKEN).build()
    
    application.add_handler(CommandHandler("start", start_cmd)); application.add_handler(CommandHandler("stop", stop_cmd))
    application.add_handler(CommandHandler("refresh", refresh_cmd)); application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CommandHandler("mute", mute_cmd)); application.add_handler(CommandHandler("unmute", unmute_cmd))
    application.add_handler(CommandHandler("dashboard", dashboard_cmd)); application.add_handler(CommandHandler("recent", recent_cmd))
    application.add_handler(CommandHandler("approve", approve_cmd)); application.add_handler(CommandHandler("addadmin", add_admin_cmd))
    
    # Schedule the update check to run once shortly after startup
    application.job_queue.run_once(check_for_updates, 5)

    logger.info("Bot with Full Security and Polished Dashboard is running...")
    application.run_polling()

if __name__ == '__main__':
    main()
