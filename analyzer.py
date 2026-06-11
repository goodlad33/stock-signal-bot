import asyncio
import anthropic
import httpx
import logging
import json
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

POLYGON_BASE = "https://api.polygon.io/v2"
ALPHA_VANTAGE_BASE = "https://www.alphavantage.co/query"
LUNARCRUSH_BASE = "https://lunarcrush.com/api4/public"

MARKET_OPEN_HOUR, MARKET_OPEN_MIN = 9, 30
MARKET_CLOSE_HOUR, MARKET_CLOSE_MIN = 16, 0
AVOID_FIRST_MINUTES = 30
AVOID_LAST_MINUTES = 30

DEFAULT_THRESHOLD = 1.5
MIN_CONFIDENCE = 7
MIN_STRENGTH = 4

MARKET_TICKERS = ["SPY", "QQQ"]


def now_et():
    return datetime.now(ZoneInfo("America/New_York"))


def now_uk():
    return datetime.now(ZoneInfo("Europe/London"))


def is_market_open() -> bool:
    n = now_et()
    if n.weekday() >= 5:
        return False
    open_time = n.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MIN, second=0, microsecond=0)
    close_time = n.replace(hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MIN, second=0, microsecond=0)
    return open_time <= n <= close_time


def is_volatile_session() -> bool:
    n = now_et()
    if not is_market_open():
        return False
    open_time = n.replace(hour=MARKET_OPEN_HOUR, minute=MARKET_OPEN_MIN, second=0, microsecond=0)
    close_time = n.replace(hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MIN, second=0, microsecond=0)
    in_open_window = (n - open_time).seconds < AVOID_FIRST_MINUTES * 60
    in_close_window = (close_time - n).seconds < AVOID_LAST_MINUTES * 60
    return in_open_window or in_close_window


