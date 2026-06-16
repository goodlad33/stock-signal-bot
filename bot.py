import os
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters
)
from analyzer import SignalAnalyzer, DEFAULT_THRESHOLD, now_uk, is_market_open

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
#  CONFIG                                                              #
# ------------------------------------------------------------------ #

TELEGRAM_TOKEN     = os.environ["TELEGRAM_TOKEN"]
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
POLYGON_API_KEY    = os.environ.get("POLYGON_API_KEY")
ALPHA_VANTAGE_KEY  = os.environ.get("ALPHA_VANTAGE_KEY")
LUNARCRUSH_API_KEY = os.environ.get("LUNARCRUSH_API_KEY")
FINNHUB_API_KEY    = os.environ.get("FINNHUB_API_KEY")

STATE_FILE       = Path("/tmp/bot_state.json")
COOLDOWN_HOURS   = 4
DEFAULT_WATCHLIST = ["NVDA", "AAPL", "MA", "META", "MSFT", "TSLA"]

SWEEP_TIMES_ET = {
    (9,  45): "Morning Sweep",
    (12, 30): "Midday Sweep",
    (15, 30): "Pre-Close Sweep",
}

# Conversation states
(ASK_TICKER, ASK_PRICE, ASK_SIZE, ASK_CONFIRM,
 SELL_ASK_TICKER, SELL_ASK_PRICE, SELL_ASK_REVIEW) = range(7)

analyzer = SignalAnalyzer(
    ANTHROPIC_API_KEY, POLYGON_API_KEY,
    ALPHA_VANTAGE_KEY, LUNARCRUSH_API_KEY,
    FINNHUB_API_KEY
)

# ------------------------------------------------------------------ #
#  STATE PERSISTENCE                                                   #
# ------------------------------------------------------------------ #

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"State load error: {e}")
    return {}


def save_state(s: dict):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(s, f, indent=2, default=str)
    except Exception as e:
        logger.error(f"State save error: {e}")


def get_chat_state(state: dict, chat_id: int) -> dict:
    key = str(chat_id)
    if key not in state:
        state[key] = {
            "tickers": list(DEFAULT_WATCHLIST),
            "threshold": DEFAULT_THRESHOLD,
            "thresholds": {},
            "paused": False,
            "entries": {},
            "cooldowns": {},
            "signal_log": [],
            "trade_log": [],
            "capital": 100.0,
            "pending_follow_ups": [],
            "earnings_blackout_notified": {},
        }
    cs = state[key]
    cs.setdefault("capital", 100.0)
    cs.setdefault("pending_follow_ups", [])
    cs.setdefault("earnings_blackout_notified", {})
    cs.setdefault("trade_log", [])
    cs.setdefault("signal_log", [])
    cs.setdefault("thresholds", {})
    return cs


def get_ticker_threshold(cs: dict, ticker: str) -> float:
    return cs.get("thresholds", {}).get(ticker, cs.get("threshold", DEFAULT_THRESHOLD))


state = load_state()

# ------------------------------------------------------------------ #
#  HELPERS                                                             #
# ------------------------------------------------------------------ #

def is_on_cooldown(chat_id: int, ticker: str) -> bool:
    cs = get_chat_state(state, chat_id)
    last_str = cs["cooldowns"].get(ticker)
    if not last_str:
        return False
    try:
        return datetime.now() - datetime.fromisoformat(last_str) < timedelta(hours=COOLDOWN_HOURS)
    except Exception:
        return False


def set_cooldown(chat_id: int, ticker: str):
    cs = get_chat_state(state, chat_id)
    cs["cooldowns"][ticker] = datetime.now().isoformat()
    save_state(state)


def check_exposure_warning(cs: dict, new_size: float) -> str | None:
    capital = cs.get("capital", 100.0)
    current = sum(e.get("size", 0) for e in cs.get("entries", {}).values())
    total_after = current + new_size
    warnings = []
    if new_size > capital * 0.20:
        warnings.append(
            f"⚠️ This position (${new_size}) is {new_size/capital*100:.0f}% of your capital (${capital})."
        )
    if total_after > capital * 0.80:
        warnings.append(
            f"⚠️ Total exposure would reach ${total_after:.0f} ({total_after/capital*100:.0f}% of ${capital})."
        )
    return "\n".join(warnings) if warnings else None


def has_earnings_been_notified(cs: dict, ticker: str) -> bool:
    last_str = cs.get("earnings_blackout_notified", {}).get(ticker)
    if not last_str:
        return False
    try:
        return datetime.now() - datetime.fromisoformat(last_str) < timedelta(hours=48)
    except Exception:
        return False


