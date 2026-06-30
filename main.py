import logging
import asyncio
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

# Setup Logging
logging.basicConfig(level=logging.INFO)

# Token ကို ဒီမှာ ထည့်ပါ (Best Practice: .env သုံးပါ)
TOKEN = "YOUR_BOT_TOKEN_HERE"
bot = Bot(token=TOKEN)
dp = Dispatcher()

# Game State Storage
games = {}

def create_board_keyboard(board, game_id):
    """4x4 Grid ကို ဖန်တီးပေးသည့် Function"""
    keyboard = []
    for r in range(4):
        row = []
        for c in range(4):
            # နေရာလွတ်ကို ' ' သို့မဟုတ် သင်္ကေတဖြင့် ပြသ
            symbol = board[r][c] if board[r][c] != ' ' else " "
            row.append(InlineKeyboardButton(text=symbol, callback_data=f"move_{game_id}_{r}_{c}"))
        keyboard.append(row)
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def check_winner(board, player):
    """4-in-a-row အနိုင်ရမရ စစ်ဆေးခြင်း"""
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

@dp.message(Command("start"))
async def start_game(message: types.Message):
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
    
    # Opponent Piece သတ်မှတ်
    opp_piece = 'O' if choice == 'X' else 'X'
    
    # Waiting for Opponent
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Join Game", callback_data="join_game")],
        [InlineKeyboardButton(text="End Game", callback_data="end_game")]
    ])
    await callback.message.edit_text(f"သင် {choice} ကို ရွေးလိုက်ပါပြီ။ Player တယောက်ကို စောင့်နေပါတယ်...", reply_markup=keyboard)

@dp.callback_query(F.data == "join_game")
async def join_game(callback: types.CallbackQuery):
    game_id = callback.message.chat.id
    creator_piece = games[game_id]["creator"]["piece"]
    opp_piece = 'O' if creator_piece == 'X' else 'X'
    
    games[game_id]["opponent"] = {"id": callback.from_user.id, "name": callback.from_user.first_name, "piece": opp_piece}
    games[game_id]["status"] = "playing"
    
    await callback.message.edit_text(f"ဂိမ်းစပါပြီ! {games[game_id]['creator']['name']} ( {creator_piece} ) VS {games[game_id]['opponent']['name']} ( {opp_piece} )", 
                                     reply_markup=create_board_keyboard(games[game_id]["board"], game_id))

@dp.callback_query(F.data.startswith("move_"))
async def handle_move(callback: types.CallbackQuery):
    _, game_id, r, c = callback.data.split("_")
    r, c = int(r), int(c)
    game_id = int(game_id)
    
    game = games[game_id]
    user_id = callback.from_user.id
    
    # Turn Check
    current_piece = 'X' if game["turn"] == 'X' else 'O'
    if (user_id == game["creator"]["id"] and game["creator"]["piece"] != current_piece) or \
       (user_id == game["opponent"]["id"] and game["opponent"]["piece"] != current_piece):
        await callback.answer("သင့်အလှည့်မဟုတ်သေးပါ!")
        return

    if game["board"][r][c] == ' ':
        game["board"][r][c] = current_piece
        
        # Win Check
        if check_winner(game["board"], current_piece):
            winner = game["creator"]["name"] if game["creator"]["piece"] == current_piece else game["opponent"]["name"]
            loser = game["opponent"]["name"] if game["creator"]["piece"] == current_piece else game["creator"]["name"]
            await callback.message.edit_text(f"ဂိမ်းပြီးဆုံးပါပြီ!\n\n🏆 {winner}\n😭 {loser}")
            del games[game_id]
            return
            
        # Draw Check
        if all(cell != ' ' for row in game["board"] for cell in row):
            await callback.message.edit_text("🤝 ဂိမ်း သရေကျသွားပါပြီ!")
            del games[game_id]
            return

        # Switch Turn
        game["turn"] = 'O' if current_piece == 'X' else 'X'
        await callback.message.edit_reply_markup(reply_markup=create_board_keyboard(game["board"], game_id))
    else:
        await callback.answer("ဒီနေရာမှာ ဆော့ပြီးသွားပါပြီ!")

@dp.callback_query(F.data == "end_game")
async def end_game(callback: types.CallbackQuery):
    game_id = callback.message.chat.id
    if callback.from_user.id == games[game_id]["creator"]["id"]:
        del games[game_id]
        await callback.message.edit_text("ဂိမ်းကို ပယ်ဖျက်လိုက်ပါပြီ။")
    else:
        await callback.answer("Creator သာ ဂိမ်းကို ပိတ်နိုင်ပါတယ်!")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

