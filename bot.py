import asyncio
import os
import json
import datetime
import base64
import httpx
import threading
from flask import Flask
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import CallbackQuery
import gspread
from oauth2client.service_account import ServiceAccountCredentials

BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENROUTER_KEY = os.getenv("OPENROUTER_KEY")
CREDENTIALS_FILE = "credentials.json"
SPREADSHEET_NAME = "Fuelbot"
MODEL_ID = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"

app = Flask(__name__)

@app.route('/')
@app.route('/health')
def health():
    return "Bot is running", 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds = ServiceAccountCredentials.from_json_keyfile_name(CREDENTIALS_FILE, scope)
sheets_client = gspread.authorize(creds)
sheet = sheets_client.open(SPREADSHEET_NAME).sheet1

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
temp_storage = {}

PROMPT = """
You are an OCR system. Extract data from the image and return ONLY JSON:
{
  "provider": "Hofmann or RHV",
  "card_number": "String",
  "mileage": "String"
}
"""

@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer("Send photo.")

# ВАЖНО: В группах бот будет реагировать на фото ТОЛЬКО если его упомянут или ответят на его сообщение
@dp.message(F.photo)
async def handle_photo(message: types.Message):
    # Проверка для групп: если это группа и бот не упомянут, игнорируем
    if message.chat.type in ["group", "supergroup"]:
        # Проверяем, есть ли упоминание бота в тексте сообщения (если есть подпись) или это ответ на сообщение бота
        is_mentioned = False
        if message.caption and f"@{bot.me.username}" in message.caption:
            is_mentioned = True
        if message.reply_to_message and message.reply_to_message.from_user.id == bot.id:
            is_mentioned = True
        
        if not is_mentioned:
            return  # Игнорируем фото в группе, если бот не упомянут

    msg = await message.answer("Loading...")
    
    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        file_path = f"temp_{photo.file_id}.jpg"
        await bot.download_file(file.file_path, file_path)
        
        with open(file_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode("utf-8")
        os.remove(file_path)

        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_KEY.strip()}", "Content-Type": "application/json"},
                json={
                    "model": MODEL_ID,
                    "max_tokens": 500,
                    "messages": [{"role": "user", "content": [{"type": "text", "text": PROMPT}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}}]}]
                },
                timeout=30.0
            )
        
        if response.status_code != 200:
            raise Exception(f"HTTP {response.status_code}: {response.text}")
            
        response_json = response.json()
        if "error" in response_json:
            raise Exception(f"API Error: {response_json['error']}")
        if "choices" not in response_json:
            raise Exception(f"Unexpected response: {response_json}")

        raw_text = response_json['choices'][0]['message']['content']
        if "```json" in raw_text:
            raw_text = raw_text.split("```json")[1].split("```")[0].strip()
        elif "```" in raw_text:
            raw_text = raw_text.split("```")[1].strip()
            
        data = json.loads(raw_text)
        check_text = f"Card: {data.get('provider')}\nNumber: {data.get('card_number')}\nMileage: {data.get('mileage')}\nOK?"
        
        nickname = f"@{message.from_user.username}" if message.from_user.username else message.from_user.full_name
        nickname = nickname.encode('ascii', 'ignore').decode('ascii')
        now = datetime.datetime.now()
        
        temp_storage[msg.message_id] = {"nick": nickname, "date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M:%S"), "data": data}

        builder = InlineKeyboardBuilder()
        builder.button(text="YES", callback_data=f"confirm_{msg.message_id}")
        builder.button(text="NO", callback_data=f"reject_{msg.message_id}")
        builder.adjust(2)

        await msg.edit_text(check_text, reply_markup=builder.as_markup())
    except Exception as e:
        try: await msg.edit_text(f"Error: {str(e)[:1000]}")
        except: pass

@dp.callback_query(F.data.startswith("confirm_"))
async def process_confirm(callback: CallbackQuery):
    msg_id = int(callback.data.split("_")[1])
    if msg_id not in temp_storage:
        await callback.answer("Expired", show_alert=True)
        return
    entry = temp_storage.pop(msg_id)
    d = entry["data"]
    row = [entry["date"], entry["time"], entry["nick"], d.get("provider", "N/A"), d.get("card_number", "N/A"), d.get("mileage", "N/A")]
    sheet.append_row(row)
    await callback.message.edit_text("Saved to table!")
    await callback.answer()

@dp.callback_query(F.data.startswith("reject_"))
async def process_reject(callback: CallbackQuery):
    msg_id = int(callback.data.split("_")[1])
    if msg_id in temp_storage: temp_storage.pop(msg_id)
    await callback.message.edit_text("Canceled. Send photo again.")
    await callback.answer()

async def main():
    threading.Thread(target=run_flask, daemon=True).start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
