#python
import asyncio
import os
import logging
import secrets
import time
import random
import math
import aiosqlite
from functools import wraps
from threading import Thread
from typing import Set, Dict, Any, Optional, List, Tuple

from flask import Flask
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InlineQueryResultArticle,
    InputTextMessageContent,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeChat
)
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter

# ==========================================
#         LOGGING & CONFIGURATION
# ==========================================
# Root/general logger -> bot.log (+ console)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

# Dedicated error.log - captures ERROR+ from ANY logger (root + children),
# since child loggers propagate up to the root handlers by default.
_error_handler = logging.FileHandler("error.log", encoding="utf-8")
_error_handler.setLevel(logging.ERROR)
_error_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(name)s - %(message)s"))
logging.getLogger().addHandler(_error_handler)

# Dedicated broadcast.log - only broadcast-related events.
broadcast_logger = logging.getLogger("broadcast")
broadcast_logger.setLevel(logging.INFO)
_broadcast_handler = logging.FileHandler("broadcast.log", encoding="utf-8")
_broadcast_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
broadcast_logger.addHandler(_broadcast_handler)
# broadcast_logger.propagate stays True (default) so broadcast entries also
# land in bot.log, and broadcast ERRORs also land in error.log automatically.

# Dedicated logger for database errors (also propagates to bot.log/error.log)
db_logger = logging.getLogger("database")
db_logger.setLevel(logging.INFO)

TOKEN: Optional[str] = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN missing in Environment Variables.")

ADMIN_IDS: List[int] = [int(x) for x in os.getenv("ADMIN_IDS", "7679480147").split(",") if x.strip()]

bot: Bot = Bot(token=TOKEN)
dp: Dispatcher = Dispatcher()

# ==========================================
#          DATABASE MANAGER LAYER
# ==========================================
class DatabaseManager:
    """
    Uses a single persistent aiosqlite connection (instead of opening/closing
    a new connection on every call) to reduce connection overhead. aiosqlite
    serializes access internally via its own worker thread, but a lock is
    still used around multi-statement writes for extra safety.
    """

    def __init__(self, db_file="database.db"):
        self.db_file = db_file
        self._conn: Optional[aiosqlite.Connection] = None
        self._write_lock: Optional[asyncio.Lock] = None

    async def _get_write_lock(self) -> asyncio.Lock:
        if self._write_lock is None:
            self._write_lock = asyncio.Lock()
        return self._write_lock

    async def connect(self) -> aiosqlite.Connection:
        if self._conn is None:
            self._conn = await aiosqlite.connect(self.db_file)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    async def init_db(self) -> None:
        try:
            conn = await self.connect()
            await conn.execute("""
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
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS groups (
                    chat_id TEXT PRIMARY KEY,
                    title TEXT,
                    type TEXT,
                    last_seen TEXT
                )
            """)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS broadcast_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_id TEXT,
                    total_target INTEGER,
                    success_count INTEGER,
                    failed_count INTEGER,
                    created_at TEXT
                )
            """)

            # --- Indexes for faster lookups on frequently queried columns ---
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_wins ON users(wins DESC)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_groups_type ON groups(type)")

            await conn.commit()
            db_logger.info("Database initialized successfully (tables + indexes ready).")
        except Exception as e:
            db_logger.error(f"Database init failed: {e}")
            raise

    async def register_user(self, user_id: int, username: Optional[str], first_name: str) -> None:
        try:
            conn = await self.connect()
            lock = await self._get_write_lock()
            async with lock:
                await conn.execute("""
                    INSERT INTO users (user_id, username, first_name, last_seen)
                    VALUES (?, ?, ?, datetime('now'))
                    ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_seen = datetime('now')
                """, (str(user_id), username, first_name))
                await conn.commit()
        except Exception as e:
            db_logger.error(f"register_user failed for {user_id}: {e}")

    async def update_stats(self, user_id: int, result: str) -> None:
        try:
            conn = await self.connect()
            lock = await self._get_write_lock()
            async with lock:
                if result == "win":
                    await conn.execute("""
                        UPDATE users SET wins = wins + 1, games_played = games_played + 1,
                        win_streak = win_streak + 1, coins = coins + 20 WHERE user_id = ?
                    """, (str(user_id),))
                elif result == "loss":
                    await conn.execute("""
                        UPDATE users SET losses = losses + 1, games_played = games_played + 1,
                        win_streak = 0 WHERE user_id = ?
                    """, (str(user_id),))
                elif result == "draw":
                    await conn.execute("""
                        UPDATE users SET draws = draws + 1, games_played = games_played + 1,
                        coins = coins + 5 WHERE user_id = ?
                    """, (str(user_id),))
                await conn.commit()
        except Exception as e:
            db_logger.error(f"update_stats failed for {user_id} ({result}): {e}")

    async def update_coins(self, user_id: int, amount: int) -> None:
        """ဒင်္ဂါးပြား ပေါင်းခြင်း သို့မဟုတ် နှုတ်ခြင်း"""
        try:
            conn = await self.connect()
            lock = await self._get_write_lock()
            async with lock:
                await conn.execute("UPDATE users SET coins = coins + ? WHERE user_id = ?", (amount, str(user_id)))
                await conn.commit()
        except Exception as e:
            db_logger.error(f"update_coins failed for {user_id} ({amount}): {e}")

    async def get_profile(self, user_id: int) -> Optional[Dict[str, Any]]:
        try:
            conn = await self.connect()
            async with conn.execute("SELECT * FROM users WHERE user_id = ?", (str(user_id),)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None
        except Exception as e:
            db_logger.error(f"get_profile failed for {user_id}: {e}")
            return None

    async def get_all_chats(self) -> List[str]:
        """User ID, Group ID နှင့် Channel ID အားလုံးကို Double မဖြစ်အောင် စုစည်းပေးမည့် စနစ်"""
        try:
            conn = await self.connect()
            async with conn.execute("SELECT user_id FROM users") as cursor:
                users = [row[0] async for row in cursor]
            async with conn.execute("SELECT chat_id FROM groups") as cursor:
                groups = [row[0] async for row in cursor]
            return list(set(users + groups))
        except Exception as e:
            db_logger.error(f"get_all_chats failed: {e}")
            return []

    async def get_leaderboard(self) -> List[tuple]:
        try:
            conn = await self.connect()
            async with conn.execute("SELECT first_name, wins FROM users ORDER BY wins DESC LIMIT 5") as cursor:
                return await cursor.fetchall()
        except Exception as e:
            db_logger.error(f"get_leaderboard failed: {e}")
            return []

    async def register_group(self, chat_id: str, title: str, chat_type: str):
        try:
            conn = await self.connect()
            lock = await self._get_write_lock()
            async with lock:
                await conn.execute("""
                    INSERT INTO groups (chat_id, title, type, last_seen)
                    VALUES (?, ?, ?, datetime('now'))
                    ON CONFLICT(chat_id) DO UPDATE SET
                        title = excluded.title,
                        type = excluded.type,
                        last_seen = datetime('now')
                """, (chat_id, title, chat_type))
                await conn.commit()
        except Exception as e:
            db_logger.error(f"register_group failed for {chat_id}: {e}")

    async def log_broadcast(self, admin_id: int, total: int, success: int, failed: int) -> None:
        try:
            conn = await self.connect()
            lock = await self._get_write_lock()
            async with lock:
                await conn.execute("""
                    INSERT INTO broadcast_history (admin_id, total_target, success_count, failed_count, created_at)
                    VALUES (?, ?, ?, ?, datetime('now'))
                """, (str(admin_id), total, success, failed))
                await conn.commit()
        except Exception as e:
            db_logger.error(f"log_broadcast failed: {e}")

db = DatabaseManager()

# ==========================================
#         GAME MANAGER (MEMORY LEAK PREVENTER)
# ==========================================
class GameManager:
    def __init__(self):
        self.games: Dict[str, Any] = {}
        self.lock = None # Event loop စတင်ပြီးမှသာ Lock ကို တည်ဆောက်မည်

    async def get_lock(self) -> asyncio.Lock:
        if self.lock is None:
            self.lock = asyncio.Lock()
        return self.lock

    def create_game(self, game_id: str, game_data: dict):
        game_data['last_active'] = time.time()
        self.games[game_id] = game_data

    def update_activity(self, game_id: str):
        if game_id in self.games:
            self.games[game_id]['last_active'] = time.time()

    async def cleanup_inactive_games(self):
        """၁ နာရီကျော်ကြာ Inactive ဖြစ်နေသော Game များကို ဖျက်ပစ်မည်"""
        while True:
            await asyncio.sleep(3600)
            now = time.time()
            lock = await self.get_lock()
            async with lock:
                to_delete = [k for k, v in self.games.items() if now - v.get('last_active', now) > 3600]
                for k in to_delete:
                    del self.games[k]
                if to_delete:
                    logging.info(f"Cleaned up {len(to_delete)} inactive games to prevent memory leak.")

gm = GameManager()

# ==========================================
#          FLASK SERVER ARCHITECTURE
# ==========================================
app: Flask = Flask(__name__)
PORT: int = int(os.getenv("PORT", "10000"))

@app.route('/')
def home() -> str:
    return "Bot Core Analytics Endpoint Active."

def run_flask() -> None:
    app.run(host='0.0.0.0', port=PORT, use_reloader=False)

# ==========================================
#             UTILITY FUNCTIONS
# ==========================================
def escape_md(text: str) -> str:
    """
    Legacy Telegram Markdown escaping. Backslash is escaped FIRST so we don't
    end up double-escaping characters we just inserted.
    """
    if not text:
        return ""
    text = str(text)
    for char in ['\\', '_', '*', '`', '[', ']']:
        text = text.replace(char, f"\\{char}")
    return text

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

# ==========================================
#         SECURITY: COOLDOWN & VALIDATION
# ==========================================
_cooldown_cache: Dict[int, float] = {}

def cooldown(seconds: float = 2.0):
    """
    Decorator for message command handlers. Silently drops the update if the
    user is calling the command again before the cooldown window elapses,
    preventing spam / accidental double-submits.
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(message: types.Message, *args, **kwargs):
            user_id = message.from_user.id
            now = time.time()
            last = _cooldown_cache.get(user_id, 0)
            if now - last < seconds:
                return
            _cooldown_cache[user_id] = now
            return await func(message, *args, **kwargs)
        return wrapper
    return decorator

