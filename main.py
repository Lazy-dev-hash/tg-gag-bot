import logging
import asyncio
import os
import sys
import yt_dlp
import random
import string
import json
from datetime import datetime, timedelta
import pytz
import httpx

from flask import Flask, render_template_string, request, session, redirect, url_for
from threading import Thread

from telegram import Update, Bot, User, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters

# --- FLASK, CONFIG, & STATE MANAGEMENT ---
app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'default-secret-key-for-local-dev')
ADMIN_USER = os.environ.get('ADMIN_USER', 'admin')
ADMIN_PASS = os.environ.get('ADMIN_PASS', 'password')

TOKEN = os.environ.get('TOKEN') # Token for the main "Hub" bot
BOT_OWNER_ID = int(os.environ.get('BOT_OWNER_ID', 0))
BOT_VERSION = os.environ.get('BOT_VERSION', '13.0.2') # Startup Fix
ADMIN_PANEL_TITLE = os.environ.get('ADMIN_PANEL_TITLE', 'Bot Control Panel')
BOT_CREATOR_NAME = os.environ.get('BOT_CREATOR_NAME', 'Sunnel')

API_STOCK_URL = "https://gagstock.gleeze.com/grow-a-garden"
API_WEATHER_URL = "https://growagardenstock.com/api/stock/weather"
TRACKING_INTERVAL_SECONDS = 45
MULTOMUSIC_URL = "https://www.youtube.com/watch?v=sPma_hV4_sU"
UPDATE_GIF_URL = "https://i.pinimg.com/originals/e5/22/07/e52207b837755b763b65b6302409feda.gif"
WELCOME_VIDEO_URL = "https://youtu.be/VaSazPeDOTM"
DATA_DIR = "/data"

# --- GLOBAL STATE ---
ACTIVE_TRACKERS, LAST_SENT_DATA, USER_ACTIVITY = {}, {}, []
AUTHORIZED_USERS, ADMIN_USERS, BANNED_USERS, RESTRICTED_USERS, PRIZED_ITEMS = set(), set(), set(), set(), set()
LAST_KNOWN_VERSION, USER_INFO_CACHE, VIP_USERS, VIP_REQUESTS, CUSTOM_COMMANDS = "", {}, {}, {}, {}
CHILD_BOTS, BOT_REGISTRATION_REQUESTS = {}, {}
BOT_START_TIME = datetime.now(pytz.utc)
PHT = pytz.timezone('Asia/Manila')

# --- LOGGING SETUP ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- PERSISTENT STORAGE ---
def load_json_from_file(filename, default_type=dict):
    filepath = os.path.join(DATA_DIR, filename)
    if not os.path.exists(filepath): return default_type()
    try:
        with open(filepath, 'r') as f: return json.load(f)
    except (json.JSONDecodeError, ValueError): return default_type()
def save_json_to_file(filename, data):
    filepath = os.path.join(DATA_DIR, filename)
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(filepath, 'w') as f: json.dump(data, f, indent=4)
def load_set_from_file(filename):
    filepath = os.path.join(DATA_DIR, filename)
    if not os.path.exists(filepath): return set()
    with open(filepath, 'r') as f: return {line.strip() for line in f if line.strip()}
def load_int_set_from_file(filename):
    filepath = os.path.join(DATA_DIR, filename);
    if not os.path.exists(filepath): return set()
    with open(filepath, 'r') as f: return {int(line.strip()) for line in f if line.strip().isdigit()}
def save_to_file(filename, data_set):
    filepath = os.path.join(DATA_DIR, filename); os.makedirs(DATA_DIR, exist_ok=True)
    with open(filepath, 'w') as f:
        for item in data_set: f.write(f"{item}\n")
def load_all_data():
    global AUTHORIZED_USERS, ADMIN_USERS, BANNED_USERS, RESTRICTED_USERS, PRIZED_ITEMS, LAST_KNOWN_VERSION, VIP_USERS, CUSTOM_COMMANDS, VIP_REQUESTS, USER_INFO_CACHE, CHILD_BOTS, BOT_REGISTRATION_REQUESTS
    AUTHORIZED_USERS = load_int_set_from_file("authorized_users.txt"); ADMIN_USERS = load_int_set_from_file("admins.txt")
    BANNED_USERS = load_int_set_from_file("banned_users.txt"); RESTRICTED_USERS = load_int_set_from_file("restricted_users.txt")
    PRIZED_ITEMS = load_set_from_file("prized_items.txt") or {"master sprinkler", "beanstalk", "advanced sprinkler", "godly sprinkler", "ember lily"}
    if BOT_OWNER_ID: AUTHORIZED_USERS.add(BOT_OWNER_ID); ADMIN_USERS.add(BOT_OWNER_ID)
    VIP_USERS = load_json_from_file("vips.json")
    USER_INFO_CACHE = load_json_from_file("user_info.json")
    CUSTOM_COMMANDS = load_json_from_file("custom_commands.json")
    VIP_REQUESTS = load_json_from_file("vip_requests.json")
    CHILD_BOTS = load_json_from_file("child_bots.json")
    BOT_REGISTRATION_REQUESTS = load_json_from_file("bot_registrations.json")
    version_path = os.path.join(DATA_DIR, "version.txt")
    if os.path.exists(version_path):
        with open(version_path, 'r') as f: LAST_KNOWN_VERSION = f.read().strip()
    logger.info(f"Loaded {len(AUTHORIZED_USERS)} users, {len(ADMIN_USERS)} admins, and {len(CHILD_BOTS)} child bots.")

async def log_user_activity(user: User, command: str, bot: Bot):
    if not user: return
    avatar_url = "https://i.imgur.com/jpfrJd3.png"
    try:
        user_id_str = str(user.id)
        if user_id_str not in USER_INFO_CACHE or (datetime.now(pytz.utc) - datetime.fromisoformat(USER_INFO_CACHE[user_id_str].get('timestamp', '1970-01-01T00:00:00+00:00'))).total_seconds() > 3600:
            p_photos = await bot.get_user_profile_photos(user.id, limit=1)
            avatar_path = (await p_photos.photos[0][0].get_file()).file_path if p_photos and p_photos.photos and p_photos.photos[0] else None
            existing_info = USER_INFO_CACHE.get(user_id_str, {})
            USER_INFO_CACHE[user_id_str] = {'first_name': user.first_name, 'username': user.username or "N/A", 'avatar_path': avatar_path, 'timestamp': datetime.now(pytz.utc).isoformat(), 'command_count': existing_info.get('command_count', 0), 'approved_date': existing_info.get('approved_date')}
        user_info = USER_INFO_CACHE[user_id_str]
        user_info['command_count'] = user_info.get('command_count', 0) + 1
        if user_info.get('avatar_path'): avatar_url = f"https://api.telegram.org/file/bot{TOKEN}/{user_info['avatar_path']}"
        activity_log = {"user_id": user.id, "first_name": user_info['first_name'], "username": user_info['username'], "command": command, "timestamp": datetime.now(pytz.utc).isoformat(), "avatar_url": avatar_url}
        USER_ACTIVITY.insert(0, activity_log); del USER_ACTIVITY[50:]
        save_json_to_file("user_info.json", USER_INFO_CACHE)
    except Exception as e: logger.warning(f"Could not log activity for {user.id}. Error: {e}")

# --- HELPER & CORE BOT FUNCTIONS ---
def get_ph_time()->datetime: return datetime.now(PHT)
def format_value(val: int) -> str:
    if val >= 1_000_000: return f"x{(val / 1_000_000):.1f}M"
    if val >= 1_000: return f"x{(val / 1_000):.1f}K"
    return f"x{val}"
def add_emoji(name: str) -> str:
    emojis = {"Common Egg": "🥚", "Uncommon Egg": "🐣", "Rare Egg": "🍳", "Legendary Egg": "🪺", "Mythical Egg": "🥚", "Bug Egg": "🪲", "Watering Can": "🚿", "Trowel": "🛠️", "Recall Wrench": "🔧", "Basic Sprinkler": "💧", "Advanced Sprinkler": "💦", "Godly Sprinkler": "⛲", "Lightning Rod": "⚡", "Master Sprinkler": "🌊", "Favorite Tool": "❤️", "Harvest Tool": "🌾", "Carrot": "🥕", "Strawberry": "🍓", "Blueberry": "🫐", "Orange Tulip": "🌷", "Tomato": "🍅", "Corn": "🌽", "Daffodil": "🌼", "Watermelon": "🍉", "Pumpkin": "🎃", "Apple": "🍎", "Bamboo": "🎍", "Coconut": "🥥", "Cactus": "🌵", "Dragon Fruit": "🍈", "Mango": "🥭", "Grape": "🍇", "Mushroom": "🍄", "Pepper": "🌶️", "Cacao": "🍫", "Beanstalk": "🌱", "Ember Lily": "🔥"}
    return f"{emojis.get(name, '❔')} {name}"
