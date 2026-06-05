import os
import logging
import asyncio
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from analyzer import SignalAnalyzer

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
POLYGON_API_KEY = os.environ["POLYGON_API_KEY"]

# Default price change threshold to trigger alert (%)
DEFAULT_THRESHOLD = 1.0

analyzer = SignalAnalyzer(ANTHROPIC_API_KEY, POLYGON_API_KEY)

# In-memory store: chat_id -> { tickers: set, threshold: float }
watchlist = {}


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📡 *Stock Signal Bot*\n\n"
        "I watch your stocks every 30 minutes during market hours (Mon–Fri, 09:30–16:00 ET). "
        "When price movement AND news sentiment both point the same way, I'll send you a *BUY* or *SELL* signal.\n\n"
        "*Commands:*\n"
        "• `/watch NVDA AAPL` — track tickers\n"
        "• `/unwatch NVDA` — stop tracking\n"
        "• `/threshold 1.5` — set % move to trigger alert (default: 1%)\n"
        "• `/list` — see your watchlist\n"
        "• `/check NVDA` — run an instant check now (ignores market hours)\n"
        "• `/help` — show this message",
        parse_mode="Markdown"
    )


async def watch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tickers = [t.upper() for t in ctx.args if t.isalpha()]
    if not tickers:
        await update.message.reply_text("Usage: `/watch NVDA AAPL MSFT`", parse_mode="Markdown")
        return

    if chat_id not in watchlist:
        watchlist[chat_id] = {"tickers": set(), "threshold": DEFAULT_THRESHOLD}
    watchlist[chat_id]["tickers"].update(tickers)

    await update.message.reply_text(
        f"✅ Tracking: *{', '.join(sorted(watchlist[chat_id]['tickers']))}*\n"
        f"Alert threshold: ±{watchlist[chat_id]['threshold']}% price move\n\n"
        f"I'll check every 30 minutes during market hours and alert you only when price AND news align.",
        parse_mode="Markdown"
    )


async def unwatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tickers = [t.upper() for t in ctx.args if t.isalpha()]
    if chat_id in watchlist:
        for t in tickers:
            watchlist[chat_id]["tickers"].discard(t)
    await update.message.reply_text(f"Removed: {', '.join(tickers)}")


async def threshold(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        val = float(ctx.args[0])
        if chat_id not in watchlist:
            watchlist[chat_id] = {"tickers": set(), "threshold": val}
        else:
            watchlist[chat_id]["threshold"] = val
        await update.message.reply_text(f"✅ Threshold set to ±{val}% price move.")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: `/threshold 1.5`", parse_mode="Markdown")


async def list_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    data = watchlist.get(chat_id)
    if data and data["tickers"]:
        await update.message.reply_text(
            f"📋 *Watchlist:* {', '.join(sorted(data['tickers']))}\n"
            f"⚡ Threshold: ±{data['threshold']}%",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("No tickers tracked yet. Use `/watch NVDA` to start.", parse_mode="Markdown")


async def check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tickers = [t.upper() for t in ctx.args if t.isalpha()]
    if not tickers:
        await update.message.reply_text("Usage: `/check NVDA`", parse_mode="Markdown")
        return
    await update.message.reply_text(f"⏳ Checking {', '.join(tickers)}...")
    for ticker in tickers:
        msg = await analyzer.run(ticker, threshold=0.0, force=True)
        if msg:
            await update.message.reply_text(msg, parse_mode="Markdown")


async def scheduled_check(ctx: ContextTypes.DEFAULT_TYPE):
    """Runs every 30 minutes. Only sends message if signal fires."""
    logger.info("Running scheduled signal check...")
    for chat_id, data in list(watchlist.items()):
        for ticker in list(data["tickers"]):
            try:
                msg = await analyzer.run(ticker, threshold=data["threshold"], force=False)
                if msg:
                    await ctx.application.bot.send_message(
                        chat_id=chat_id,
                        text=msg,
                        parse_mode="Markdown"
                    )
            except Exception as e:
                logger.error(f"Error checking {ticker}: {e}")


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("watch", watch))
    app.add_handler(CommandHandler("unwatch", unwatch))
    app.add_handler(CommandHandler("threshold", threshold))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("check", check))

    # Check every 30 minutes
    app.job_queue.run_repeating(scheduled_check, interval=1800, first=60)

    logger.info("Signal bot started.")
    app.run_polling()


if __name__ == "__main__":
    main()
