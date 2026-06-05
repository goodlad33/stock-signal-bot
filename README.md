# 📡 Stock Signal Bot — Setup Guide

Sends you a BUY or SELL alert on Telegram **only** when price movement AND news sentiment agree.
Runs 24/7 on Railway (free tier). You never need to touch your laptop.

---

## What you need (all free to set up)

| What | Where | Time |
|---|---|---|
| Telegram bot token | @BotFather on Telegram | 2 min |
| Anthropic API key | console.anthropic.com | 2 min |
| Alpaca API key | alpaca.markets | 3 min |
| Railway account | railway.app | 3 min |

---

## Step 1 — Create your Telegram bot

1. Open Telegram, search for **@BotFather**
2. Send `/newbot`
3. Choose a name (e.g. "My Stock Signals") and a username (e.g. `mystocksignal_bot`)
4. Copy the token it gives you — looks like `7123456789:AAF-abc123...`

---

## Step 2 — Get your Anthropic API key

1. Go to https://console.anthropic.com
2. Sign in → **API Keys** → **Create Key**
3. Copy the key (starts with `sk-ant-...`)
4. Add a small amount of credit ($5 lasts a long time at ~$0.02/check)

---

## Step 3 — Get your Alpaca API key (free, no trading needed)

1. Go to https://alpaca.markets and create a free account
2. Go to **Paper Trading** (not live — you don't need to fund it)
3. Click **View** under API Keys
4. Copy both the **API Key** and **Secret Key**

> Alpaca's free IEX feed gives real-time US stock prices at no cost.

---

## Step 4 — Deploy to Railway (runs 24/7)

1. Go to https://railway.app and sign up (free tier is enough)
2. Click **New Project** → **Deploy from GitHub repo**
   - Push this folder to a GitHub repo first, or use Railway's CLI
3. In your Railway project, go to **Variables** and add:

```
TELEGRAM_TOKEN=your_telegram_token
ANTHROPIC_API_KEY=your_anthropic_key
ALPACA_API_KEY=your_alpaca_key
ALPACA_SECRET_KEY=your_alpaca_secret
```

4. Railway will build the Dockerfile automatically and start your bot.

---

## Step 5 — Use it

Open your Telegram bot and send:

```
/watch NVDA AAPL MSFT       ← start tracking
/threshold 1.5              ← alert on ±1.5% move (default is 1%)
/check NVDA                 ← instant check right now
/list                       ← see your watchlist
/unwatch AAPL               ← stop tracking
```

---

## How signals work

The bot checks every **30 minutes**. It only sends a message when:

- Price moved ≥ your threshold AND news sentiment agrees

| Price | Sentiment | Alert |
|---|---|---|
| Up ≥1% | Bullish 🟢 | 🚀 BUY signal |
| Down ≥1% | Bearish 🔴 | 🔻 SELL signal |
| Anything else | — | Silence (no spam) |

### Example alert:
```
🚀 BUY SIGNAL — NVDA

📈 Price: $1,247.50 (+1.8% in last 30 min)
🟢 News: Bullish (confidence: 8/10)
_Strong earnings beat drives after-hours surge_
📰 Nvidia Q2 revenue smashes estimates by 15%

⏱ Jun 01, 14:30 · You decide whether to act on this.
```

---

## Cost estimate

| Service | Cost |
|---|---|
| Railway | Free tier (enough for this bot) |
| Alpaca | Free |
| Telegram | Free |
| Anthropic API | ~$0.02 per check × 48 checks/day × tickers |

For 3 tickers: roughly **$3/month** in Anthropic API costs.