def format_timedelta(td: timedelta, short=False) -> str:
    total_seconds = int(td.total_seconds())
    if total_seconds < 0: total_seconds = 0
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if days > 0: parts.append(f"{days}d")
    if hours > 0: parts.append(f"{hours}h")
    if minutes > 0: parts.append(f"{minutes}m")
    if not short or not parts: parts.append(f"{seconds}s")
    return " ".join(parts) if parts else "0s"
def calculate_next_restock_times() -> dict[str, datetime]:
    now = get_ph_time(); next_times = {}
    next_5_min = now.replace(second=0, microsecond=0); next_minute_val = (now.minute // 5 + 1) * 5
    if next_minute_val >= 60: next_5_min = (next_5_min + timedelta(hours=1)).replace(minute=0)
    else: next_5_min = next_5_min.replace(minute=next_minute_val)
    next_times["Gear"] = next_times["Seed"] = next_5_min
    next_egg_min = now.replace(second=0, microsecond=0)
    if now.minute < 30: next_egg_min = next_egg_min.replace(minute=30)
    else: next_egg_min = (next_egg_min + timedelta(hours=1)).replace(minute=0)
    next_times["Egg"] = next_egg_min
    next_times["Honey"] = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    restock_hours = [0, 7, 14, 21]; next_cosmetic_time = now.replace(minute=0, second=0, microsecond=0)
    for h in restock_hours:
        if now.hour < h: next_cosmetic_time = next_cosmetic_time.replace(hour=h); break
    else: next_cosmetic_time = (next_cosmetic_time + timedelta(days=1)).replace(hour=0)
    next_times["Cosmetics"] = next_cosmetic_time
    return next_times
def format_category_message(category_name: str, items: list, restock_timer: str) -> str:
    header_emojis = {"Gear": "🛠️ 𝗚𝗲𝗮𝗿", "Seed": "🌱 𝗦𝗲𝗲𝗱𝘀", "Egg": "🥚 𝗘𝗴𝗴𝘀", "Cosmetics": "🎨 𝗖𝗼𝘀𝗺𝗲𝘁𝗶𝗰𝘀", "Honey": "🍯 𝗛𝗼𝗻𝗲𝘆"}
    header = f"{header_emojis.get(category_name, '📦 Stock')}"
    item_list = "\n".join([f"• {add_emoji(i['name'])}: {format_value(i['value'])}" for i in items]) if items else "<i>No items currently in stock.</i>"
    return f"<b>{header}</b>\n\n{item_list}\n\n⏳ Restock In: {restock_timer}"
def format_weather_message(weather_data: dict) -> str:
    icon = weather_data.get("icon", "❓")
    name = weather_data.get("name", "Unknown")
    bonus = weather_data.get("cropBonuses", "None")
    return f"{icon} <b>Current Weather:</b> {name}\n🌾 <b>Crop Bonus:</b> {bonus}"
async def fetch_all_data() -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            stock_res, weather_res = await asyncio.gather(client.get(API_STOCK_URL), client.get(API_WEATHER_URL))
            stock_res.raise_for_status(); weather_res.raise_for_status()
            stock_data_raw, weather_data_raw = stock_res.json()['data'], weather_res.json()
            
            weather_name = "Unknown"; weather_icon = "❓"; weather_bonus = "None"
            if isinstance(weather_data_raw, dict):
                weather_name = weather_data_raw.get("currentWeather", "Unknown")
                weather_icon = weather_data_raw.get("icon", "❓")
                weather_bonus = weather_data_raw.get("cropBonuses", "None")

            all_data = {"stock": {}, "weather": {"name": weather_name, "icon": weather_icon, "cropBonuses": weather_bonus}}
            for cat, details in stock_data_raw.items():
                if 'items' in details: all_data["stock"][cat.capitalize()] = [{'name': item['name'], 'value': int(item['quantity'])} for item in details.get('items', [])]
            return all_data
    except Exception as e: logger.error(f"Error fetching all data: {e}"); return None
async def send_music_vm(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    try:
        ydl_opts = {'format': 'bestaudio/best', 'outtmpl': f'{chat_id}_%(title)s.%(ext)s', 'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3'}], 'quiet': True}
        loop = asyncio.get_running_loop()
        with yt_dlp.YoutubeDL(ydl_opts) as ydl: info = await loop.run_in_executor(None, lambda: ydl.extract_info(MULTOMUSIC_URL, download=True)); filename = ydl.prepare_filename(info).replace('.webm', '.mp3').replace('.m4a', '.mp3')
        await context.bot.send_audio(chat_id=chat_id, audio=open(filename, 'rb'), title="Multo", performer="Cup of Joe"); os.remove(filename)
    except Exception as e: logger.error(f"Failed to send music to {chat_id}: {e}")
async def tracking_loop(chat_id: int, bot: Bot, context: ContextTypes.DEFAULT_TYPE, filters: list[str]):
    logger.info(f"Starting tracking for chat_id: {chat_id}")
    try:
        while True:
            await asyncio.sleep(TRACKING_INTERVAL_SECONDS)
            tracker_info = ACTIVE_TRACKERS.get(chat_id)
            if not tracker_info: break
            is_muted = tracker_info.get('is_muted', True)
            
            new_data = await fetch_all_data()
            if not new_data: continue

            old_data = LAST_SENT_DATA.get(chat_id, {"stock": {}, "weather": {}})

            if not is_muted and new_data.get("weather") != old_data.get("weather"):
                weather_report = format_weather_message(new_data.get("weather", {}))
                try: await bot.send_message(chat_id, text=f"🌦️ <b>The weather has changed!</b>\n\n{weather_report}", parse_mode=ParseMode.HTML)
                except Exception as e: logger.error(f"Failed weather alert to {chat_id}: {e}")
            
            old_prized = {item['name'].lower() for cat in old_data.get('stock', {}).values() for item in cat}
            new_prized = {item['name'].lower() for cat in new_data.get('stock', {}).values() for item in cat}
            just_appeared = new_prized - old_prized
            prized_items_in_stock = just_appeared.intersection(PRIZED_ITEMS)
            if prized_items_in_stock and not is_muted:
                item_details = [item for cat in new_data['stock'].values() for item in cat if item['name'].lower() in prized_items_in_stock]
                alert_list = "\n".join([f"› {add_emoji(i['name'])}: {format_value(i['value'])}" for i in item_details])
                alert_message = f"🚨 <b>PRIZED ITEM ALERT!</b> 🚨\n\n{alert_list}"
                try: await bot.send_message(chat_id, text=alert_message, parse_mode=ParseMode.HTML); await send_music_vm(context, chat_id)
                except Exception as e: logger.error(f"Failed prized alert to {chat_id}: {e}")
            
            for category_name, new_items in new_data["stock"].items():
                old_items_set = {frozenset(item.items()) for item in old_data.get("stock", {}).get(category_name, [])}; new_items_set = {frozenset(item.items()) for item in new_items}
                if old_items_set != new_items_set:
                    if len(new_items_set - old_items_set) == 1 and any(item['name'].lower() in prized_items_in_stock for item in new_items): continue
                    if not is_muted:
                        items_to_show = [item for item in new_items if not filters or any(f in item['name'].lower() for f in filters)]
                        if items_to_show:
                            next_restock_times = calculate_next_restock_times()
                            time_left = next_restock_times.get(category_name, get_ph_time()) - get_ph_time()
                            countdown_str = format_timedelta(time_left, short=True)
                            
                            category_message = format_category_message(category_name, items_to_show, countdown_str)
                            alert_message = f"🔄 <b>{category_name.upper()} HAS BEEN UPDATED!</b>"
                            try: await bot.send_message(chat_id, text=alert_message, parse_mode=ParseMode.HTML); await bot.send_message(chat_id, text=category_message, parse_mode=ParseMode.HTML)
                            except Exception as e: logger.error(f"Failed category alert to {chat_id}: {e}")
            LAST_SENT_DATA[chat_id] = new_data
    except asyncio.CancelledError: logger.info(f"Tracking loop for {chat_id} cancelled.")
    finally:
        if chat_id in ACTIVE_TRACKERS: del ACTIVE_TRACKERS[chat_id]
        if chat_id in LAST_SENT_DATA: del LAST_SENT_DATA[chat_id]

