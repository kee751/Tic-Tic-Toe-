import asyncio
import os
import logging
import secrets
import sqlite3
import time
from threading import Thread
from typing import Set, Dict, Any, Optional, List
from flask import Flask
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    InlineQueryResultArticle, 
    InputTextMessageContent
)

# --- Logging Configurations ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("bot_metrics.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

TOKEN: Optional[str] = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN missing in Environment Variables.")

# Admin IDs ကို Env ကဖတ်မည်
ADMIN_IDS: List[int] = [int(x) for x in os.getenv("ADMIN_IDS", "7679480147").split(",")]

bot: Bot = Bot(token=TOKEN)
dp: Dispatcher = Dispatcher()

# --- Async Architecture Locks ---
db_lock: asyncio.Lock = asyncio.Lock()
game_lock: asyncio.Lock = asyncio.Lock()

games: Dict[str, Any] = {}
user_cooldowns: Dict[int, float] = {}

# --- SQLite Database Layer ---
DB_FILE = "database.db"

def init_db() -> None:
    """Database နှင့် Table များကို စတင်တည်ဆောက်ခြင်း။"""
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                wins INTEGER DEFAULT 0,
                losses INTEGER DEFAULT 0,
                draws INTEGER DEFAULT 0,
                games_played INTEGER DEFAULT 0,
                win_streak INTEGER DEFAULT 0,
                coins INTEGER DEFAULT 100,
                last_seen TEXT
            )
        """)
        conn.commit()

init_db()

# --- DB Helper Functions ---
async def db_register_user(user_id: int, username: Optional[str], first_name: str) -> None:
    def _run():
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO users (user_id, username, first_name, last_seen)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_seen = datetime('now')
            """, (str(user_id), username, first_name))
            conn.commit()
    async with db_lock:
        await asyncio.to_thread(_run)

async def db_update_stats(user_id: int, result: str) -> None:
    def _run():
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            if result == "win":
                cursor.execute("""
                    UPDATE users SET wins = wins + 1, games_played = games_played + 1, 
                    win_streak = win_streak + 1, coins = coins + 20 WHERE user_id = ?
                """, (str(user_id),))
            elif result == "loss":
                cursor.execute("""
                    UPDATE users SET losses = losses + 1, games_played = games_played + 1, 
                    win_streak = 0 WHERE user_id = ?
                """, (str(user_id),))
            elif result == "draw":
                cursor.execute("""
                    UPDATE users SET draws = draws + 1, games_played = games_played + 1, 
                    coins = coins + 5 WHERE user_id = ?
                """, (str(user_id),))
            conn.commit()
    async with db_lock:
        await asyncio.to_thread(_run)

async def db_get_profile(user_id: int) -> Optional[Dict[str, Any]]:
    def _run():
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE user_id = ?", (str(user_id),))
            row = cursor.fetchone()
            return dict(row) if row else None
    async with db_lock:
        return await asyncio.to_thread(_run)

async def db_get_top() -> List[Dict[str, Any]]:
    def _run():
        with sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT first_name, wins FROM users ORDER BY wins DESC LIMIT 10")
            return [dict(row) for row in cursor.fetchall()]
    async with db_lock:
        return await asyncio.to_thread(_run)

async def db_get_global_stats() -> Dict[str, int]:
    def _run():
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM users")
            total_users = cursor.fetchone()[0]
            return {"total_users": total_users}
    async with db_lock:
        return await asyncio.to_thread(_run)

# --- Utilities & Security Escaping ---
def escape_md(text: str) -> str:
    for char in ['_', '*', '`', '[']:
        text = text.replace(char, f"\\{char}")
    return text

def flood_protection(user_id: int) -> bool:
    current_time = time.time()
    last_time = user_cooldowns.get(user_id, 0)
    if current_time - last_time < 0.5:
        return False
    user_cooldowns[user_id] = current_time
    return True

# --- Flask Server Architecture ---
app: Flask = Flask(__name__)
PORT: int = int(os.getenv("PORT", "10000"))

@app.route('/')
def home() -> str:
    return "Bot Core Analytics Endpoint Active."

def run_flask() -> None:
    app.run(host='0.0.0.0', port=PORT)