def is_game_participant(game: Dict[str, Any], user_id: int) -> bool:
    """Returns True only if user_id is the creator or opponent of this game."""
    if not game:
        return False
    return user_id in (game.get("creator", {}).get("id"), game.get("opponent", {}).get("id"))

# ==========================================
#             CORE GAME LOGIC & AI
# ==========================================
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

def _get_empty_cells(board: list) -> List[Tuple[int, int]]:
    return [(r, c) for r in range(4) for c in range(4) if board[r][c] == '']

def _get_all_lines(board: list) -> List[List[str]]:
    lines = []
    for i in range(4):
        lines.append([board[i][j] for j in range(4)])   # rows
        lines.append([board[j][i] for j in range(4)])   # columns
    lines.append([board[i][i] for i in range(4)])        # diagonal
    lines.append([board[i][3 - i] for i in range(4)])    # anti-diagonal
    return lines

def _evaluate_board(board: list, ai_piece: str, human_piece: str) -> int:
    """Heuristic score used when minimax reaches its depth cutoff."""
    score = 0
    line_weight = {0: 0, 1: 1, 2: 10, 3: 100, 4: 1000}
    for line in _get_all_lines(board):
        ai_count = line.count(ai_piece)
        human_count = line.count(human_piece)
        if human_count == 0:
            score += line_weight[ai_count]
        if ai_count == 0:
            score -= line_weight[human_count]
    return score

def _minimax(board: list, depth: int, alpha: float, beta: float,
             is_maximizing: bool, ai_piece: str, human_piece: str) -> int:
    if check_winner(board, ai_piece):
        return 10000 + depth
    if check_winner(board, human_piece):
        return -10000 - depth
    if depth == 0 or check_draw(board):
        return _evaluate_board(board, ai_piece, human_piece)

    if is_maximizing:
        best = -math.inf
        for r, c in _get_empty_cells(board):
            board[r][c] = ai_piece
            score = _minimax(board, depth - 1, alpha, beta, False, ai_piece, human_piece)
            board[r][c] = ''
            best = max(best, score)
            alpha = max(alpha, score)
            if beta <= alpha:
                break
        return best
    else:
        best = math.inf
        for r, c in _get_empty_cells(board):
            board[r][c] = human_piece
            score = _minimax(board, depth - 1, alpha, beta, True, ai_piece, human_piece)
            board[r][c] = ''
            best = min(best, score)
            beta = min(beta, score)
            if beta <= alpha:
                break
        return best