# --- AESTHETIC HTML TEMPLATES ---
DASHBOARD_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Bot Dashboard</title><script src="https://cdn.jsdelivr.net/npm/tsparticles-slim@2.12.0/tsparticles.slim.bundle.min.js"></script><style>:root{--bg:#0d1117;--primary:#c9a4ff;--secondary:#58a6ff;--surface:#161b22;--on-surface:#e6edf3;--border:#3036d;--red:#f85149;}body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background-color:var(--bg);color:var(--on-surface);margin:0;padding:1.5rem;overflow-x:hidden;}#tsparticles{position:fixed;top:0;left:0;width:100%;height:100%;z-index:-1;}.container{max-width:1200px;margin:auto;animation:fadeIn 0.8s ease-out;}.header{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;border-bottom:1px solid var(--border);padding-bottom:1rem;margin-bottom:2rem;}h1, h2{font-weight:600;color:white;letter-spacing:-1px;}h1{margin:0;font-size:1.8rem;} h2{border-bottom:1px solid var(--border);padding-bottom:10px;margin:2.5rem 0 1.5rem 0;}h2 i{margin-right:0.5rem;color:var(--primary);}.logout-btn{color:var(--red);text-decoration:none;background-color:rgba(248,81,73,0.1);padding:10px 15px;border-radius:6px;border:1px solid var(--red);font-weight:500;transition:all 0.2s;}.logout-btn:hover{background-color:rgba(248,81,73,0.2);transform:translateY(-2px);}.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:1.5rem;margin-bottom:2.5rem;}.stat-card{background:linear-gradient(145deg,rgba(255,255,255,0.05),rgba(255,255,255,0));backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);padding:1.5rem;border-radius:12px;border:1px solid var(--border);display:flex;align-items:center;gap:1.5rem;transition:all 0.3s ease;}.stat-card:hover{transform:translateY(-5px);box-shadow:0 10px 20px rgba(0,0,0,0.2);}.stat-card .icon{font-size:1.8rem;color:var(--primary);background:linear-gradient(145deg,rgba(201,164,255,0.1),rgba(201,164,255,0.2));width:60px;height:60px;border-radius:50%;display:grid;place-items:center;}.stat-card .value{font-size:2.8rem;font-weight:700;color:white;} .stat-card .label{font-size:1rem;color:#8b949e;}.user-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:1.5rem;}.user-card{background-color:var(--surface);border-radius:12px;border:1px solid var(--border);padding:1.5rem;display:flex;align-items:center;gap:1rem;transition:all 0.3s ease;}.user-card:hover{transform:translateY(-5px);box-shadow:0 10px 20px rgba(0,0,0,0.2);}.user-card img{width:50px;height:50px;border-radius:50%;border:2px solid var(--border);}.user-card .name{font-weight:600;color:white;} .user-card .username{color:#8b949e;font-size:0.9em;}.user-card .status{margin-left:auto;padding:5px 10px;border-radius:20px;font-size:0.8rem;font-weight:600;}.status.muted{background-color:rgba(248,81,73,0.1);color:var(--red);} .status.active{background-color:rgba(46,160,67,0.15);color:#3fb950;}.activity-log{background-color:var(--surface);border-radius:12px;border:1px solid var(--border);overflow:hidden;box-shadow:0 5px 15px rgba(0,0,0,0.1);}table{width:100%;border-collapse:collapse;}th,td{text-align:left;padding:16px 20px;}th{background-color:rgba(187,134,252,0.05);color:var(--primary);font-weight:600;text-transform:uppercase;font-size:0.8rem;letter-spacing:0.5px;}tbody tr{border-bottom:1px solid var(--border);transition:background-color 0.2s;}tbody tr:last-child{border-bottom:none;}tbody tr:hover{background-color:rgba(88,166,255,0.08);}.user-cell{display:flex;align-items:center;gap:15px;}.user-cell img{width:45px;height:45px;border-radius:50%;border:2px solid var(--border);}.user-cell .name{font-weight:600;color:white;}.user-cell .username{color:#8b949e;font-size:0.9em;}code{background-color:#2b2b2b;color:var(--secondary);padding:4px 8px;border-radius:4px;font-family:"SF Mono","Fira Code",monospace;}@keyframes fadeIn{from{opacity:0;transform:translateY(20px);}to{opacity:1;transform:translateY(0);}}@media(max-width:768px){body{padding:1rem;}.header,h1{flex-direction:column;gap:1rem;text-align:center;}.stats-grid,.user-grid{grid-template-columns:1fr;}h1{font-size:1.5rem;}.stat-card .value{font-size:2.2rem;}}</style><link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css"></head><body><div id="tsparticles"></div><div class="container"><div class="header"><h1><i class="fa-solid fa-shield-halved"></i> GAG Bot Dashboard</h1><a href="/logout" class="logout-btn"><i class="fa-solid fa-arrow-right-from-bracket"></i> Logout</a></div><div class="stats-grid"><div class="stat-card"><div class="icon"><i class="fa-solid fa-users"></i></div><div><div class="value" data-target="{{ stats.authorized_users }}">0</div><div class="label">Total Authorized Users</div></div></div><div class="stat-card"><div class="icon"><i class="fa-solid fa-user-shield"></i></div><div><div class="value" data-target="{{ stats.admins }}">0</div><div class="label">Admins</div></div></div></div><h2><i class="fa-solid fa-satellite-dish"></i> Active Trackers ({{ stats.active_trackers }})</h2><div class="user-grid">{% for user in active_users %}<div class="user-card"><img src="{{ user.avatar_url }}" alt="Avatar"><div><div class="name">{{ user.first_name }}</div><div class="username">@{{ user.username }}</div></div><div class="status {{ 'muted' if user.is_muted else 'active' }}">{{ 'MUTED' if user.is_muted else 'ACTIVE' }}</div></div>{% else %} <p>No users are currently tracking.</p> {% endfor %}</div><h2><i class="fa-solid fa-chart-line"></i> Recent Activity</h2><div class="activity-log"><table><thead><tr><th>User</th><th>Command</th><th>Time</th></tr></thead><tbody>{% for log in activity %}<tr><td><div class="user-cell"><img src="{{ log.avatar_url }}" alt="Avatar"><div><div class="name">{{ log.first_name }}</div><div class="username">@{{ log.username }}</div></div></div></td><td><code>{{ log.command }}</code></td><td>{{ log.time_ago }} ago</td></tr>{% endfor %}</tbody></table></div></div><script>document.addEventListener("DOMContentLoaded",function(){tsParticles.load("tsparticles",{preset:"stars",background:{color:{value:"#0d1117"}},particles:{color:{value:"#ffffff"},links:{color:"#ffffff",distance:150,enable:!0,opacity:.1,width:1},move:{enable:!0,speed:.5},number:{density:{enable:!0,area:800},value:40}}});document.querySelectorAll(".value").forEach(e=>{const t=+e.getAttribute("data-target"),o=()=>{const a=+e.innerText;if(a<t){e.innerText=`${Math.ceil(a+t/100)}`;setTimeout(o,20)}else{e.innerText=t}};o()})});</script></body></html>"""
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
    for user_id, tracker_data in ACTIVE_TRACKERS.items():
        user_info = USER_INFO_CACHE.get(str(user_id))
        if user_info:
            avatar_url = f"https://api.telegram.org/file/bot{TOKEN}/{user_info['avatar_path']}" if user_info.get('avatar_path') else "https://i.imgur.com/jpfrJd3.png"
            active_users.append({'first_name': user_info['first_name'], 'username': user_info['username'], 'avatar_url': avatar_url, 'is_muted': tracker_data['is_muted']})
    for log in USER_ACTIVITY:
        time_diff = datetime.now(pytz.utc) - datetime.fromisoformat(log['timestamp'])
        display_activity.append({**log, "time_ago": format_timedelta(time_diff)})
    stats = {"active_trackers": len(ACTIVE_TRACKERS), "authorized_users": len(AUTHORIZED_USERS), "admins": len(ADMIN_USERS)}
    return render_template_string(DASHBOARD_HTML, activity=display_activity, stats=stats, active_users=active_users)
@app.route('/logout')
def logout_route(): session.pop('logged_in', None); return redirect(url_for('login_route'))

# --- TELEGRAM COMMAND HANDLERS ---
async def send_full_stock_report(update: Update, context: ContextTypes.DEFAULT_TYPE, filters: list[str]):
    loader_message = await update.message.reply_text("🛰️ Connecting to GAG Network... Please wait.")
    data = await fetch_all_data()
    if not data: await loader_message.edit_text("⚠️ Could not fetch data."); return None
    
    await loader_message.edit_text("🌦️ Fetching weather report...")
    weather_report = format_weather_message(data.get("weather", {}))
    await context.bot.send_message(update.effective_chat.id, text=weather_report, parse_mode=ParseMode.HTML)
    await asyncio.sleep(0.3)
    
    await loader_message.edit_text("📊 Syncing stock data...")
    next_restock_times = calculate_next_restock_times()
    now = get_ph_time()
    sent_anything = False
    for category_name, items in data["stock"].items():
        items_to_show = [item for item in items if not filters or any(f in item['name'].lower() for f in filters)]
        if items_to_show:
            sent_anything = True
            time_left = next_restock_times.get(category_name, now) - now
            countdown_str = format_timedelta(time_left, short=True)
            category_message = format_category_message(category_name, items_to_show, countdown_str)
            await context.bot.send_message(update.effective_chat.id, text=category_message, parse_mode=ParseMode.HTML)

    if not sent_anything and filters: await context.bot.send_message(update.effective_chat.id, text="Your filter didn't match any items.")
    await loader_message.delete()
    if sent_anything: await send_music_vm(context, update.effective_chat.id)
    return data
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user; await log_user_activity(user, "/start", context.bot)
    if user.id in BANNED_USERS: await update.message.reply_text("❌ You have been banned from using this bot."); return
    if user.id not in AUTHORIZED_USERS:
        code = "GAG-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=3)) + '-' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=3))
        user_msg = f"👋 <b>Welcome! This is a private bot.</b>\n\nTo get access, send this code to the admin for approval:\n\n🔑 Approval Code: <code>{code}</code>"
        admin_msg = f"👤 <b>New User Request</b>\n\n<b>Name:</b> {user.first_name}\n<b>User ID:</b> <code>{user.id}</code>\n\nTo approve, use: <code>/approve {user.id}</code>"
        await update.message.reply_html(user_msg)
        for admin_id in ADMIN_USERS:
            try: await context.bot.send_message(chat_id=admin_id, text=admin_msg, parse_mode=ParseMode.HTML)
            except Exception as e: logger.error(f"Failed to send approval notice to admin {admin_id}: {e}")
        return
    if user.id in RESTRICTED_USERS: await update.message.reply_text("⚠️ Your account is restricted. You can refresh stock but cannot start a new tracker. Please contact an admin."); return
    is_vip = str(user.id) in VIP_USERS and datetime.fromisoformat(VIP_USERS.get(str(user.id), '1970-01-01T00:00:00+00:00')) > datetime.now(pytz.utc)
    if is_vip:
        chat_id = user.id
        if chat_id in ACTIVE_TRACKERS:
            tracker_version = ACTIVE_TRACKERS[chat_id].get('version', '0.0.0')
            if tracker_version != BOT_VERSION:
                await update.message.reply_text("✨ <b>An update is available!</b> Let's get you on the latest version...")
                await update_cmd(update, context)
            else:
                await update.message.reply_text("📡 ⭐ VIP tracking is already active and up-to-date!")
            return
        
        filters = [f.strip().lower() for f in " ".join(context.args).split('|') if f.strip()]
        initial_data = await send_full_stock_report(update, context, filters)
        if initial_data:
            LAST_SENT_DATA[chat_id] = initial_data; task = asyncio.create_task(tracking_loop(chat_id, context.bot, context, filters))
            ACTIVE_TRACKERS[chat_id] = {'task': task, 'filters': filters, 'is_muted': False, 'first_name': user.first_name, 'version': BOT_VERSION}
            await context.bot.send_message(chat_id, text=f"✅ ⭐ <b>VIP Tracking Activated!</b>\nYou'll get automatic notifications for stock changes.", parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text("This command starts automatic background tracking for <b>VIP members</b>.\n\nAs a regular user, you can use /refresh to check stock at any time.\n\nTo become a VIP, you can <code>/requestvip</code>.", parse_mode=ParseMode.HTML)
async def next_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id in BANNED_USERS or user.id not in AUTHORIZED_USERS: return
    await log_user_activity(user, "/next", context.bot)
    now = get_ph_time(); next_times = calculate_next_restock_times()
    schedule_lines = []
    category_emojis = {"Seed": "🌱", "Gear": "🛠️", "Egg": "🥚", "Honey": "🍯", "Cosmetics": "🎨"}
    ordered_categories = ["Seed", "Gear", "Egg", "Honey", "Cosmetics"]
    for category in ordered_categories:
        if category in next_times:
            next_time = next_times[category]; time_left = next_time - now
            emoji = category_emojis.get(category, "❓"); time_str = next_time.strftime('%I:%M:%S %p')
            countdown_str = format_timedelta(time_left, short=True)
            schedule_lines.append(f"{emoji} <b>{category}:</b> <code>{time_str}</code> (in {countdown_str})")
    message = "🗓️ <b>Next Restock Schedule</b>\n<i>(Philippine Time, PHT)</i>\n\n" + "\n".join(schedule_lines)
    await update.message.reply_html(message)
async def register_bot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id in BANNED_USERS or user.id not in AUTHORIZED_USERS: return
    await log_user_activity(user, "/registerbot", context.bot)
    is_vip = str(user.id) in VIP_USERS and datetime.fromisoformat(VIP_USERS.get(str(user.id), '1970-01-01T00:00:00+00:00')) > datetime.now(pytz.utc)
    if not is_vip:
        await update.message.reply_html("❌ <b>VIP Membership Required</b>\n\nThis is an exclusive feature for our VIP members. Use /requestvip to learn more.")
        return
    if len(context.args) < 2:
        await update.message.reply_html("⚠️ <b>Usage:</b> <code>/registerbot [TOKEN] [Your Bot Name]</code>\n\n1. Get a token from @BotFather.\n2. Choose a name for your bot.")
        return
    token = context.args[0]
    bot_name = " ".join(context.args[1:])
    try:
        test_bot = Bot(token); bot_info = await test_bot.get_me(); bot_username = bot_info.username
    except Exception:
        await update.message.reply_html("❌ <b>Invalid Token</b>\nThe token you provided seems to be incorrect. Please get a valid one from @BotFather.")
        return
    request_code = f"BRR-{user.id}-{random.randint(1000, 9999)}"
    BOT_REGISTRATION_REQUESTS[request_code] = {"user_id": user.id, "user_first_name": user.first_name, "bot_name": bot_name, "bot_token": token, "bot_username": bot_username}
    save_json_to_file("bot_registrations.json", BOT_REGISTRATION_REQUESTS)
    user_msg = f"⏳ <b>Registration Submitted!</b>\n\nYour request to register '<b>{bot_name}</b>' has been sent to the admins for approval.\n\n<b>Request Code:</b> <code>{request_code}</code>"
    admin_msg = f"🤖 <b>New Bot Registration Request</b>\n\n<b>User:</b> {user.full_name} (<code>{user.id}</code>)\n<b>Requested Bot Name:</b> {bot_name}\n<b>Bot Username:</b> @{bot_username}\n\nTo approve, use: <code>/approvebot {request_code}</code>"
    await update.message.reply_html(user_msg)
    for admin_id in ADMIN_USERS:
        try: await context.bot.send_message(chat_id=admin_id, text=admin_msg, parse_mode=ParseMode.HTML)
        except Exception as e: logger.error(f"Failed to send bot reg notice to admin {admin_id}: {e}")

# --- ADMIN COMMANDS ---
async def approve_bot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_USERS: return
    await log_user_activity(admin, "/approvebot", context.bot)
    if not context.args:
        await update.message.reply_html("⚠️ <b>Usage:</b> <code>/approvebot [request_code]</code>")
        return
    request_code = context.args[0]
    request_data = BOT_REGISTRATION_REQUESTS.get(request_code)
    if not request_data:
        await update.message.reply_html(f"❌ <b>Invalid Code</b>\n\nNo registration request found for code <code>{request_code}</code>.")
        return
    user_id = request_data["user_id"]; bot_name = request_data["bot_name"]; bot_token = request_data["bot_token"]; bot_username = request_data["bot_username"]
    CHILD_BOTS[bot_token] = {"name": bot_name, "owner_id": user_id, "username": bot_username, "approved_by": admin.id, "created_at": datetime.now(pytz.utc).isoformat()}
    save_json_to_file("child_bots.json", CHILD_BOTS)
    del BOT_REGISTRATION_REQUESTS[request_code]
    save_json_to_file("bot_registrations.json", BOT_REGISTRATION_REQUESTS)
    await update.message.reply_html(f"✅ <b>Success!</b>\n\nYou have approved @{bot_username}.\n\n<b>IMPORTANT:</b> A <code>/restart</code> is required to activate this new bot.")
    success_message = f"🎉 <b>Bot Approved!</b> 🎉\n\nCongratulations! Your bot '<b>{bot_name}</b>' has been approved by an admin.\n\n➡️ <b>Your bot's link:</b> https://t.me/{bot_username}\n\nIt will become active after the next system restart."
    try: await context.bot.send_message(chat_id=user_id, text=success_message, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Failed to send bot approval success message to {user_id}: {e}")
        await update.message.reply_html(f"⚠️ Could not notify the user. Please message them manually.")
async def uptime_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_USERS: return
    await log_user_activity(user, "/uptime", context.bot)
    uptime_delta = datetime.now(pytz.utc) - BOT_START_TIME
    uptime_str = format_timedelta(uptime_delta)
    await update.message.reply_html(f"🕒 <b>Bot Uptime:</b> {uptime_str}")
async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_USERS: return
    await log_user_activity(user, "/admin", context.bot)
    base_url = os.environ.get('RENDER_EXTERNAL_URL', f'http://localhost:{os.environ.get("PORT", 8080)}')
    dashboard_url = f"{base_url}/login"
    keyboard = [[InlineKeyboardButton("🌐 Open Dashboard", url=dashboard_url)],[InlineKeyboardButton("👤 Manage Authorized", callback_data='admin_users_0')],[InlineKeyboardButton("⚠️ Manage Restricted", callback_data='admin_restricted_0')],[InlineKeyboardButton("🚫 Manage Banned", callback_data='admin_banned_0')],[InlineKeyboardButton("💎 Prized Items", callback_data='admin_prized')],[InlineKeyboardButton("📊 Bot Stats", callback_data='admin_stats')],[InlineKeyboardButton("📢 Broadcast Message", callback_data='admin_broadcast')],[InlineKeyboardButton("❌ Close", callback_data='admin_close')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    message_to_use = update.message
    if hasattr(update, 'callback_query') and update.callback_query:
        message_to_use = update.callback_query.message
        try: await message_to_use.edit_text(f"👑 <b>{ADMIN_PANEL_TITLE}</b>\n\nSelect an action from the menu below.", reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.warning(f"Could not edit message for admin panel, sending new one. Error: {e}")
            await context.bot.send_message(chat_id=user.id, text=f"👑 <b>{ADMIN_PANEL_TITLE}</b>\n\nSelect an action.", reply_markup=reply_markup, parse_mode=ParseMode.HTML)
    else: await message_to_use.reply_html(f"👑 <b>{ADMIN_PANEL_TITLE}</b>\n\nSelect an action.", reply_markup=reply_markup)
async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    admin_id = query.from_user.id
    if admin_id not in ADMIN_USERS: await query.edit_message_text("❌ You are not authorized for this action."); return
    data = query.data.split('_'); command = data[0]
    if command != "admin": return
    action = data[1]
    if action == "main": await admin_cmd(update, context); return
    if action == "close": await query.delete_message(); return

    list_map = {"banned": {"title": "🚫 Banned Users", "data": BANNED_USERS},"restricted": {"title": "⚠️ Restricted Users", "data": RESTRICTED_USERS},"users": {"title": "👤 Authorized Users", "data": AUTHORIZED_USERS}}
    if action in list_map:
        page = int(data[2]); users_per_page = 5; config = list_map[action]; user_list = sorted(list(config["data"]))
        start_index, end_index = page * users_per_page, page * users_per_page + users_per_page
        keyboard = []
        if not user_list: keyboard.append([InlineKeyboardButton("This list is empty.", callback_data="admin_noop")])
        else:
            for uid in user_list[start_index:end_index]:
                user_info = USER_INFO_CACHE.get(str(uid), {'first_name': f'User {uid}', 'username': 'N/A'})
                text = f"{user_info['first_name']} (@{user_info['username']})"
                keyboard.append([InlineKeyboardButton(text, callback_data=f"admin_user_manage_{uid}")])
        pagination_row = []
        if page > 0: pagination_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"admin_{action}_{page-1}"))
        if end_index < len(user_list): pagination_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"admin_{action}_{page+1}"))
        if pagination_row: keyboard.append(pagination_row)
        keyboard.append([InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='admin_main')])
        await query.edit_message_text(f"<b>{config['title']}</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
        return
    if action == "user":
        action_type = data[2]; target_id = int(data[3])
        if action_type == "manage":
            user_info = USER_INFO_CACHE.get(str(target_id), {'first_name': f'User {target_id}', 'username': 'N/A'})
            status, status_icon = "Active", "✅"
            if target_id in BANNED_USERS: status, status_icon = "Banned", "🚫"
            elif target_id in RESTRICTED_USERS: status, status_icon = "Restricted", "⚠️"
            elif target_id in ADMIN_USERS: status, status_icon = "Admin", "👑"
            if str(target_id) in VIP_USERS and datetime.fromisoformat(VIP_USERS.get(str(target_id), '1970-01-01T00:00:00+00:00')) > datetime.now(pytz.utc): status += " (VIP)"
            keyboard = [[InlineKeyboardButton("✅ Unban" if target_id in BANNED_USERS else "🚫 Ban", callback_data=f"admin_user_unban_{target_id}" if target_id in BANNED_USERS else f"admin_user_ban_{target_id}")],[InlineKeyboardButton("✅ Unrestrict" if target_id in RESTRICTED_USERS else "⚠️ Restrict", callback_data=f"admin_user_unrestrict_{target_id}" if target_id in RESTRICTED_USERS else f"admin_user_restrict_{target_id}")],[InlineKeyboardButton("Demote" if target_id in ADMIN_USERS else "👑 Promote", callback_data=f"admin_user_deladmin_{target_id}" if target_id in ADMIN_USERS else f"admin_user_addadmin_{target_id}")],[InlineKeyboardButton("⬅️ Back to Main Menu", callback_data='admin_main')]]
            await query.edit_message_text(f"<b>Managing:</b> {user_info['first_name']}\n<b>ID:</b> <code>{target_id}</code>\n<b>Status:</b> {status_icon} {status}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
            return
        if target_id == BOT_OWNER_ID: await query.edit_message_text("❌ This action cannot be performed on the bot owner."); return
        text = ""
        if action_type == "ban": BANNED_USERS.add(target_id); AUTHORIZED_USERS.discard(target_id); RESTRICTED_USERS.discard(target_id); VIP_USERS.pop(str(target_id), None); save_to_file("banned_users.txt", BANNED_USERS); save_to_file("authorized_users.txt", AUTHORIZED_USERS); save_to_file("restricted_users.txt", RESTRICTED_USERS); save_json_to_file("vips.json", VIP_USERS); text = f"🚫 User {target_id} has been banned."
        elif action_type == "unban": BANNED_USERS.discard(target_id); AUTHORIZED_USERS.add(target_id); save_to_file("banned_users.txt", BANNED_USERS); save_to_file("authorized_users.txt", AUTHORIZED_USERS); text = f"✅ User {target_id} has been unbanned."
        elif action_type == "restrict": RESTRICTED_USERS.add(target_id); save_to_file("restricted_users.txt", RESTRICTED_USERS); text = f"⚠️ User {target_id} is now restricted."
        elif action_type == "unrestrict": RESTRICTED_USERS.discard(target_id); save_to_file("restricted_users.txt", RESTRICTED_USERS); text = f"✅ User {target_id} is no longer restricted."
        elif action_type == "addadmin": ADMIN_USERS.add(target_id); save_to_file("admins.txt", ADMIN_USERS); text = f"👑 User {target_id} is now an admin."
        elif action_type == "deladmin": ADMIN_USERS.discard(target_id); save_to_file("admins.txt", ADMIN_USERS); text = f"User {target_id} is no longer an admin."
        await query.edit_message_text(text); await asyncio.sleep(2); await admin_cmd(update, context)
        return
    if action == "stats":
        uptime_delta = datetime.now(pytz.utc) - BOT_START_TIME
        uptime_str = format_timedelta(uptime_delta)
        text = f"📊 <b>Bot Statistics</b>\n\n- <b>Uptime:</b> {uptime_str}\n- <b>Authorized Users:</b> {len(AUTHORIZED_USERS)}\n- <b>VIP Members:</b> {len([uid for uid, exp in VIP_USERS.items() if datetime.fromisoformat(exp) > datetime.now(pytz.utc)])}\n- <b>Admins:</b> {len(ADMIN_USERS)}\n- <b>Active Trackers:</b> {len(ACTIVE_TRACKERS)}\n- <b>Banned Users:</b> {len(BANNED_USERS)}\n- <b>Restricted Users:</b> {len(RESTRICTED_USERS)}\n- <b>Recent Activities Logged:</b> {len(USER_ACTIVITY)}"
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data='admin_main')]]), parse_mode=ParseMode.HTML)
    elif action == "prized":
        message = "💎 <b>Current Prized Items:</b>\n\n" + ("\n".join([f"• <code>{item}</code>" for item in sorted(list(PRIZED_ITEMS))]) or "The list is empty.")
        await query.edit_message_text(message, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data='admin_main')]]), parse_mode=ParseMode.HTML)
    elif action == "broadcast":
        await query.message.reply_text("Please use the command: <code>/broadcast [your message]</code>", parse_mode=ParseMode.HTML)
