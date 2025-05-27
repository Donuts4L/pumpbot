import os
import json
import asyncio
import logging
from fastapi import FastAPI, Request
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler
import httpx
import websockets
from websockets.exceptions import ConnectionClosedError, InvalidURI, WebSocketException
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type
from openai import AsyncOpenAI, APIStatusError, RateLimitError, APIConnectionError, AuthenticationError
from typing import List, Dict, Any
from cachetools import TTLCache

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Environment validation
required_envs = ["TELEGRAM_BOT_TOKEN", "OPENAI_API_KEY", "WEBHOOK_URL"]
missing = [env for env in required_envs if not os.getenv(env)]
if missing:
    logger.critical(f"Missing environment variables: {', '.join(missing)}")
    raise ValueError("Required environment variables are missing")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
BAIL_ON_ZERO_TRADES = os.getenv("BAIL_ON_ZERO_TRADES", "0") == "1"

if not OPENAI_API_KEY.startswith("sk-"):
    logger.critical("Invalid OPENAI_API_KEY format")
    raise ValueError("OPENAI_API_KEY must start with 'sk-'")

app = FastAPI()
application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

active_tasks = {}
listen_sema = asyncio.Semaphore(5)
command_debounce = TTLCache(maxsize=1000, ttl=5)
http_client = httpx.AsyncClient()

SYSTEM_PROMPT = '''
ANALYSIS PROTOCOL:
Use EXACTLY these 5 fields:
Verdict: [Bullish/Bearish/Neutral/Trash]
Confidence: [1-5]
Strategy: [Action or "Monitor"]
Stop Loss: [Range/N/A]
Take Profit: [Range/N/A]
Wrap analysis in a savage degen tone. You're a ruthless Solana meme sniper.
If format can't be followed, respond with "FORMAT ERROR"
'''

client = AsyncOpenAI(api_key=OPENAI_API_KEY)

@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info(f"→ {request.method} {request.url}")
    return await call_next(request)

@app.on_event("startup")
async def startup_tasks():
    logger.info("Starting application initialization")
    try:
        await application.initialize()
        await application.bot.delete_webhook(drop_pending_updates=True)
        await application.bot.set_webhook(url=f"{WEBHOOK_URL}/webhook")
        logger.info("Telegram bot initialized and webhook set")
    except Exception as e:
        logger.critical(f"Failed to initialize Telegram application: {type(e).__name__}: {str(e)}")
        raise

    try:
        test_response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "Test"}],
            max_tokens=10
        )
        logger.info("OpenAI API key validated successfully")
    except Exception as e:
        logger.error(f"OpenAI API key validation failed: {type(e).__name__}: {str(e)}")

    logger.info("Application startup complete")

@app.on_event("shutdown")
async def shutdown():
    await http_client.aclose()
    for task in active_tasks.values():
        task.cancel()
    logger.info("Application shutdown complete")

@app.get("/")
async def root():
    return "OK"

@app.get("/test-websocket/{ca}")
async def test_ws(ca: str):
    uri = "wss://pumpportal.fun/api/data"
    try:
        async with websockets.connect(uri) as ws:
            logger.info(f"WebSocket connected for test with CA: {ca}")
            await ws.send(json.dumps({
                "method": "subscribeTokenTrade",
                "keys": [ca]
            }))
            msg = await asyncio.wait_for(ws.recv(), timeout=5)
            logger.info(f"WebSocket message received: {msg}")
            return {"status": "success", "message": msg}
    except Exception as e:
        logger.error(f"WebSocket test failed: {type(e).__name__}: {str(e)}")
        return {"status": "error", "message": f"{type(e).__name__}: {str(e)}"}

@app.get("/test-openai")
async def test_openai():
    try:
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": "Test"}],
            max_tokens=10
        )
        logger.info("OpenAI test successful")
        return {"status": "success", "response": response.choices[0].message.content}
    except Exception as e:
        logger.error(f"OpenAI test failed: {type(e).__name__}: {str(e)}")
        return {"status": "error", "message": f"{type(e).__name__}: {str(e)}"}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return "ok"

async def listen_for_trade(ca: str, chat_id: int, duration: int):
    uri = "wss://pumpportal.fun/api/data"
    end_time = asyncio.get_event_loop().time() + duration
    events = []

    try:
        async with listen_sema:
            async with websockets.connect(uri) as ws:
                await ws.send(json.dumps({
                    "method": "subscribeTokenTrade",
                    "keys": [ca]
                }))
                logger.info(f"Listening to trades for {ca}")
                while asyncio.get_event_loop().time() < end_time:
                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=duration)
                        data = json.loads(message)
                        if data.get("method") == "tokenTrade" and "params" in data:
                            events.append(data["params"])
                    except (asyncio.TimeoutError, json.JSONDecodeError):
                        break
    except (ConnectionClosedError, InvalidURI, WebSocketException) as e:
        logger.error(f"WebSocket error: {type(e).__name__}: {str(e)}")

    del active_tasks[ca]

    trades_json = json.dumps(events[-10:], indent=2) if events else "{}"
    prompt = SYSTEM_PROMPT.strip() + "\n\nRecent Trades (JSON):\n```json\n" + trades_json + "\n```"

    try:
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300
        )
        reply = response.choices[0].message.content.strip()
    except Exception as e:
        reply = f"⚠️ GPT Error: {type(e).__name__}: {str(e)}"

    await application.bot.send_message(chat_id=chat_id, text=f"🚀 {ca[:6]} Degen Verdict:\n{reply}")

async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /analyze <TOKEN_MINT> [duration_seconds]")
        return

    ca = context.args[0]
    if not ca or len(ca) < 6:
        await update.message.reply_text("Invalid TOKEN_MINT")
        return

    duration = int(context.args[1]) if len(context.args) > 1 and context.args[1].isdigit() else 30
    if duration <= 0:
        await update.message.reply_text("Duration must be a positive number")
        return

    chat_id = update.effective_chat.id
    now = asyncio.get_event_loop().time()
    key = f"{update.effective_user.id}-{ca}"
    if command_debounce.get(key, 0) > now:
        await update.message.reply_text(f"⏳ Wait 5s between {ca} analyses")
        return

    command_debounce[key] = now + 5

    if ca in active_tasks:
        await update.message.reply_text("⏳ Already analyzing this token…")
        return

    await update.message.reply_text(f"💅 Listening for trades on {ca} for {duration}s…")
    task = asyncio.create_task(listen_for_trade(ca, chat_id, duration))
    active_tasks[ca] = task

application.add_handler(CommandHandler("analyze", analyze_command))