def get_ai_move_easy(board: list) -> tuple:
    """Easy: fully random legal move."""
    empty_cells = _get_empty_cells(board)
    return random.choice(empty_cells) if empty_cells else (0, 0)

def get_ai_move_medium(board: list, ai_piece: str = 'O', human_piece: str = 'X') -> tuple:
    """Medium: take a winning move, else block opponent, else center, else first open cell."""
    for r, c in _get_empty_cells(board):
        board[r][c] = ai_piece
        if check_winner(board, ai_piece):
            board[r][c] = ''
            return r, c
        board[r][c] = ''

    for r, c in _get_empty_cells(board):
        board[r][c] = human_piece
        if check_winner(board, human_piece):
            board[r][c] = ''
            return r, c
        board[r][c] = ''

    center_spots = [(1, 1), (1, 2), (2, 1), (2, 2)]
    for r, c in center_spots:
        if board[r][c] == '':
            return r, c

    empty_cells = _get_empty_cells(board)
    return empty_cells[0] if empty_cells else (0, 0)

def get_ai_move_hard(board: list, ai_piece: str = 'O', human_piece: str = 'X') -> tuple:
    """
    Hard: instant win/block check first (fast + guarantees no tactical blunder),
    then a depth-limited minimax with alpha-beta pruning. Depth adapts to how
    many cells remain empty so the search stays fast on a 4x4 board.
    """
    empty_cells = _get_empty_cells(board)
    if not empty_cells:
        return 0, 0

    # Immediate win
    for r, c in empty_cells:
        board[r][c] = ai_piece
        if check_winner(board, ai_piece):
            board[r][c] = ''
            return r, c
        board[r][c] = ''

    # Immediate block
    for r, c in empty_cells:
        board[r][c] = human_piece
        if check_winner(board, human_piece):
            board[r][c] = ''
            return r, c
        board[r][c] = ''

    total_empty = len(empty_cells)
    if total_empty > 13:
        depth = 2
    elif total_empty > 9:
        depth = 3
    elif total_empty > 5:
        depth = 4
    else:
        depth = 6

    best_score = -math.inf
    best_move = empty_cells[0]
    for r, c in empty_cells:
        board[r][c] = ai_piece
        score = _minimax(board, depth - 1, -math.inf, math.inf, False, ai_piece, human_piece)
        board[r][c] = ''
        if score > best_score:
            best_score = score
            best_move = (r, c)
    return best_move

def get_ai_move(board: list, difficulty: str = "hard") -> tuple:
    """
    Difficulty dispatcher. Kept backward compatible: calling get_ai_move(board)
    with no difficulty argument behaves like the old "smart defensive" AI
    (now mapped to the strongest / hard tier).
    """
    if difficulty == "easy":
        return get_ai_move_easy(board)
    elif difficulty == "medium":
        return get_ai_move_medium(board)
    return get_ai_move_hard(board)

def get_turn_text(game: Dict[str, Any]) -> str:
    current_piece = game["turn"]
    c_theme = game["theme"][current_piece]
    p1 = game["creator"]
    p2 = game["opponent"]
    
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
            display_text = theme.get(symbol, symbol) if symbol != '' else "➖"
            row.append(InlineKeyboardButton(text=display_text, callback_data=f"move_{game_id}_{r}_{c}"))
        keyboard.append(row)

    if not is_game_over:
        if game.get("status") == "playing" and len(game.get("moves", [])) >= 2:
            keyboard.append([InlineKeyboardButton(text="↩️ Undo (50 coins)", callback_data=f"undo_{game_id}")])
        if game.get("status") == "playing":
            keyboard.append([InlineKeyboardButton(text="🏳️ Leave Game (အရှုံးပေးရန်)", callback_data=f"leave_{game_id}")])
    else:
        play_again_btn = InlineKeyboardButton(text="🔄 ထပ်ကစားမည် (AI နှင့်)", callback_data="play_ai") if game["opponent"]["id"] == 0 else InlineKeyboardButton(text="🔄 ထပ်ကစားမည် (သူငယ်ချင်းနှင့်)", callback_data="play_pvp")
        keyboard.append([play_again_btn])
        keyboard.append([InlineKeyboardButton(text="❌ ပိတ်မည်", callback_data="close_message")])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# ==========================================
