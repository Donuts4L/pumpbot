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

Do not include extra commentary. Do NOT wrap your answer in markdown or code fences. Just the 5 lines in that order.
"""

client = AsyncOpenAI(api_key=OPENAI_API_KEY)

@app.get("/debug-openai-key")
async def debug_openai_key():
    key = os.getenv("OPENAI_API_KEY")
    return {"OPENAI_API_KEY": key[:8] + "..." + key[-6:] if key else "MISSING"}

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
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": "Say hello"}
            ],
            max_tokens=10
        )
        logger.info("✅ GPT Startup Test Response: " + response.choices[0].message.content)
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

# Remaining logic unchanged
# ...
