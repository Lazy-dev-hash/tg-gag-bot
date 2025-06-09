# --- This code is ready for Replit ---
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
from telegram.ext import Application, CommandHandler, ContextTypes

# --- WEB SERVER FOR 24/7 UPTIME ---
# This part keeps the bot alive on Replit
app = Flask('')

@app.route('/')
def home():
    return "I'm alive!"

def run_flask():
  app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run_flask)
    t.start()
# --- END OF WEB SERVER CODE ---


# --- CONFIGURATION ---
# The token is now loaded securely from Replit's "Secrets"
# This is the CORRECT way
TOKEN = os.environ['TELEGRAM_TOKEN']
API_STOCK_URL = "https://gagstock.gleeze.com/grow-a-garden"
API_WEATHER_URL = "https://growagardenstock.com/api/stock/weather"
TRACKING_INTERVAL_SECONDS = 60

# --- GLOBAL STATE MANAGEMENT ---
ACTIVE_TRACKERS = {}
LAST_SENT_DATA = {}

# --- LOGGING SETUP ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- HELPER FUNCTIONS (Ported from JavaScript) ---
PHT = pytz.timezone('Asia/Manila')

def get_ph_time() -> datetime:
    return datetime.now(PHT)

def get_countdown(target: datetime) -> str:
    now = get_ph_time()
    ms_left = target - now
    if ms_left.total_seconds() <= 0:
        return "00h 00m 00s"
    total_seconds = int(ms_left.total_seconds())
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    s = total_seconds % 60
    return f"{h:02}h {m:02}m {s:02}s"