def mark_earnings_notified(cs: dict, ticker: str):
    cs.setdefault("earnings_blackout_notified", {})[ticker] = datetime.now().isoformat()


def reschedule_pending_follow_ups(app: Application):
    for chat_id_str, cs in state.items():
        for follow_up in cs.get("pending_follow_ups", []):
            try:
                due_time = datetime.fromisoformat(follow_up["due_time_iso"])
                delay = max(30, (due_time - datetime.now()).total_seconds())
                app.job_queue.run_once(
                    thesis_follow_up,
                    when=delay,
                    data={
                        "chat_id": int(chat_id_str),
                        "ticker": follow_up["ticker"],
                        "iteration": follow_up["iteration"],
                    }
                )
                logger.info(f"Rescheduled follow-up: {follow_up['ticker']} in {delay:.0f}s")
            except Exception as e:
                logger.error(f"Reschedule error: {e}")

# ------------------------------------------------------------------ #
#  COMMANDS                                                            #
# ------------------------------------------------------------------ #

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📡 *Stock Signal Bot*\n\n"
        "Monitors stocks every 30 min during market hours (Mon–Fri 09:30–16:00 ET).\n"
        "Signals fire only when price, news, and social sentiment agree at high confidence.\n\n"
        "*Commands:*\n"
        "• `/watch NVDA AAPL` — add tickers\n"
        "• `/unwatch NVDA` — remove ticker\n"
        "• `/list` — watchlist and positions\n"
        "• `/check NVDA` — instant deep analysis\n"
        "• `/scan` — ranked overview of full watchlist now\n"
        "• `/brief` — morning analysis of all stocks\n"
        "• `/bought` — log a buy\n"
        "• `/sold` — log a sale\n"
        "• `/entries` — open positions\n"
        "• `/trades` — trade history and P&L\n"
        "• `/capital 500` — set your total capital\n"
        "• `/pause` — silence alerts\n"
        "• `/resume` — re-enable alerts\n"
        "• `/threshold 2.0` — global price move threshold\n"
        "• `/threshold TSLA 2.5` — per-ticker threshold\n"
        "• `/help` — show this message\n\n"
        f"_Default watchlist: {', '.join(DEFAULT_WATCHLIST)}_",
        parse_mode="Markdown"
    )


async def watch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tickers = [t.upper() for t in ctx.args if t.isalpha()]
    if not tickers:
        await update.message.reply_text("Usage: `/watch NVDA AAPL`", parse_mode="Markdown")
        return
    cs = get_chat_state(state, chat_id)
    cs["tickers"] = list(set(cs["tickers"] + tickers))
    save_state(state)
    await update.message.reply_text(
        f"✅ Now tracking: *{', '.join(sorted(cs['tickers']))}*",
        parse_mode="Markdown"
    )


async def unwatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tickers = [t.upper() for t in ctx.args if t.isalpha()]
    cs = get_chat_state(state, chat_id)
    cs["tickers"] = [t for t in cs["tickers"] if t not in tickers]
    for t in tickers:
        cs["entries"].pop(t, None)
    save_state(state)
    await update.message.reply_text(f"Removed: {', '.join(tickers)}")


