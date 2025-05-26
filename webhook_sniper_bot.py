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

async def send_telegram_message(chat_id, text):
    try:
        await http_client.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text}
        )
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")

@retry(stop=stop_after_attempt(2), wait=wait_fixed(1))
async def fetch_metadata(mint: str) -> dict:
    res = await http_client.get(f"https://pump.fun/api/token/{mint}")
    res.raise_for_status()
    return res.json()

async def fetch_gpt_response(prompt: str, models=("gpt-4-turbo", "gpt-3.5-turbo")) -> str:
    for model in models:
        try:
            stream = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f" Stick to 5-field format!\n\n{prompt}"}
                ],
                max_tokens=300,
                temperature=0.7,
                stream=True
            )
            content = []
            async for chunk in stream:
                delta = chunk.choices[0].delta.content
                if delta:
                    content.append(delta)
            logger.info(f"Used model: {model}")
            return "".join(content).strip()
        except (RateLimitError, APIConnectionError, APIStatusError, AuthenticationError) as e:
            logger.warning(f"{model} error: {type(e).__name__}: {e}")
            continue
    raise RuntimeError("All GPT model attempts failed")

async def analyze_with_gpt(events: List[Dict[str, Any]], chat_id: int, duration: int) -> None:
    token_meta = {}
    if events:
        try:
            token_meta = await fetch_metadata(events[0].get("mint"))
            token_meta['ageMinutes'] = round((asyncio.get_event_loop().time() - token_meta.get('createdUnixTimestamp', 0)) / 60)
        except Exception as e:
            logger.warning(f"Token metadata fetch failed: {e}")

    token = (events[0].get("mint", "UNKNOWN")[:6] if events else "UNKNOWN")
    low_note = "\n⚠️ LOW ACTIVITY – Analyzing micro-patterns" if 0 < len(events) < 3 else ""

    prompt = (
        f"Yo sniper — scoped {len(events)} trades in {duration}s on {token} "
        f"(MC: ${token_meta.get('marketCap', 'N/A')}, Age: {token_meta.get('ageMinutes', 'N/A')}m). Let's dissect:{low_note}\n\n"
        "Data:\n"
        f"{json.dumps(events, indent=2) if events else 'No trades – market stagnant'}"
    )

    try:
        result = await fetch_gpt_response(prompt)
    except Exception as e:
        logger.error(f"GPT call failed: {e}")
        await send_telegram_message(chat_id, f"❌ GPT call failed: {type(e).__name__}")
        return

    await send_telegram_message(chat_id, f"🚀 {token} Degen Verdict:\n{result}")

async def listen_for_trade(ca: str, chat_id: int, duration: int) -> None:
    collected = []
    uri = "wss://pumpportal.fun/api/data"
    timeout_count = 0
    max_timeouts = 10
    try:
        async with listen_sema:
            async with websockets.connect(uri) as ws:
                await ws.send(json.dumps({
                    "method": "subscribeTokenTrade",
                    "keys": [ca]
                }))
                end = asyncio.get_event_loop().time() + duration
                while True:
                    rem = end - asyncio.get_event_loop().time()
                    if rem <= 0:
                        break
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=min(rem, 5))
                        timeout_count = 0
                        try:
                            data = json.loads(msg)
                            if data.get("method") == "tokenTrade" and "params" in data:
                                p = data["params"]
                                if p.get("mint") == ca and p.get("txType") in ("buy", "sell"):
                                    collected.append({
                                        "ts": p.get("ts"),
                                        "type": p.get("txType"),
                                        "amount": p.get("amount"),
                                        "price": p.get("price")
                                    })
                        except json.JSONDecodeError:
                            logger.warning(f"Invalid JSON from WebSocket: {msg}")
                    except asyncio.TimeoutError:
                        timeout_count += 1
                        if timeout_count >= max_timeouts:
                            logger.warning("Max WebSocket timeouts reached")
                            break
                        continue
    except (ConnectionClosedError, InvalidURI, WebSocketException) as e:
        logger.error(f"WebSocket error ({type(e).__name__}): {e}")
        await send_telegram_message(chat_id, f"🔌 WebSocket issue: {type(e).__name__}")
    except Exception as e:
        logger.error(f"Unexpected WS error: {e}")
        await send_telegram_message(chat_id, "❌ Analysis failed")
    finally:
        active_tasks.pop(ca, None)
        await analyze_with_gpt(collected, chat_id, duration)

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

@app.post("/webhook")
async def telegram_webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return "ok"