# --- Safe Edit System ---
async def safe_edit(callback: types.CallbackQuery, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None) -> None:
    try:
        if callback.inline_message_id:
            await callback.bot.edit_message_text(
                text=text, inline_message_id=callback.inline_message_id,
                reply_markup=reply_markup, parse_mode="Markdown"
            )
        else:
            await callback.message.edit_text(text=text, reply_markup=reply_markup, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Safe Edit Matrix Exception: {e}")

# --- Core Game Logic Mechanics ---
def get_turn_text(game: Dict[str, Any]) -> str:
    current_piece = game["turn"]
    c_theme = game["theme"][current_piece]
    
    p1 = game["creator"]
    p2 = game["opponent"]
    
    current_player_name = p1["name"] if p1["piece"] == current_piece else p2["name"]
    
    return (
        f"🎮 **Tic-Tac-Toe 4x4 ပွဲစဉ်**\n\n"
        f"🔴 {escape_md(p1['name'])} ({game['theme'][p1['piece']]})\n"
        f"🔵 {escape_md(p2['name'])} ({game['theme'][p2['piece']]})\n\n"
        f"⏳ **ယခုအလှည့်:** {escape_md(current_player_name)} ({c_theme}) ရဲ့အလှည့်"
    )

def create_board_keyboard(board: list, game_id: str, theme: Dict[str, str]) -> InlineKeyboardMarkup:
    keyboard = []
    for r in range(4):
        row = []
        for c in range(4):
            symbol = board[r][c]
            display_text = theme[symbol] if symbol != ' ' else " "
            row.append(InlineKeyboardButton(text=display_text, callback_data=f"move_{game_id}_{r}_{c}"))
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton(text="🚪 Leave Game (အရှုံးပေးရန်)", callback_data=f"leave_{game_id}")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def check_winner(board: list, player: str) -> bool:
    for i in range(4):
        if all(board[i][j] == player for j in range(4)) or \
           all(board[j][i] == player for j in range(4)):
            return True
    if all(board[i][i] == player for i in range(4)) or \
       all(board[i][3-i] == player for i in range(4)):
        return True
    return False

# --- Smart 4x4 Defensive Bot AI Logic ---
def get_ai_move(board: list) -> tuple:
    for r in range(4):
        for c in range(4):
            if board[r][c] == ' ':
                board[r][c] = 'O'
                if check_winner(board, 'O'):
                    return r, c
                board[r][c] = ' '
                
    for r in range(4):
        for c in range(4):
            if board[r][c] == ' ':
                board[r][c] = 'X'
                if check_winner(board, 'X'):
                    board[r][c] = ' '
                    return r, c
                board[r][c] = ' '
                
    center_spots = [(1,1), (1,2), (2,1), (2,2)]
    for r, c in center_spots:
        if board[r][c] == ' ':
            return r, c
            
    for r in range(4):
        for c in range(4):
            if board[r][c] == ' ':
                return r, c
    return 0, 0

# --- Inline Query Processor ---
@dp.inline_query()
async def inline_handler(inline_query: types.InlineQuery) -> None:
    user = inline_query.from_user
    await db_register_user(user.id, user.username, user.first_name)
    
    game_id = f"in_{secrets.token_hex(4)}"
    
    item_classic = InlineQueryResultArticle(
        id=f"{game_id}_classic",
        title="❌ ⭕ Classic Theme ဆော့ကစားမည်",
        description="ဂန္ထဝင် ❌ နှင့် ⭕ Theme ဖြင့် ကစားပွဲဖိတ်ခေါ်ချက်ထုတ်ရန်",
        input_message_content=InputTextMessageContent(
            message_text=f"🎮 **Tic-Tac-Toe 4x4 (Classic Theme)**\n\n👤 **Player 1:** {escape_md(user.first_name)} (❌)\n⏳ **Player 2:** စောင့်ဆိုင်းနေပါသည်...\n\nအောက်က 'Join Game' ကိုနှိပ်ပြီး ဝင်ဆော့နိုင်ပါပြီ။",
            parse_mode="Markdown"
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🤝 Join Game", callback_data=f"join_{game_id}_X_{user.id}_classic")]
        ])
    )
    
    item_element = InlineQueryResultArticle(
        id=f"{game_id}_element",
        title="🔥 ❄️ Elements Theme ဆော့ကစားမည်",
        description="မီး 🔥 နှင့် ရေခဲ ❄️ Theme ဖြင့် ကစားပွဲဖိတ်ခေါ်ချက်ထုတ်ရန်",
        input_message_content=InputTextMessageContent(
            message_text=f"🎮 **Tic-Tac-Toe 4x4 (Elements Theme)**\n\n👤 **Player 1:** {escape_md(user.first_name)} (🔥)\n⏳ **Player 2:** စောင့်ဆိုင်းနေပါသည်...\n\nအောက်က 'Join Game' ကိုနှိပ်ပြီး ဝင်ဆော့နိုင်ပါပြီ။",
            parse_mode="Markdown"
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🤝 Join Game", callback_data=f"join_{game_id}_X_{user.id}_element")]
        ])
    )
    
    await inline_query.answer([item_classic, item_element], cache_time=1)

