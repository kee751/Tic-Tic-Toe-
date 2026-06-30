import asyncio
import os
import logging
from threading import Thread
from flask import Flask
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# Setup Logging
logging.basicConfig(level=logging.INFO)

# Token ကို Environment Variable ကနေ ဖတ်ယူခြင်း
TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise ValueError("BOT_TOKEN ကို Render Environment Variable မှာ မတွေ့ရှိပါ။")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- [စနစ်သစ်] Database (users.txt) ဖြင့် User များကို အမြဲတမ်း မှတ်သားသည့်စနစ် ---
def load_users():
    if os.path.exists("users.txt"):
        with open("users.txt", "r") as f:
            return set(line.strip() for line in f)
    return set()

def save_user(user_id):
    with open("users.txt", "a") as f:
        f.write(f"{user_id}\n")

# Server ပွင့်တာနဲ့ users.txt ထဲက လူစာရင်းကို အလိုလို ဖတ်ယူပါမည်
user_ids = load_users()  

# Game State Storage
games = {}

# Flask Web Server အပိုင်း (UptimeRobot အတွက်)
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive!"

def run():
    app.run(host='0.0.0.0', port=10000)

# သူ့အလှည့်/ကိုယ့်အလှည့် ပြပေးမည့် စာသားဖန်တီးပေးသည့် လုပ်ဆောင်ချက်
def get_turn_text(game):
    current_piece = game["turn"]
    creator_name = game["creator"]["name"]
    creator_piece = game["creator"]["piece"]
    opponent_name = game["opponent"]["name"]
    opponent_piece = game["opponent"]["piece"]
    
    if creator_piece == current_piece:
        current_player = creator_name
    else:
        current_player = opponent_name
        
    return (
        f"🎮 **Tic-Tac-Toe 4x4 ပွဲစဉ်**\n\n"
        f"🔴 {creator_name} ({creator_piece})\n"
        f"🔵 {opponent_name} ({opponent_piece})\n\n"
        f"⏳ **ယခုအလှည့်:** {current_player} ( {current_piece} ) ရဲ့ အလှည့်ဖြစ်ပါတယ်ဗျာ။"
    )

def create_board_keyboard(board, game_id):
    keyboard = []
    for r in range(4):
        row = []
        for c in range(4):
            symbol = board[r][c] if board[r][c] != ' ' else " "
            row.append(InlineKeyboardButton(text=symbol, callback_data=f"move_{game_id}_{r}_{c}"))
        keyboard.append(row)
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def check_winner(board, player):
    # Rows & Columns
    for i in range(4):
        if all(board[i][j] == player for j in range(4)) or \
           all(board[j][i] == player for j in range(4)):
            return True
    # Diagonals
    if all(board[i][i] == player for i in range(4)) or \
       all(board[i][3-i] == player for i in range(4)):
        return True
    return False

# --- Chat ထဲတွင် /end ရိုက်ပြီး ဂိမ်းပိတ်သည့် စနစ် ---
@dp.message(Command("end"))
async def end_game_command(message: types.Message):
    game_id = message.chat.id
    if game_id in games:
        del games[game_id]
        await message.answer("🛑 လက်ရှိကစားနေတဲ့ ဂိမ်းကို ရပ်တန့်လိုက်ပါပြီ။\nဂိမ်းအသစ်ပြန်စရန် /start ကို နှိပ်ပါ။")
    else:
        await message.answer("❌ လောလောဆယ် ကစားနေတဲ့ ဂိမ်းမရှိသေးပါဘူး ခင်ဗျာ။")

# --- [စနစ်သစ်] Admin Broadcast (စာလှမ်းကြေညာခြင်း) စနစ် ---
@dp.message(Command("broadcast"))
async def broadcast_handler(message: types.Message):
    ADMIN_ID = 7679480147  
    
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ သင်က Admin မဟုတ်တဲ့အတွက် ဒီ Command ကို သုံးခွင့်မရှိပါဘူး။")
        return
        
    command_args = message.text.split(maxsplit=1)
    if len(command_args) < 2:
        await message.answer("❌ ကြေညာမယ့် စာသားထည့်ပေးပါ။\nပုံစံ: `/broadcast သတင်းအသစ်တက်လာပါပြီ`")
        return
        
    text_to_send = command_args[1]
    success_count = 0
    
    for uid in list(user_ids):
        try:
            await bot.send_message(chat_id=int(uid), text=text_to_send)
            success_count += 1
        except Exception:
            pass  # User က Bot ကို Block ထားရင် ကျော်သွားမည်
            
    await message.answer(
        f"📢 **Broadcast ပို့ခြင်း အောင်မြင်ပါတယ်ဗျာ!**\n\n"
        f"👥 စုစုပေါင်း လူ **{success_count}** ယောက်ဆီ စာသားများ ပို့ဆောင်ပေးခဲ့ပြီးပါပြီ။"
    )

