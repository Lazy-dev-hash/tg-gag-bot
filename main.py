import logging
import asyncio
import json
import os
from datetime import datetime, timedelta
import pytz
import httpx
from flask import Flask
from threading import Thread

from telegram import Update, Bot
from telegram.constants import ParseMode
from telegram.error import BadRequest

# --- WEB SERVER FOR 24/7 UPTIME ---
app = Flask('')
@app.route('/')
def home():
    return "Bot is alive and running!"

def run_flask():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run_flask)
    t.start()
# --- END OF WEB SERVER CODE ---

# --- CONFIGURATION ---
TOKEN = os.environ.get('TELEGRAM_TOKEN') # Use .get for safety
API_STOCK_URL = "https://gagstock.gleeze.com/grow-a-garden"
API_WEATHER_URL = "https://growagardenstock.com/api/stock/weather"
TRACKING_INTERVAL_SECONDS = 60

# --- GLOBAL STATE MANAGEMENT ---
# ACTIVE_TRACKERS will now store a dictionary for each user
# {'task': asyncio.Task, 'filters': list[str]}
ACTIVE_TRACKERS = {}
LAST_SENT_DATA = {}

# --- LOGGING SETUP ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- HELPER FUNCTIONS ---
PHT = pytz.timezone('Asia/Manila')

def get_ph_time() -> datetime:
    return datetime.now(PHT)

# ... (get_countdown, format_value, and add_emoji functions remain the same as before) ...
def get_countdown(target: datetime) -> str:
    now = get_ph_time()
    ms_left = target - now
    if ms_left.total_seconds() <= 0:
        return "Restocked!"
    total_seconds = int(ms_left.total_seconds())
    h, m, s = total_seconds // 3600, (total_seconds % 3600) // 60, total_seconds % 60
    return f"{h:02}h {m:02}m {s:02}s"