async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user;
    if admin.id not in ADMIN_USERS: return
    await log_user_activity(admin, f"/approve", context.bot)
    try:
        target_id = int(context.args[0])
        if target_id in AUTHORIZED_USERS: await update.message.reply_text("This user is already authorized."); return
        AUTHORIZED_USERS.add(target_id); save_to_file("authorized_users.txt", AUTHORIZED_USERS)
        
        user_id_str = str(target_id)
        if user_id_str not in USER_INFO_CACHE: USER_INFO_CACHE[user_id_str] = {}
        USER_INFO_CACHE[user_id_str]['approved_date'] = datetime.now(pytz.utc).isoformat()
        save_json_to_file("user_info.json", USER_INFO_CACHE)

        try:
            target_user = await context.bot.get_chat(target_id)
            await log_user_activity(target_user, "[Approved]", context.bot)
        except Exception as e: logger.error(f"Could not get chat for newly approved user {target_id}: {e}")
        await update.message.reply_text(f"✅ User <code>{target_id}</code> has been authorized!", parse_mode=ParseMode.HTML)
        await context.bot.send_message(chat_id=target_id, text="🎉 <b>You have been approved!</b>\n\nYou can now use the bot's commands. See /help for details.")
        await send_welcome_video(context, target_id)
    except (IndexError, ValueError): await update.message.reply_text("⚠️ Usage: <code>/approve [user_id]</code>", parse_mode=ParseMode.HTML)
    except Exception as e: await update.message.reply_text(f"❌ Error approving user: {e}")