#      BROADCAST QUEUE + BACKGROUND WORKER
# ==========================================
class BroadcastManager:
    """
    Admin command -> Broadcast Queue -> Background Worker -> Telegram send

    A single background worker task consumes jobs from an asyncio.Queue, so
    the bot's main event loop / polling never freezes while a broadcast is
    in progress. Only one broadcast job is processed at a time; additional
    /broadcast calls simply queue up behind it.
    """

    def __init__(self):
        self.queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue()
        self.worker_task: Optional[asyncio.Task] = None
        self.is_running: bool = False
        self.cancel_requested: bool = False
        self.current_stats: Dict[str, Any] = {}

    def start_worker(self) -> None:
        if self.worker_task is None or self.worker_task.done():
            self.worker_task = asyncio.create_task(self._worker_loop())
            broadcast_logger.info("Broadcast background worker started.")

    async def enqueue(self, admin_id: int, chats: List[str], from_chat_id: int, msg_id: int) -> None:
        await self.queue.put({
            "admin_id": admin_id,
            "chats": chats,
            "from_chat_id": from_chat_id,
            "msg_id": msg_id
        })
        broadcast_logger.info(f"Broadcast job queued by admin {admin_id} for {len(chats)} targets.")
        self.start_worker()

    def cancel(self) -> bool:
        if self.is_running:
            self.cancel_requested = True
            return True
        return False

    def get_status(self) -> Optional[Dict[str, Any]]:
        if not self.is_running:
            return None
        return dict(self.current_stats)

    async def _worker_loop(self) -> None:
        while True:
            job = await self.queue.get()
            try:
                await self._process_job(job)
            except Exception as e:
                broadcast_logger.error(f"Broadcast worker crashed while processing a job: {e}")
            finally:
                self.queue.task_done()

    async def _process_job(self, job: Dict[str, Any]) -> None:
        admin_id = job["admin_id"]
        chats = job["chats"]
        from_chat_id = job["from_chat_id"]
        msg_id = job["msg_id"]

        self.is_running = True
        self.cancel_requested = False
        self.current_stats = {
            "admin_id": admin_id,
            "total": len(chats),
            "sent": 0,
            "failed": 0,
            "started_at": time.time(),
        }

        broadcast_logger.info(f"Broadcast STARTED by admin {admin_id} -> {len(chats)} targets.")
        try:
            await bot.send_message(admin_id, f"🚀 Chat အရေအတွက် ({len(chats)}) ဆီသို့ Broadcast စတင်ပို့ဆောင်နေပါပြီ...")
        except Exception as e:
            broadcast_logger.warning(f"Could not notify admin {admin_id} of broadcast start: {e}")

        for chat_id in chats:
            if self.cancel_requested:
                broadcast_logger.info(f"Broadcast CANCELLED by admin {admin_id} after "
                                       f"{self.current_stats['sent']} sent / {self.current_stats['failed']} failed.")
                break
            try:
                await bot.copy_message(chat_id=int(chat_id), from_chat_id=from_chat_id, message_id=msg_id)
                self.current_stats["sent"] += 1
                await asyncio.sleep(0.05)  # Telegram flood-limit safe delay
            except TelegramRetryAfter as e:
                broadcast_logger.warning(f"Flood control hit, sleeping {e.retry_after}s (target={chat_id}).")
                await asyncio.sleep(e.retry_after)
                try:
                    await bot.copy_message(chat_id=int(chat_id), from_chat_id=from_chat_id, message_id=msg_id)
                    self.current_stats["sent"] += 1
                except Exception as e2:
                    self.current_stats["failed"] += 1
                    broadcast_logger.warning(f"Failed to send to {chat_id} after retry: {e2}")
            except Exception as e:
                self.current_stats["failed"] += 1
                broadcast_logger.warning(f"Failed to send to {chat_id}: {e}")

        total = self.current_stats["total"]
        sent = self.current_stats["sent"]
        failed = self.current_stats["failed"]
        cancelled = self.cancel_requested

        self.is_running = False
        broadcast_logger.info(f"Broadcast FINISHED (admin={admin_id}) sent={sent} failed={failed} "
                               f"total={total} cancelled={cancelled}")

        status_line = "❌ **Broadcast ပယ်ဖျက်လိုက်ပါပြီ။**" if cancelled else "✅ **Broadcast ပြီးဆုံးပါပြီ။**"
        try:
            await bot.send_message(
                admin_id,
                f"{status_line}\n\nစုစုပေါင်း Target: {total}\nအောင်မြင်: {sent} ခု\nမအောင်မြင်: {failed} ခု",
                parse_mode="Markdown"
            )
        except Exception as e:
            broadcast_logger.warning(f"Could not notify admin {admin_id} of broadcast result: {e}")

        await db.log_broadcast(admin_id, total, sent, failed)
        self.current_stats = {}

broadcast_manager = BroadcastManager()


# ==========================================
#         TELEGRAM COMMAND HANDLERS
# ==========================================
def _welcome_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 Play with AI (AI ဖြင့်ဆော့မည်)", callback_data="ai_menu")],
        [InlineKeyboardButton(text="👥 Play with Friend (သူငယ်ချင်းနှင့်ဆော့မည်)", callback_data="play_pvp")],
        [InlineKeyboardButton(text="👤 My Profile (ပရိုဖိုင်)", callback_data="profile")],
        [InlineKeyboardButton(text="❌ ပိတ်မည်", callback_data="close_message")]
    ])

@dp.message(Command("start"))
@cooldown(2.0)
async def cmd_start(message: types.Message):
    await db.register_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    
    welcome_text = (
        f"👋 မင်္ဂလာပါ {escape_md(message.from_user.first_name)}!\n\n"
        f"🎮 4x4 Tic-Tac-Toe ဂိမ်း Bot မှ ကြိုဆိုပါတယ်။\n"
        f"အောက်ပါ ခလုတ်ကိုနှိပ်ပြီး AI နဲ့ဖြစ်စေ၊ သူငယ်ချင်းနဲ့ဖြစ်စေ ယှဉ်ပြိုင်ကစားနိုင်ပါပြီ။"
    )
    
    await message.answer(welcome_text, reply_markup=_welcome_keyboard(), parse_mode="Markdown")

@dp.channel_post()
@dp.message(F.chat.type.in_({"group", "supergroup", "channel"}))
async def register_group_and_channel(message: types.Message):
    chat_id = str(message.chat.id)
    title = message.chat.title or "Unknown"
    chat_type = message.chat.type
    await db.register_group(chat_id, title, chat_type)

@dp.my_chat_member()
async def on_bot_membership_change(event: types.ChatMemberUpdated):
    """
    Fires the instant the bot's own membership status changes in a chat
    (added to a group/channel, promoted, kicked, etc). Unlike the message
    handler above, this event reaches the bot regardless of Privacy Mode,
    so groups/channels get registered immediately on add instead of waiting
    for someone to send the first message.
    """
    chat = event.chat
    new_status = event.new_chat_member.status
    if chat.type not in ("group", "supergroup", "channel"):
        return

    if new_status in ("member", "administrator"):
        await db.register_group(str(chat.id), chat.title or "Unknown", chat.type)
        logging.info(f"Bot added to {chat.type} '{chat.title}' ({chat.id}) — registered.")
    elif new_status in ("left", "kicked"):
        logging.info(f"Bot removed from {chat.type} '{chat.title}' ({chat.id}).")


