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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# –– Environment & Config ––
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
BAIL_ON_ZERO_TRADES = os.getenv("BAIL_ON_ZERO_TRADES", "0") == "1"

# –– FastAPI & Telegram setup ––
app = FastAPI()
application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

# –– Concurrency & State ––
active_tasks = {}                          # token → asyncio.Task
listen_sema = asyncio.Semaphore(5)         # max 5 concurrent WS → Pump.fun
command_debounce = {}                      # "user-token" → expire_time (monotonic)

# –– System prompt enforcing 5-field format ––
SYSTEM_PROMPT = '''
ANALYSIS PROTOCOL:
1. Use EXACTLY these 5 fields:
   Verdict: [Bullish/Bearish/Neutral/Trash]
   Confidence: [1-5]
   Strategy: [Action or "Monitor"]
   Stop Loss: [Range/N/A]
   Take Profit: [Range/N/A]
2. If format can't be followed, respond with "FORMAT ERROR"
'''

# –– OpenAI client ––
client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# –– Middleware to log incoming HTTP requests ––
@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info(f"→ {request.method} {request.url}")
    return await call_next(request)

@app.get("/")
async def root():
    return "OK"

# –– Helper: send a Telegram message ––
async def send_telegram_message(chat_id, text):
    try:
        async with httpx.AsyncClient() as hc:
            await hc.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text}
            )
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")

# –– Background task: prune old debounce entries ––
async def clean_debounce_cache():
    while True:
        await asyncio.sleep(60)
        now = asyncio.get_event_loop().time()
        expired = [k for k,v in command_debounce.items() if v < now]
        for k in expired:
            del command_debounce[k]
        if expired:
            logger.info(f"Cleaned {len(expired)} debounce entries")

# –– Startup: launch cache cleanup & webhook ––
@app.on_event("startup")
async def startup_tasks():
    asyncio.create_task(clean_debounce_cache())
    await application.initialize()
    await application.bot.set_webhook(url=f"{WEBHOOK_URL}/webhook")
    logger.info("Telegram webhook set and application initialized.")

# –– Fetch GPT response with model-fallback & streaming ––
@retry(
    stop=stop_after_attempt(3),
    wait=wait_fixed(2),
    retry=retry_if_exception_type((RateLimitError, APIConnectionError)),
    reraise=True
)
async def fetch_gpt_response(prompt, models=("gpt-4-turbo", "gpt-3.5-turbo")):
    last_exc = None
    for model in models:
        try:
            stream = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": f"⚠️ Stick to 5-field format!\n\n{prompt}"}
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
            result = "".join(content).strip()
            logger.info(f"Used model: {model}")
            return result

        except RateLimitError as e:
            logger.warning(f"{model} rate-limited, trying next model…")
            last_exc = e
            continue

        except APIStatusError as e:
            logger.error(f"APIStatusError on {model}: {e.code} – {e.message}")
            last_exc = e
            continue

        except AuthenticationError:
            logger.critical("OpenAI authentication failed – check API key")
            raise

        except Exception as e:
            logger.error(f"Unexpected error with {model}: {e}")
            last_exc = e
            break

    raise last_exc or RuntimeError("All OpenAI model attempts failed")

# –– Core analysis ––
async def analyze_with_gpt(events, chat_id, duration):
    # Bail early if zero trades and configured to do so
    if not events and BAIL_ON_ZERO_TRADES:
        await send_telegram_message(chat_id, "🎗 No trades detected – market's asleep.")
        return

    token = (events[0].get("mint", "UNKNOWN")[:6] if events else "UNKNOWN")
    low_note = "\n⚠️ LOW ACTIVITY – Analyzing micro-patterns" if 0 < len(events) < 3 else ""

    prompt = (
        f"Analyze {len(events)} trades over {duration}s for {token}:{low_note}\n\n"
        "Data:\n"
        f"{json.dumps(events, indent=2) if events else 'No trades – market stagnant'}"
    )

    try:
        result = await fetch_gpt_response(prompt)
    except AuthenticationError:
        await send_telegram_message(chat_id, "❌ OpenAI authentication error")
        return
    except RateLimitError:
        await send_telegram_message(chat_id, "🚧 OpenAI quota exceeded, try later")
        return
    except Exception as e:
        await send_telegram_message(chat_id, f"❌ Analysis failed: {type(e).__name__}")
        logger.error(f"fetch_gpt_response error: {e}")
        return

    await send_telegram_message(chat_id, f"\U0001F4CA {token} Analysis:\n{result}")

# –– Listen & collect trades from Pump.fun WS ––
async def listen_for_trade(ca, chat_id, duration):
    collected = []
    uri = "wss://pumpportal.fun/api/data"
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
                    except asyncio.TimeoutError:
                        continue

                    data = json.loads(msg)
                    if data.get("method") == "tokenTrade" and "params" in data:
                        p = data["params"]
                        if p.get("mint") == ca and p.get("txType") in ("buy", "sell"):
                            collected.append({
                                "ts":   p.get("ts"),
                                "type": p.get("txType"),
                                "amount": p.get("amount"),
                                "price":  p.get("price")
                            })

    except (ConnectionClosedError, InvalidURI, WebSocketException) as e:
        logger.error(f"WS error ({type(e).__name__}): {e}")
        await send_telegram_message(chat_id, f"🔌 WebSocket issue: {type(e).__name__}")
    except Exception as e:
        logger.error(f"Unexpected WS error: {e}")
        await send_telegram_message(chat_id, "❌ Analysis failed")
    finally:
        active_tasks.pop(ca, None)

    if not collected:
        if not BAIL_ON_ZERO_TRADES:
            await send_telegram_message(chat_id, f"🎗 No trades for {ca} in {duration}s")
        return

    logger.info(f"Collected {len(collected)} trades; sample keys: {collected[0].keys()}")
    await analyze_with_gpt(collected, chat_id, duration)

# –– /analyze command handler ––
async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /analyze <TOKEN_MINT> [duration_seconds]")
        return

    ca = context.args[0]
    duration = int(context.args[1]) if len(context.args) > 1 and context.args[1].isdigit() else 30
    chat_id = update.effective_chat.id

    # Debounce using monotonic clock
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

# –– Telegram webhook endpoint ––
@app.post("/webhook")
async def telegram_webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return "ok"