async def add_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user;
    if admin.id not in ADMIN_USERS: return
    await log_user_activity(admin, f"/addadmin", context.bot)
    try:
        target_id = int(context.args[0])
        if target_id in ADMIN_USERS: await update.message.reply_text("This user is already an admin."); return
        ADMIN_USERS.add(target_id); save_to_file("admins.txt", ADMIN_USERS)
        if target_id not in AUTHORIZED_USERS: AUTHORIZED_USERS.add(target_id); save_to_file("authorized_users.txt", AUTHORIZED_USERS)
        await update.message.reply_text(f"👑 User <code>{target_id}</code> is now an admin!", parse_mode=ParseMode.HTML)
        await context.bot.send_message(chat_id=target_id, text="🛡️ <b>You have been promoted to an Admin!</b>")
    except (IndexError, ValueError): await update.message.reply_text("Usage: <code>/addadmin [user_id]</code>", parse_mode=ParseMode.HTML)
async def msg_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user;
    if admin.id not in ADMIN_USERS: return
    await log_user_activity(admin, f"/msg", context.bot)
    try:
        if len(context.args) < 2: await update.message.reply_text("⚠️ Usage: <code>/msg [user_id] [your message]</code>", parse_mode=ParseMode.HTML); return
        target_id, message_text = int(context.args[0]), " ".join(context.args[1:])
        user_info = USER_INFO_CACHE.get(str(target_id), {'first_name': f"User {target_id}"})
        message_to_user = f"✉️ <b>A message from the Bot Admin:</b>\n\n<i>{message_text}</i>\n\n\n—\n<pre>Reply to this message to talk to the admin.</pre>"
        await context.bot.send_message(chat_id=target_id, text=message_to_user, parse_mode=ParseMode.HTML)
        await update.message.reply_text(f"✅ Message sent successfully to {user_info['first_name']} (<code>{target_id}</code>).", parse_mode=ParseMode.HTML)
    except (IndexError, ValueError): await update.message.reply_text("⚠️ Invalid format. Usage: <code>/msg [user_id] [your message]</code>", parse_mode=ParseMode.HTML)
    except Exception as e: await update.message.reply_text(f"❌ Could not send message. Error: {e}")