@dp.message(Command("leaderboard"))
@cooldown(2.0)
async def cmd_leaderboard(message: types.Message):
    data = await db.get_leaderboard()
    text = "🏆 **Top 5 Players (Leaderboard)** 🏆\n\n"
    for i, (name, wins) in enumerate(data, 1):
        text += f"{i}. {escape_md(name)} - {wins} wins\n"
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("help"))
@cooldown(2.0)
async def cmd_help(message: types.Message):
    is_admin = message.from_user.id in ADMIN_IDS

    text = (
        "📖 **Bot သုံးနည်း — Command List**\n\n"
        "🎮 **အဓိက Command များ**\n"
        "/start — Main Menu ဖွင့်ရန် (AI/PvP ရွေးရန်)\n"
        "/leaderboard — Top 5 ကစားသမား စာရင်း\n"
        "/help — ဒီ command list ကို ပြန်ကြည့်ရန်\n\n"
        "🕹 **ဂိမ်းကစားနည်း**\n"
        "• 🎮 Play with AI — Difficulty (Easy / Medium / Hard) ရွေးပြီး AI နှင့်ကစားနိုင်ပါသည်\n"
        "• 👥 Play with Friend — သူငယ်ချင်းနှင့် PvP ဂိမ်းဖန်တီးပြီး 'Join Game' ဖြင့်ဝင်ကစားနိုင်ပါသည်\n"
        "• 👤 My Profile — နိုင်/ရှုံး/သရေ/ဒင်္ဂါးပြား စသည့် မှတ်တမ်းများ ကြည့်ရှုနိုင်ပါသည်\n"
        "• ↩️ Undo — ဒင်္ဂါးပြား 50 ဖြင့် လှုပ်ရှားမှု နောက်ဆုတ်နိုင်ပါသည်\n"
        "• 🏳️ Leave Game — ကစားနေသော ဂိမ်းမှ အရှုံးပေး ထွက်ခွာနိုင်ပါသည်\n"
        "• Inline Mode — Chat မည်သည့်နေရာမဆို Bot Username ကို @ ခေါ်ပြီး `play` ရိုက်လျှင် သူငယ်ချင်းကို တိုက်ရိုက်ဖိတ်ခေါ်နိုင်ပါသည်\n"
    )

    if is_admin:
        text += (
            "\n🛠 **Admin Command များ (Admin သာ သုံးနိုင်)**\n"
            "/broadcast — Reply ပြန်ထားသော message ကို Chat/Group အားလုံးသို့ ပို့ရန်\n"
            "/broadcast_status — လက်ရှိ Broadcast ၏ progress (sent/failed) ကြည့်ရန်\n"
            "/broadcast_cancel — လုပ်ဆောင်ဆဲ Broadcast ကို ရပ်တန့်ရန်\n"
        )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Main Menu", callback_data="back_to_menu")]
    ])
    await message.answer(text, reply_markup=keyboard, parse_mode="Markdown")

@dp.message(Command("broadcast"))
@cooldown(5.0)
async def cmd_broadcast(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.reply("ဤ command ကို Admin သာ အသုံးပြုနိုင်ပါသည်။")
        return
        
    if not message.reply_to_message:
        await message.reply("⚠️ ကျေးဇူးပြု၍ သင် Broadcast လုပ်လိုသော စာ၊ ပုံ (သို့) ဗီဒီယိုကို **Reply** ပြန်ပြီး `/broadcast` ဟု ရိုက်ပါ။", parse_mode="Markdown")
        return

    chats = await db.get_all_chats()
    # Pushed onto the asyncio.Queue-backed broadcast worker so the bot never freezes.
    await broadcast_manager.enqueue(message.from_user.id, chats, message.chat.id, message.reply_to_message.message_id)
    await message.reply(f"📥 Broadcast Queue ထဲသို့ ထည့်သွင်းပြီးပါပြီ။ (Target: {len(chats)})\n`/broadcast_status` ဖြင့် တိုးတက်မှုကို စစ်ဆေးနိုင်ပါသည်။", parse_mode="Markdown")


@dp.message(Command("broadcast_status"))
async def cmd_broadcast_status(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.reply("ဤ command ကို Admin သာ အသုံးပြုနိုင်ပါသည်။")
        return

    status = broadcast_manager.get_status()
    if not status:
        await message.reply("📊 **Broadcast Status**\n\n🔴 လက်ရှိ Broadcast လုပ်ဆောင်နေခြင်း မရှိပါ။", parse_mode="Markdown")
        return

    elapsed = int(time.time() - status["started_at"])
    text = (
        f"📊 **Broadcast Status**\n\n"
        f"🟢 Running: Yes\n"
        f"🎯 Total Targets: {status['total']}\n"
        f"✅ Sent: {status['sent']}\n"
        f"❌ Failed: {status['failed']}\n"
        f"⏱ Elapsed: {elapsed}s"
    )
    await message.reply(text, parse_mode="Markdown")


@dp.message(Command("broadcast_cancel"))
async def cmd_broadcast_cancel(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.reply("ဤ command ကို Admin သာ အသုံးပြုနိုင်ပါသည်။")
        return

    cancelled = broadcast_manager.cancel()
    if cancelled:
        await message.reply("🛑 Broadcast ကို ပယ်ဖျက်ရန် တောင်းဆိုလိုက်ပါပြီ (လက်ရှိပို့ဆဲ chat ပြီးဆုံးပြီးနောက် ရပ်တန့်သွားပါမည်)။")
    else:
        await message.reply("⚠️ လက်ရှိ Broadcast လုပ်ဆောင်နေခြင်း မရှိပါ။ ပယ်ဖျက်စရာမရှိပါ။")


# ==========================================
#            CALLBACK HANDLERS
# ==========================================
@dp.callback_query(F.data == "profile")
async def profile_callback(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    profile = await db.get_profile(user_id)
    
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
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 နောက်သို့", callback_data="back_to_menu")],
        [InlineKeyboardButton(text="❌ ပိတ်မည်", callback_data="close_message")]
    ])
    await safe_edit(callback, text, keyboard)

@dp.callback_query(F.data == "back_to_menu")
async def back_to_menu_callback(callback: types.CallbackQuery):
    welcome_text = (
        f"👋 မင်္ဂလာပါ {escape_md(callback.from_user.first_name)}!\n\n"
        f"🎮 4x4 Tic-Tac-Toe ဂိမ်း Bot မှ ကြိုဆိုပါတယ်။\n"
        f"အောက်ပါ ခလုတ်ကိုနှိပ်ပြီး AI နဲ့ဖြစ်စေ၊ သူငယ်ချင်းနဲ့ဖြစ်စေ ယှဉ်ပြိုင်ကစားနိုင်ပါပြီ။"
    )
    await safe_edit(callback, welcome_text, _welcome_keyboard())

@dp.callback_query(F.data == "close_message")
async def close_message_callback(callback: types.CallbackQuery):
    try:
        if callback.inline_message_id:
            await callback.bot.edit_message_text(
                text="❌ ကစားပွဲကို ပိတ်လိုက်ပါပြီ။", 
                inline_message_id=callback.inline_message_id,
                reply_markup=None
            )
        else:
            await callback.message.delete()
    except Exception as e:
        logging.error(f"Error deleting message: {e}")
        await safe_edit(callback, "❌ ကစားပွဲကို ပိတ်လိုက်ပါပြီ။", None)

@dp.callback_query(F.data == "ai_menu")
async def ai_menu_callback(callback: types.CallbackQuery):
    text = (
        "🤖 **AI Difficulty ရွေးချယ်ပါ**\n\n"
        "😌 Easy — ကျပန်း လှုပ်ရှားမှု\n"
        "⚖️ Medium — တိုက်ခိုက်/ကာကွယ် logic\n"
        "🔥 Hard — Minimax algorithm (အခက်ဆုံး)"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="😌 Easy", callback_data="play_ai_easy")],
        [InlineKeyboardButton(text="⚖️ Medium", callback_data="play_ai_medium")],
        [InlineKeyboardButton(text="🔥 Hard", callback_data="play_ai_hard")],
        [InlineKeyboardButton(text="🔙 နောက်သို့", callback_data="back_to_menu")]
    ])
    await safe_edit(callback, text, keyboard)