@dp.message(Command("start"))
async def start_game(message: types.Message):
    # --- [ပြင်ဆင်ချက်] User တွေကို Database ထဲ သိမ်းမည့်အပိုင်း ---
    user_id_str = str(message.chat.id)
    if user_id_str not in user_ids:
        save_user(user_id_str)      # users.txt ထဲ ရေးထည့်မည်
        user_ids.add(user_id_str)   # RAM ထဲ မှတ်ထားမည်

    game_id = message.chat.id
    games[game_id] = {
        "board": [[' ' for _ in range(4)] for _ in range(4)],
        "creator": {"id": message.from_user.id, "name": message.from_user.first_name, "piece": None},
        "opponent": None,
        "turn": 'X',
        "status": "choosing"
    }
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="I want X", callback_data="pick_X"),
         InlineKeyboardButton(text="I want O", callback_data="pick_O")],
        [InlineKeyboardButton(text="End Game", callback_data="end_game")]
    ])
    await message.answer("Tic-Tac-Toe 4x4 ကို ကြိုဆိုပါတယ်။ သင် ဘာကို ရွေးမလဲ?", reply_markup=keyboard)

@dp.callback_query(F.data.startswith("pick_"))
async def pick_piece(callback: types.CallbackQuery):
    game_id = callback.message.chat.id
    if game_id not in games: return

    choice = callback.data.split("_")[1]
    games[game_id]["creator"]["piece"] = choice
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Join Game", callback_data="join_game")],
        [InlineKeyboardButton(text="End Game", callback_data="end_game")]
    ])
    await callback.message.edit_text(f"သင် {choice} ကို ရွေးလိုက်ပါပြီ။ Player တယောက်ကို စောင့်နေပါတယ်...", reply_markup=keyboard)

@dp.callback_query(F.data == "join_game")
async def join_game(callback: types.CallbackQuery):
    game_id = callback.message.chat.id
    if game_id not in games: return
    
    creator_piece = games[game_id]["creator"]["piece"]
    opp_piece = 'O' if creator_piece == 'X' else 'X'
    
    games[game_id]["opponent"] = {"id": callback.from_user.id, "name": callback.from_user.first_name, "piece": opp_piece}
    games[game_id]["status"] = "playing"
    
    text = get_turn_text(games[game_id])
    await callback.message.edit_text(text, reply_markup=create_board_keyboard(games[game_id]["board"], game_id))

@dp.callback_query(F.data.startswith("move_"))
async def handle_move(callback: types.CallbackQuery):
    _, game_id, r, c = callback.data.split("_")
    r, c = int(r), int(c)
    game_id = int(game_id)
    
    if game_id not in games: return
    game = games[game_id]
    user_id = callback.from_user.id
    
    if user_id != game["creator"]["id"] and user_id != game["opponent"]["id"]:
        await callback.answer("သင်က ဒီပွဲမှာ ပါဝင်သူမဟုတ်ပါဘူး!")
        return
        
    current_piece = 'X' if game["turn"] == 'X' else 'O'
    if (user_id == game["creator"]["id"] and game["creator"]["piece"] != current_piece) or \
       (user_id == game["opponent"]["id"] and game["opponent"]["piece"] != current_piece):
        await callback.answer("သင့်အလှည့်မဟုတ်သေးပါ!")
        return

    if game["board"][r][c] == ' ':
        game["board"][r][c] = current_piece
        
        if check_winner(game["board"], current_piece):
            winner = game["creator"]["name"] if game["creator"]["piece"] == current_piece else game["opponent"]["name"]
            loser = game["opponent"]["name"] if game["creator"]["piece"] == current_piece else game["creator"]["name"]
            await callback.message.edit_text(f"ဂိမ်းပြီးဆုံးပါပြီ!\n\n🏆 အနိုင်ရရှိသူ: {winner}\n😭 အရှုံးရရှိသူ: {loser}")
            del games[game_id]
            return
            
        if all(cell != ' ' for row in game["board"] for cell in row):
            await callback.message.edit_text("🤝 ဂိမ်း သရေကျသွားပါပြီ!")
            del games[game_id]
            return

        game["turn"] = 'O' if current_piece == 'X' else 'X'
        
        text = get_turn_text(game)
        await callback.message.edit_text(text, reply_markup=create_board_keyboard(game["board"], game_id))
    else:
        await callback.answer("ဒီနေရာမှာ ဆော့ပြီးသွားပါပြီ!")

@dp.callback_query(F.data == "end_game")
async def end_game(callback: types.CallbackQuery):
    game_id = callback.message.chat.id
    if game_id not in games: return
    
    if callback.from_user.id == games[game_id]["creator"]["id"]:
        del games[game_id]
        await callback.message.edit_text("ဂိမ်းကို ပယ်ဖျက်လိုက်ပါပြီ။")
    else:
        await callback.answer("Creator သာ ဂိမ်းကို ပိတ်နိုင်ပါတယ်!")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    t = Thread(target=run)
    t.start()
    
    asyncio.run(main())