async def adminlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user;
    if admin.id not in ADMIN_USERS: return
    await log_user_activity(admin, "/adminlist", context.bot)
    admin_list_text = "<b>🛡️ Current Bot Admins</b>\n\n"
    for admin_id in ADMIN_USERS:
        info = USER_INFO_CACHE.get(str(admin_id), {'first_name': f"Admin {admin_id}", 'username': 'N/A'})
        admin_list_text += f"• {info['first_name']} (@{info['username']}) - <code>{admin_id}</code>\n"
    await update.message.reply_html(admin_list_text)
async def addprized_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user;
    if admin.id not in ADMIN_USERS: return
    await log_user_activity(admin, f"/addprized", context.bot)
    item_name = " ".join(context.args).lower().strip()
    if not item_name: await update.message.reply_text("Usage: <code>/addprized [item name]</code>", parse_mode=ParseMode.HTML); return
    PRIZED_ITEMS.add(item_name); save_to_file("prized_items.txt", PRIZED_ITEMS)
    await update.message.reply_text(f"✅ '<code>{item_name}</code>' has been added to the prized list.", parse_mode=ParseMode.HTML)
async def delprized_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user;
    if admin.id not in ADMIN_USERS: return
    await log_user_activity(admin, f"/delprized", context.bot)
    item_name = " ".join(context.args).lower().strip()
    if not item_name: await update.message.reply_text("Usage: <code>/delprized [item name]</code>", parse_mode=ParseMode.HTML); return
    PRIZED_ITEMS.discard(item_name); save_to_file("prized_items.txt", PRIZED_ITEMS)
    await update.message.reply_text(f"🗑️ '<code>{item_name}</code>' has been removed from the prized list.", parse_mode=ParseMode.HTML)
async def listprized_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id in BANNED_USERS or user.id not in AUTHORIZED_USERS: return
    await log_user_activity(user, "/listprized", context.bot)
    if not PRIZED_ITEMS: message = "The prized item list is currently empty."
    else: message = "💎 <b>Current Prized Items:</b>\n\n" + "\n".join([f"• <code>{item}</code>" for item in sorted(list(PRIZED_ITEMS))])
    await update.message.reply_html(message)
async def restart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_USERS: return
    await log_user_activity(admin, "/restart", context.bot)
    await update.message.reply_text("🚀 Gracefully restarting the bot now...")
    os.execv(sys.executable, ['python'] + sys.argv)
async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_USERS: return
    await log_user_activity(admin, "/broadcast", context.bot)
    message_to_send = " ".join(context.args)
    if not message_to_send: await update.message.reply_text("Usage: <code>/broadcast [your message]</code>", parse_mode=ParseMode.HTML); return
    broadcast_message = f"📣 <b>Broadcast from Admin:</b>\n\n<i>{message_to_send}</i>"
    sent_count = 0
    await update.message.reply_text(f"Sending broadcast to {len(AUTHORIZED_USERS)} users...")
    for user_id in AUTHORIZED_USERS:
        if user_id not in BANNED_USERS:
            try: await context.bot.send_message(chat_id=user_id, text=broadcast_message, parse_mode=ParseMode.HTML); sent_count += 1; await asyncio.sleep(0.1)
            except Exception as e: logger.error(f"Failed to send broadcast to {user_id}: {e}")
    await update.message.reply_text(f"✅ Broadcast complete. Message sent to {sent_count} users.")
async def extendvip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_USERS: return
    await log_user_activity(admin, "/extendvip", context.bot)
    try:
        if len(context.args) != 2: raise ValueError
        target_id, days = int(context.args[0]), int(context.args[1])
        if target_id not in AUTHORIZED_USERS: await update.message.reply_text("❌ This user must be authorized first."); return
        current_expiration_str = VIP_USERS.get(str(target_id))
        current_expiration = datetime.fromisoformat(current_expiration_str) if current_expiration_str else datetime.now(pytz.utc)
        if current_expiration < datetime.now(pytz.utc): current_expiration = datetime.now(pytz.utc)
        new_expiration = current_expiration + timedelta(days=days)
        VIP_USERS[str(target_id)] = new_expiration.isoformat(); save_json_to_file("vips.json", VIP_USERS)
        await update.message.reply_text(f"✅ VIP status for user <code>{target_id}</code> extended by {days} days. New expiration: {new_expiration.strftime('%B %d, %Y')}", parse_mode=ParseMode.HTML)
        await context.bot.send_message(chat_id=target_id, text=f"🎉 Your VIP status has been extended! It now expires on {new_expiration.strftime('%B %d, %Y')}.")
    except (IndexError, ValueError): await update.message.reply_text("⚠️ Usage: <code>/extendvip [user_id] [days]</code>", parse_mode=ParseMode.HTML)