async def list_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cs = get_chat_state(state, chat_id)
    if not cs["tickers"]:
        await update.message.reply_text("No tickers tracked. Use `/watch NVDA`.", parse_mode="Markdown")
        return
    lines = [f"📋 *Watchlist* {'⏸ PAUSED' if cs['paused'] else '▶️ Active'}\n"]
    for t in sorted(cs["tickers"]):
        entry = cs["entries"].get(t)
        if entry:
            size_str = f" · ${entry['size']}" if entry.get("size") else ""
            lines.append(
                f"• *{t}*{size_str} — entry ${entry['entry_price']} · "
                f"target +{entry['target_pct']}% · stop -{entry['stop_pct']}%"
            )
        else:
            lines.append(f"• {t}")
    capital = cs.get("capital", 100.0)
    exposure = sum(e.get("size", 0) for e in cs["entries"].values())
    lines.append(f"\n⚡ Threshold: ±{cs['threshold']}% · Capital: ${capital} · Exposure: ${exposure:.0f}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    tickers = [t.upper() for t in ctx.args if t.isalpha()]
    if not tickers:
        await update.message.reply_text("Usage: `/check NVDA`", parse_mode="Markdown")
        return
    cs = get_chat_state(state, chat_id)
    await update.message.reply_text(f"⏳ Running deep analysis on {', '.join(tickers)}...")
    for ticker in tickers:
        entry = cs["entries"].get(ticker)
        msg, _, _ = await analyzer.run(ticker, threshold=0.0, force=True, entry=entry)
        if msg:
            await update.message.reply_text(msg, parse_mode="Markdown")


async def brief(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cs = get_chat_state(state, chat_id)
    tickers = sorted(cs["tickers"])
    if not tickers:
        await update.message.reply_text("No tickers on watchlist.", parse_mode="Markdown")
        return
    await update.message.reply_text(f"⏳ Preparing morning brief for {', '.join(tickers)}...")
    summary = analyzer.get_brief(tickers, cs.get("entries"))
    now = now_uk().strftime("%b %d, %H:%M") + " UK"
    await update.message.reply_text(
        f"🌅 *Morning Brief — {now}*\n\n{summary}",
        parse_mode="Markdown"
    )


async def entries_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cs = get_chat_state(state, chat_id)
    if not cs["entries"]:
        await update.message.reply_text(
            "No open positions. Use `/bought` to log a trade.", parse_mode="Markdown"
        )
        return
    lines = ["📌 *Open Positions*\n"]
    for ticker, e in cs["entries"].items():
        size_str = f" · ${e['size']}" if e.get("size") else ""
        lines.append(
            f"• *{ticker}*{size_str} — entry ${e['entry_price']} · "
            f"target +{e['target_pct']}% · stop -{e['stop_pct']}%"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def trades_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cs = get_chat_state(state, chat_id)
    log = cs.get("trade_log", [])
    if not log:
        await update.message.reply_text("No completed trades yet.", parse_mode="Markdown")
        return
    lines = ["📊 *Trade History*\n"]
    total_pnl = 0
    for t in log[-20:]:
        pnl = t.get("pnl_pct", 0)
        total_pnl += pnl
        sign = "+" if pnl >= 0 else ""
        dollar = t.get("pnl_dollar", 0)
        dollar_str = f" (≈ {'+' if dollar >= 0 else ''}${abs(dollar):.2f})" if dollar else ""
        outcome = "✅" if pnl >= 0 else "❌"
        lines.append(
            f"{outcome} *{t['ticker']}* — {sign}{pnl:.1f}%{dollar_str} "
            f"| ${t.get('entry_price','?')} → ${t.get('exit_price','?')} "
            f"| {t.get('exit_reason','manual')}"
        )
    sign = "+" if total_pnl >= 0 else ""
    lines.append(f"\n*Total P&L: {sign}{total_pnl:.1f}%*")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def capital_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cs = get_chat_state(state, chat_id)
    if not ctx.args:
        await update.message.reply_text(
            f"Current capital: *${cs.get('capital', 100.0)}*\n\nUsage: `/capital 500`",
            parse_mode="Markdown"
        )
        return
    try:
        val = float(ctx.args[0])
        cs["capital"] = val
        save_state(state)
        await update.message.reply_text(
            f"✅ Capital set to *${val}*\n\n"
            f"I'll warn if a position exceeds 20% or total exposure exceeds 80%.",
            parse_mode="Markdown"
        )
    except ValueError:
        await update.message.reply_text("Usage: `/capital 500`", parse_mode="Markdown")


async def pause(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    get_chat_state(state, chat_id)["paused"] = True
    save_state(state)
    await update.message.reply_text("⏸ Paused. Watchlist saved. Send `/resume` to restart.")


async def resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    get_chat_state(state, chat_id)["paused"] = False
    save_state(state)
    await update.message.reply_text("▶️ Resumed. Monitoring your watchlist.")


async def debug_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cs = get_chat_state(state, chat_id)
    now = now_uk()
    await update.message.reply_text(
        f"🔧 *Debug*\n\n"
        f"UK time now: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"morning_ping_sent: {cs.get('morning_ping_sent', 'never')}\n"
        f"weekly_summary_sent: {cs.get('weekly_summary_sent', 'never')}\n"
        f"paused: {cs.get('paused')}\n"
        f"tickers: {', '.join(cs.get('tickers', []))}",
        parse_mode="Markdown"
    )


async def threshold_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cs = get_chat_state(state, chat_id)
    args = ctx.args

    if not args:
        lines = [f"⚡ *Thresholds*\n\nGlobal default: ±{cs.get('threshold', DEFAULT_THRESHOLD)}%"]
        if cs.get("thresholds"):
            lines.append("\nPer-ticker overrides:")
            for t, v in cs["thresholds"].items():
                lines.append(f"• {t}: ±{v}%")
        lines.append("\n_Usage:_\n`/threshold 2.0` — global\n`/threshold TSLA 2.5` — per ticker")
        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
        return

    if len(args) == 1:
        if args[0].replace(".", "").isdigit():
            try:
                val = float(args[0])
                cs["threshold"] = val
                save_state(state)
                await update.message.reply_text(f"✅ Global threshold set to ±{val}%")
            except ValueError:
                await update.message.reply_text("Usage: `/threshold 2.0`", parse_mode="Markdown")
        else:
            ticker = args[0].upper()
            thresh = cs.get("thresholds", {}).get(ticker, cs.get("threshold", DEFAULT_THRESHOLD))
            source = "per-ticker" if ticker in cs.get("thresholds", {}) else "global default"
            await update.message.reply_text(
                f"*{ticker}* threshold: ±{thresh}% ({source})\n\nTo change: `/threshold {ticker} 2.5`",
                parse_mode="Markdown"
            )
        return

    if len(args) == 2:
        ticker = args[0].upper()
        try:
            val = float(args[1])
            cs.setdefault("thresholds", {})[ticker] = val
            save_state(state)
            await update.message.reply_text(
                f"✅ *{ticker}* threshold set to ±{val}%\n"
                f"_Global default remains ±{cs.get('threshold', DEFAULT_THRESHOLD)}%_",
                parse_mode="Markdown"
            )
        except ValueError:
            await update.message.reply_text("Usage: `/threshold TSLA 2.5`", parse_mode="Markdown")


# ------------------------------------------------------------------ #
#  /bought — conversational flow                                       #
# ------------------------------------------------------------------ #

async def bought_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Which ticker did you buy? (e.g. NVDA)")
    return ASK_TICKER


async def bought_ticker(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ticker = update.message.text.strip().upper()
    if not ticker.isalpha():
        await update.message.reply_text("Please enter a valid ticker (letters only).")
        return ASK_TICKER
    ctx.user_data["bought_ticker"] = ticker
    await update.message.reply_text(
        f"At what price did you buy *{ticker}*? (e.g. 210.50)",
        parse_mode="Markdown"
    )
    return ASK_PRICE


async def bought_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(update.message.text.strip().replace("$", "").replace(",", ""))
    except ValueError:
        await update.message.reply_text("Please enter a valid price (e.g. 210.50)")
        return ASK_PRICE
    ctx.user_data["bought_price"] = price
    chat_id = update.effective_chat.id
    cs = get_chat_state(state, chat_id)
    capital = cs.get("capital", 100.0)
    await update.message.reply_text(
        f"How much are you investing? (e.g. 50)\n\n_Your stated capital: ${capital}_",
        parse_mode="Markdown"
    )
    return ASK_SIZE


async def bought_size(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        size = float(update.message.text.strip().replace("$", "").replace(",", ""))
    except ValueError:
        await update.message.reply_text("Please enter a dollar amount (e.g. 50)")
        return ASK_SIZE

    chat_id = update.effective_chat.id
    ticker = ctx.user_data["bought_ticker"]
    price = ctx.user_data["bought_price"]
    cs = get_chat_state(state, chat_id)
    ctx.user_data["bought_size"] = size
    exposure_warning = check_exposure_warning(cs, size)

    await update.message.reply_text(
        f"⏳ Calculating volatility-based targets for *{ticker}*...",
        parse_mode="Markdown"
    )

    vol = await analyzer.get_volatility(ticker)
    stop_pct  = vol["stop_pct"]
    target_pct = vol["target_pct"]
    avg_move  = vol["avg_daily_move"]
    ctx.user_data["stop_pct"]   = stop_pct
    ctx.user_data["target_pct"] = target_pct

    stop_price     = round(price * (1 - stop_pct / 100), 2)
    target_price   = round(price * (1 + target_pct / 100), 2)
    max_loss       = round(size * stop_pct / 100, 2)
    potential_gain = round(size * target_pct / 100, 2)

    msg = (
        f"📊 *{ticker} — Position Analysis*\n\n"
        f"Position: *${size}* at *${price}*\n"
        f"Avg daily move (30d): {avg_move:.1f}%\n\n"
        f"Suggested levels:\n"
        f"• Stop loss: -*{stop_pct}%* → ${stop_price} (max loss: *-${max_loss}*)\n"
        f"• Target: +*{target_pct}%* → ${target_price} (potential: *+${potential_gain}*)\n\n"
    )
    if exposure_warning:
        msg += f"{exposure_warning}\n\n"
    msg += "Type *confirm* to accept, or enter your own: `target% stop%`\nExample: `20 8`"

    await update.message.reply_text(msg, parse_mode="Markdown")
    return ASK_CONFIRM


async def bought_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    chat_id = update.effective_chat.id
    ticker = ctx.user_data["bought_ticker"]
    price  = ctx.user_data["bought_price"]
    size   = ctx.user_data.get("bought_size", 0)

    if text == "confirm":
        target_pct = ctx.user_data["target_pct"]
        stop_pct   = ctx.user_data["stop_pct"]
    else:
        try:
            parts      = text.split()
            target_pct = float(parts[0])
            stop_pct   = float(parts[1])
        except (ValueError, IndexError):
            await update.message.reply_text(
                "Type *confirm* or enter `target% stop%` (e.g. `20 8`)",
                parse_mode="Markdown"
            )
            return ASK_CONFIRM

    cs = get_chat_state(state, chat_id)
    if ticker not in cs["tickers"]:
        cs["tickers"].append(ticker)

    cs["entries"][ticker] = {
        "entry_price": price,
        "size": size,
        "target_pct": target_pct,
        "stop_pct": stop_pct,
        "entry_date": now_uk().isoformat(),
    }
    save_state(state)

    stop_price     = round(price * (1 - stop_pct / 100), 2)
    target_price   = round(price * (1 + target_pct / 100), 2)
    max_loss       = round(size * stop_pct / 100, 2) if size else 0
    potential_gain = round(size * target_pct / 100, 2) if size else 0

    await update.message.reply_text(
        f"✅ *{ticker} logged*\n\n"
        f"Position: *${size}* at *${price}*\n"
        f"Stop loss: -{stop_pct}% → *${stop_price}*"
        + (f" (max loss: -${max_loss})" if max_loss else "") + "\n"
        f"Target: +{target_pct}% → *${target_price}*"
        + (f" (potential: +${potential_gain})" if potential_gain else "") + "\n\n"
        f"_Monitoring active. You'll be alerted on any exit signal._",
        parse_mode="Markdown"
    )
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ------------------------------------------------------------------ #
#  /sold — conversational flow                                         #
# ------------------------------------------------------------------ #

async def sold_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Which ticker did you sell?")
    return SELL_ASK_TICKER


async def sold_ticker(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ticker = update.message.text.strip().upper()
    ctx.user_data["sold_ticker"] = ticker
    await update.message.reply_text(
        f"At what price did you sell *{ticker}*?",
        parse_mode="Markdown"
    )
    return SELL_ASK_PRICE


async def sold_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        price = float(update.message.text.strip().replace("$", "").replace(",", ""))
    except ValueError:
        await update.message.reply_text("Please enter a valid price.")
        return SELL_ASK_PRICE

    chat_id = update.effective_chat.id
    ticker  = ctx.user_data["sold_ticker"]
    cs      = get_chat_state(state, chat_id)
    entry   = cs["entries"].get(ticker)
    ctx.user_data["sold_price"] = price

    if entry:
        entry_price = entry["entry_price"]
        size        = entry.get("size", 0)
        pnl_pct     = ((price - entry_price) / entry_price) * 100
        pnl_dollar  = round(size * pnl_pct / 100, 2) if size else 0
        ctx.user_data["pnl_pct"]    = pnl_pct
        ctx.user_data["pnl_dollar"] = pnl_dollar
        sign       = "+" if pnl_pct >= 0 else ""
        dollar_str = f" (≈ {'+' if pnl_dollar >= 0 else ''}${abs(pnl_dollar):.2f})" if size else ""
        outcome    = "✅ Profit" if pnl_pct >= 0 else "❌ Loss"
        await update.message.reply_text(
            f"{outcome}: *{sign}{pnl_pct:.1f}%*{dollar_str}\n"
            f"Entry: ${entry_price} → Exit: ${price}\n\n"
            f"*Quick review — reply with a number:*\n"
            f"1. Followed the bot signal\n"
            f"2. Overrode — sold earlier\n"
            f"3. Overrode — held longer\n"
            f"4. Manual decision\n\n"
            f"Add notes after a space. Example: `1 Clean signal`",
            parse_mode="Markdown"
        )
    else:
        ctx.user_data["pnl_pct"]    = 0
        ctx.user_data["pnl_dollar"] = 0
        await update.message.reply_text("No entry found. Add any notes, or type 'skip'.")
    return SELL_ASK_REVIEW


async def sold_review(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text    = update.message.text.strip()
    chat_id = update.effective_chat.id
    ticker  = ctx.user_data["sold_ticker"]
    cs      = get_chat_state(state, chat_id)

    review_map = {
        "1": "Followed signal",
        "2": "Overrode — sold earlier",
        "3": "Overrode — held longer",
        "4": "Manual decision",
    }
    parts       = text.split(" ", 1)
    exit_reason = review_map.get(parts[0], "Manual decision")
    notes       = parts[1] if len(parts) > 1 else ""
    pnl_pct     = ctx.user_data.get("pnl_pct", 0)
    pnl_dollar  = ctx.user_data.get("pnl_dollar", 0)
    entry       = cs["entries"].get(ticker, {})

    cs["trade_log"].append({
        "ticker":      ticker,
        "entry_price": entry.get("entry_price"),
        "exit_price":  ctx.user_data["sold_price"],
        "size":        entry.get("size", 0),
        "pnl_pct":     pnl_pct,
        "pnl_dollar":  pnl_dollar,
        "exit_reason": exit_reason,
        "notes":       notes,
        "date":        now_uk().isoformat(),
    })
    cs["entries"].pop(ticker, None)
    save_state(state)

    sign       = "+" if pnl_pct >= 0 else ""
    dollar_str = f" (≈ {'+' if pnl_dollar >= 0 else ''}${abs(pnl_dollar):.2f})" if entry.get("size") else ""

    await update.message.reply_text(
        f"✅ *{ticker} trade logged*\n\n"
        f"P&L: *{sign}{pnl_pct:.1f}%*{dollar_str}\n"
        f"Exit: {exit_reason}\n"
        + (f"Notes: _{notes}_\n" if notes else "")
        + "\n_Use /trades to see your full history._",
        parse_mode="Markdown"
    )
    return ConversationHandler.END


async def scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    cs = get_chat_state(state, chat_id)
    tickers = sorted(cs["tickers"])
    if not tickers:
        await update.message.reply_text("No tickers on watchlist.", parse_mode="Markdown")
        return
    await update.message.reply_text(f"⏳ Scanning {', '.join(tickers)}...")
    result = await analyzer.scan_watchlist(tickers, cs.get("entries"))
    await update.message.reply_text(result, parse_mode="Markdown")


async def scheduled_sweep(ctx: ContextTypes.DEFAULT_TYPE):
    from zoneinfo import ZoneInfo
    n = datetime.now(ZoneInfo("America/New_York"))
    if n.weekday() >= 5:
        return
    label = SWEEP_TIMES_ET.get((n.hour, n.minute))
    if not label:
        return
    for chat_id_str, cs in list(state.items()):
        try:
            chat_id = int(chat_id_str)
        except ValueError:
            continue
        if cs.get("paused"):
            continue
        try:
            tickers = cs.get("tickers", [])
            if not tickers:
                continue
            result = await analyzer.get_sweep_summary(tickers, cs.get("entries"))
            await ctx.application.bot.send_message(
                chat_id=chat_id, text=result, parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Sweep error {chat_id}: {e}")


# ------------------------------------------------------------------ #
#  THESIS FOLLOW-UP                                                    #
# ------------------------------------------------------------------ #

async def thesis_follow_up(ctx: ContextTypes.DEFAULT_TYPE):
    data      = ctx.job.data
    chat_id   = data["chat_id"]
    ticker    = data["ticker"]
    iteration = data["iteration"]
    cs        = get_chat_state(state, chat_id)
    entry     = cs["entries"].get(ticker)
    now       = now_uk().strftime("%b %d, %H:%M") + " UK"

    # Remove this iteration from pending
    cs["pending_follow_ups"] = [
        f for f in cs.get("pending_follow_ups", [])
        if not (f["ticker"] == ticker and f["iteration"] == iteration)
    ]

    try:
        price_data     = await analyzer.get_price_change(ticker)
        market_context = await analyzer.get_market_context()
        lunar          = await analyzer.get_lunarcrush_sentiment(ticker)
        sentiment      = analyzer.get_sentiment(ticker, market_context, lunar)
        thesis_status  = sentiment.get("thesis_status", "intact")
        thesis_note    = sentiment.get("thesis_note", "")

        if thesis_status == "intact":
            msg = (
                f"✅ *{ticker} thesis cleared*\n\n"
                f"Concern has passed. Thesis intact.\n"
                f"_{thesis_note}_\n\n"
                f"⏱ {now}"
            )

        elif thesis_status == "under_review" and iteration < 3:
            next_iter = iteration + 1
            due_time  = datetime.now() + timedelta(hours=2)
            cs["pending_follow_ups"].append({
                "ticker":       ticker,
                "iteration":    next_iter,
                "due_time_iso": due_time.isoformat(),
            })
            ctx.application.job_queue.run_once(
                thesis_follow_up,
                when=7200,
                data={"chat_id": chat_id, "ticker": ticker, "iteration": next_iter}
            )
            msg = (
                f"⚠️ *{ticker} thesis still under review* (update {iteration}/3)\n\n"
                f"_{thesis_note}_\n\n"
                f"Next check in 2 hours.\n"
                f"⏱ {now}"
            )

        elif thesis_status == "under_review":
            msg = (
                f"⚠️ *{ticker} thesis unresolved after 6 hours*\n\n"
                f"_{thesis_note}_\n\n"
                f"Recommend manual review. Normal monitoring resumes.\n"
                f"⏱ {now}"
            )

        else:  # broken
            exit_result = (
                analyzer.check_exit_trigger(ticker, entry, price_data, sentiment)
                if entry and price_data else None
            )
            if exit_result:
                msg, _ = exit_result
            else:
                msg = (
                    f"🔴 *THESIS BROKEN — {ticker}*\n\n"
                    f"_{thesis_note}_\n\n"
                    f"The core reason to hold this stock no longer exists.\n"
                    f"*Review your position immediately.*\n"
                    f"⏱ {now}"
                )

        save_state(state)
        await ctx.application.bot.send_message(
            chat_id=chat_id, text=msg, parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"Thesis follow-up error for {ticker}: {e}")


# ------------------------------------------------------------------ #
#  SCHEDULED JOBS                                                      #
# ------------------------------------------------------------------ #

async def scheduled_check(ctx: ContextTypes.DEFAULT_TYPE):
    logger.info("Running scheduled check...")
    for chat_id_str, cs in list(state.items()):
        try:
            chat_id = int(chat_id_str)
        except ValueError:
            continue
        if cs.get("paused"):
            continue
        for ticker in list(cs.get("tickers", [])):
            if is_on_cooldown(chat_id, ticker):
                continue
            try:
                entry = cs["entries"].get(ticker)
                msg, strength, signal_type = await analyzer.run(
                    ticker,
                    threshold=get_ticker_threshold(cs, ticker),
                    force=False,
                    entry=entry,
                )
                if not msg:
                    continue

                # Earnings blackout — notify once per 48h
                if signal_type == "EARNINGS_BLACKOUT":
                    if not has_earnings_been_notified(cs, ticker):
                        await ctx.application.bot.send_message(
                            chat_id=chat_id, text=msg, parse_mode="Markdown"
                        )
                        mark_earnings_notified(cs, ticker)
                        save_state(state)
                    continue

                # Thesis under review — soft alert + schedule follow-up
                if signal_type == "THESIS_UNDER_REVIEW":
                    await ctx.application.bot.send_message(
                        chat_id=chat_id, text=msg, parse_mode="Markdown"
                    )
                    set_cooldown(chat_id, ticker)
                    due_time = datetime.now() + timedelta(hours=2)
                    cs["pending_follow_ups"] = [
                        f for f in cs.get("pending_follow_ups", [])
                        if f["ticker"] != ticker
                    ]
                    cs["pending_follow_ups"].append({
                        "ticker":       ticker,
                        "iteration":    1,
                        "due_time_iso": due_time.isoformat(),
                    })
                    ctx.application.job_queue.run_once(
                        thesis_follow_up,
                        when=7200,
                        data={"chat_id": chat_id, "ticker": ticker, "iteration": 1}
                    )
                    save_state(state)
                    continue

                # Regular signal or exit trigger
                await ctx.application.bot.send_message(
                    chat_id=chat_id, text=msg, parse_mode="Markdown"
                )
                set_cooldown(chat_id, ticker)
                cs["signal_log"].append({
                    "ticker":   ticker,
                    "signal":   signal_type or "SIGNAL",
                    "strength": strength,
                    "ts":       datetime.now().isoformat(),
                })
                save_state(state)

            except Exception as e:
                logger.error(f"Check error {ticker}: {e}")


async def morning_ping(ctx: ContextTypes.DEFAULT_TYPE):
    now = now_uk()
    if now.weekday() >= 5:
        return
    if not (now.hour == 13 and 54 <= now.minute <= 56):
        return
    today = now.strftime("%Y-%m-%d")
    for chat_id_str, cs in list(state.items()):
        try:
            chat_id = int(chat_id_str)
        except ValueError:
            continue
        if cs.get("paused"):
            continue
        # Fire exactly once per day
        if cs.get("morning_ping_sent") == today:
            continue
        try:
            tickers = cs.get("tickers", [])
            pulse   = analyzer.get_market_pulse(tickers, cs.get("entries"))
            entries_note = ""
            if cs.get("entries"):
                entries_note = "\n📌 Open: " + ", ".join(
                    f"{t} (${e['entry_price']})" for t, e in cs["entries"].items()
                )
            await ctx.application.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🌅 *Good afternoon — US market opens in 35 minutes*\n\n"
                    f"{pulse}"
                    f"{entries_note}"
                ),
                parse_mode="Markdown"
            )
            # Send brief as one message per stock to avoid Telegram 4096 char limit
            for ticker in tickers:
                ticker_brief = analyzer.get_brief([ticker], cs.get("entries"))
                await ctx.application.bot.send_message(
                    chat_id=chat_id,
                    text=f"📋 *{ticker} — Pre-Market Brief*\n\n{ticker_brief}",
                    parse_mode="Markdown"
                )
            cs["morning_ping_sent"] = today
            save_state(state)
        except Exception as e:
            logger.error(f"Morning ping error {chat_id}: {e}")


async def weekly_summary_job(ctx: ContextTypes.DEFAULT_TYPE):
    now = now_uk()
    if now.weekday() != 4:
        return
    if not (now.hour == 21 and 4 <= now.minute <= 6):
        return
    today = now.strftime("%Y-%m-%d")
    for chat_id_str, cs in list(state.items()):
        try:
            chat_id = int(chat_id_str)
        except ValueError:
            continue
        # Fire exactly once per week
        if cs.get("weekly_summary_sent") == today:
            continue
        try:
            msg = analyzer.format_weekly_summary(
                cs.get("signal_log", []),
                cs.get("trade_log", [])
            )
            await ctx.application.bot.send_message(
                chat_id=chat_id, text=msg, parse_mode="Markdown"
            )
            cs["signal_log"] = []
            cs["weekly_summary_sent"] = today
            save_state(state)
        except Exception as e:
            logger.error(f"Weekly summary error {chat_id}: {e}")


# ------------------------------------------------------------------ #
#  MAIN                                                                #
# ------------------------------------------------------------------ #

async def post_init(application: Application) -> None:
    reschedule_pending_follow_ups(application)


def main():
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    bought_handler = ConversationHandler(
        entry_points=[CommandHandler("bought", bought_start)],
        states={
            ASK_TICKER:  [MessageHandler(filters.TEXT & ~filters.COMMAND, bought_ticker)],
            ASK_PRICE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, bought_price)],
            ASK_SIZE:    [MessageHandler(filters.TEXT & ~filters.COMMAND, bought_size)],
            ASK_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, bought_confirm)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    sold_handler = ConversationHandler(
        entry_points=[CommandHandler("sold", sold_start)],
        states={
            SELL_ASK_TICKER: [MessageHandler(filters.TEXT & ~filters.COMMAND, sold_ticker)],
            SELL_ASK_PRICE:  [MessageHandler(filters.TEXT & ~filters.COMMAND, sold_price)],
            SELL_ASK_REVIEW: [MessageHandler(filters.TEXT & ~filters.COMMAND, sold_review)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(bought_handler)
    app.add_handler(sold_handler)
    app.add_handler(CommandHandler("start",     start))
    app.add_handler(CommandHandler("help",      start))
    app.add_handler(CommandHandler("watch",     watch))
    app.add_handler(CommandHandler("unwatch",   unwatch))
    app.add_handler(CommandHandler("list",      list_cmd))
    app.add_handler(CommandHandler("check",     check))
    app.add_handler(CommandHandler("scan",      scan))
    app.add_handler(CommandHandler("brief",     brief))
    app.add_handler(CommandHandler("entries",   entries_cmd))
    app.add_handler(CommandHandler("trades",    trades_cmd))
    app.add_handler(CommandHandler("capital",   capital_cmd))
    app.add_handler(CommandHandler("pause",     pause))
    app.add_handler(CommandHandler("resume",    resume))
    app.add_handler(CommandHandler("threshold", threshold_cmd))
    app.add_handler(CommandHandler("debug",     debug_cmd))

    app.job_queue.run_repeating(scheduled_check,    interval=1800, first=60)
    app.job_queue.run_repeating(morning_ping,        interval=60,   first=30)
    app.job_queue.run_repeating(weekly_summary_job,  interval=60,   first=30)
    app.job_queue.run_repeating(scheduled_sweep,     interval=60,   first=45)

    logger.info("Signal bot started — full stack active.")
    app.run_polling()


if __name__ == "__main__":
    main()
