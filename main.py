import logging
import asyncio
import os
import yt_dlp
from datetime import datetime, timedelta
import pytz
import httpx
from flask import Flask
from threading import Thread

from telegram import Update, Bot
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# --- WEB SERVER FOR 24/7 UPTIME ---
app = Flask('')
@app.route('/')
def home():
    return "Bot is alive and running!"

def run_flask():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_flask)
    t.start()
# --- END OF WEB SERVER CODE ---

# --- CONFIGURATION ---
TOKEN = os.environ.get('TELEGRAM_TOKEN')
API_STOCK_URL = "https://gagstock.gleeze.com/grow-a-garden"
API_WEATHER_URL = "https://growagardenstock.com/api/stock/weather"
TRACKING_INTERVAL_SECONDS = 45
MULTOMUSIC_URL = "https://open.spotify.com/track/4cBm8rv2B5BJWU2pDaHVbF?si=z3vYI8w7QWGltQ9cWa_R_Q" # Cup of Joe - Multo

# --- GLOBAL STATE MANAGEMENT ---
ACTIVE_TRACKERS = {}
LAST_SENT_DATA = {}

# --- LOGGING SETUP ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- HELPER & FORMATTING FUNCTIONS ---
PHT = pytz.timezone('Asia/Manila')

def get_ph_time() -> datetime:
    return datetime.now(PHT)

def get_countdown(target: datetime) -> str:
    """FIXED: Accurate countdown timer."""
    now = get_ph_time()
    time_left = target - now
    if time_left.total_seconds() <= 0:
        return "Restocked!"
    total_seconds = int(time_left.total_seconds())
    h, m, s = total_seconds // 3600, (total_seconds % 3600) // 60, total_seconds % 60
    return f"{h:02}h {m:02}m {s:02}s"

