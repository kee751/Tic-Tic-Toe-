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
from aiogram.exceptions import TelegramAPIError

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
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        # ... existing users table ...
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
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS groups (
                chat_id TEXT PRIMARY KEY,
                title TEXT,
                type TEXT,
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

async def db_update_coins(user_id: int, amount: int) -> None:
    """Add or subtract coins (positive adds, negative subtracts)"""
    def _run():
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET coins = coins + ? WHERE user_id = ?", (amount, str(user_id)))
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

async def db_get_all_users() -> List[str]:
    """Broadcast အတွက် User အားလုံး၏ ID များကို ရယူခြင်း"""
    def _run():
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM users")
            return [row[0] for row in cursor.fetchall()]

    async with db_lock:
        return await asyncio.to_thread(_run)
        
async def db_get_all_chats() -> List[str]:
    """User ID နှင့် Group Chat ID အားလုံးကို ပြန်ပေးမည်"""
    def _run():
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM users")
            user_ids = [row[0] for row in cursor.fetchall()]
            cursor.execute("SELECT chat_id FROM groups")
            group_ids = [row[0] for row in cursor.fetchall()]
            return user_ids + group_ids
    async with db_lock:
        return await asyncio.to_thread(_run)

async def db_get_leaderboard() -> List[tuple]:
    """Leaderboard အတွက် အချက်အလက်ယူခြင်း"""
    def _run():
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT first_name, wins FROM users ORDER BY wins DESC LIMIT 5")
            return cursor.fetchall()
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
    
    # ယာယီစောင့်နေသော အခြေအနေ
    if p2["id"] == -1:
        return (
            f"⚔️ **Tic-Tac-Toe 4x4 (PvP Mode)** ⚔️\n\n"
            f"👤 {escape_md(p1['name'])} ({game['theme'][p1['piece']]})\n"
            f"⏳ ကစားဖော်အား စောင့်ဆိုင်းနေပါသည်...\n\n"
            f"အောက်ပါ 'Join Game' ကိုနှိပ်ပြီး ဝင်ရောက်ကစားပါ။"
        )
        
    current_player_name = p1["name"] if p1["piece"] == current_piece else p2["name"]
    
    return (
        f"⚔️ **Tic-Tac-Toe 4x4** ⚔️\n\n"
        f"👤 {escape_md(p1['name'])} ({game['theme'][p1['piece']]})\n"
        f"🤖/👤 {escape_md(p2['name'])} ({game['theme'][p2['piece']]})\n\n"
        f"▶️ **အလှည့်:** {escape_md(current_player_name)} ({c_theme})"
    )

def create_board_keyboard(board: list, game_id: str, theme: Dict[str, str], game: Dict[str, Any], is_game_over: bool = False) -> InlineKeyboardMarkup:
    keyboard = []
    rows = len(board)
    cols = len(board[0]) if rows > 0 else 0
    for r in range(rows):
        row = []
        for c in range(cols):
            symbol = board[r][c]
            # theme ထဲမှာ မရှိရင် symbol ကိုပဲ ပြမယ်
            display_text = theme.get(symbol, symbol) if symbol != '' else "➖"
            row.append(InlineKeyboardButton(text=display_text, callback_data=f"move_{game_id}_{r}_{c}"))
        keyboard.append(row)

    if not is_game_over:
        # undo ကို move ၂ ခုရှိမှပဲ ပြမယ်
        if game.get("status") == "playing" and len(game.get("moves", [])) >= 2:
            keyboard.append([InlineKeyboardButton(text="↩️ Undo (50 coins)", callback_data=f"undo_{game_id}")])
        # leave ကိုလည်း playing ဖြစ်မှပြမယ်
        if game.get("status") == "playing":
            keyboard.append([InlineKeyboardButton(text="🏳️ Leave Game (အရှုံးပေးရန်)", callback_data=f"leave_{game_id}")])
    else:
        if game["opponent"]["id"] == 0:
            keyboard.append([InlineKeyboardButton(text="🔄 ထပ်ကစားမည် (AI နှင့်)", callback_data="play_ai")])
        else:
            keyboard.append([InlineKeyboardButton(text="🔄 ထပ်ကစားမည် (သူငယ်ချင်းနှင့်)", callback_data="play_pvp")])

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

def check_draw(board: list) -> bool:
    for r in range(4):
        for c in range(4):
            if board[r][c] == '':
                return False
    return True

# --- Smart 4x4 Defensive Bot AI Logic ---
def get_ai_move(board: list) -> tuple:
    for r in range(4):
        for c in range(4):
            if board[r][c] == '':
                board[r][c] = 'O'
                if check_winner(board, 'O'): 
                    board[r][c] = ''
                    return r, c
                board[r][c] = ''
                
    for r in range(4):
        for c in range(4):
            if board[r][c] == '':
                board[r][c] = 'X'
                if check_winner(board, 'X'):
                    board[r][c] = ''
                    return r, c
                board[r][c] = ''
                
    center_spots = [(1,1), (1,2), (2,1), (2,2)]
    for r, c in center_spots:
        if board[r][c] == '': 
            return r, c
            
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
        f"အောက်ပါ ခလုတ်ကိုနှိပ်ပြီး AI နဲ့ဖြစ်စေ၊ သူငယ်ချင်းနဲ့ဖြစ်စေ ယှဉ်ပြိုင်ကစားနိုင်ပါပြီ။"
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 Play with AI (AI ဖြင့်ဆော့မည်)", callback_data="play_ai")],
        [InlineKeyboardButton(text="👥 Play with Friend (သူငယ်ချင်းနှင့်ဆော့မည်)", callback_data="play_pvp")],
        [InlineKeyboardButton(text="👤 My Profile (ပရိုဖိုင်)", callback_data="profile")]
    ])
    
    await message.answer(welcome_text, reply_markup=keyboard, parse_mode="Markdown")