def get_next_restocks() -> dict:
    now = get_ph_time()
    timers = {}
    next_egg = now.replace(second=0, microsecond=0)
    if now.minute < 30: next_egg = next_egg.replace(minute=30)
    else: next_egg = (next_egg + timedelta(hours=1)).replace(minute=0)
    timers['Egg'] = get_countdown(next_egg)
    next_5 = now.replace(second=0, microsecond=0)
    next_m = (now.minute // 5 + 1) * 5
    if next_m >= 60: next_5 = (next_5 + timedelta(hours=1)).replace(minute=0)
    else: next_5 = next_5.replace(minute=next_m)
    timers['Gear & Seed'] = get_countdown(next_5)
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
    emojis = {
        "Common Egg": "🥚", "Uncommon Egg": "🐣", "Rare Egg": "🍳", "Legendary Egg": "🪺", "Mythical Egg": "🥚", "Bug Egg": "🪲",
        "Watering Can": "🚿", "Trowel": "🛠️", "Recall Wrench": "🔧", "Basic Sprinkler": "💧", "Advanced Sprinkler": "💦", "Godly Sprinkler": "⛲",
        "Lightning Rod": "⚡", "Master Sprinkler": "🌊", "Favorite Tool": "❤️", "Harvest Tool": "🌾", "Carrot": "🥕", "Strawberry": "🍓",
        "Blueberry": "🫐", "Orange Tulip": "🌷", "Tomato": "🍅", "Corn": "🌽", "Daffodil": "🌼", "Watermelon": "🍉", "Pumpkin": "🎃",
        "Apple": "🍎", "Bamboo": "🎍", "Coconut": "🥥", "Cactus": "🌵", "Dragon Fruit": "🍈", "Mango": "🥭", "Grape": "🍇",
        "Mushroom": "🍄", "Pepper": "🌶️", "Cacao": "🍫", "Beanstalk": "🌱"
    }
    return f"{emojis.get(name, '❔')} {name}"

# --- CORE BOT LOGIC ---

async def fetch_and_format_message(filters: list[str]) -> tuple[str | None, str | None]:
    """Fetches, combines, and formats data into a single message."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            stock_res, weather_res = await asyncio.gather(
                client.get(API_STOCK_URL), client.get(API_WEATHER_URL)
            )
            stock_res.raise_for_status()
            weather_res.raise_for_status()
            stock_data_raw = stock_res.json()['data']
            weather_data = weather_res.json()

        # Combine all items into a single list
        all_items = []
        for category in ['gear', 'seed', 'egg', 'cosmetics', 'honey']:
            all_items.extend(
                [{'name': item['name'], 'value': int(item['quantity'])}
                 for item in stock_data_raw.get(category, {}).get('items', [])]
            )
        
        # Create a unique key for the current stock state
        current_data_key = json.dumps(all_items, sort_keys=True)

        # Apply user filters if any
        items_to_show = [
            item for item in all_items
            if not filters or any(f in item['name'].lower() for f in filters)
        ]

        # Format the single list of items
        if items_to_show:
            item_list = "\n".join(
                f"• {add_emoji(i['name'])}: {format_value(i['value'])}" for i in items_to_show
            )
        else:
            item_list = "Your filter returned no items." if filters else "No items currently in stock."

        # Format restock timers
        restocks = get_next_restocks()
        restock_timers = "\n".join([f"› {cat}: {time}" for cat, time in restocks.items()])

        # Format weather details
        weather = weather_data.get('currentWeather', 'Unknown')
        icon = weather_data.get('icon', '🌤️')
        bonus = weather_data.get('cropBonuses', 'None')
        
        message = (
            f"🌾 <b>Grow A Garden — Stock</b>\n\n"
            f"📦 <b><u>Available Items</u></b>\n{item_list}\n\n"
            f"⏳ <b><u>Restock Timers</u></b>\n{restock_timers}\n\n"
            f"🌤️ <b><u>Weather</u></b>\n"
            f"› {icon} {weather} (Bonus: {bonus})"
        )
        return current_data_key, message

    except Exception as e:
        logger.error(f"Error fetching/formatting: {e}")
        return None, "⚠️ Could not fetch data. The game's server might be down or the API changed."

async def tracking_loop(chat_id: int, bot: Bot, filters: list[str]):
    """Background loop that checks for updates and sends them."""
    logger.info(f"Starting tracking for chat_id: {chat_id} with filters: {filters}")
    try:
        while True:
            data_key, message = await fetch_and_format_message(filters)
            if data_key and data_key != LAST_SENT_DATA.get(chat_id):
                try:
                    await bot.send_message(chat_id, text=message, parse_mode=ParseMode.HTML)
                    LAST_SENT_DATA[chat_id] = data_key
                    logger.info(f"Sent update to {chat_id}")
                except Exception as e:
                    logger.error(f"Failed to send to {chat_id}: {e}")
                    break
            await asyncio.sleep(TRACKING_INTERVAL_SECONDS)
    except asyncio.CancelledError:
        logger.info(f"Tracking loop for {chat_id} cancelled.")
    finally:
        if chat_id in ACTIVE_TRACKERS: del ACTIVE_TRACKERS[chat_id]
        if chat_id in LAST_SENT_DATA: del LAST_SENT_DATA[chat_id]

# --- TELEGRAM COMMAND HANDLERS ---

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in ACTIVE_TRACKERS:
        await update.message.reply_text("📡 You are already tracking! Use /stop first, or /refresh for a manual update.")
        return

    # --- Loader Message ---
    loader_message = await update.message.reply_text("⏳ Fetching latest stock data...")

    filters = [f.strip().lower() for f in " ".join(context.args).split('|') if f.strip()]
    
    data_key, message = await fetch_and_format_message(filters)
    
    # --- Edit Loader with Final Message ---
    try:
        await loader_message.edit_text(message, parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Error editing message: {e}")
            await update.message.reply_text(message, parse_mode=ParseMode.HTML) # Fallback

    if data_key:
        LAST_SENT_DATA[chat_id] = data_key
        task = asyncio.create_task(tracking_loop(chat_id, context.bot, filters))
        ACTIVE_TRACKERS[chat_id] = {'task': task, 'filters': filters}
        await context.bot.send_message(
            chat_id, 
            text=f"✅ Tracking started! You'll get an alert when stock changes. (Filters: {', '.join(filters) or 'None'})"
        )

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in ACTIVE_TRACKERS:
        ACTIVE_TRACKERS[chat_id]['task'].cancel()
        # The loop's finally block will clean up the dicts
        await update.message.reply_text("🛑 Gagstock tracking stopped.")
    else:
        await update.message.reply_text("⚠️ You don't have an active tracking session. Use /start to begin.")

async def refresh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in ACTIVE_TRACKERS:
        await update.message.reply_text("⚠️ You aren't tracking anything. Use /start first.")
        return
    
    # --- Loader Message ---
    loader_message = await update.message.reply_text("⏳ Refreshing stock data...")

    # Use the filters from the active session
    filters = ACTIVE_TRACKERS[chat_id]['filters']
    data_key, message = await fetch_and_format_message(filters)

    # --- Edit Loader with Final Message ---
    try:
        await loader_message.edit_text(message, parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Error editing message: {e}")
            await update.message.reply_text(message, parse_mode=ParseMode.HTML) # Fallback

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "<b>Welcome to the Grow A Garden Tracker!</b>\n\n"
        "Here are the commands you can use:\n\n"
        "▶️  <b>/start</b>\n"
        "Starts the tracker. The bot will notify you automatically when the stock changes. You can also add filters.\n"
        "› <i>Example:</i> <code>/start</code>\n"
        "› <i>Example with filters:</i> <code>/start Carrot | Watering Can</code>\n\n"
        "🔄  <b>/refresh</b>\n"
        "Manually fetches and shows the current stock right now. This is useful if you just want a quick look without restarting the tracker.\n\n"
        "⏹️  <b>/stop</b>\n"
        "Stops the tracker and all notifications.\n\n"
        "❓  <b>/help</b>\n"
        "Shows this help message."
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)


def main():
    if not TOKEN:
        logger.critical("TELEGRAM_TOKEN environment variable not found! The bot cannot start.")
        return

    keep_alive()
    application = Application.builder().token(TOKEN).build()
    
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("stop", stop_cmd))
    application.add_handler(CommandHandler("refresh", refresh_cmd))
    application.add_handler(CommandHandler("help", help_cmd))

    logger.info("Bot is running...")
    application.run_polling()

if __name__ == '__main__':
    main()