async def access_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_USERS: return
    await log_user_activity(admin, "/access", context.bot)
    if len(context.args) != 1:
        await update.message.reply_text("⚠️ Usage: <code>/access [ticket_code]</code>", parse_mode=ParseMode.HTML); return
    ticket_code = context.args[0]
    if ticket_code in VIP_REQUESTS:
        target_id = VIP_REQUESTS[ticket_code]
        del VIP_REQUESTS[ticket_code]; save_json_to_file("vip_requests.json", VIP_REQUESTS)
        expiration_date = datetime.now(pytz.utc) + timedelta(days=30); VIP_USERS[str(target_id)] = expiration_date.isoformat(); save_json_to_file("vips.json", VIP_USERS)
        user_info = USER_INFO_CACHE.get(str(target_id), {'first_name': f'User {target_id}'})
        await update.message.reply_text(f"✅ <b>VIP Access Granted!</b>\n\nUser {user_info['first_name']} (<code>{target_id}</code>) is now a VIP until {expiration_date.strftime('%B %d, %Y')}.", parse_mode=ParseMode.HTML)
        await context.bot.send_message(chat_id=target_id, text=f"🎉 <b>Congratulations!</b>\n\nYour VIP access has been granted and is active until {expiration_date.strftime('%B %d, %Y')}.\n\nUse /start to activate VIP tracking!")
        await log_user_activity(admin, f"[VIP Granted for {target_id}]", context.bot)
    else:
        await update.message.reply_text("❌ Invalid or expired VIP ticket code.")
async def requestvip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id in BANNED_USERS or user.id not in AUTHORIZED_USERS: return
    await log_user_activity(user, "/requestvip", context.bot)
    nickname = user.first_name.split(" ")[0].capitalize().replace(" ", "")
    random_part = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    ticket_code = f"{nickname}-{random_part}"
    VIP_REQUESTS[ticket_code] = user.id; save_json_to_file("vip_requests.json", VIP_REQUESTS)
    user_msg = f"✨ <b>Your VIP Access Ticket is Ready!</b> ✨\n\nTo complete your request, please send the following ticket code to an admin:\n\n🎫 <b>Ticket Code:</b> <code>{ticket_code}</code>\n\n<i>(Click the code to copy it)</i>"
    admin_msg = f"⭐ <b>New VIP Request Ticket</b>\n\n<b>User:</b> {user.full_name} (<code>{user.id}</code>)\n<b>Ticket Code:</b> <code>{ticket_code}</code>\n\nTo approve, use: <code>/access {ticket_code}</code>"
    await update.message.reply_html(user_msg)
    for admin_id in ADMIN_USERS:
        try: await context.bot.send_message(chat_id=admin_id, text=admin_msg, parse_mode=ParseMode.HTML)
        except Exception as e: logger.error(f"Failed to send VIP request notice to admin {admin_id}: {e}")
async def addcommand_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_USERS: return
    await log_user_activity(admin, "/addcommand", context.bot)
    try:
        if len(context.args) < 3: raise ValueError
        name, permission, response = context.args[0].lower(), context.args[1].lower(), " ".join(context.args[2:])
        if not name.isalnum(): await update.message.reply_text("❌ Command name can only contain letters and numbers."); return
        if permission not in ["user", "admin", "both"]: await update.message.reply_text("❌ Permission must be 'user', 'admin', or 'both'."); return
        CUSTOM_COMMANDS[name] = {"response": response, "permission": permission}; save_json_to_file("custom_commands.json", CUSTOM_COMMANDS)
        await update.message.reply_text(f"✅ Custom command `/{name}` created!\n\nUse /restart for the new command to become active.", parse_mode=ParseMode.HTML)
    except (IndexError, ValueError): await update.message.reply_text("⚠️ Usage: <code>/addcommand [name] [permission] [response]</code>\n\n- <b>Permission</b> can be: user, admin, or both.", parse_mode=ParseMode.HTML)
async def delcommand_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_USERS: return
    await log_user_activity(admin, "/delcommand", context.bot)
    try:
        name = context.args[0].lower()
        if name in CUSTOM_COMMANDS:
            del CUSTOM_COMMANDS[name]; save_json_to_file("custom_commands.json", CUSTOM_COMMANDS)
            await update.message.reply_text(f"🗑️ Custom command `/{name}` deleted.\n\nUse /restart for this change to take effect.", parse_mode=ParseMode.HTML)
        else: await update.message.reply_text(f"❌ Command `/{name}` not found.")
    except IndexError: await update.message.reply_text("⚠️ Usage: <code>/delcommand [name]</code>", parse_mode=ParseMode.HTML)
async def listcommands_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_USERS: return
    await log_user_activity(user, "/listcommands", context.bot)
    if not CUSTOM_COMMANDS: await update.message.reply_text("There are no custom commands currently set."); return
    message = "<b>🔧 Custom Commands List</b>\n\n" + "\n".join([f"• <code>/{name}</code> (Permission: {data['permission']})" for name, data in CUSTOM_COMMANDS.items()])
    await update.message.reply_html(message)

# --- USER COMMANDS & REPLY HANDLER ---
async def recent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user;
    if user.id in BANNED_USERS or user.id not in AUTHORIZED_USERS: return
    await log_user_activity(user, "/recent", context.bot)
    chat_data = LAST_SENT_DATA.get(user.id)
    if not chat_data or not chat_data.get("stock"): await update.message.reply_text("I don't have recent stock data. Please run /start or /refresh."); return
    recent_items = [items[0] for items in chat_data["stock"].values() if items]
    if not recent_items: await update.message.reply_text("The stock is completely empty right now."); return
    message = "<b>📈 Most Recent Stock Items</b>\n\n" + "\n".join([f"• {add_emoji(i['name'])}: {format_value(i['value'])}" for i in recent_items])
    await update.message.reply_html(message)
async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id in BANNED_USERS or user.id not in AUTHORIZED_USERS: return
    await log_user_activity(user, "/stop", context.bot); chat_id = user.id
    if chat_id in ACTIVE_TRACKERS: ACTIVE_TRACKERS[chat_id]['task'].cancel(); await update.message.reply_text("🛑 Tracking stopped.")
    else: await update.message.reply_text("⚠️ Not tracking anything.")
async def refresh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id in BANNED_USERS or user.id not in AUTHORIZED_USERS: return
    await log_user_activity(user, "/refresh", context.bot); filters = ACTIVE_TRACKERS.get(user.id, {}).get('filters', [])
    await send_full_stock_report(update, context, filters)
async def mute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id in BANNED_USERS or user.id not in AUTHORIZED_USERS: return
    await log_user_activity(user, "/mute", context.bot); chat_id = user.id; tracker_info = ACTIVE_TRACKERS.get(chat_id)
    if not tracker_info: await update.message.reply_text("⚠️ Not tracking. Use /start first."); return
    if tracker_info.get('is_muted'): await update.message.reply_text("Notifications already muted.")
    else: tracker_info['is_muted'] = True; await update.message.reply_text("🔇 Notifications muted. Use /unmute to resume.")
