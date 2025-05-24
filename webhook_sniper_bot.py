import os, json, asyncio, logging, httpx, openai
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
import websockets
from tenacity import retry, stop_after_attempt, wait_fixed

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()
active_tasks = {}

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

openai.api_key = OPENAI_API_KEY
application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info(f"→ {request.method} {request.url}")
    return await call_next(request)

@app.get("/")
async def root():
    return "OK"

async def send_telegram_message(chat_id, text):
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text}
            )
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")

@retry(stop=stop_after_attempt(3), wait=wait_fixed(2))
async def fetch_gpt_response(prompt):
    res = await openai.ChatCompletion.acreate(
        model="gpt-3.5-turbo",
        messages=[
            {"role": "system", "content": "You're an elite Solana meme coin analyst. Give blunt, tactical analysis only."},
            {"role": "user", "content": prompt}
        ],
        max_tokens=300,
        temperature=0.5
    )
    return res.choices[0].message.content

async def analyze_with_gpt(events, chat_id, duration):
    token = events[0].get('mint', 'UNKNOWN')[:6]
    prompt = f"""
You're a ruthless sniper bot. Analyze this meme coin's pump.fun trades over {duration} seconds.
Determine if it's bullish, bearish, or just trash. Be blunt and execution-focused.
Suggest a buy strategy (if any), stop loss range, and take profit range.

Recent Trades:
{json.dumps(events, indent=2)}
    """
    try:
        logger.info("Sending prompt to GPT-3.5...")
        result = await fetch_gpt_response(prompt)
    except Exception as e:
        logger.error(f"GPT error: {e}")
        await send_telegram_message(chat_id, f"❌ GPT error: {e}")
        return

    logger.info(f"GPT result: {result}")
    await send_telegram_message(chat_id, f"📊 GPT Verdict on {token}:
{result}")

async def listen_for_trade(ca, chat_id, duration):
    uri = "wss://pumpportal.fun/api/data"
    collected = []
    try:
        logger.info(f"Connecting to Pump.fun WS for {ca}")
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({
                "method": "subscribeTokenTrade",
                "keys": [ca]
            }))
            logger.info(f"Subscribed to {ca} — collecting for {duration}s")
            end_time = asyncio.get_event_loop().time() + duration
            while asyncio.get_event_loop().time() < end_time:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=end_time - asyncio.get_event_loop().time())
                    data = json.loads(msg)
                    logger.info(f"WS ▶ {data}")
                    if data.get("txType") in ("buy", "sell") and data.get("mint") == ca:
                        collected.append(data)
                except asyncio.TimeoutError:
                    break
        if collected:
            await analyze_with_gpt(collected, chat_id, duration)
        else:
            await send_telegram_message(chat_id, f"🤷 No trades detected on {ca} during the {duration}s window.")
    except Exception as e:
        logger.error(f"WebSocket error for {ca}: {e}")
        await send_telegram_message(chat_id, f"❌ WebSocket error for {ca}: {e}")
    finally:
        active_tasks.pop(ca, None)

async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Received command from user {update.effective_user.id}")
    if not context.args:
        await context.bot.send_message(chat_id=update.effective_chat.id, text="Usage: /analyze <TOKEN_MINT> [duration_seconds]")
        return

    ca = context.args[0]
    duration = int(context.args[1]) if len(context.args) > 1 and context.args[1].isdigit() else 30
    chat_id = update.effective_chat.id

    if ca in active_tasks:
        await context.bot.send_message(chat_id=chat_id, text="⏳ Already analyzing this token...")
        return

    await context.bot.send_message(chat_id=chat_id, text=f"💅 Listening for trades on {ca} for {duration}s...")
    task = asyncio.create_task(listen_for_trade(ca, chat_id, duration))
    active_tasks[ca] = task

application.add_handler(CommandHandler("analyze", analyze_command))

@app.on_event("startup")
async def on_startup():
    await application.initialize()
    await application.bot.set_webhook(url=f"{WEBHOOK_URL}/webhook")
    logger.info("Webhook set and application initialized.")

@app.post("/webhook")
async def telegram_webhook(req: Request):
    data = await req.json()
    logger.info(f"Webhook data: {data}")
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return "ok"