def get_next_restocks() -> dict:
    now = get_ph_time()
    timers = {}
    next_egg = now.replace(second=0, microsecond=0)
    if now.minute < 30:
        next_egg = next_egg.replace(minute=30)
    else:
        next_egg = (next_egg + timedelta(hours=1)).replace(minute=0)
    timers['egg'] = get_countdown(next_egg)
    next_5 = now.replace(second=0, microsecond=0)
    next_m = (now.minute // 5 + 1) * 5
    if next_m >= 60:
        next_5 = (next_5 + timedelta(hours=1)).replace(minute=0)
    else:
        next_5 = next_5.replace(minute=next_m)
    timers['gear'] = timers['seed'] = get_countdown(next_5)
    next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    timers['honey'] = get_countdown(next_hour)
    next_7 = now.replace(minute=0, second=0, microsecond=0)
    next_7h = (now.hour // 7 + 1) * 7
    if next_7h >= 24:
        next_7 = (next_7 + timedelta(days=1)).replace(hour=next_7h % 24)
    else:
        next_7 = next_7.replace(hour=next_7h)
    timers['cosmetics'] = get_countdown(next_7)
    return timers

def format_value(val: int) -> str:
    if val >= 1_000_000:
        return f"x{val / 1_000_000:.1f}M"
    if val >= 1_000:
        return f"x{val / 1_000:.1f}K"
    return f"x{val}"

def add_emoji(name: str) -> str:
    emojis = {
        "Common Egg": "🥚", "Uncommon Egg": "🐣", "Rare Egg": "🍳", "Legendary Egg": "🪺", "Mythical Egg": "🥚", "Bug Egg": "🪲",
        "Watering Can": "🚿", "Trowel": "🛠️", "Recall Wrench": "🔧", "Basic Sprinkler": "💧",
        "Advanced Sprinkler": "💦", "Godly Sprinkler": "⛲", "Lightning Rod": "⚡", "Master Sprinkler": "🌊",
        "Favorite Tool": "❤️", "Harvest Tool": "🌾", "Carrot": "🥕", "Strawberry": "🍓", "Blueberry": "🫐",
        "Orange Tulip": "🌷", "Tomato": "🍅", "Corn": "🌽", "Daffodil": "🌼", "Watermelon": "🍉", "Pumpkin": "🎃",
        "Apple": "🍎", "Bamboo": "🎍", "Coconut": "🥥", "Cactus": "🌵", "Dragon Fruit": "🍈", "Mango": "🥭",
        "Grape": "🍇", "Mushroom": "🍄", "Pepper": "🌶️", "Cacao": "🍫", "Beanstalk": "🌱"
    }
    return f"{emojis.get(name, '❔')} {name}"

# --- CORE BOT LOGIC (same as before) ---
async def fetch_and_format_message(filters: list[str]) -> tuple[str | None, str | None]:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            stock_res, weather_res = await asyncio.gather(
                client.get(API_STOCK_URL), client.get(API_WEATHER_URL)
            )
            stock_res.raise_for_status()
            weather_res.raise_for_status()
            stock_data_raw = stock_res.json()['data']
            weather_data = weather_res.json()
        stock_data = {
            category: [{'name': item['name'], 'value': int(item['quantity'])} for item in details['items']]
            for category, details in stock_data_raw.items() if 'items' in details
        }
        current_data_key = json.dumps(stock_data, sort_keys=True)
        restocks = get_next_restocks()
        weather = weather_data.get('currentWeather', 'Unknown')
        icon = weather_data.get('icon', '🌤️')
        bonus = weather_data.get('cropBonuses', 'None')
        updated_at = get_ph_time().strftime('%b %d, %Y, %I:%M:%S %p')
        weather_details = (
            f"<b>🌤️ Weather:</b> {icon} {weather}\n"
            f"<b>🌾 Crop Bonus:</b> {bonus}\n"
            f"<i>Last updated (PHT): {updated_at}</i>"
        )
        categories = [
            {"label": "🛠️ <b>Gear</b>", "items": stock_data.get('gear', []), "restock": restocks['gear']},
            {"label": "🌱 <b>Seeds</b>", "items": stock_data.get('seed', []), "restock": restocks['seed']},
            {"label": "🥚 <b>Eggs</b>", "items": stock_data.get('egg', []), "restock": restocks['egg']},
            {"label": "🎨 <b>Cosmetics</b>", "items": stock_data.get('cosmetics', []), "restock": restocks['cosmetics']},
            {"label": "🍯 <b>Honey</b>", "items": stock_data.get('honey', []), "restock": restocks['honey']},
        ]
        filtered_content = ""
        for cat in categories:
            items_to_show = [
                item for item in cat['items'] 
                if not filters or any(f in item['name'].lower() for f in filters)
            ]
            if items_to_show:
                item_list = "\n".join([f"• {add_emoji(i['name'])}: {format_value(i['value'])}" for i in items_to_show])
                filtered_content += f"{cat['label']}\n{item_list}\n⏳ Restock in: {cat['restock']}\n\n"
        if not filtered_content.strip():
            return current_data_key, "Your filter didn't match any currently stocked items. Try a broader filter or use `/start` with no filter to see everything."
        message = f"🌾 <b>Grow A Garden — Tracker</b>\n\n{filtered_content}{weather_details}"
        return current_data_key, message
    except Exception as e:
        logger.error(f"Error fetching/formatting: {e}")
        return None, "⚠️ Could not fetch data. The server might be down or the API changed."

async def tracking_loop(chat_id: int, bot: Bot, filters: list[str]):
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

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in ACTIVE_TRACKERS:
        await update.message.reply_text("📡 You are already tracking! Use /stop first.")
        return
    filters = [f.strip().lower() for f in " ".join(context.args).split('|') if f.strip()]
    await update.message.reply_text(f"✅ Tracking started! Filters: {', '.join(filters) if filters else 'All Items'}.")
    task = asyncio.create_task(tracking_loop(chat_id, context.bot, filters))
    ACTIVE_TRACKERS[chat_id] = task
    data_key, message = await fetch_and_format_message(filters)
    if message:
        await context.bot.send_message(chat_id, text=message, parse_mode=ParseMode.HTML)
        if data_key: LAST_SENT_DATA[chat_id] = data_key

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in ACTIVE_TRACKERS:
        ACTIVE_TRACKERS[chat_id].cancel()
        await update.message.reply_text("🛑 Gagstock tracking stopped.")
    else:
        await update.message.reply_text("⚠️ No active tracking session. Use /start.")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "<b>Welcome to the Grow A Garden Tracker Bot!</b>\n\n"
        "• <b>/start</b> - Track all items.\n"
        "• <b>/start <item1> | <item2></b> - Track specific items. E.g., <code>/start Watering Can | Carrot</code>\n"
        "• <b>/stop</b> - Stop tracking.\n"
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)

def main():
    # This function starts the bot
    keep_alive() # Start the web server to keep the bot alive
    application = Application.builder().token(TOKEN).build()
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("stop", stop_cmd))
    application.add_handler(CommandHandler("help", help_cmd))
    print("Bot is running...")
    application.run_polling()

if __name__ == '__main__':
    main()