async def unmute_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id in BANNED_USERS or user.id not in AUTHORIZED_USERS: return
    await log_user_activity(user, "/unmute", context.bot); chat_id = user.id; tracker_info = ACTIVE_TRACKERS.get(chat_id)
    if not tracker_info: await update.message.reply_text("⚠️ Not tracking. Use /start first."); return
    if not tracker_info.get('is_muted'): await update.message.reply_text("Notifications already on.")
    else: tracker_info['is_muted'] = False; await update.message.reply_text("🔊 Notifications resumed!")
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id in BANNED_USERS: return
    if user.id not in AUTHORIZED_USERS: await update.message.reply_text("You need to be approved to use this bot. Send /start to begin the approval process."); return
    await log_user_activity(user, "/help", context.bot)
    is_vip = str(user.id) in VIP_USERS and datetime.fromisoformat(VIP_USERS.get(str(user.id), '1970-01-01T00:00:00+00:00')) > datetime.now(pytz.utc)
    guide = f"📘 <b>GAG Stock Alerter Guide</b> (v{BOT_VERSION})\n\n<b><u>👤 User Commands</u></b>\n▶️  <b>/start</b> › " + ("Starts VIP background tracking." if is_vip else "Shows current stock.") + "\n🔄  <b>/refresh</b> › Manually shows current stock.\n🗓️  <b>/next</b> › Shows the next restock schedule.\n🤖  <b>/registerbot</b> <code>[token] [name]</code> › Register your own bot (VIP Only).\n📈  <b>/recent</b> › Shows recent items.\n📊  <b>/stats</b> › View your personal bot usage stats.\n💎  <b>/listprized</b> › Shows the prized items list.\n"
    if not is_vip: guide += "⭐  <b>/requestvip</b> › Request a ticket for VIP status.\n"
    if is_vip: guide += "🔇  <b>/mute</b> & 🔊 <b>/unmute</b> › Toggles VIP notifications.\n⏹️  <b>/stop</b> › Stops the VIP tracker completely.\n"
    guide += "✨  <b>/update</b> › Restarts your session to the latest bot version.\n\n"
    if user.id in ADMIN_USERS: guide += "<b><u>🛡️ Admin Commands</u></b>\n👑  <b>/admin</b> › Opens the main admin panel.\n🤖  <b>/approvebot</b> <code>[code]</code> › Approves a new user bot.\n🕒  <b>/uptime</b> › Shows the bot's current running time.\n📢  <b>/broadcast</b> <code>[msg]</code> › Send a message to all users.\n✉️  <b>/msg</b> <code>[id] [msg]</code> › Sends a message to a user.\n✅  <b>/approve</b> <code>[id]</code> › Authorizes a new user.\n🎟️  <b>/access</b> <code>[ticket]</code> › Grants VIP using a ticket code.\n⏳  <b>/extendvip</b> <code>[id] [days]</code> › Extends a user's VIP.\n➕  <b>/addprized</b> <code>[item]</code> › Adds to prized list.\n➖  <b>/delprized</b> <code>[item]</code> › Removes from prized list.\n🚀  <b>/restart</b> › Restarts the bot process.\n"
    await update.message.reply_html(guide)
async def reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id in BANNED_USERS or user.id not in AUTHORIZED_USERS: return
    if update.message.reply_to_message and update.message.reply_to_message.text and "A message from the Bot Admin" in update.message.reply_to_message.text:
        await log_user_activity(user, "[Reply to Admin]", context.bot)
        reply_text = f"🗣️ <b>New Reply from User:</b>\n\n<b>From:</b> {user.first_name} (<code>{user.id}</code>)\n<b>Message:</b> <i>{update.message.text}</i>\n\nTo reply, use <code>/msg {user.id} [your message]</code>"
        for admin_id in ADMIN_USERS:
            try: await context.bot.send_message(chat_id=admin_id, text=reply_text, parse_mode=ParseMode.HTML)
            except Exception as e: logger.error(f"Failed to forward reply to admin {admin_id}: {e}")
        await update.message.reply_text("✅ Your reply has been sent to the admins.")
async def update_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id in BANNED_USERS or user.id not in AUTHORIZED_USERS: return
    await log_user_activity(user, "/update", context.bot)
    if BOT_VERSION == LAST_KNOWN_VERSION:
        await update.message.reply_text(f"✅ <b>You're all set!</b>\n\nYou are already running the latest version (v{BOT_VERSION}).")
        return
    await update.message.reply_text("✅ Great! Updating you to the latest version now...")
    if user.id in ACTIVE_TRACKERS: ACTIVE_TRACKERS[user.id]['task'].cancel()
    await start_cmd(update, context)
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id in BANNED_USERS or user.id not in AUTHORIZED_USERS: return
    await log_user_activity(user, "/stats", context.bot)
    user_info = USER_INFO_CACHE.get(str(user.id), {})
    command_count = user_info.get('command_count', 0)
    approved_date_str = user_info.get('approved_date')
    stats_message = f"📊 <b>Your Personal Bot Stats</b>\n\n<b>Name:</b> {user.first_name}\n<b>Commands Used:</b> {command_count}\n"
    if approved_date_str:
        approved_date = datetime.fromisoformat(approved_date_str)
        days_since = (datetime.now(pytz.utc) - approved_date).days
        stats_message += f"<b>Member Since:</b> {approved_date.strftime('%B %d, %Y')} ({days_since} days ago)\n"
    
    status_line = "<b>Status:</b> ✅ Standard User"
    if str(user.id) in VIP_USERS and datetime.fromisoformat(VIP_USERS.get(str(user.id), '1970-01-01T00:00:00+00:00')) > datetime.now(pytz.utc):
        vip_exp_date = datetime.fromisoformat(VIP_USERS[str(user.id)])
        status_line = f"<b>Status:</b> ⭐ VIP (Expires: {vip_exp_date.strftime('%B %d, %Y')})"
    
    stats_message += status_line
    await update.message.reply_html(stats_message)
async def check_for_updates(application: Application):
    global LAST_KNOWN_VERSION
    if BOT_VERSION != LAST_KNOWN_VERSION:
        logger.info(f"Version change detected! New: {BOT_VERSION}, Old: {LAST_KNOWN_VERSION}")
        if LAST_KNOWN_VERSION != "":
            update_message = f"🚀 <b>A new version (v{BOT_VERSION}) is available!</b>\n\nI've been upgraded with new features and improvements.\n\nTo get the latest version, you can use the /update command."
            for chat_id, tracker_data in list(ACTIVE_TRACKERS.items()):
                try: await application.bot.send_animation(chat_id=chat_id, animation=UPDATE_GIF_URL, caption=update_message, parse_mode=ParseMode.HTML)
                except Exception as e: logger.error(f"Failed to send update notice to {chat_id}: {e}")
        version_filepath = os.path.join(DATA_DIR, "version.txt")
        with open(version_filepath, "w") as f: f.write(BOT_VERSION)
        LAST_KNOWN_VERSION = BOT_VERSION
async def send_welcome_video(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    processing_msg = None
    try:
        processing_msg = await context.bot.send_message(chat_id=chat_id, text="🎁 Preparing your welcome video...")
        ydl_opts = {'format': 'best[ext=mp4][height<=720]/best[ext=mp4]/best','outtmpl': f'{chat_id}_welcome_video.%(ext)s','quiet': True}
        loop = asyncio.get_running_loop()
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(WELCOME_VIDEO_URL, download=True))
            filename = ydl.prepare_filename(info).replace('.webm', '.mp4')
        caption_text = "✨ <b>Welcome to the GAG Stock Alerter!</b> ✨\n\nThis video is a small token to welcome you to our community. I'm here to help you track all the latest items.\n\nType /help to see all available commands."
        with open(filename, 'rb') as video_file:
            await context.bot.send_video(chat_id=chat_id, video=video_file, caption=caption_text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Failed to send welcome video to {chat_id}: {e}")
        await context.bot.send_message(chat_id=chat_id, text="Sorry, I couldn't prepare your welcome video, but you have full access to the bot!")
    finally:
        if processing_msg: await processing_msg.delete()
        if 'filename' in locals() and os.path.exists(filename): os.remove(filename)

def main():
    if not TOKEN or not BOT_OWNER_ID: 
        logger.critical("Main bot TOKEN and BOT_OWNER_ID are not set!"); 
        return
    load_all_data()
    
    Thread(target=app.run, kwargs={'host': '0.0.0.0', 'port': int(os.environ.get('PORT', 8080))}, daemon=True).start()
    
    application = Application.builder().token(TOKEN).build()
    
    # Add approved child bots to the application instance
    for bot_token, bot_data in CHILD_BOTS.items():
        logger.info(f"Registering child bot: {bot_data['name']} (@{bot_data['username']})")
        try:
            application.add_bot(Bot(token=bot_token))
        except Exception as e:
            logger.error(f"Failed to register child bot @{bot_data['username']} with token {bot_token[:8]}...: {e}")

    # Register all handlers. They will apply to the main bot and all child bots.
    user_commands = ["start", "stop", "refresh", "next", "register_bot", "help", "mute", "unmute", "recent", "listprized", "update", "stats", "requestvip"]
    user_handlers = [CommandHandler(cmd.replace('_', ''), globals()[f"{cmd}_cmd"]) for cmd in user_commands]
    application.add_handlers(user_handlers)
    
    admin_commands = ["admin", "approve_bot", "uptime", "approve", "addadmin", "msg", "adminlist", "addprized", "delprized", "restart", "broadcast", "extendvip", "access", "addcommand", "delcommand", "listcommands"]
    admin_handlers = [CommandHandler(cmd.replace('_', ''), globals()[f"{cmd}_cmd"]) for cmd in admin_commands]
    application.add_handlers(admin_handlers)
    
    application.add_handler(CallbackQueryHandler(admin_callback_handler, pattern='^admin_'))
    application.add_handler(MessageHandler(filters.REPLY, reply_handler))
    
    application.job_queue.run_once(check_for_updates, 5)
    logger.info(f"Bot Factory [v{BOT_VERSION}] is running with {len(application.bots)} bot(s)...")
    application.run_polling()

if __name__ == '__main__':
    main()