def get_all_restock_timers() -> dict:
    """Uses the accurate countdown to build a dictionary of all timers."""
    now = get_ph_time()
    timers = {}
    
    # Egg: On the hour or half-hour
    next_egg = now.replace(second=0, microsecond=0)
    if now.minute < 30: next_egg = next_egg.replace(minute=30)
    else: next_egg = (next_egg + timedelta(hours=1)).replace(minute=0)
    timers['Eggs'] = get_countdown(next_egg)
    
    # Gear & Seed: Every 5 minutes
    next_5 = now.replace(second=0, microsecond=0)
    next_m = (now.minute // 5 + 1) * 5
    if next_m >= 60: next_5 = (next_5 + timedelta(hours=1)).replace(minute=0)
    else: next_5 = next_5.replace(minute=next_m)
    timers['Gear'] = timers['Seeds'] = get_countdown(next_5)
    
    # Honey: Every hour
    next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    timers['Honey'] = get_countdown(next_hour)

    # Cosmetics: Every 7 hours
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
    emojis = {
        "Common Egg": "🥚", "Uncommon Egg": "🐣", "Rare Egg": "🍳", "Legendary Egg": "🪺", "Mythical Egg": "🥚", "Bug Egg": "🪲",
        "Watering Can": "🚿", "Trowel": "🛠️", "Recall Wrench": "🔧", "Basic Sprinkler": "💧", "Advanced Sprinkler": "💦", "Godly Sprinkler": "⛲",
        "Lightning Rod": "⚡", "Master Sprinkler": "🌊", "Favorite Tool": "❤️", "Harvest Tool": "🌾", "Carrot": "🥕", "Strawberry": "🍓",
        "Blueberry": "🫐", "Orange Tulip": "🌷", "Tomato": "🍅", "Corn": "🌽", "Daffodil": "🌼", "Watermelon": "🍉", "Pumpkin": "🎃",
        "Apple": "🍎", "Bamboo": "🎍", "Coconut": "🥥", "Cactus": "🌵", "Dragon Fruit": "🍈", "Mango": "🥭", "Grape": "🍇",
        "Mushroom": "🍄", "Pepper": "🌶️", "Cacao": "🍫", "Beanstalk": "🌱"
    }
    return f"{emojis.get(name, '❔')} {name}"

def format_category_message(category_name: str, items: list, restock_timer: str) -> str:
    header_emojis = {"Gear": "🛠️", "Seeds": "🌱", "Eggs": "🥚", "Cosmetics": "🎨", "Honey": "🍯"}
    header = f"{header_emojis.get(category_name, '📦')} <b>Grow A Garden — {category_name} Stock</b>"
    item_list = "\n".join([f"• {add_emoji(i['name'])}: {format_value(i['value'])}" for i in items]) if items else "<i>No items currently in stock.</i>"
    return f"{header}\n\n{item_list}\n\n⏳ Restock in: {restock_timer}"

# --- NEW AUDIO SENDING FEATURE ---
async def send_music_vm(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """Downloads and sends the 'Multo' song as an audio file."""
    logger.info(f"Preparing to send music to {chat_id}")
    try:
        # Options for yt-dlp to get the best audio and convert to mp3
        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': f'{chat_id}_%(title)s.%(ext)s', # Unique filename per user
            'postprocessors': [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'}],
            'quiet': True,
        }

        loop = asyncio.get_running_loop()
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Run the blocking download in a separate thread
            info = await loop.run_in_executor(None, lambda: ydl.extract_info(MULTOMUSIC_URL, download=True))
            filename = ydl.prepare_filename(info).replace('.webm', '.mp3').replace('.m4a', '.mp3')
        
        logger.info(f"Downloaded '{filename}', sending now...")
        await context.bot.send_audio(chat_id=chat_id, audio=open(filename, 'rb'), title="Multo", performer="Cup of Joe")
        
        # Clean up the file after sending
        os.remove(filename)
        logger.info(f"Cleaned up file: {filename}")

    except Exception as e:
        logger.error(f"Failed to send music to {chat_id}: {e}")

# --- CORE BOT LOGIC (MODIFIED) ---
async def fetch_all_data() -> dict | None:
    # ... (This function remains the same)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # ... (rest of the fetch logic) ...
            stock_res, weather_res = await asyncio.gather(client.get(API_STOCK_URL), client.get(API_WEATHER_URL))
            stock_res.raise_for_status(); weather_res.raise_for_status()
            stock_data_raw, weather_data = stock_res.json()['data'], weather_res.json()
            all_data = {"stock": {}, "weather": weather_data}
            for cat, details in stock_data_raw.items():
                if 'items' in details:
                    all_data["stock"][cat.capitalize()] = [{'name': item['name'], 'value': int(item['quantity'])} for item in details.get('items', [])]
            return all_data
    except Exception as e:
        logger.error(f"Error fetching all data: {e}")
        return None


async def tracking_loop(chat_id: int, bot: Bot, context: ContextTypes.DEFAULT_TYPE, filters: list[str]):
    # ... (The logic inside remains mostly the same, but now calls send_music_vm)
    logger.info(f"Starting tracking for chat_id: {chat_id}")
    try:
        while True:
            await asyncio.sleep(TRACKING_INTERVAL_SECONDS)
            new_data = await fetch_all_data()
            if not new_data: continue
            old_data = LAST_SENT_DATA.get(chat_id)
            if not old_data:
                LAST_SENT_DATA[chat_id] = new_data
                continue

            for category_name, new_items in new_data["stock"].items():
                old_items_set = {frozenset(item.items()) for item in old_data["stock"].get(category_name, [])}
                new_items_set = {frozenset(item.items()) for item in new_items}

                if old_items_set != new_items_set: # More accurate check for any change
                    items_to_show = [item for item in new_items if not filters or any(f in item['name'].lower() for f in filters)]
                    if items_to_show:
                        restock_timers = get_all_restock_timers()
                        category_message = format_category_message(category_name, items_to_show, restock_timers.get(category_name, "N/A"))
                        header_emojis = {"Gear": "🛠️", "Seeds": "🌱", "Eggs": "🥚", "Cosmetics": "🎨", "Honey": "🍯"}
                        alert_message = f"✅ <b>{header_emojis.get(category_name, '')} {category_name} has been restocked!</b>"
                        try:
                            await bot.send_message(chat_id, text=alert_message, parse_mode=ParseMode.HTML)
                            await bot.send_message(chat_id, text=category_message, parse_mode=ParseMode.HTML)
                            await send_music_vm(context, chat_id)
                            logger.info(f"Sent restock alert for '{category_name}' to {chat_id}")
                        except Exception as e:
                            logger.error(f"Failed to send restock alert to {chat_id}: {e}")
                            if chat_id in ACTIVE_TRACKERS: ACTIVE_TRACKERS[chat_id]['task'].cancel()
                            return
            LAST_SENT_DATA[chat_id] = new_data
    except asyncio.CancelledError:
        logger.info(f"Tracking loop for {chat_id} cancelled.")
    finally:
        if chat_id in ACTIVE_TRACKERS: del ACTIVE_TRACKERS[chat_id]
        if chat_id in LAST_SENT_DATA: del LAST_SENT_DATA[chat_id]

# --- TELEGRAM COMMAND HANDLERS (MODIFIED) ---
async def send_full_stock_report(update: Update, context: ContextTypes.DEFAULT_TYPE, filters: list[str]):
    loader_message = await update.message.reply_text("⏳ Fetching all stock categories...")
    data = await fetch_all_data()
    if not data:
        await loader_message.edit_text("⚠️ Could not fetch data. The game's server might be down.")
        return None

    restock_timers = get_all_restock_timers()
    sent_anything = False
    for category_name, items in data["stock"].items():
        items_to_show = [item for item in items if not filters or any(f in item['name'].lower() for f in filters)]
        if items_to_show:
            sent_anything = True
            category_message = format_category_message(category_name, items_to_show, restock_timers.get(category_name, "N/A"))
            await context.bot.send_message(update.effective_chat.id, text=category_message, parse_mode=ParseMode.HTML)
    
    if not sent_anything and filters:
         await context.bot.send_message(update.effective_chat.id, text="Your filter didn't match any items.")
    
    await loader_message.delete()
    if sent_anything:
        await send_music_vm(context, update.effective_chat.id)
    return data

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in ACTIVE_TRACKERS:
        await update.message.reply_text("📡 You are already tracking! Use /stop first, or /refresh.")
        return

    filters = [f.strip().lower() for f in " ".join(context.args).split('|') if f.strip()]
    initial_data = await send_full_stock_report(update, context, filters)
    
    if initial_data:
        LAST_SENT_DATA[chat_id] = initial_data
        task = asyncio.create_task(tracking_loop(chat_id, context.bot, context, filters))
        ACTIVE_TRACKERS[chat_id] = {'task': task, 'filters': filters}
        await context.bot.send_message(
            chat_id, 
            text=f"✅ <b>Tracking started!</b>\nYou'll get alerts when a category restocks.\n(Filters: <code>{', '.join(filters) or 'None'}</code>)",
            parse_mode=ParseMode.HTML
        )

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in ACTIVE_TRACKERS:
        ACTIVE_TRACKERS[chat_id]['task'].cancel()
        await update.message.reply_text("🛑 Gagstock tracking stopped.")
    else:
        await update.message.reply_text("⚠️ You don't have an active tracking session. Use /start.")

async def refresh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    filters = ACTIVE_TRACKERS.get(update.effective_chat.id, {}).get('filters', [])
    await send_full_stock_report(update, context, filters)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "<b>Welcome to the GAG Stock Alerter!</b>\n\n"
        "▶️  <b>/start</b>\n"
        "Shows current stock and starts the tracker. You'll get an alert & a song when a category restocks.\n"
        "› <i>Filter example:</i> <code>/start Carrot | Watering Can</code>\n\n"
        "🔄  <b>/refresh</b>\n"
        "Manually shows the current stock for all categories.\n\n"
        "⏹️  <b>/stop</b>\n"
        "Stops all restock notifications.\n\n"
        "❓  <b>/help</b>\n"
        "Shows this help message."
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)

def main():
    if not TOKEN:
        logger.critical("TELEGRAM_TOKEN environment variable not found!")
        return

    keep_alive()
    application = Application.builder().token(TOKEN).build()
    
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("stop", stop_cmd))
    application.add_handler(CommandHandler("refresh", refresh_cmd))
    application.add_handler(CommandHandler("help", help_cmd))

    logger.info("Bot is running with audio features and fixed timers...")
    application.run_polling()

if __name__ == '__main__':
    main()