@dp.callback_query(F.data.in_({"play_pvp"}) | F.data.startswith("play_ai"))
async def start_game(callback: types.CallbackQuery):
    game_id = secrets.token_hex(4)
    board = [['' for _ in range(4)] for _ in range(4)]
    is_ai = callback.data.startswith("play_ai")

    # "play_ai" (no suffix) is kept for backward compatibility with the
    # "Play Again" button, and defaults to the hardest difficulty.
    difficulty = "hard"
    if callback.data == "play_ai_easy":
        difficulty = "easy"
    elif callback.data == "play_ai_medium":
        difficulty = "medium"

    difficulty_label = {"easy": "Easy 😌", "medium": "Medium ⚖️", "hard": "Hard 🔥"}[difficulty]
    
    lock = await gm.get_lock()
    async with lock:
        gm.create_game(game_id, {
            "board": board,
            "turn": "X",
            "theme": {"X": "❌", "O": "⭕"},
            "creator": {"id": callback.from_user.id, "name": callback.from_user.first_name, "piece": "X"},
            "opponent": {"id": 0 if is_ai else -1, "name": f"AI Bot ({difficulty_label})" if is_ai else "Waiting...", "piece": "O"},
            "status": "playing" if is_ai else "waiting",
            "moves": [],
            "ai_difficulty": difficulty
        })
    
    game = gm.games[game_id]
    text = get_turn_text(game)
    
    if is_ai:
        keyboard = create_board_keyboard(board, game_id, game["theme"], game)
    else:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎮 Join Game (ဝင်ကစားမည်)", callback_data=f"join_{game_id}")],
            [InlineKeyboardButton(text="❌ ပွဲပယ်ဖျက်မည်", callback_data=f"end_{game_id}")],
            [InlineKeyboardButton(text="🔗 Share to Friend (DM တွင်သူငယ်ချင်းကိုဖိတ်ရန်)", switch_inline_query="play")]
        ])
    await safe_edit(callback, text, keyboard)


@dp.callback_query(F.data.startswith("join_"))
async def join_pvp_game(callback: types.CallbackQuery):
    _, game_id = callback.data.split("_")
    user_id = callback.from_user.id
    
    lock = await gm.get_lock()
    async with lock:
        if game_id not in gm.games:
            await callback.answer("ဒီပွဲစဉ် ပျက်သွားပါပြီ။", show_alert=True)
            return
            
        game = gm.games[game_id]
        
        if game["status"] != "waiting":
            await callback.answer("ဒီပွဲမှာ လူပြည့်သွားပါပြီ။", show_alert=True)
            return
            
        if game["creator"]["id"] == user_id:
            await callback.answer("သင်က ပွဲဖန်တီးသူ ဖြစ်နေပါသည်။ အခြားသူကို စောင့်ပါ။", show_alert=True)
            return
            
        # အခြား Player ဝင်ရောက်လာခြင်း
        game["opponent"] = {"id": user_id, "name": callback.from_user.first_name, "piece": "O"}
        game["status"] = "playing"
        gm.update_activity(game_id)
        
        await db.register_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
        
        text = get_turn_text(game)
        keyboard = create_board_keyboard(game["board"], game_id, game["theme"], game)
        
    await safe_edit(callback, text, keyboard)
    await callback.answer("ဂိမ်းထဲသို့ အောင်မြင်စွာ ဝင်ရောက်ပြီးပါပြီ။")

@dp.callback_query(F.data.startswith("leave_"))
async def leave_game(callback: types.CallbackQuery):
    _, game_id = callback.data.split("_")
    user_id = callback.from_user.id
    lock = await gm.get_lock()
    async with lock:
        if game_id not in gm.games:
            return await callback.answer("ဂိမ်းမရှိတော့ပါ။", show_alert=True)

        game = gm.games[game_id]
        # Security: only participants of this exact game may leave it.
        if not is_game_participant(game, user_id):
            return await callback.answer("⚠️ သင်သည် ဒီဂိမ်းတွင် ပါဝင်နေသူ မဟုတ်ပါ။", show_alert=True)

        del gm.games[game_id]
    await callback.answer("သင်က ဂိမ်းမှ ထွက်ခွာသွားပါပြီ။", show_alert=True)
    await safe_edit(callback, "🚪 ဂိမ်း ပြီးဆုံးသွားပါပြီ။", None)

