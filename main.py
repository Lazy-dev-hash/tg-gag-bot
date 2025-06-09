import logging
import asyncio
import json
import os
import random
from datetime import datetime, timedelta
import pytz
import httpx
from flask import Flask
from threading import Thread

from telegram import Update, Bot
from telegram.constants import ParseMode
from telegram.error import BadRequest
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

# --- CONFIGURATION & AESTHETICS ---
TOKEN = os.environ.get('TELEGRAM_TOKEN')
API_STOCK_URL = "https://gagstock.gleeze.com/grow-a-garden"
API_WEATHER_URL = "https://growagardenstock.com/api/stock/weather"
TRACKING_INTERVAL_SECONDS = 45 # Check a bit more frequently for restocks

# Curated playlist for the "Now Playing" feature
SPOTIFY_PLAYLIST = [
    {'artist': 'December Avenue', 'title': 'Eroplanong papel', 'url': 'https://open.spotify.com/track/7MrDNu3hC1MjczXhi1citM?si=CfrvWgjHQSyhtC45Ay_ofA'},
    {'artist': 'Cup of Joe', 'title': 'Multo', 'url': 'https://open.spotify.com/track/4cBm8rv2B5BJWU2pDaHVbF?si=LRZ0yZGuRsuBgVWICVrAHQ%0A'},
    {'artist': 'YOASOBI', 'title': 'Idol', 'url': 'https://open.spotify.com/track/4ihbctyA9P8T6e2a27l5e7'},
    {'artist': 'Vaundy', 'title': 'Todome no Ichigeki', 'url': 'https://open.spotify.com/track/2S5S6hL0aV24i02cW9r52k'},
    {'artist': 'Tatsuya Kitani', 'title': 'Ao no Sumika', 'url': 'https://open.spotify.com/track/1Lnz19n3RsdX2i9b5T2ylH'},
    {'artist': 'Fujii Kaze', 'title': 'Shinunoga E-Wa', 'url': 'https://open.spotify.com/track/0CRiQkEZA4e9p2n2wnc0Tj'}
]

# --- GLOBAL STATE MANAGEMENT ---
ACTIVE_TRACKERS = {}
LAST_SENT_DATA = {}

# --- LOGGING SETUP ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- HELPER & FORMATTING FUNCTIONS ---
PHT = pytz.timezone('Asia/Manila')

def get_ph_time() -> datetime:
    return datetime.now(PHT)

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

def format_spotify_footer() -> str:
    """Selects a random song and creates a styled footer."""
    song = random.choice(SPOTIFY_PLAYLIST)
    return (
        f"\n\n· · ───────── ·· ───────── · ·\n"
        f"🎧  Now Playing...\n"
        f"🎵  <a href='{song['url']}'><b>{song['title']}</b></a>\n"
        f"🎤  <i>{song['artist']}</i>"
    )

def format_category_message(category_name: str, items: list, restock_timer: str) -> str:
    """Formats a message for a single stock category."""
    header_emojis = {"Gear": "🛠️", "Seeds": "🌱", "Eggs": "🥚", "Cosmetics": "🎨", "Honey": "🍯"}
    header = f"{header_emojis.get(category_name, '📦')} <b>Grow A Garden — {category_name} Stock</b>"
    
    if not items:
        item_list = "<i>No items currently in stock for this category.</i>"
    else:
        item_list = "\n".join([f"• {add_emoji(i['name'])}: {format_value(i['value'])}" for i in items])
        
    return f"{header}\n\n{item_list}\n\n⏳ Restock in: {restock_timer}"

# --- CORE BOT LOGIC ---