# --- Command Handler Matrices ---
@dp.message(Command("start"))
async def start_cmd(message: types.Message) -> None:
    user = message.from_user
    await db_register_user(user.id, user.username, user.first_name)
    
    welcome_text = (
        f"👋 **မင်္ဂလာပါ {escape_md(user.first_name)} ဗျာ။**\n\n"
        f"🤖 ကျွန်တော်ကတော့ **Tic-Tac-Toe 4x4 Pro Bot** ဖြစ်ပါတယ်။\n"
        f"Group Chat တွေထဲမှာ ဒီအတိုင်း စာရိုက်ကွက်ထဲ `@xoBot` (သင့်ဘော့တ်အိုင်ဒီ) ဟု ရိုက်နှိပ်ပြီး "
        f"Inline စနစ်ဖြင့် ပွဲအလှည့်ကျ နေရာမရွေး လွယ်ကူစွာ တစ်ပြိုင်တည်း ကစားနိုင်ပါတယ်ဗျာ။\n\n"
        f"⚙️ **ကစားနိုင်သော စနစ်များ -**\n"
        f"တစ်ယောက်တည်း လေ့ကျင့်ချင်ပါက အောက်က **'🤖 Play with AI'** ခလုတ်ကို နှိပ်ပြီး ဆော့ကစားနိုင်ပါတယ်!"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Play with AI (တစ်ယောက်တည်းဆော့ရန်)", callback_data=f"ai_start_{user.id}")],
        [InlineKeyboardButton(text="🏆 Leaderboard ကြည့်ရန်", callback_data="top_board")]
    ])
    await message.answer(welcome_text, reply_markup=kb, parse_mode="Markdown")

@dp.message(Command("profile"))
async def profile_cmd(message: types.Message) -> None:
    p = await db_get_profile(message.from_user.id)
    if not p: return
    
    msg = (
        f"👤 **{escape_md(p['first_name'])} ရဲ့ Profile စာရင်းဇယား**\n\n"
        f"💰 **Coins:** {p['coins']} Coins\n"
        f"🎮 **ဆော့ခဲ့သမျှပွဲစုစုပေါင်း:** {p['games_played']} ပွဲ\n"
        f"🏆 **နိုင်ပွဲ (Wins):** {p['wins']} ပွဲ\n"
        f"😭 **ရှုံးပွဲ (Losses):** {p['losses']} ပွဲ\n"
        f"🤝 **သရေပွဲ (Draws):** {p['draws']} ပွဲ\n"
        f"🎯 **လက်ရှိ Win Streak:** {p['win_streak']} ပွဲဆက်တိုက်"
    )
    await message.answer(msg, parse_mode="Markdown")

@dp.message(Command("top"))
async def top_cmd(message: types.Message) -> None:
    top_list = await db_get_top()
    msg = "🏆 **Tic-Tac-Toe Top 10 Leaderboard**\n\n"
    for idx, row in enumerate(top_list, 1):
        msg += f"{idx}. {escape_md(row['first_name'])} — {row['wins']} နိုင်ပွဲ\n"
    await message.answer(msg, parse_mode="Markdown")

@dp.message(Command("stats"))
async def global_stats_cmd(message: types.Message) -> None:
    g_stats = await db_get_global_stats()
    async with game_lock:
        active_games = len(games)
    msg = (
        f"📊 **Bot Global System Metrics**\n\n"
        f"👥 **စုစုပေါင်း အသုံးပြုသူဇယား:** {g_stats['total_users']} ဦး\n"
        f"⚔️ **လက်ရှိဆော့ကစားနေဆဲ Active ပွဲစဉ်:** {active_games} ပွဲ"
    )
    await message.answer(msg, parse_mode="Markdown")