@dp.callback_query(F.data.startswith("undo_"))
async def undo_move(callback: types.CallbackQuery):
    _, game_id = callback.data.split("_", 1)
    user_id = callback.from_user.id

    lock = await gm.get_lock()
    async with lock:
        if game_id not in gm.games:
            return await callback.answer("ဂိမ်းမရှိတော့ပါ။", show_alert=True)

        game = gm.games[game_id]
        if game["status"] != "playing":
            return await callback.answer("ဂိမ်းက ကစားနေဆဲမဟုတ်ပါ။", show_alert=True)

        # Security: only participants of this exact game may undo.
        if not is_game_participant(game, user_id):
            return await callback.answer("⚠️ သင်သည် ဒီဂိမ်းတွင် ပါဝင်နေသူ မဟုတ်ပါ။", show_alert=True)

        moves = game.get("moves", [])
        if len(moves) < 2:
            return await callback.answer("နောက်ပြန်ဆုတ်ရန် လုံလောက်သော လှုပ်ရှားမှုမရှိသေးပါ။", show_alert=True)

        my_piece = game["creator"]["piece"] if game["creator"]["id"] == user_id else game["opponent"]["piece"]

        if game["turn"] != my_piece:
            return await callback.answer("သင်အလှည့်မဟုတ်သေးပါ။", show_alert=True)

        last_move_1 = moves[-1]
        last_move_2 = moves[-2]
        if last_move_1[0] == my_piece or last_move_2[0] != my_piece:
            return await callback.answer("လှုပ်ရှားမှု အချက်အလက် မကိုက်ညီပါ။", show_alert=True)

        profile = await db.get_profile(user_id)
        if not profile or profile["coins"] < 50:
            return await callback.answer("သင့်တွင် ဒင်္ဂါးပြား 50 မရှိပါ။", show_alert=True)

        await db.update_coins(user_id, -50)

        moves.pop()
        moves.pop()
        _, r1, c1 = last_move_1
        _, r2, c2 = last_move_2
        game["board"][r1][c1] = ''
        game["board"][r2][c2] = ''

        game["turn"] = my_piece
        gm.update_activity(game_id)

        text = get_turn_text(game)
        keyboard = create_board_keyboard(game["board"], game_id, game["theme"], game)

    await safe_edit(callback, text, keyboard)
    await callback.answer("✅ သင်နှင့် ပြိုင်ဘက်၏ လှုပ်ရှားမှုကို နောက်ဆုတ်လိုက်ပါပြီ။ (50 coins ကုန်ဆုံး)", show_alert=False)


@dp.callback_query(F.data.startswith("end_"))
async def end_game_callback(callback: types.CallbackQuery):
    _, game_id = callback.data.split("_")
    user_id = callback.from_user.id
    
    lock = await gm.get_lock()
    async with lock:
        if game_id in gm.games and gm.games[game_id]["creator"]["id"] == user_id and gm.games[game_id]["status"] == "waiting":
            del gm.games[game_id]
            await callback.answer("✅ ပွဲကို အောင်မြင်စွာ ပယ်ဖျက်လိုက်ပါပြီ။", show_alert=True)
            await safe_edit(callback, "❌ ပွဲကို ဖျက်လိုက်ပါပြီ။", None)
        else:
            await callback.answer("⚠️ ပွဲကို ဖျက်၍မရပါ (သို့) သင်သည် ပွဲဖန်တီးသူ မဟုတ်ပါ။", show_alert=True)


@dp.inline_query()
async def inline_game_handler(inline_query: types.InlineQuery):
    user_id = inline_query.from_user.id
    first_name = inline_query.from_user.first_name
    game_id = secrets.token_hex(4)
    
    board = [['' for _ in range(4)] for _ in range(4)]
    lock = await gm.get_lock()
    async with lock:
        gm.create_game(game_id, {
            "board": board,
            "turn": "X",
            "theme": {"X": "❌", "O": "⭕"},
            "creator": {"id": user_id, "name": first_name, "piece": "X"},
            "opponent": {"id": -1, "name": "Waiting...", "piece": "O"},
            "status": "waiting",
            "moves": []
        })
    
    text = (
        f"⚔️ **Tic-Tac-Toe 4x4 (DM Mode)** ⚔️\n\n"
        f"👤 ဖန်တီးသူ: {escape_md(first_name)} (❌)\n"
        f"⏳ ကစားဖော်အား စောင့်ဆိုင်းနေပါသည်...\n\n"
        f"ချက်တင်ထဲက သူငယ်ချင်းသည် အောက်ပါ 'Join Game' ကိုနှိပ်ပြီး တိုက်ရိုက်ဝင်ဆော့နိုင်ပါပြီ။"
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 Join Game (ဝင်ကစားမည်)", callback_data=f"join_{game_id}")]
    ])
    
    result = InlineQueryResultArticle(
        id=game_id,
        title="👥 Play 4x4 Tic-Tac-Toe Here (သူငယ်ချင်းနှင့် ဆော့မည်)",
        description="ဒီနေရာကိုနှိပ်ပြီး DM Chat / Group ထဲတွင် တိုက်ရိုက်ခေါ်ဆော့ပါ",
        input_message_content=InputTextMessageContent(message_text=text, parse_mode="Markdown"),
        reply_markup=keyboard
    )
    
    await inline_query.answer([result], cache_time=1, is_personal=True)


