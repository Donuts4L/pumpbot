
import asyncio, websockets, json, openai, requests, os

# 🔐 Load your keys from environment variables
openai.api_key = os.getenv("OPENAI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TOKEN_MINT = os.getenv("TOKEN_MINT")

# 📤 Send a message to Telegram
def send_telegram_message(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    requests.post(url, json=payload)

# 🤖 Send trade event to GPT for analysis
async def analyze_with_gpt(event):
    prompt = f"""
You're a crypto sniper bot analyzing Pump.fun token trades. 
Given this trade data, explain if it's bullish, bearish, or risky.

Event:
{json.dumps(event, indent=2)}
    """
    try:
        res = openai.ChatCompletion.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are a meme coin sniper AI."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=200,
            temperature=0.4
        )
        result = res["choices"][0]["message"]["content"]
        short_token = TOKEN_MINT[:5] + "..."
        send_telegram_message(f"📊 Token {short_token}\n\nGPT: {result}")
    except Exception as e:
        print("❌ GPT error:", e)

# 🔁 WebSocket to listen for trades
async def subscribe_to_trades():
    uri = "wss://pumpportal.fun/api/data"
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({
            "method": "subscribeTokenTrade",
            "keys": [TOKEN_MINT]
        }))
        print("🟢 Connected and listening...")
        async for msg in ws:
            data = json.loads(msg)
            if data.get("method") == "tokenTrade":
                await analyze_with_gpt(data)

# 🧠 Run loop
asyncio.get_event_loop().run_until_complete(subscribe_to_trades())