@dp.message(Command("help"))
async def help_cmd(message: types.Message) -> None:
    msg = (
        "📖 **ဘော့တ်အသုံးပြုနည်း လမ်းညွှန်ချက်**\n\n"
        "• `/start` — စတင်ရန်နှင့် AI မုဒ်ခေါ်ရန်\n"
        "• `/profile` — မိမိရဲ့ နိုင်/ရှုံး စာရင်းကြည့်ရန်\n"
        "• `/top` — ကမ္ဘာ့အဆင့် Leaderboard ကြည့်ရန်\n"
        "• `/stats` — စနစ်တစ်ခုလုံးရဲ့ Dynamic Data များကြည့်ရန်\n"
        "• `@သင့်ဘော့တ်အိုင်ဒီ [space]` — Group များထဲတွင် တန်းစီခေါ်ယူဆော့ကစားရန်"
    )
    await message.answer(msg, parse_mode="Markdown")

# --- Core Callback Handling Framework ---
@dp.callback_query(F.data.startswith("ai_start_"))
async def ai_start_callback(callback: types.CallbackQuery) -> None:
    _, _, uid_str = callback.data.split("_")
    if callback.from_user.id != int(uid_str):
        await callback.answer("သင်ကိုယ်တိုင် /start ခေါ်ပြီးမှ ဆော့ကစားပါ!")
        return
        
    game_id = f"ai_{callback.from_user.id}"
    theme_map = {'X': '❌', 'O': '🤖', ' ': ' '}
    
    async with game_lock:
        games[game_id] = {
            "board": [[' ' for _ in range(4)] for _ in range(4)],
            "creator": {"id": callback.from_user.id, "name": callback.from_user.first_name, "piece": 'X'},
            "opponent": {"id": 0, "name": "AI Bot Pro", "piece": 'O'},
            "turn": 'X',
            "status": "playing",
            "theme": theme_map,
            "chat_msg_id": callback.message.message_id
        }
    
    await safe_edit(callback, f"🎮 **AI နှင့် စိန်ခေါ်ပွဲစတင်ပါပြီ။**\nသင့်အလှည့်ဖြစ်ပါတယ်ဗျာ။", create_board_keyboard(games[game_id]["board"], game_id, theme_map))

@dp.callback_query(F.data.startswith("join_"))
async def join_callback(callback: types.CallbackQuery) -> None:
    _, game_id, creator_piece, creator_id_str, theme_name = callback.data.split("_")
    creator_id = int(creator_id_str)
    
    if callback.from_user.id == creator_id:
        await callback.answer("ကိုယ့်ဘာသာကိုယ် ပြန်လည် Join ၍မရနိုင်ပါ!")
        return
        
    theme_map = {'X': '❌', 'O': '⭕', ' ': ' '} if theme_name == "classic" else {'X': '🔥', 'O': '❄️', ' ': ' '}
    
    async with game_lock:
        if game_id in games and games[game_id]["status"] == "playing":
            await callback.answer("🚫 စိတ်မကောင်းပါဘူးဗျာ၊ ဒီပွဲကို တခြားသူ ဝင်ရောက်ဆော့ကစားနေပါပြီ။", show_alert=True)
            return
            
        games[game_id] = {
            "board": [[' ' for _ in range(4)] for _ in range(4)],
            "creator": {"id": creator_id, "name": "Player 1", "piece": creator_piece},
            "opponent": {"id": callback.from_user.id, "name": callback.from_user.first_name, "piece": 'O' if creator_piece == 'X' else 'X'},
            "turn": 'X',
            "status": "playing",
            "theme": theme_map,
            "inline_msg_id": callback.inline_message_id
        }
        game = games[game_id]
        
    await safe_edit(callback, get_turn_text(game), create_board_keyboard(game["board"], game_id, theme_map))

