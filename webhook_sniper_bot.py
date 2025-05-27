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
from openai import AsyncOpenAI, RateLimitError
from typing import List, Dict, Any
from cachetools import TTLCache

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

SYSTEM_PROMPT = """
You are a ruthless Solana meme coin sniper. Analyze the provided trade data and respond with EXACTLY the following 5 fields, nothing else:

Verdict: [Bullish/Bearish/Neutral/Trash]
Confidence: [1–5]
Strategy: [Action or "Monitor"]
Stop Loss: [Range/N/A]
Take Profit: [Range/N/A]

If trades are empty, respond:
Verdict: Neutral
Confidence: 1
Strategy: Monitor
Stop Loss: N/A
Take Profit: N/A

Example:
Verdict: Bullish
Confidence: 4
Strategy: Accumulate before breakout
Stop Loss: 0.002-0.003
Take Profit: 0.006-0.008

Do not include extra commentary. Just the 5 lines in that order.
"""

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
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "Test"}
            ],
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
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "Test"}
            ],
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

@retry(wait=wait_fixed(1), stop=stop_after_attempt(3), retry=retry_if_exception_type(RateLimitError))
async def call_openai(messages):
    return await client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        temperature=0.2,
        max_tokens=500
    )

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

    if not events:
        reply = """
Verdict: Neutral
Confidence: 1
Strategy: Monitor
Stop Loss: N/A
Take Profit: N/A
""".strip()
    else:
        slim_trades = [{
            "price": t.get("price"),
            "amount": t.get("amount"),
            "buyer": t.get("buyer"),
            "ts": t.get("ts")
        } for t in events[-5:]]

        trades_json = json.dumps(slim_trades, indent=2)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Recent Trades:\n```json\n{trades_json}\n```"}
        ]

        try:
            response = await call_openai(messages)
            reply = response.choices[0].message.content.strip()
            logger.info(f"Raw GPT Response: {reply}")

            lines = [line.strip().lower() for line in reply.split("\n") if line.strip()]
            expected_fields = ["verdict:", "confidence:", "strategy:", "stop loss:", "take profit:"]
            if not (len(lines) == 5 and all(lines[i].startswith(expected_fields[i]) for i in range(5))):
                reply = "FORMAT ERROR"
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
