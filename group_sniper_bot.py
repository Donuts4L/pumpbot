
import asyncio, websockets, json, openai, requests, os
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

# Load environment variables
openai.api_key = os.getenv("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
AUTHORIZED_USER_ID = int(os.getenv("AUTHORIZED_USER_ID"))  # Your Telegram ID as int

active_tasks = {}

# Send message to a Telegram chat
def send_telegram_message(chat_id, text):
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={"chat_id": chat_id, "text": text}
    )

# Analyze a trade event with GPT
async def analyze_with_gpt(event, chat_id):
    token = event['params'].get('mint', 'UNKNOWN')[:6]
    prompt = f"""
You're a crypto sniper bot analyzing Pump.fun token trades.
Given this live trade event, tell me if it's bullish, bearish, or risky.

Event:
{json.dumps(event, indent=2)}
    """
    try:
        res = openai.ChatCompletion.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You're an expert Solana meme coin analyst."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=200,
            temperature=0.4
        )
        result = res["choices"][0]["message"]["content"]
        send_telegram_message(chat_id, f"📊 GPT Verdict on {token}...

{result}")
    except Exception as e:
        send_telegram_message(chat_id, f"❌ GPT error: {e}")

# WebSocket listener for a single token
async def listen_for_trade(ca, chat_id):
    uri = "wss://pumpportal.fun/api/data"
    try:
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({
                "method": "subscribeTokenTrade",
                "keys": [ca]
            }))
            async for msg in ws:
                data = json.loads(msg)
                if data.get("method") == "tokenTrade" and data['params']['mint'] == ca:
                    await analyze_with_gpt(data, chat_id)
                    break
    except Exception as e:
        send_telegram_message(chat_id, f"❌ WebSocket error for {ca}: {e}")
    finally:
        active_tasks.pop(ca, None)

# Telegram /analyze command handler
async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != AUTHORIZED_USER_ID:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="⛔ Not authorized.")
        return

    if not context.args:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Usage: /analyze <TOKEN_MINT>")
        return

    ca = context.args[0]
    chat_id = update.effective_chat.id

    if ca in active_tasks:
        await context.bot.send_message(chat_id=chat_id, text="⏳ Already analyzing this token...")
        return

    await context.bot.send_message(chat_id=chat_id, text=f"📡 Listening for trades on {ca}...")

    task = asyncio.create_task(listen_for_trade(ca, chat_id))
    active_tasks[ca] = task

# Main bot setup
def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("analyze", analyze_command))
    app.run_polling()

if __name__ == "__main__":
    main()