@dp.callback_query(F.data.startswith("move_"))
async def move_callback(callback: types.CallbackQuery) -> None:
    _, game_id, r_str, c_str = callback.data.split("_")
    r, c = int(r_str), int(c_str)
    
    async with game_lock:
        if game_id not in games:
            await callback.answer("ဤပွဲစဉ်မှာ ပြီးဆုံး သို့မဟုတ် ပျက်ပြယ်သွားပါပြီ။")
            return
            
        game = games[game_id]
        
    user_id = callback.from_user.id
    if user_id != game["creator"]["id"] and user_id != game["opponent"]["id"]:
        await callback.answer("🚫 သင်က ဒီပွဲမှာ ပါဝင်သူ မဟုတ်ပါဘူးဗျာ!")
        return
        
    current_piece = game["turn"]
    current_allowed_id = game["creator"]["id"] if game["creator"]["piece"] == current_piece else game["opponent"]["id"]
    
    if user_id != current_allowed_id:
        await callback.answer("⏳ သင့်အလှည့် မဟုတ်သေးပါဗျာ။ စောင့်ဆိုင်းပေးပါ။")
        return
        
    if game["board"][r][c] != ' ':
        await callback.answer("ဒီနေရာမှာ ကစားပြီးသား ဖြစ်နေပါတယ်။")
        return
        
    game["board"][r][c] = current_piece
    
    if check_winner(game["board"], current_piece):
        winner = game["creator"] if game["creator"]["piece"] == current_piece else game["opponent"]
        loser = game["opponent"] if game["creator"]["piece"] == current_piece else game["creator"]
        
        if game["opponent"]["id"] != 0:
            await db_update_stats(winner["id"], "win")
            await db_update_stats(loser["id"], "loss")
            
        rematch_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 ထပ် ပလေးမလေး (Rematch)", callback_data=f"rematch_{game_id}")],
            [InlineKeyboardButton(text="❌ ပွဲသိမ်းမည်", callback_data=f"end_{game_id}")]
        ])
        await safe_edit(callback, f"🎮 **Tic-Tac-Toe 4x4 ပြီးဆုံးပါပြီ!**\n\n🏆 အနိုင်ရရှိသူ: {escape_md(winner['name'])}\n😭 အရှုံးရရှိသူ: {escape_md(loser['name'])}", rematch_kb)
        async with game_lock:
            if game_id in games and "rematch_votes" not in games[game_id]:
                games[game_id]["status"] = "ended"
                games[game_id]["rematch_votes"] = set()
        return

    if all(cell != ' ' for row in game["board"] for cell in row):
        if game["opponent"]["id"] != 0:
            await db_update_stats(game["creator"]["id"], "draw")
            await db_update_stats(game["opponent"]["id"], "draw")
            
        rematch_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 ထပ် ပလေးမလေး (Rematch)", callback_data=f"rematch_{game_id}")]
        ])
        await safe_edit(callback, "🤝 **ပွဲစဉ်သည် သရေဖြင့် ပြီးဆုံးသွားပါပြီဗျာ!**", rematch_kb)
        async with game_lock:
            if game_id in games:
                games[game_id]["status"] = "ended"
                games[game_id]["rematch_votes"] = set()
        return

    game["turn"] = 'O' if current_piece == 'X' else 'X'
    
    if game["opponent"]["id"] == 0 and game["turn"] == 'O':
        ai_r, ai_c = get_ai_move(game["board"])
        game["board"][ai_r][ai_c] = 'O'
        
        if check_winner(game["board"], 'O'):
            await db_update_stats(game["creator"]["id"], "loss")
            rematch_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 ထပ် ပလေးမလေး", callback_data=f"ai_start_{game['creator']['id']}")]
            ])
            await safe_edit(callback, f"🎮 **Tic-Tac-Toe 4x4 ပြီးဆုံးပါပြီ!**\n\n🏆 အနိုင်ရရှိသူ: 🤖 AI Bot Pro\n😭 အရှုံးရရှိသူ: {escape_md(game['creator']['name'])}", rematch_kb)
            async with game_lock:
                del games[game_id]
            return
            
        game["turn"] = 'X'

    await safe_edit(callback, get_turn_text(game), create_board_keyboard(game["board"], game_id, game["theme"]))

