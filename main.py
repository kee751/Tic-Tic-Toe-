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

# --- Configuration & Initialization ---
TOKEN: Optional[str] = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN missing in Environment Variables.")

ADMIN_IDS: List[int] = [int(x) for x in os.getenv("ADMIN_IDS", "7679480147").split(",") if x.strip()]

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

# --- Utilities & Security Escaping ---
def escape_md(text: str) -> str:
    if not text:
        return ""
    for char in ['_', '*', '`', '[']:
        text = text.replace(char, f"\\{char}")
    return text

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
            await callback.message.edit_text(
                text=text, reply_markup=reply_markup,
                parse_mode="Markdown"
            )
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
        f"⚔️ **Tic-Tac-Toe 4x4** ⚔️\n\n"
        f"👤 {escape_md(p1['name'])} ({game['theme'][p1['piece']]})\n"
        f"🤖 {escape_md(p2['name'])} ({game['theme'][p2['piece']]})\n\n"
        f"▶️ **အလှည့်:** {escape_md(current_player_name)} ({c_theme})"
    )

def create_board_keyboard(board: list, game_id: str, theme: Dict[str, str]) -> InlineKeyboardMarkup:
    keyboard = []
    for r in range(4):
        row = []
        for c in range(4):
            symbol = board[r][c]
            display_text = theme[symbol] if symbol != '' else "➖"
            row.append(InlineKeyboardButton(text=display_text, callback_data=f"move_{game_id}_{r}_{c}"))
        keyboard.append(row)
        
    keyboard.append([InlineKeyboardButton(text="🏳️ Leave Game (အရှုံးပေးရန်)", callback_data=f"leave_{game_id}")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def check_winner(board: list, player: str) -> bool:
    for i in range(4):
        # Rows and Columns
        if all(board[i][j] == player for j in range(4)) or \
           all(board[j][i] == player for j in range(4)):
            return True
            
    # Diagonals
    if all(board[i][i] == player for i in range(4)) or \
       all(board[i][3-i] == player for i in range(4)):
        return True
        
    return False

def check_draw(board: list) -> bool:
    for r in range(4):
        for c in range(4):
            if board[r][c] == '':
                return False
    return True

# --- Smart 4x4 Defensive Bot AI Logic ---
def get_ai_move(board: list) -> tuple:
    # 1. AI Win Check
    for r in range(4):
        for c in range(4):
            if board[r][c] == '':
                board[r][c] = 'O'
                if check_winner(board, 'O'): 
                    board[r][c] = ''
                    return r, c
                board[r][c] = ''
                
    # 2. Block Player Win
    for r in range(4):
        for c in range(4):
            if board[r][c] == '':
                board[r][c] = 'X'
                if check_winner(board, 'X'):
                    board[r][c] = ''
                    return r, c
                board[r][c] = ''
                
    # 3. Take Center Spots
    center_spots = [(1,1), (1,2), (2,1), (2,2)]
    for r, c in center_spots:
        if board[r][c] == '': 
            return r, c
            
    # 4. Take First Available
    for r in range(4):
        for c in range(4):
            if board[r][c] == '': 
                return r, c
                
    return 0, 0

# ==========================================
#         TELEGRAM COMMAND HANDLERS
# ==========================================

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await db_register_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    
    welcome_text = (
        f"👋 မင်္ဂလာပါ {escape_md(message.from_user.first_name)}!\n\n"
        f"🎮 4x4 Tic-Tac-Toe ဂိမ်း Bot မှ ကြိုဆိုပါတယ်။\n"
        f"အောက်ပါ ခလုတ်ကိုနှိပ်ပြီး AI နဲ့ ယှဉ်ပြိုင်ကစားနိုင်ပါပြီ။"
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 Play with AI (ကစားမည်)", callback_data="play_ai")],
        [InlineKeyboardButton(text="👤 My Profile (ပရိုဖိုင်)", callback_data="profile")]
    ])
    
    await message.answer(welcome_text, reply_markup=keyboard, parse_mode="Markdown")

@dp.callback_query(F.data == "profile")
async def profile_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    profile = await db_get_profile(user_id)
    
    if not profile:
        await callback.answer("မှတ်တမ်း မတွေ့ပါ။ /start ကိုနှိပ်ပါ။", show_alert=True)
        return
        
    text = (
        f"👤 **ပရိုဖိုင်မှတ်တမ်း - {escape_md(profile['first_name'])}**\n\n"
        f"🏆 နိုင်ပွဲ: {profile['wins']}\n"
        f"💀 ရှုံးပွဲ: {profile['losses']}\n"
        f"🤝 သရေပွဲ: {profile['draws']}\n"
        f"🎮 ကစားပွဲစုစုပေါင်း: {profile['games_played']}\n"
        f"🔥 ဆက်တိုက်နိုင်ပွဲ: {profile['win_streak']}\n"
        f"💰 ဒင်္ဂါးပြား: {profile['coins']}"
    )
    
    await safe_edit(callback, text, None)

@dp.callback_query(F.data == "play_ai")
async def start_ai_game(callback: types.CallbackQuery):
    game_id = secrets.token_hex(4)
    board = [['' for _ in range(4)] for _ in range(4)]
    
    async with game_lock:
        games[game_id] = {
            "board": board,
            "turn": "X",
            "theme": {"X": "❌", "O": "⭕"},
            "creator": {"id": callback.from_user.id, "name": callback.from_user.first_name, "piece": "X"},
            "opponent": {"id": 0, "name": "AI Bot", "piece": "O"}, # 0 is AI
            "status": "playing"
        }
    
    game = games[game_id]
    text = get_turn_text(game)
    keyboard = create_board_keyboard(board, game_id, game["theme"])
    
    await safe_edit(callback, text, keyboard)

# ==========================================
#            GAME MOVE HANDLER
# ==========================================

@dp.callback_query(F.data.startswith("move_"))
async def move_callback(callback: types.CallbackQuery):
    _, game_id, r, c = callback.data.split("_")
    r, c = int(r), int(c)
    
    async with game_lock:
        if game_id not in games:
            await callback.answer("ဒီပွဲစဉ် ပြီးဆုံးသွားပါပြီ။", show_alert=True)
            return
            
        game = games[game_id]
        user_id = callback.from_user.id
        
        # Check if it's player's turn
        if game["creator"]["id"] != user_id:
            await callback.answer("သင်က ဒီပွဲကို ကစားနေသူ မဟုတ်ပါ။", show_alert=True)
            return
            
        if game["turn"] != game["creator"]["piece"]:
            await callback.answer("သင့်အလှည့် မရောက်သေးပါ။", show_alert=True)
            return
            
        board = game["board"]
        
        if board[r][c] != '':
            await callback.answer("ဒီနေရာမှာ ချပြီးသားပါ။ တခြားနေရာ ရွေးပါ။", show_alert=True)
            return
            
        # --- Player Move ---
        board[r][c] = game["creator"]["piece"]
        
        # Check Player Win
        if check_winner(board, game["creator"]["piece"]):
            await db_update_stats(user_id, "win")
            text = f"🏆 **ဂုဏ်ယူပါတယ်! သင် အနိုင်ရသွားပါပြီ!**\n\n👤 {escape_md(game['creator']['name'])} နိုင်ပါသည်။"
            keyboard = create_board_keyboard(board, game_id, game["theme"])
            await safe_edit(callback, text, keyboard)
            del games[game_id]
            return
            
        # Check Draw
        if check_draw(board):
            await db_update_stats(user_id, "draw")
            text = f"🤝 **သရေကျသွားပါသည်!**\n\nနောက်တစ်ပွဲ ပြန်ကြိုးစားကြည့်ပါ။"
            keyboard = create_board_keyboard(board, game_id, game["theme"])
            await safe_edit(callback, text, keyboard)
            del games[game_id]
            return
            
        # --- AI Move ---
        game["turn"] = game["opponent"]["piece"]
        # Edit message to show AI is thinking
        await safe_edit(callback, get_turn_text(game), create_board_keyboard(board, game_id, game["theme"]))
        
        # Small delay for realism
        await asyncio.sleep(0.5)
        
        ai_r, ai_c = get_ai_move(board)
        board[ai_r][ai_c] = game["opponent"]["piece"]
        
        # Check AI Win
        if check_winner(board, game["opponent"]["piece"]):
            await db_update_stats(user_id, "loss")
            text = f"💀 **ရှုံးသွားပါပြီ!**\n\n🤖 AI Bot မှ အနိုင်ရရှိသွားပါသည်။"
            keyboard = create_board_keyboard(board, game_id, game["theme"])
            await safe_edit(callback, text, keyboard)
            del games[game_id]
            return
            
        # Check Draw after AI move
        if check_draw(board):
            await db_update_stats(user_id, "draw")
            text = f"🤝 **သရေကျသွားပါသည်!**\n\nနောက်တစ်ပွဲ ပြန်ကြိုးစားကြည့်ပါ။"
            keyboard = create_board_keyboard(board, game_id, game["theme"])
            await safe_edit(callback, text, keyboard)
            del games[game_id]
            return
            
        # Pass turn back to player
        game["turn"] = game["creator"]["piece"]
        await safe_edit(callback, get_turn_text(game), create_board_keyboard(board, game_id, game["theme"]))

@dp.callback_query(F.data.startswith("leave_"))
async def leave_callback(callback: types.CallbackQuery) -> None:
    parts = callback.data.split("_", 1)
    if len(parts) < 2: 
        return
    game_id = parts[1]

    async with game_lock:
        if game_id not in games:
            return
        game = games[game_id]

    uid = callback.from_user.id
    if uid not in [game["creator"]["id"], game["opponent"]["id"]]:
        await callback.answer("သင်က ပွဲစဉ်ထဲက လူမဟုတ်ပါ။", show_alert=True)
        return

    loser = game["creator"] if game["creator"]["id"] == uid else game["opponent"]
    winner = game["opponent"] if game["creator"]["id"] == uid else game["creator"]

    if game["opponent"]["id"] != 0: # 0 means AI/Bot ID usually
        await db_update_stats(winner["id"], "win")
        await db_update_stats(loser["id"], "loss")
    else:
        # User lost to AI by leaving
        await db_update_stats(loser["id"], "loss")

    await safe_edit(
        callback, 
        f"🏳️ **Leave Game!**\n\n👤 {escape_md(loser['name'])} သည် ပွဲအတွင်းမှ ထွက်ခွာသွားသဖြင့် အလိုအလျောက် အရှုံးပေးလိုက်ပါသည်။\n\n🏆 အနိုင်ရရှိသူ: {escape_md(winner['name'])}", 
        None
    )

    async with game_lock:
        if game_id in games:
            del games[game_id]

# --- Main Entry Point ---
async def main() -> None:
    print("Bot is Starting...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    t: Thread = Thread(target=run_flask)
    t.start()
    asyncio.run(main())