@dp.callback_query(F.data.startswith("move_"))
async def move_callback(callback: types.CallbackQuery):
    _, game_id, r, c = callback.data.split("_")
    r, c = int(r), int(c)
    user_id = callback.from_user.id
    
    lock = await gm.get_lock()
    async with lock:
        if game_id not in gm.games:
            return await callback.answer("ဒီပွဲစဉ် ပြီးဆုံးသွားပါပြီ။", show_alert=True)
            
        game = gm.games[game_id]
        
        if game["status"] == "waiting":
            return await callback.answer("ကစားဖော် မရှိသေးပါ။ တစ်ယောက်ယောက် ဝင်လာသည်အထိ စောင့်ပါ။", show_alert=True)

        current_piece = game["turn"]
        current_player = game["creator"] if game["creator"]["piece"] == current_piece else game["opponent"]
        
        if current_player["id"] != user_id:
            if user_id in [game["creator"]["id"], game["opponent"]["id"]]:
                await callback.answer("သင့်အလှည့် မရောက်သေးပါ။", show_alert=True)
            else:
                await callback.answer("သင်က ဒီပွဲကို ကစားနေသူ မဟုတ်ပါ။", show_alert=True)
            return
            
        board = game["board"]
        
        if board[r][c] != '':
            return await callback.answer("ဒီနေရာမှာ ချပြီးသားပါ။ တခြားနေရာ ရွေးပါ။", show_alert=True)
            
        board[r][c] = current_piece
        game["moves"].append((current_piece, r, c))
        gm.update_activity(game_id)
        
        if check_winner(board, current_piece):
            winner = current_player
            loser = game["opponent"] if current_player == game["creator"] else game["creator"]
            
            await db.update_stats(winner["id"], "win")
            if loser["id"] != 0: 
                await db.update_stats(loser["id"], "loss")
                
            text = f"🏆 **ဂုဏ်ယူပါတယ်! ပွဲပြီးဆုံးသွားပါပြီ!**\n\n👤 {escape_md(winner['name'])} မှ အနိုင်ရရှိသွားပါသည်။"
            
            keyboard = create_board_keyboard(board, game_id, game["theme"], game, is_game_over=True)
            await safe_edit(callback, text, keyboard)
            del gm.games[game_id]
            return
            
        if check_draw(board):
            await db.update_stats(game["creator"]["id"], "draw")
            if game["opponent"]["id"] != 0:
                await db.update_stats(game["opponent"]["id"], "draw")
                
            text = f"🤝 **သရေကျသွားပါသည်!**\n\nနောက်တစ်ပွဲ ပြန်ကြိုးစားကြည့်ပါ။"
            
            keyboard = create_board_keyboard(board, game_id, game["theme"], game, is_game_over=True)
            await safe_edit(callback, text, keyboard)
            del gm.games[game_id]
            return
            
        next_piece = "O" if current_piece == "X" else "X"
        game["turn"] = next_piece
        ai_mode = (game["opponent"]["id"] == 0)
        ai_difficulty = game.get("ai_difficulty", "hard")
    
    if ai_mode:
        await safe_edit(callback, get_turn_text(game), create_board_keyboard(board, game_id, game["theme"], game))
        await asyncio.sleep(0.5) # User ကို ခဏပေးမြင်စေရန်
        
        async with lock:
            if game_id not in gm.games:
                return
            game = gm.games[game_id]
            if game["status"] != "playing":
                return
            board = game["board"]
            
            # Minimax (hard mode) can be CPU-heavy, so it's run in a worker
            # thread via asyncio.to_thread to avoid blocking the event loop.
            ai_r, ai_c = await asyncio.to_thread(get_ai_move, board, ai_difficulty)
            board[ai_r][ai_c] = game["opponent"]["piece"]
            game["moves"].append((game["opponent"]["piece"], ai_r, ai_c))
            gm.update_activity(game_id)
            
            if check_winner(board, game["opponent"]["piece"]):
                await db.update_stats(game["creator"]["id"], "loss")
                text = f"💀 **ရှုံးသွားပါပြီ!**\n\n🤖 AI Bot မှ အနိုင်ရရှိသွားပါသည်။"
                
                keyboard = create_board_keyboard(board, game_id, game["theme"], game, is_game_over=True)
                await safe_edit(callback, text, keyboard)
                del gm.games[game_id]
                return
                
            if check_draw(board):
                await db.update_stats(game["creator"]["id"], "draw")
                text = f"🤝 **သရေကျသွားပါသည်!**\n\nနောက်တစ်ပွဲ ပြန်ကြိုးစားကြည့်ပါ။"
                
                keyboard = create_board_keyboard(board, game_id, game["theme"], game, is_game_over=True)
                await safe_edit(callback, text, keyboard)
                del gm.games[game_id]
                return
                
            game["turn"] = game["creator"]["piece"]
            await safe_edit(callback, get_turn_text(game), create_board_keyboard(board, game_id, game["theme"], game))
    else:
        await safe_edit(callback, get_turn_text(game), create_board_keyboard(board, game_id, game["theme"], game))

# ==========================================
#      TELEGRAM NATIVE COMMAND MENU (/)
# ==========================================
PUBLIC_COMMANDS: List[BotCommand] = [
    BotCommand(command="start", description="ဂိမ်း Main Menu ဖွင့်ရန်"),
    BotCommand(command="help", description="Command များ အကူအညီ"),
    BotCommand(command="leaderboard", description="Top 5 ကစားသမား စာရင်း"),
]

ADMIN_COMMANDS: List[BotCommand] = PUBLIC_COMMANDS + [
    BotCommand(command="broadcast", description="[Admin] Chat/Group အားလုံးသို့ စာပို့ရန်"),
    BotCommand(command="broadcast_status", description="[Admin] Broadcast progress ကြည့်ရန်"),
    BotCommand(command="broadcast_cancel", description="[Admin] Broadcast ပယ်ဖျက်ရန်"),
]

async def setup_bot_commands() -> None:
    """Registers the '/' command menu shown by Telegram clients."""
    try:
        await bot.set_my_commands(PUBLIC_COMMANDS, scope=BotCommandScopeDefault())
        for admin_id in ADMIN_IDS:
            try:
                await bot.set_my_commands(ADMIN_COMMANDS, scope=BotCommandScopeChat(chat_id=admin_id))
            except Exception as e:
                logging.warning(f"Could not set admin command menu for {admin_id}: {e}")
    except Exception as e:
        logging.error(f"Failed to set bot command menu: {e}")

# ==========================================
#            MAIN EXECUTION
# ==========================================
async def main():
    await db.init_db()
    
    # Memory Leak ကာကွယ်ရန် Background Worker အား ဖွင့်ထားခြင်း
    asyncio.create_task(gm.cleanup_inactive_games())

    # Broadcast queue background worker
    broadcast_manager.start_worker()

    # "/" ကိုနှိပ်ရင် ပေါ်လာမည့် command menu ကို register လုပ်ခြင်း
    await setup_bot_commands()
    
    logging.info("Bot is starting successfully...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    # Flask server အား သီးသန့် Thread တွင် ဖွင့်ထားခြင်း
    Thread(target=run_flask, daemon=True).start()
    asyncio.run(main())