@dp.callback_query(F.data.startswith("leave_"))
async def leave_callback(callback: types.CallbackQuery) -> None:
    _, game_id = callback.data.split("_")
    async with game_lock:
                if game_id not in games: 
            return
        game = games[game_id]
        
    uid = callback.from_user.id
    if uid not in [game["creator"]["id"], game["opponent"]["id"]]:
        await callback.answer("သင်က ပွဲစဉ်ထဲက လူမဟုတ်ပါ။")
        return
        
    loser = game["creator"] if game["creator"]["id"] == uid else game["opponent"]
    winner = game["opponent"] if game["creator"]["id"] == uid else game["creator"]
    
    if game["opponent"]["id"] != 0:
        await db_update_stats(winner["id"], "win")
        await db_update_stats(loser["id"], "loss")
        
    await safe_edit(callback, f"🚪 **Leave Game! ပွဲစဉ်ကို လက်လျှော့လိုက်ပါပြီ။**\n\n😭 {escape_md(loser['name'])} သည် ပွဲအတွင်းမှ ထွက်ခွာသွားသဖြင့် အလိုအလျောက် ရှုံးနိမ့်သွားပါသည်။\n🏆 အနိုင်ရရှိသူ: {escape_md(winner['name'])}", None)
    async with game_lock:
        if game_id in games: 
            del games[game_id]

@dp.callback_query(F.data.startswith("rematch_"))
async def rematch_callback(callback: types.CallbackQuery) -> None:
    _, game_id = callback.data.split("_")
    async with game_lock:
        if game_id not in games:
            await callback.answer("ဒီပွဲသက်တမ်း ကုန်ဆုံးသွားပါပြီ။ အသစ်ပြန်ဆော့ပါဗျာ။")
            return
        game = games[game_id]
        
    uid = callback.from_user.id
    if uid not in [game["creator"]["id"], game["opponent"]["id"]]:
        await callback.answer("ပွဲစဉ်တွင် ပါဝင်သူများသာ Rematch တောင်းဆိုနိုင်ပါသည်။")
        return
        
    game["rematch_votes"].add(uid)
    
    if len(game["rematch_votes"]) < 2:
        await callback.answer("🔄 Rematch လက်ခံလိုက်ပါပြီ။ တခြားတစ်ယောက် အတည်ပြုရန် စောင့်ဆိုင်းနေပါသည်။")
        return
        
    game["board"] = [[' ' for _ in range(4)] for _ in range(4)]
    game["turn"] = 'X'
    game["status"] = "playing"
    game["rematch_votes"] = set()
    
    await safe_edit(callback, get_turn_text(game), create_board_keyboard(game["board"], game_id, game["theme"]))

@dp.callback_query(F.data.startswith("end_"))
async def end_callback(callback: types.CallbackQuery) -> None:
    _, game_id = callback.data.split("_")
    async with game_lock:
        if game_id in games:
            del games[game_id]
    await safe_edit(callback, "🛑 ကစားပွဲစင်္ကြံကို အပြီးပိတ်သိမ်းလိုက်ပါပြီဗျာ။", None)

@dp.callback_query(F.data == "top_board")
async def top_board_callback(callback: types.CallbackQuery) -> None:
    top_list = await db_get_top()
    msg = "🏆 **Tic-Tac-Toe Top 10 Leaderboard**\n\n"
    for idx, row in enumerate(top_list, 1):
        msg += f"{idx}. {escape_md(row['first_name'])} — {row['wins']} နိုင်ပွဲ\n"
    await safe_edit(callback, msg, None)

# --- Admin Broadcast System Engine ---
@dp.message(Command("broadcast"))
async def broadcast_handler(message: types.Message) -> None:
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ သင်သည် ဤစနစ်၏ Admin မဟုတ်ပါ။")
        return
        
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("❌ ထုတ်ပြန်ကြေညာမည့် စာသားဖြည့်စွက်ပေးပါ။")
        return
        
    text_to_send = args[1]
    
    def _get_all_ids():
        with sqlite3.connect(DB_FILE) as conn:
            return [row[0] for row in conn.execute("SELECT user_id FROM users").fetchall()]
            
    all_uids = await asyncio.to_thread(_get_all_ids)
    success_count = 0
    
    for uid in all_uids:
        try:
            await bot.send_message(chat_id=int(uid), text=text_to_send)
            success_count += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logging.error(f"Broadcast Performance Dropped for Node {uid}: {e}")
            
    await message.answer(f"📢 **မြန်နှုန်းမြင့် Broadcast ပြီးဆုံးပါပြီ!**\n\n👥 ပို့ဆောင်အောင်မြင်မှု: လူဦးရေ **{success_count}** ယောက်။")

# --- Production Main Async Process Coroutine ---
async def main() -> None:
    await dp.start_polling(bot)

if __name__ == "__main__":
    t: Thread = Thread(target=run_flask)
    t.start()
    asyncio.run(main())