async def fetch_all_data() -> dict | None:
    """Fetches all data from APIs and structures it."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            stock_res, weather_res = await asyncio.gather(
                client.get(API_STOCK_URL), client.get(API_WEATHER_URL)
            )
            stock_res.raise_for_status()
            weather_res.raise_for_status()
            stock_data_raw = stock_res.json()['data']
            weather_data = weather_res.json()

        # Structure the data cleanly
        all_data = {"stock": {}, "weather": weather_data}
        for cat, details in stock_data_raw.items():
            if 'items' in details:
                all_data["stock"][cat.capitalize()] = [{'name': item['name'], 'value': int(item['quantity'])} for item in details.get('items', [])]
        return all_data
    except Exception as e:
        logger.error(f"Error fetching all data: {e}")
        return None

async def tracking_loop(chat_id: int, bot: Bot, filters: list[str]):
    logger.info(f"Starting tracking for chat_id: {chat_id} with filters: {filters}")
    try:
        while True:
            await asyncio.sleep(TRACKING_INTERVAL_SECONDS)
            
            new_data = await fetch_all_data()
            if not new_data:
                continue
                
            old_data = LAST_SENT_DATA.get(chat_id)
            if not old_data: # Should not happen after first run, but as a safeguard
                LAST_SENT_DATA[chat_id] = new_data
                continue

            # Compare old stock with new stock for each category
            for category_name, new_items in new_data["stock"].items():
                old_items = old_data["stock"].get(category_name, [])
                
                # A simple way to check if the stock has meaningfully changed
                old_item_names = {item['name'] for item in old_items}
                new_item_names = {item['name'] for item in new_items}

                if old_item_names != new_item_names:
                    # A restock/change happened!
                    # Now check if the user's filters match the new items
                    items_to_show = [item for item in new_items if not filters or any(f in item['name'].lower() for f in filters)]
                    
                    if items_to_show:
                        # Only notify if the restocked category is relevant to the user
                        restock_timers = get_restock_timers()
                        category_message = format_category_message(category_name, items_to_show, restock_timers.get(category_name, "N/A"))
                        
                        header_emojis = {"Gear": "🛠️", "Seeds": "🌱", "Eggs": "🥚", "Cosmetics": "🎨", "Honey": "🍯"}
                        alert_message = f"✅ <b>{header_emojis.get(category_name, '')} {category_name} has been restocked!</b>\n\n{category_message}"
                        
                        try:
                            await bot.send_message(chat_id, text=alert_message + format_spotify_footer(), parse_mode=ParseMode.HTML)
                            logger.info(f"Sent restock alert for '{category_name}' to {chat_id}")
                        except Exception as e:
                            logger.error(f"Failed to send restock alert to {chat_id}: {e}")
                            # Stop tracking for this user if we can't send messages
                            if chat_id in ACTIVE_TRACKERS:
                                ACTIVE_TRACKERS[chat_id]['task'].cancel()
                            return

            # Update the cache with the new data for the next comparison
            LAST_SENT_DATA[chat_id] = new_data

    except asyncio.CancelledError:
        logger.info(f"Tracking loop for {chat_id} cancelled.")
    finally:
        if chat_id in ACTIVE_TRACKERS: del ACTIVE_TRACKERS[chat_id]
        if chat_id in LAST_SENT_DATA: del LAST_SENT_DATA[chat_id]


def get_restock_timers() -> dict:
    # Simplified from the full countdown logic for this specific use case
    now = get_ph_time()
    timers = {}
    
    egg_target = (now + timedelta(minutes=30 - now.minute % 30)).replace(second=0)
    timers["Eggs"] = f"~{int((egg_target - now).total_seconds() / 60)}m"

    gear_seed_target = (now + timedelta(minutes=5 - now.minute % 5)).replace(second=0)
    timers["Gear"] = timers["Seeds"] = f"~{int((gear_seed_target - now).total_seconds() / 60)}m"
    
    honey_target = (now + timedelta(hours=1)).replace(minute=0, second=0)
    timers["Honey"] = f"~{int((honey_target - now).total_seconds() / 60)}m"
    
    cosmetics_target_hour = (now.hour // 7 + 1) * 7
    cosmetics_target = now.replace(hour=cosmetics_target_hour % 24, minute=0, second=0)
    if cosmetics_target <= now: cosmetics_target += timedelta(hours=7)
    timers["Cosmetics"] = f"~{int((cosmetics_target - now).total_seconds() / 3600)}h"

    return timers

# --- TELEGRAM COMMAND HANDLERS ---

async def send_full_stock_report(update: Update, context: ContextTypes.DEFAULT_TYPE, filters: list[str]):
    loader_message = await update.message.reply_text("⏳ Fetching all stock categories...")
    
    data = await fetch_all_data()
    if not data:
        await loader_message.edit_text("⚠️ Could not fetch data. The game's server might be down.")
        return None

    restock_timers = get_restock_timers()
    sent_anything = False
    
    for category_name, items in data["stock"].items():
        items_to_show = [item for item in items if not filters or any(f in item['name'].lower() for f in filters)]
        
        if items_to_show:
            sent_anything = True
            category_message = format_category_message(category_name, items_to_show, restock_timers.get(category_name, "N/A"))
            await context.bot.send_message(update.effective_chat.id, text=category_message + format_spotify_footer(), parse_mode=ParseMode.HTML)
    
    if not sent_anything and filters:
         await context.bot.send_message(update.effective_chat.id, text="Your filter didn't match any items in any category.")

    await loader_message.delete()
    return data


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in ACTIVE_TRACKERS:
        await update.message.reply_text("📡 You are already tracking! Use /stop first, or /refresh for a manual update.")
        return

    filters = [f.strip().lower() for f in " ".join(context.args).split('|') if f.strip()]
    
    initial_data = await send_full_stock_report(update, context, filters)
    
    if initial_data:
        LAST_SENT_DATA[chat_id] = initial_data
        task = asyncio.create_task(tracking_loop(chat_id, context.bot, filters))
        ACTIVE_TRACKERS[chat_id] = {'task': task, 'filters': filters}
        await context.bot.send_message(
            chat_id, 
            text=f"✅ <b>Tracking started!</b>\nYou will now receive alerts when a category restocks.\n(Filters: <code>{', '.join(filters) or 'None'}</code>)",
            parse_mode=ParseMode.HTML
        )

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in ACTIVE_TRACKERS:
        ACTIVE_TRACKERS[chat_id]['task'].cancel()
        await update.message.reply_text("🛑 Gagstock tracking stopped.")
    else:
        await update.message.reply_text("⚠️ You don't have an active tracking session. Use /start to begin.")

async def refresh_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    filters = []
    if update.effective_chat.id in ACTIVE_TRACKERS:
        filters = ACTIVE_TRACKERS[update.effective_chat.id]['filters']
    
    await send_full_stock_report(update, context, filters)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "<b>Welcome to the GAG Stock Alerter!</b>\n\n"
        "This bot has been enhanced with smart notifications and a unique style.\n\n"
        "▶️  <b>/start</b>\n"
        "Shows the current stock for all categories and starts the tracker. The bot will then <b>only message you when a category restocks</b> with new items.\n"
        "› <i>You can add filters:</i> <code>/start Carrot | Watering Can</code>\n\n"
        "🔄  <b>/refresh</b>\n"
        "Manually fetches and shows the current stock for all categories, just like /start, but doesn't change your tracking status.\n\n"
        "⏹️  <b>/stop</b>\n"
        "Stops the tracker and all restock notifications.\n\n"
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

    logger.info("Bot is running with enhanced features...")
    application.run_polling()

if __name__ == '__main__':
    main()
