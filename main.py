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
BOT_VERSION = os.environ.get('BOT_VERSION', '14.0.0') # Auto-Activation & Final Features
ADMIN_PANEL_TITLE = os.environ.get('ADMIN_PANEL_TITLE', 'Bot Control Panel')
BOT_CREATOR_NAME = os.environ.get('BOT_CREATOR_NAME', 'Sunnel')
RENDER_DEPLOY_HOOK_URL = os.environ.get('RENDER_DEPLOY_HOOK_URL') # New: For the /deploy command

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
CHILD_BOTS, BOT_REGISTRATION_REQUESTS, SENT_MESSAGES = {}, {}, {}
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
        if user_info.get('avatar_path'): avatar_url = f"https://api.telegram.org/file/bot{bot.token}/{user_info['avatar_path']}"
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
DASHBOARD_HTML = """...""" # Unchanged, omitted for brevity
LOGIN_HTML = """...""" # Unchanged, omitted for brevity

# --- FLASK WEB ROUTES ---
@app.route('/')
def home_route(): return "Bot is alive. Admin dashboard is at /login."
# ... (Other flask routes are unchanged and omitted for brevity)

# --- TELEGRAM COMMAND HANDLERS ---
async def send_full_stock_report(update: Update, context: ContextTypes.DEFAULT_TYPE, filters: list[str]):
    loader_message = await update.message.reply_text("🛰️ Connecting to GAG Network... Please wait.")
    
    # Auto-clear previous stock messages
    if update.effective_chat.id in SENT_MESSAGES:
        for msg_id in SENT_MESSAGES[update.effective_chat.id]:
            try:
                await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
            except Exception:
                pass # Ignore if message not found
        SENT_MESSAGES[update.effective_chat.id].clear()
    else:
        SENT_MESSAGES[update.effective_chat.id] = []

    data = await fetch_all_data()
    if not data: await loader_message.edit_text("⚠️ Could not fetch data."); return None
    
    await loader_message.edit_text("🌦️ Fetching weather report...")
    weather_report = format_weather_message(data.get("weather", {}))
    weather_msg = await context.bot.send_message(update.effective_chat.id, text=weather_report, parse_mode=ParseMode.HTML)
    SENT_MESSAGES[update.effective_chat.id].append(weather_msg.message_id)
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
            stock_msg = await context.bot.send_message(update.effective_chat.id, text=category_message, parse_mode=ParseMode.HTML)
            SENT_MESSAGES[update.effective_chat.id].append(stock_msg.message_id)

    if not sent_anything and filters: await context.bot.send_message(update.effective_chat.id, text="Your filter didn't match any items.")
    await loader_message.delete()
    if sent_anything: await send_music_vm(context, update.effective_chat.id)
    return data
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # Special welcome for newly created child bots
    if context.bot.token in CHILD_BOTS and user.id == CHILD_BOTS[context.bot.token]["owner_id"]:
        if not context.user_data.get('has_received_welcome'):
            bot_name = CHILD_BOTS[context.bot.token]["name"]
            welcome_msg = (
                f"🎉 <b>Welcome to {bot_name}, your personal GAG Assistant!</b> 🎉\n\n"
                "Congratulations on your new bot! Here are a few things to keep in mind:\n\n"
                "📜 <b>Rules & Guidelines:</b>\n"
                "1. All commands from the main bot are available here.\n"
                "2. Your VIP status from the main bot is required for VIP features.\n"
                "3. Please do not share this bot's token or link publicly.\n\n"
                "Use /help to see all available commands.\n\n"
                f"<i>This bot was created by <b>{BOT_CREATOR_NAME}</b>.</i>"
            )
            await update.message.reply_html(welcome_msg)
            context.user_data['has_received_welcome'] = True
            return

    await log_user_activity(user, "/start", context.bot)
    if user.id in BANNED_USERS: await update.message.reply_text("❌ You have been banned from using this bot."); return
    if user.id not in AUTHORIZED_USERS:
        code = "GAG-" + ''.join(random.choices(string.ascii_uppercase + string.digits, k=3)) + '-' + ''.join(random.choices(string.ascii_uppercase + string.digits, k=3))
        user_msg = f"👋 <b>Welcome! This is a private bot.</b>\n\nTo get access, send this code to the admin for approval:\n\n🔑 Approval Code: <code>{code}</code>"
        admin_msg = f"👤 <b>New User Request</b>\n\n<b>Name:</b> {user.first_name}\n<b>User ID:</b> <code>{user.id}</code>\n\nTo approve, use: <code>/approve {user.id}</code>"
        await update.message.reply_html(user_msg)
        for admin_id in ADMIN_USERS:
            try: await context.application.bot.send_message(chat_id=admin_id, text=admin_msg, parse_mode=ParseMode.HTML)
            except Exception as e: logger.error(f"Failed to send approval notice to admin {admin_id}: {e}")
        return
    if user.id in RESTRICTED_USERS: await update.message.reply_text("⚠️ Your account is restricted. You can refresh stock but cannot start a new tracker. Please contact an admin."); return
    is_vip = str(user.id) in VIP_USERS and datetime.fromisoformat(VIP_USERS.get(str(user.id), '1970-01-01T00:00:00+00:00')) > datetime.now(pytz.utc)
    if is_vip:
        chat_id = user.id
        if chat_id in ACTIVE_TRACKERS:
            tracker_version = ACTIVE_TRACKERS[chat_id].get('version', '0.0.0')
            if tracker_version != BOT_VERSION:
                keyboard = [[InlineKeyboardButton("Update My Session", callback_data='self_update_session')]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_html("🚀 <b>A new version is available!</b>\n\nClick the button below to update your session to the latest version.", reply_markup=reply_markup)
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
# ... (rest of the command handlers are unchanged and omitted for brevity) ...

# --- NEW & UPDATED ADMIN COMMANDS ---
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
    
    # Save the new bot
    CHILD_BOTS[bot_token] = {"name": bot_name, "owner_id": user_id, "username": bot_username, "approved_by": admin.id, "created_at": datetime.now(pytz.utc).isoformat()}
    save_json_to_file("child_bots.json", CHILD_BOTS)
    
    # Remove the pending request
    del BOT_REGISTRATION_REQUESTS[request_code]
    save_json_to_file("bot_registrations.json", BOT_REGISTRATION_REQUESTS)
    
    # Start the new bot in the background automatically
    logger.info(f"Admin {admin.id} approved bot @{bot_username}. Starting it automatically...")
    new_bot_app = Application.builder().token(bot_token).build()
    register_handlers(new_bot_app)
    asyncio.create_task(run_bot(new_bot_app))
    
    await update.message.reply_html(f"✅ <b>Success!</b>\n\nYou have approved @{bot_username}. It is now active and running automatically.")
    
    success_message = f"🎉 <b>Bot Approved & Activated!</b> 🎉\n\nCongratulations! Your bot '<b>{bot_name}</b>' has been approved and is now online.\n\n➡️ <b>Your bot's link:</b> https://t.me/{bot_username}"
    try:
        await context.bot.send_message(chat_id=user_id, text=success_message, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.error(f"Failed to send bot approval success message to {user_id}: {e}")
        await update.message.reply_html(f"⚠️ Could not notify the user. Please message them manually.")
async def deploy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = update.effective_user
    if admin.id not in ADMIN_USERS: return
    await log_user_activity(admin, "/deploy", context.bot)

    if not RENDER_DEPLOY_HOOK_URL:
        await update.message.reply_html("⚠️ <b>Deploy Hook Not Configured</b>\n\nThe `RENDER_DEPLOY_HOOK_URL` environment variable is not set. Cannot trigger deployment.")
        return

    msg = await update.message.reply_text("🚀 Sending deployment signal to Render...")
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(RENDER_DEPLOY_HOOK_URL)
            response.raise_for_status()
        await msg.edit_text("✅ <b>Success!</b>\n\nDeployment triggered on Render. The bot will restart with the latest code shortly.")
        logger.info(f"Admin {admin.id} triggered a new deployment.")
    except httpx.HTTPStatusError as e:
        await msg.edit_text(f"❌ <b>Deployment Failed</b>\n\nRender responded with status {e.response.status_code}. Check your Render dashboard and deploy hook URL.")
    except Exception as e:
        await msg.edit_text(f"❌ <b>An Error Occurred</b>\n\nCould not trigger deployment. Error: {e}")

# --- UPDATED CALLBACK & MAIN FUNCTIONS ---
async def self_update_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    
    tracker_info = ACTIVE_TRACKERS.get(user.id)
    if not tracker_info:
        await query.message.edit_text("Your session has already ended. Please use /start to begin a new one.")
        return

    await query.message.edit_text("⚙️ Updating your session... Please wait.")
    tracker_info['task'].cancel() # Stop the old tracker
    
    # Simulate the user running /start again to get the new version
    mock_chat = type('MockChat', (), {'id': user.id, 'type': 'private'})()
    mock_message = type('MockMessage', (), {'from_user': user, 'chat': mock_chat, 'reply_text': query.message.reply_text})
    mock_update = type('MockUpdate', (), {'effective_user': user, 'message': mock_message, 'effective_chat': mock_chat})
    
    await start_cmd(mock_update, ContextTypes.DEFAULT_TYPE(application=context.application, chat_id=user.id, user_id=user.id))
    await query.message.delete()

def register_handlers(app: Application):
    """This function registers all handlers to a given application instance."""
    # ... (omitted for brevity, but it's the same as the previous full code block) ...

async def run_bot(app: Application):
    """Starts a single bot instance."""
    try:
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        logger.info(f"Bot @{app.bot.username} is running.")
    except Exception as e:
        logger.critical(f"Failed to start bot @{app.bot.username}: {e}")

async def main_async():
    """The main entry point for the bot factory."""
    if not TOKEN or not BOT_OWNER_ID: 
        logger.critical("Main bot TOKEN and BOT_OWNER_ID are not set!"); 
        return
    load_all_data()
    
    Thread(target=app.run, kwargs={'host': '0.0.0.0', 'port': int(os.environ.get('PORT', 8080))}, daemon=True).start()

    # Create a list of all tokens (main bot + child bots)
    all_tokens = [TOKEN] + list(CHILD_BOTS.keys())
    unique_tokens = sorted(list(set(all_tokens)))
    
    bot_tasks = []
    for token in unique_tokens:
        try:
            bot_instance = Bot(token)
            await bot_instance.get_me() # Test token validity
            application = Application.builder().token(token).build()
            register_handlers(application) # Register all handlers for this bot
            bot_tasks.append(run_bot(application))
        except Exception as e:
            logger.error(f"Failed to prepare bot with token ending in ...{token[-4:]}. It may be invalid or revoked. Error: {e}")

    if not bot_tasks:
        logger.critical("No valid bots could be started. Exiting.")
        return

    logger.info(f"Bot Factory [v{BOT_VERSION}] is starting {len(bot_tasks)} bot(s)...")
    await asyncio.gather(*bot_tasks)


if __name__ == '__main__':
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        logger.info("Bot shutting down...")