def signal_strength(price_pct: float, confidence: int, threshold: float) -> int:
    price_score = min(5, int(abs(price_pct) / threshold * 2.5))
    conf_score = min(5, int(confidence / 2))
    return max(1, min(5, (price_score + conf_score) // 2))


def strength_stars(strength: int) -> str:
    return "⭐" * strength + "☆" * (5 - strength)


class SignalAnalyzer:
    def __init__(self, anthropic_key: str, polygon_key: str,
                 alpha_vantage_key: str = None, lunarcrush_key: str = None,
                 finnhub_key: str = None):
        self.claude = anthropic.Anthropic(api_key=anthropic_key)
        self.polygon_key = polygon_key
        self.finnhub_key = finnhub_key
        self.alpha_vantage_key = alpha_vantage_key
        self.lunarcrush_key = lunarcrush_key if lunarcrush_key != "placeholder" else None

    # ------------------------------------------------------------------ #
    #  PRICE DATA                                                          #
    # ------------------------------------------------------------------ #

    async def get_price_change(self, ticker: str) -> dict:
        url = "https://finnhub.io/api/v1/quote"
        params = {"symbol": ticker, "token": self.finnhub_key}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(url, params=params)
                data = r.json()
            current = data.get("c", 0)
            previous = data.get("pc", 0)
            if not current or not previous:
                return {}
            change_pct = ((current - previous) / previous) * 100
            return {
                "ticker": ticker,
                "current": round(current, 2),
                "previous": round(previous, 2),
                "change": round(current - previous, 2),
                "change_pct": round(change_pct, 2),
            }
        except Exception as e:
            logger.error(f"Finnhub price error for {ticker}: {e}")
            return {}

    # ------------------------------------------------------------------ #
    #  VOLATILITY                                                          #
    # ------------------------------------------------------------------ #

    async def get_volatility(self, ticker: str) -> dict:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=35)
        url = (
            f"{POLYGON_BASE}/aggs/ticker/{ticker}/range/1/day"
            f"/{start.strftime('%Y-%m-%d')}/{end.strftime('%Y-%m-%d')}"
        )
        params = {"adjusted": "true", "sort": "asc", "limit": 35, "apiKey": self.polygon_key}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(url, params=params)
                data = r.json()
            bars = data.get("results", [])
            if len(bars) < 10:
                return {"avg_daily_move": 2.0, "stop_pct": 4.0, "target_pct": 6.0}
            closes = [b["c"] for b in bars]
            daily_moves = [
                abs((closes[i] - closes[i - 1]) / closes[i - 1]) * 100
                for i in range(1, len(closes))
            ]
            avg = sum(daily_moves) / len(daily_moves)
            return {
                "avg_daily_move": round(avg, 2),
                "stop_pct": round(avg * 2, 1),
                "target_pct": round(avg * 3, 1),
                "current_price": closes[-1],
            }
        except Exception as e:
            logger.error(f"Volatility error for {ticker}: {e}")
            return {"avg_daily_move": 2.0, "stop_pct": 4.0, "target_pct": 6.0}

    # ------------------------------------------------------------------ #
    #  MARKET CONTEXT                                                      #
    # ------------------------------------------------------------------ #

    async def get_market_context(self) -> dict:
        results = {}
        for ticker in MARKET_TICKERS:
            data = await self.get_price_change(ticker)
            if data:
                results[ticker] = data.get("change_pct", 0)
        if not results:
            return {"context": "unknown", "spy_pct": 0, "qqq_pct": 0}
        spy = results.get("SPY", 0)
        qqq = results.get("QQQ", 0)
        avg = (spy + qqq) / 2
        context = "broadly bullish" if avg >= 1.0 else "broadly bearish" if avg <= -1.0 else "neutral"
        return {"context": context, "spy_pct": spy, "qqq_pct": qqq, "avg_pct": avg}

    # ------------------------------------------------------------------ #
    #  EARNINGS CALENDAR                                                   #
    # ------------------------------------------------------------------ #

    async def get_upcoming_earnings(self, ticker: str) -> dict | None:
        if not self.alpha_vantage_key:
            return None
        try:
            params = {
                "function": "EARNINGS_CALENDAR",
                "symbol": ticker,
                "horizon": "3month",
                "apikey": self.alpha_vantage_key,
            }
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(ALPHA_VANTAGE_BASE, params=params)
                text = r.text
            lines = text.strip().split("\n")
            if len(lines) < 2:
                return None
            for line in lines[1:]:
                parts = line.split(",")
                if len(parts) < 3:
                    continue
                symbol, _, report_date = parts[0], parts[1], parts[2]
                if symbol.upper() != ticker.upper():
                    continue
                try:
                    earnings_dt = datetime.strptime(report_date.strip(), "%Y-%m-%d")
                    days_until = (earnings_dt.date() - datetime.now().date()).days
                    if 0 <= days_until <= 2:
                        return {"ticker": ticker, "date": report_date.strip(), "days_until": days_until}
                except ValueError:
                    continue
        except Exception as e:
            logger.error(f"Earnings error for {ticker}: {e}")
        return None

    # ------------------------------------------------------------------ #
    #  LUNARCRUSH                                                          #
    # ------------------------------------------------------------------ #

    async def get_lunarcrush_sentiment(self, ticker: str) -> dict | None:
        if not self.lunarcrush_key:
            return None
        try:
            url = f"{LUNARCRUSH_BASE}/stocks/{ticker}/v1"
            headers = {"Authorization": f"Bearer {self.lunarcrush_key}"}
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(url, headers=headers)
                if r.status_code == 429:
                    logger.warning(f"LunarCrush rate limit hit for {ticker}")
                    return None
                data = r.json()
            item = data.get("data", {})
            if not item:
                return None
            score = item.get("sentiment", 50)
            galaxy_score = item.get("galaxy_score", 50)
            social_volume = item.get("social_volume_24h", 0)
            sentiment = "bullish" if score >= 65 else "bearish" if score <= 35 else "neutral"
            return {"sentiment": sentiment, "score": score,
                    "galaxy_score": galaxy_score, "social_volume_24h": social_volume}
        except Exception as e:
            logger.error(f"LunarCrush error for {ticker}: {e}")
            return None

    # ------------------------------------------------------------------ #
    #  DEEP SENTIMENT                                                      #
    # ------------------------------------------------------------------ #

    def get_sentiment(self, ticker: str, market_context: dict = None,
                      lunar: dict = None) -> dict:
        market_note = ""
        if market_context:
            market_note = (
                f"The broader market is currently {market_context.get('context', 'unknown')} "
                f"(SPY {market_context.get('spy_pct', 0):+.1f}%, "
                f"QQQ {market_context.get('qqq_pct', 0):+.1f}%). "
                f"If {ticker} is moving purely with the market, that is NOT a stock-specific signal."
            )

        lunar_note = ""
        if lunar:
            lc_direction = lunar.get("sentiment", "neutral")
            lunar_note = (
                f"LunarCrush social data shows {lc_direction} sentiment "
                f"(score {lunar.get('score')}/100, galaxy score {lunar.get('galaxy_score')}/100, "
                f"24h social volume {lunar.get('social_volume_24h')}).\n"
                f"Calibrate your confidence:\n"
                f"- LunarCrush CONFIRMS news direction → raise confidence 1-2 points\n"
                f"- LunarCrush CONTRADICTS news direction → lower confidence 1-2 points, set conflicting_signals=true\n"
                f"- LunarCrush neutral → no adjustment\n"
                f"Confidence must reflect BOTH news quality AND social alignment as one unified number."
            )

        prompt = f"""You are a professional equity analyst. Search the web for the latest news about {ticker} stock from the past 2 hours.

{market_note}
{lunar_note}

Reason through ALL of the following before giving your verdict:
1. Is the news stock-specific or just broad market movement?
2. Is the catalyst genuine (earnings, contract, product, regulatory) or just analyst commentary?
3. Is news volume increasing or fading?
4. Are multiple credible sources confirming the same direction?
5. Is there conflicting news that could reverse this move?
6. Is the core long-term investment case for {ticker} still intact?

Return ONLY this JSON object, no markdown, no preamble:
{{
  "sentiment": "bullish" or "bearish" or "neutral",
  "confidence": number 1-10,
  "reason": "one sentence — the specific catalyst, not generic commentary",
  "top_headline": "the single most impactful headline you found",
  "momentum": "accelerating" or "steady" or "fading",
  "is_market_driven": true or false,
  "conflicting_signals": true or false,
  "thesis_status": "intact" or "under_review" or "broken",
  "thesis_note": "one sentence explaining the thesis assessment"
}}

thesis_status definitions:
- intact: core investment case unchanged, nothing material happened today
- under_review: something happened worth monitoring but does not yet invalidate the thesis
- broken: the fundamental reason to hold this stock no longer exists

Be strict. Confidence below 7 means no signal fires."""

        try:
            response = self.claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1024,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{"role": "user", "content": prompt}]
            )
            text = "".join(b.text for b in response.content if hasattr(b, "text")).strip()
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                result = json.loads(match.group())
                if result.get("is_market_driven"):
                    result["confidence"] = min(result.get("confidence", 0), 4)
                    result["reason"] = f"[Market-driven] {result.get('reason', '')}"
                return result
        except Exception as e:
            logger.error(f"Claude sentiment error for {ticker}: {e}")

        return {
            "sentiment": "neutral", "confidence": 0,
            "reason": "Could not retrieve news.", "top_headline": "",
            "momentum": "steady", "is_market_driven": False,
            "conflicting_signals": False,
            "thesis_status": "intact",
            "thesis_note": "Analysis unavailable.",
        }

    # ------------------------------------------------------------------ #
    #  BRIEF                                                               #
    # ------------------------------------------------------------------ #

    def get_brief(self, tickers: list, entries: dict = None) -> str:
        ticker_list = ", ".join(tickers)
        position_context = ""
        if entries:
            lines = [
                f"{t}: entry ${e['entry_price']}, size ${e.get('size', '?')}, "
                f"target +{e['target_pct']}%, stop -{e['stop_pct']}%"
                for t, e in entries.items()
            ]
            position_context = "Open positions:\n" + "\n".join(lines)

        prompt = f"""You are a professional equity analyst preparing a morning briefing for a private investor.

Search the web for:
1. US market futures and overall market mood for today
2. Key economic events or Fed speakers scheduled today
3. Latest news on each of these stocks: {ticker_list}

{position_context}

For each stock provide:
- Current sentiment (bullish/bearish/neutral) with specific reason
- One key catalyst driving it today
- One risk to watch
- If a position is held, state clearly whether the thesis still holds

End with a 2-sentence market summary and one actionable observation.

Plain text, professional, direct. No generic filler."""

        try:
            response = self.claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1200,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{"role": "user", "content": prompt}]
            )
            return "".join(b.text for b in response.content if hasattr(b, "text")).strip()
        except Exception as e:
            logger.error(f"Brief error: {e}")
            return "Could not retrieve morning brief. Try again shortly."

    # ------------------------------------------------------------------ #
    #  MARKET PULSE                                                        #
    # ------------------------------------------------------------------ #

    def get_market_pulse(self, tickers: list, entries: dict = None) -> str:
        ticker_list = ", ".join(tickers)
        position_lines = ""
        if entries:
            position_lines = "Open positions: " + ", ".join(
                f"{t} (entry ${e['entry_price']})" for t, e in entries.items()
            )

        prompt = f"""Search the web for current US market futures and pre-market conditions.

Return a concise market pulse covering:
1. S&P 500 and Nasdaq futures direction and magnitude
2. One key event scheduled for today
3. Any pre-market movers relevant to: {ticker_list}
4. One sentence overall market mood

{position_lines}

Under 100 words. Direct, factual, no fluff."""

        try:
            response = self.claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{"role": "user", "content": prompt}]
            )
            return "".join(b.text for b in response.content if hasattr(b, "text")).strip()
        except Exception as e:
            logger.error(f"Market pulse error: {e}")
            return "Could not retrieve market pulse."

    # ------------------------------------------------------------------ #
    #  WEEKLY SUMMARY                                                      #
    # ------------------------------------------------------------------ #

    def format_weekly_summary(self, signal_log: list, trade_log: list = None) -> str:
        now = now_uk().strftime("%b %d")
        lines = [f"📋 *Weekly Summary — {now}*\n"]

        if not signal_log:
            lines += [
                "No signals fired this week.",
                "Market conditions did not meet the threshold.",
                "\n_No signal is better than a weak signal._",
            ]
        else:
            buys = [s for s in signal_log if "BUY" in s.get("signal", "")]
            sells = [s for s in signal_log if "SELL" in s.get("signal", "")]
            tickers_hit = list(set(s["ticker"] for s in signal_log))
            lines += [
                f"Total signals: *{len(signal_log)}*",
                f"🚀 BUY: {len(buys)} · 🔻 SELL: {len(sells)}",
                f"Tickers triggered: {', '.join(tickers_hit)}",
            ]

        if trade_log:
            lines.append("\n*Trades this week:*")
            total_pnl = 0
            for t in trade_log:
                pnl = t.get("pnl_pct", 0)
                total_pnl += pnl
                sign = "+" if pnl >= 0 else ""
                dollar = t.get("pnl_dollar", 0)
                dollar_str = f" (≈ {'+' if dollar >= 0 else ''}${abs(dollar):.2f})" if dollar else ""
                outcome = "✅" if pnl >= 0 else "❌"
                lines.append(
                    f"{outcome} *{t['ticker']}* — {sign}{pnl:.1f}%{dollar_str} "
                    f"| {t.get('exit_reason', 'manual')}"
                )
            sign = "+" if total_pnl >= 0 else ""
            lines.append(f"\nNet P&L this week: *{sign}{total_pnl:.1f}%*")

        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    #  EXIT TRIGGER                                                        #
    # ------------------------------------------------------------------ #

    def check_exit_trigger(self, ticker: str, entry: dict,
                           price_data: dict, sentiment: dict) -> tuple | None:
        """Returns (message, exit_type) or None."""
        if not price_data or not entry:
            return None

        current = price_data["current"]
        entry_price = entry["entry_price"]
        target_pct = entry["target_pct"]
        stop_pct = entry["stop_pct"]
        size = entry.get("size", 0)
        gain_pct = ((current - entry_price) / entry_price) * 100
        now = now_uk().strftime("%b %d, %H:%M") + " UK"

        # Priority 1 — Stop loss (firm, no nuance)
        if gain_pct <= -stop_pct:
            loss_amount = round(size * abs(gain_pct) / 100, 2) if size else 0
            msg = (
                f"🔴 *STOP LOSS — {ticker}*\n\n"
                f"Entry: ${entry_price} · Current: ${current}\n"
                f"Loss: *{gain_pct:.1f}%*"
                + (f" (≈ -${loss_amount})" if loss_amount else "")
                + f" — stop at -{stop_pct}% hit\n\n"
                f"*Cut this position now.*\n"
                f"⏱ {now}"
            )
            return msg, "STOP_LOSS"

        # Priority 2 — Thesis broken
        if sentiment.get("thesis_status") == "broken":
            thesis_note = sentiment.get("thesis_note", "")
            sign = "+" if gain_pct >= 0 else ""
            msg = (
                f"🔴 *THESIS BROKEN — {ticker}*\n\n"
                f"_{thesis_note}_\n\n"
                f"Entry: ${entry_price} · Current: ${current} ({sign}{gain_pct:.1f}%)\n"
                f"The core reason to hold this stock no longer exists.\n\n"
                f"*Exit this position.*\n"
                f"⏱ {now}"
            )
            return msg, "THESIS_BROKEN"

        # Priority 3 — Target reached + momentum fading
        if gain_pct >= target_pct:
            momentum = sentiment.get("momentum", "steady")
            s = sentiment.get("sentiment", "neutral")
            if momentum == "fading" or s == "bearish":
                profit_amount = round(size * gain_pct / 100, 2) if size else 0
                msg = (
                    f"🎯 *TARGET REACHED — {ticker}*\n\n"
                    f"Entry: ${entry_price} · Current: ${current}\n"
                    f"Gain: *+{gain_pct:.1f}%*"
                    + (f" (≈ +${profit_amount})" if profit_amount else "")
                    + f" — target of +{target_pct}% reached\n"
                    f"Momentum: *{momentum}* · Sentiment turning: *{s}*\n\n"
                    f"_Conditions suggest reviewing your position._\n"
                    f"⏱ {now}"
                )
                return msg, "TARGET_REACHED"

        return None

    # ------------------------------------------------------------------ #
    #  THESIS FORMATTERS                                                   #
    # ------------------------------------------------------------------ #

    def format_thesis_under_review(self, ticker: str, sentiment: dict,
                                   price_data: dict, entry: dict) -> str:
        now = now_uk().strftime("%b %d, %H:%M") + " UK"
        thesis_note = sentiment.get("thesis_note", "")
        position_line = ""
        if entry and price_data:
            current = price_data["current"]
            entry_price = entry["entry_price"]
            gain_pct = ((current - entry_price) / entry_price) * 100
            sign = "+" if gain_pct >= 0 else ""
            position_line = (
                f"Entry: ${entry_price} · Current: ${current} ({sign}{gain_pct:.1f}%)\n\n"
            )
        return (
            f"⚠️ *THESIS UNDER REVIEW — {ticker}*\n\n"
            f"_{thesis_note}_\n\n"
            f"{position_line}"
            f"Not an exit signal — monitor closely.\n"
            f"I will check again in 2 hours and update you.\n\n"
            f"⏱ {now}"
        )

    def format_earnings_warning(self, ticker: str, earnings: dict) -> str:
        now = now_uk().strftime("%b %d, %H:%M") + " UK"
        days = earnings["days_until"]
        date = earnings["date"]
        when = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
        return (
            f"⚠️ *EARNINGS BLACKOUT — {ticker}*\n\n"
            f"Earnings report {when} ({date}).\n"
            f"Signals suspended for 48 hours.\n\n"
            f"_Volatility around earnings makes signals unreliable.\n"
            f"Monitoring resumes after the report._\n\n"
            f"⏱ {now}"
        )

    # ------------------------------------------------------------------ #
    #  SCAN — on-demand ranked overview of full watchlist                 #
    # ------------------------------------------------------------------ #

    async def scan_watchlist(self, tickers: list, entries: dict = None) -> str:
        """
        Full on-demand scan. Gets prices in parallel, sentiment sequentially.
        Returns tickers ranked by combined price + sentiment score.
        """
        price_tasks = [self.get_price_change(t) for t in tickers]
        prices = await asyncio.gather(*price_tasks, return_exceptions=True)
        price_map = {
            t: p for t, p in zip(tickers, prices)
            if isinstance(p, dict) and p
        }
        market_context = await self.get_market_context()

        results = []
        for ticker in tickers:
            price_data = price_map.get(ticker, {})
            lunar      = await self.get_lunarcrush_sentiment(ticker)
            sentiment  = self.get_sentiment(ticker, market_context, lunar)
            pct        = price_data.get("change_pct", 0) if price_data else 0
            confidence = sentiment.get("confidence", 0)
            score      = abs(pct) * confidence
            results.append({
                "ticker":       ticker,
                "price_data":   price_data,
                "sentiment":    sentiment,
                "pct":          pct,
                "confidence":   confidence,
                "s":            sentiment.get("sentiment", "neutral"),
                "momentum":     sentiment.get("momentum", "steady"),
                "thesis_status":sentiment.get("thesis_status", "intact"),
                "score":        score,
            })

        results.sort(key=lambda x: x["score"], reverse=True)

        now = now_uk().strftime("%b %d, %H:%M") + " UK"
        lines = [f"📡 *Watchlist Scan — {now}*\n"]

        if market_context.get("context") != "unknown":
            lines.append(
                f"🌍 Market: *{market_context['context']}* "
                f"(SPY {market_context['spy_pct']:+.1f}% · "
                f"QQQ {market_context['qqq_pct']:+.1f}%)\n"
            )

        for i, r in enumerate(results, 1):
            ticker       = r["ticker"]
            pct          = r["pct"]
            s            = r["s"]
            confidence   = r["confidence"]
            momentum     = r["momentum"]
            thesis_status = r["thesis_status"]
            sign         = "+" if pct >= 0 else ""

            if pct >= 1.5 and s == "bullish":
                direction = "🚀"
            elif pct >= 0.3:
                direction = "📈"
            elif pct <= -1.5 and s == "bearish":
                direction = "🔻"
            elif pct <= -0.3:
                direction = "📉"
            else:
                direction = "🟡"

            thesis_flag = " ⚠️" if thesis_status == "under_review" else " ✗" if thesis_status == "broken" else ""

            position_str = ""
            entry = (entries or {}).get(ticker)
            if entry and r["price_data"]:
                current    = r["price_data"]["current"]
                gain_pct   = ((current - entry["entry_price"]) / entry["entry_price"]) * 100
                gain_sign  = "+" if gain_pct >= 0 else ""
                position_str = f" · pos {gain_sign}{gain_pct:.1f}%"

            lines.append(
                f"{i}. {direction} *{ticker}* {sign}{pct:.1f}% · "
                f"{s.capitalize()} {confidence}/10 · {momentum}"
                f"{thesis_flag}{position_str}"
            )

        # Top opportunity callout
        top = results[0] if results else None
        if top and top["s"] != "neutral" and top["confidence"] >= MIN_CONFIDENCE:
            lines.append(
                f"\n_Top opportunity: *{top['ticker']}* — "
                f"price and sentiment aligned. Use /check {top['ticker']} for full analysis._"
            )
        elif top:
            lines.append("\n_No strong signals right now. Market appears quiet._")

        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    #  SWEEP — lightweight scheduled market overview                      #
    # ------------------------------------------------------------------ #

    async def get_sweep_summary(self, tickers: list, entries: dict = None) -> str:
        """
        Lightweight sweep: prices in parallel, sentiment only for movers >1%.
        Used for scheduled 3x daily market overviews.
        """
        price_tasks = [self.get_price_change(t) for t in tickers]
        prices      = await asyncio.gather(*price_tasks, return_exceptions=True)
        price_map   = {
            t: p for t, p in zip(tickers, prices)
            if isinstance(p, dict) and p
        }
        market_context = await self.get_market_context()

        n_et     = now_et()
        n_uk     = now_uk()
        et_str   = n_et.strftime("%I:%M %p ET")
        uk_str   = n_uk.strftime("%b %d, %H:%M UK")
        lines    = [f"📊 *Market Sweep — {et_str} ({uk_str})*\n"]

        if market_context.get("context") != "unknown":
            lines.append(
                f"🌍 *Market: {market_context['context']}* "
                f"(SPY {market_context['spy_pct']:+.1f}% · "
                f"QQQ {market_context['qqq_pct']:+.1f}%)\n"
            )

        # Sort by absolute move
        ticker_moves = []
        for ticker in tickers:
            pd  = price_map.get(ticker, {})
            pct = pd.get("change_pct", 0) if pd else 0
            ticker_moves.append((ticker, pct, pd))
        ticker_moves.sort(key=lambda x: abs(x[1]), reverse=True)

        lines.append("*Your watchlist:*")
        active_tickers = []

        for ticker, pct, price_data in ticker_moves:
            sign = "+" if pct >= 0 else ""
            if abs(pct) >= 1.0:
                direction = "🚀" if pct >= 0 else "🔻"
                active_tickers.append(ticker)
            elif abs(pct) >= 0.3:
                direction = "📈" if pct >= 0 else "📉"
            else:
                direction = "🟡"

            entry = (entries or {}).get(ticker)
            position_str = ""
            if entry and price_data:
                gain_pct   = ((price_data["current"] - entry["entry_price"]) / entry["entry_price"]) * 100
                gain_sign  = "+" if gain_pct >= 0 else ""
                position_str = f" · pos {gain_sign}{gain_pct:.1f}%"

            lines.append(f"{direction} *{ticker}* {sign}{pct:.1f}%{position_str}")

        # Next sweep time
        et_hm        = n_et.hour * 60 + n_et.minute
        sweep_times  = [(9, 45), (12, 30), (15, 30)]
        next_sweep   = next(
            (f"{h:02d}:{m:02d} ET" for h, m in sweep_times if h * 60 + m > et_hm),
            None
        )

        if active_tickers:
            lines.append(
                f"\n_Active movers: {', '.join(active_tickers)} — "
                f"use /check or /scan for deeper analysis_"
            )
        else:
            lines.append("\n_No significant moves. Market quiet._")

        if next_sweep:
            lines.append(f"_Next sweep: {next_sweep}_")

        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    #  SIGNAL EVALUATION                                                   #
    # ------------------------------------------------------------------ #

    def evaluate_signal(self, price_data: dict, sentiment: dict,
                        threshold: float, market_context: dict = None) -> str | None:
        if not price_data:
            return None
        pct = price_data["change_pct"]
        s = sentiment.get("sentiment", "neutral")
        confidence = sentiment.get("confidence", 0)
        if confidence < MIN_CONFIDENCE:
            return None
        if sentiment.get("is_market_driven"):
            return None
        if sentiment.get("conflicting_signals"):
            return None
        if pct >= threshold and s == "bullish":
            return "BUY"
        if pct <= -threshold and s == "bearish":
            return "SELL"
        return None

    # ------------------------------------------------------------------ #
    #  FULL PIPELINE                                                       #
    # ------------------------------------------------------------------ #

    async def run(self, ticker: str, threshold: float, force: bool = False,
                  entry: dict = None) -> tuple:
        """Returns (message, strength, signal_type)."""
        if not force and not is_market_open():
            return None, 0, None

        # Earnings blackout — check before anything else
        if not force:
            earnings = await self.get_upcoming_earnings(ticker)
            if earnings:
                return self.format_earnings_warning(ticker, earnings), 0, "EARNINGS_BLACKOUT"

        price_data = await self.get_price_change(ticker)
        market_context = await self.get_market_context()
        lunar = await self.get_lunarcrush_sentiment(ticker)
        sentiment = self.get_sentiment(ticker, market_context, lunar)

        # Thesis under review for open positions — soft alert, triggers follow-up in bot.py
        if entry and sentiment.get("thesis_status") == "under_review":
            msg = self.format_thesis_under_review(ticker, sentiment, price_data, entry)
            return msg, 3, "THESIS_UNDER_REVIEW"

        # Exit triggers (stop loss, thesis broken, target reached)
        if entry and price_data:
            exit_result = self.check_exit_trigger(ticker, entry, price_data, sentiment)
            if exit_result:
                exit_msg, exit_type = exit_result
                return exit_msg, 5, exit_type

        # Regular buy/sell signal
        signal = self.evaluate_signal(
            price_data, sentiment, threshold if not force else 0.0, market_context
        )

        if not signal and not force:
            return None, 0, None

        strength = (
            signal_strength(price_data.get("change_pct", 0),
                            sentiment.get("confidence", 0), threshold)
            if signal else 1
        )

        if signal and strength < MIN_STRENGTH and not force:
            logger.info(f"{ticker}: strength {strength} below minimum, suppressed")
            return None, 0, None

        volatile = is_volatile_session()
        msg = self._format_message(
            ticker, price_data, sentiment, signal, force,
            strength, market_context, lunar, volatile
        )
        return msg, strength, signal

    # ------------------------------------------------------------------ #
    #  MESSAGE FORMATTER                                                   #
    # ------------------------------------------------------------------ #

    def _format_message(self, ticker, price_data, sentiment, signal, force,
                        strength, market_context=None, lunar=None,
                        volatile_session=False) -> str:
        now = now_uk().strftime("%b %d, %H:%M") + " UK"

        if signal == "BUY":
            header = f"🚀 *BUY SIGNAL — {ticker}*"
        elif signal == "SELL":
            header = f"🔻 *SELL SIGNAL — {ticker}*"
        else:
            header = f"📊 *{ticker} Check — {now}*"

        parts = [header, ""]

        if volatile_session:
            parts.append("⚠️ _Signal in first/last 30 min — spreads may be wide._\n")

        # Price
        if price_data:
            arrow = "📈" if price_data["change_pct"] >= 0 else "📉"
            sign = "+" if price_data["change_pct"] >= 0 else ""
            parts.append(
                f"{arrow} Price: *${price_data['current']}* "
                f"({sign}{price_data['change_pct']}% in last 30 min)"
            )
        else:
            parts.append("⚠️ Price data unavailable")

        # Market context
        if market_context and market_context.get("context") != "unknown":
            parts.append(
                f"🌍 Market: *{market_context['context']}* "
                f"(SPY {market_context['spy_pct']:+.1f}% · "
                f"QQQ {market_context['qqq_pct']:+.1f}%)"
            )

        # Combined sentiment — one verdict, evidence beneath
        s = sentiment.get("sentiment", "neutral")
        emoji = {"bullish": "🟢", "bearish": "🔴", "neutral": "🟡"}.get(s, "🟡")
        confidence = sentiment.get("confidence", "?")
        label = "Combined Sentiment" if lunar else "Sentiment"
        parts.append(f"{emoji} *{label}: {s.capitalize()}* (confidence: {confidence}/10)")

        reason = sentiment.get("reason", "").replace("[Market-driven] ", "")
        if reason:
            parts.append(f"  └ News: _{reason}_")

        if lunar:
            lc_s = lunar.get("sentiment", "neutral").capitalize()
            parts.append(
                f"  └ Social: {lc_s} momentum "
                f"(LunarCrush {lunar.get('score')}/100 · "
                f"galaxy {lunar.get('galaxy_score')}/100)"
            )

        # Thesis with note inline
        thesis_status = sentiment.get("thesis_status", "intact")
        thesis_note = sentiment.get("thesis_note", "")
        thesis_icons = {"intact": "✓", "under_review": "⚠️", "broken": "✗"}
        thesis_icon = thesis_icons.get(thesis_status, "✓")
        thesis_display = thesis_status.replace("_", " ")
        momentum = sentiment.get("momentum", "steady")
        thesis_line = f"  └ Momentum: {momentum} · Thesis: {thesis_display} {thesis_icon}"
        if thesis_note:
            thesis_line += f" — _{thesis_note}_"
        parts.append(thesis_line)

        if sentiment.get("top_headline"):
            parts.append(f"\n📰 {sentiment['top_headline']}")

        if sentiment.get("is_market_driven"):
            parts.append("⚠️ _Move appears market-driven — not stock-specific_")

        if sentiment.get("conflicting_signals"):
            parts.append("⚠️ _News and social sentiment conflict — high caution_")

        if signal and strength:
            parts += [
                "",
                f"Signal strength: {strength_stars(strength)} ({strength}/5)",
                f"⏱ {now} · Your decision.",
            ]

        return "\n".join(parts)