@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def register_group(message: types.Message):
    chat_id = str(message.chat.id)
    title = message.chat.title or "Unknown"
    chat_type = message.chat.type
    async with db_lock:
        def _run():
            with sqlite3.connect(DB_FILE) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO groups (chat_id, title, type, last_seen)
                    VALUES (?, ?, ?, datetime('now'))
                    ON CONFLICT(chat_id) DO UPDATE SET
                        title = excluded.title,
                        type = excluded.type,
                        last_seen = datetime('now')
                """, (chat_id, title, chat_type))
                conn.commit()
        await asyncio.to_thread(_run)

@dp.message(Command("leaderboard"))
async def cmd_leaderboard(message: types.Message):
    data = await db_get_leaderboard()
    text = "🏆 **Top 5 Players (Leaderboard)** 🏆\n\n"
    for i, (name, wins) in enumerate(data, 1):
        text += f"{i}. {escape_md(name)} - {wins} wins\n"
    await message.answer(text, parse_mode="Markdown")

# --- Broadcast Command (Admin Only) ---
# --- Broadcast Command (Admin Only) ---
@dp.message(Command("broadcast"))
async def cmd_broadcast(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.reply("ဤ command ကို Admin သာ အသုံးပြုနိုင်ပါသည်။")
        return
        
    # Reply ပြန်ပြီးမှ Broadcast လုပ်ခိုင်းမည့်စနစ် (ပုံ၊ ဗီဒီယိုပါ ပို့နိုင်ရန်)
    if not message.reply_to_message:
        await message.reply("⚠️ ကျေးဇူးပြု၍ သင် Broadcast လုပ်လိုသော စာ၊ ပုံ (သို့) ဗီဒီယိုကို **Reply** ပြန်ပြီး `/broadcast` ဟု ရိုက်ပါ။", parse_mode="Markdown")
        return

    # မူလကုဒ်တွင် ရှိပြီးသား Database Function ကို အသုံးပြုခြင်း
    chats = await db_get_all_chats()   
    success_count = 0
    fail_count = 0
    
    await message.reply(f"🚀 Chat အရေအတွက် ({len(chats)}) ဆီသို့ Broadcast စတင်ပို့ဆောင်နေပါပြီ...")

    for chat_id in chats:
        try:
            # စာသားသာမက Media များကိုပါ Copy ကူး၍ ပို့ဆောင်ပေးမည်
            await bot.copy_message(
                chat_id=int(chat_id),
                from_chat_id=message.chat.id,
                message_id=message.reply_to_message.message_id
            )
            success_count += 1
            await asyncio.sleep(0.05)  # Rate limit ကာကွယ်ရန်
        except Exception as e:
            logging.error(f"Broadcast error to {chat_id}: {e}")
            fail_count += 1
            
    await message.reply(f"✅ **Broadcast ပြီးဆုံးပါပြီ။**\n\nအောင်မြင်: {success_count} ခု\nမအောင်မြင်: {fail_count} ခု (Bot ကို block ထားသူများ သို့မဟုတ် Group မှ ဖယ်ရှားခံရမှုများ)")


# ==========================================
#            CALLBACK HANDLERS
# ==========================================

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
            "status": "playing",
            "moves": []  # Store (player, r, c) tuples
        }
    
    game = games[game_id]
    text = get_turn_text(game)
    keyboard = create_board_keyboard(board, game_id, game["theme"], game)
    
    await safe_edit(callback, text, keyboard)

@dp.callback_query(F.data == "play_pvp")
async def start_pvp_game(callback: types.CallbackQuery):
    game_id = secrets.token_hex(4)
    board = [['' for _ in range(4)] for _ in range(4)]
    
    async with game_lock:
        games[game_id] = {
            "board": board,
            "turn": "X",
            "theme": {"X": "❌", "O": "⭕"},
            "creator": {"id": callback.from_user.id, "name": callback.from_user.first_name, "piece": "X"},
            "opponent": {"id": -1, "name": "Waiting...", "piece": "O"}, # -1 is Waiting for Real Player
            "status": "waiting",
            "moves": []
        }
    
    game = games[game_id]
    text = get_turn_text(game)
    
    # သူငယ်ချင်း ဝင်Join ရန်ခလုတ်
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 Join Game (ဝင်ကစားမည်)", callback_data=f"join_{game_id}")]
    ])
    
    await safe_edit(callback, text, keyboard)

@dp.callback_query(F.data.startswith("join_"))
async def join_pvp_game(callback: types.CallbackQuery):
    _, game_id = callback.data.split("_")
    user_id = callback.from_user.id
    
    async with game_lock:
        if game_id not in games:
            await callback.answer("ဒီပွဲစဉ် ပျက်သွားပါပြီ။", show_alert=True)
            return
            
        game = games[game_id]
        
        if game["status"] != "waiting":
            await callback.answer("ဒီပွဲမှာ လူပြည့်သွားပါပြီ။", show_alert=True)
            return
            
        if game["creator"]["id"] == user_id:
            await callback.answer("သင်က ပွဲဖန်တီးသူ ဖြစ်နေပါသည်။ အခြားသူကို စောင့်ပါ။", show_alert=True)
            return
            
        # အခြား Player ဝင်ရောက်လာခြင်း
        game["opponent"] = {"id": user_id, "name": callback.from_user.first_name, "piece": "O"}
        game["status"] = "playing"
        
        # User အသစ်ကိုပါ Database ထဲမှတ်ပေးမည်
        await db_register_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
        
        text = get_turn_text(game)
        keyboard = create_board_keyboard(game["board"], game_id, game["theme"], game)
        
    await safe_edit(callback, text, keyboard)
    await callback.answer("ဂိမ်းထဲသို့ အောင်မြင်စွာ ဝင်ရောက်ပြီးပါပြီ။")

# --- Leave Game Handler ---
@dp.callback_query(F.data.startswith("leave_"))
async def leave_game(callback: types.CallbackQuery):
    _, game_id = callback.data.split("_")
    async with game_lock:
        if game_id in games:
            del games[game_id]
    await callback.answer("သင်က ဂိမ်းမှ ထွက်ခွာသွားပါပြီ။", show_alert=True)
    await safe_edit(callback, "🚪 ဂိမ်း ပြီးဆုံးသွားပါပြီ။", None)

# --- Undo Handler ---
@dp.callback_query(F.data.startswith("undo_"))
async def undo_move(callback: types.CallbackQuery):
    # "undo_GAME_ID" → ["undo", "GAME_ID"] (game_id မှာ _ ပါလည်း အားလုံးပါမယ်)
    _, game_id = callback.data.split("_", 1)   # maxsplit=1
    user_id = callback.from_user.id

    async with game_lock:
        if game_id not in games:
            await callback.answer("ဂိမ်းမရှိတော့ပါ။", show_alert=True)
            return

        game = games[game_id]
        if game["status"] != "playing":
            await callback.answer("ဂိမ်းက ကစားနေဆဲမဟုတ်ပါ။", show_alert=True)
            return

        moves = game.get("moves", [])
        if len(moves) < 2:
            await callback.answer("နောက်ပြန်ဆုတ်ရန် လုံလောက်သော လှုပ်ရှားမှုမရှိသေးပါ။", show_alert=True)
            return

        my_piece = game["creator"]["piece"] if game["creator"]["id"] == user_id else game["opponent"]["piece"]

        # မိမိအလှည့်မှသာ undo ခွင့်ပြုရန်
        if game["turn"] != my_piece:
            await callback.answer("သင်အလှည့်မဟုတ်သေးပါ။", show_alert=True)
            return

        # နောက်ဆုံး ၂ ခု၏ ပိုင်ရှင်စစ်ဆေးခြင်း
        last_move_1 = moves[-1]
        last_move_2 = moves[-2]
        if last_move_1[0] == my_piece or last_move_2[0] != my_piece:
            await callback.answer("လှုပ်ရှားမှု အချက်အလက် မကိုက်ညီပါ။", show_alert=True)
            return

        # ဒင်္ဂါးပြား စစ်ဆေးခြင်း
        profile = await db_get_profile(user_id)
        if not profile or profile["coins"] < 50:
            await callback.answer("သင့်တွင် ဒင်္ဂါးပြား 50 မရှိပါ။", show_alert=True)
            return

        await db_update_coins(user_id, -50)

        # undo လုပ်ဆောင်ခြင်း
        moves.pop()  # ပြိုင်ဘက်၏ move
        moves.pop()  # မိမိ၏ move
        _, r1, c1 = last_move_1
        _, r2, c2 = last_move_2
        game["board"][r1][c1] = ''
        game["board"][r2][c2] = ''

        game["turn"] = my_piece

        text = get_turn_text(game)
        keyboard = create_board_keyboard(game["board"], game_id, game["theme"], game)

    await safe_edit(callback, text, keyboard)
    await callback.answer(f"✅ သင်နှင့် ပြိုင်ဘက်၏ လှုပ်ရှားမှုကို နောက်ဆုတ်လိုက်ပါပြီ။ (50 coins ကုန်ဆုံး)", show_alert=False)

# ==========================================
#            GAME MOVE HANDLER
# ==========================================

@dp.callback_query(F.data.startswith("move_"))
async def move_callback(callback: types.CallbackQuery):
    _, game_id, r, c = callback.data.split("_")
    r, c = int(r), int(c)
    user_id = callback.from_user.id
    
    # --- Phase 1: Lock only to read and validate ---
    async with game_lock:
        if game_id not in games:
            await callback.answer("ဒီပွဲစဉ် ပြီးဆုံးသွားပါပြီ။", show_alert=True)
            return
            
        game = games[game_id]
        
        if game["status"] == "waiting":
            await callback.answer("ကစားဖော် မရှိသေးပါ။ တစ်ယောက်ယောက် ဝင်လာသည်အထိ စောင့်ပါ။", show_alert=True)
            return

        current_piece = game["turn"]
        current_player = game["creator"] if game["creator"]["piece"] == current_piece else game["opponent"]
        
        # အလှည့်စစ်ဆေးခြင်း
        if current_player["id"] != user_id:
            if user_id in [game["creator"]["id"], game["opponent"]["id"]]:
                await callback.answer("သင့်အလှည့် မရောက်သေးပါ။", show_alert=True)
            else:
                await callback.answer("သင်က ဒီပွဲကို ကစားနေသူ မဟုတ်ပါ။", show_alert=True)
            return
            
        board = game["board"]
        
        if board[r][c] != '':
            await callback.answer("ဒီနေရာမှာ ချပြီးသားပါ။ တခြားနေရာ ရွေးပါ။", show_alert=True)
            return
            
        # --- Apply Player Move (inside lock) ---
        board[r][c] = current_piece
        game["moves"].append((current_piece, r, c))
        
        # Check Win
        if check_winner(board, current_piece):
            winner = current_player
            loser = game["opponent"] if current_player == game["creator"] else game["creator"]
            
            await db_update_stats(winner["id"], "win")
            if loser["id"] != 0: # AI မဟုတ်ရင် ရှုံးတဲ့သူကို မှတ်မယ်
                await db_update_stats(loser["id"], "loss")
                
            text = f"🏆 **ဂုဏ်ယူပါတယ်! ပွဲပြီးဆုံးသွားပါပြီ!**\n\n👤 {escape_md(winner['name'])} မှ အနိုင်ရရှိသွားပါသည်။"
            keyboard = create_board_keyboard(board, game_id, game["theme"], game)
            await safe_edit(callback, text, keyboard)
            del games[game_id]
            return
            
        # Check Draw
        if check_draw(board):
            await db_update_stats(game["creator"]["id"], "draw")
            if game["opponent"]["id"] != 0:
                await db_update_stats(game["opponent"]["id"], "draw")
                
            text = f"🤝 **သရေကျသွားပါသည်!**\n\nနောက်တစ်ပွဲ ပြန်ကြိုးစားကြည့်ပါ။"
            keyboard = create_board_keyboard(board, game_id, game["theme"], game)
            await safe_edit(callback, text, keyboard)
            del games[game_id]
            return
            
        # --- Move Next Turn ---
        next_piece = "O" if current_piece == "X" else "X"
        game["turn"] = next_piece
        
        # If AI mode, we need to handle AI move after releasing lock
        ai_mode = (game["opponent"]["id"] == 0)
    
    # --- Phase 2: AI move (outside lock) ---
    if ai_mode:
        # Update board UI for player's move
        await safe_edit(callback, get_turn_text(game), create_board_keyboard(board, game_id, game["theme"], game))
        await asyncio.sleep(0.5) # AI thinking delay
        
        # Lock again to perform AI move
        async with game_lock:
            # Re-check game existence and status
            if game_id not in games:
                return
            game = games[game_id]
            if game["status"] != "playing":
                return
            board = game["board"]
            
            ai_r, ai_c = get_ai_move(board)
            board[ai_r][ai_c] = game["opponent"]["piece"]
            game["moves"].append((game["opponent"]["piece"], ai_r, ai_c))
            
            if check_winner(board, game["opponent"]["piece"]):
                await db_update_stats(game["creator"]["id"], "loss")
                text = f"💀 **ရှုံးသွားပါပြီ!**\n\n🤖 AI Bot မှ အနိုင်ရရှိသွားပါသည်။"
                keyboard = create_board_keyboard(board, game_id, game["theme"], game)
                await safe_edit(callback, text, keyboard)
                del games[game_id]
                return
                
            if check_draw(board):
                await db_update_stats(game["creator"]["id"], "draw")
                text = f"🤝 **သရေကျသွားပါသည်!**\n\nနောက်တစ်ပွဲ ပြန်ကြိုးစားကြည့်ပါ။"
                keyboard = create_board_keyboard(board, game_id, game["theme"], game)
                await safe_edit(callback, text, keyboard)
                del games[game_id]
                return
                
            game["turn"] = game["creator"]["piece"] # လူအလှည့် ပြန်ပေး
            await safe_edit(callback, get_turn_text(game), create_board_keyboard(board, game_id, game["theme"], game))
    else:
        # PvP Mode: just update board for next player
        await safe_edit(callback, get_turn_text(game), create_board_keyboard(board, game_id, game["theme"], game))

# ==========================================
#            MAIN EXECUTION
# ==========================================
if __name__ == "__main__":
    # Flask server in background thread
    Thread(target=run_flask, daemon=True).start()
    # Start bot polling
    asyncio.run(dp.start_polling(bot))