import anthropic
import httpx
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

POLYGON_BASE = "https://api.polygon.io/v2"


def is_market_open() -> bool:
    """
    Returns True only if current time is within US market hours:
    Monday–Friday, 09:30–16:00 Eastern Time.
    """
    from zoneinfo import ZoneInfo
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if now_et.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now_et <= market_close


class SignalAnalyzer:
    def __init__(self, anthropic_key: str, polygon_key: str):
        self.claude = anthropic.Anthropic(api_key=anthropic_key)
        self.polygon_key = polygon_key

    # ------------------------------------------------------------------ #
    #  PRICE DATA                                                          #
    # ------------------------------------------------------------------ #

    async def get_price_change(self, ticker: str) -> dict:
        """
        Fetches the last two 30-minute bars from Polygon.io.
        Returns current price, previous price, and % change.
        """
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=2)

        url = (
            f"{POLYGON_BASE}/aggs/ticker/{ticker}/range/30/minute"
            f"/{start.strftime('%Y-%m-%d')}/{end.strftime('%Y-%m-%d')}"
        )

        params = {
            "adjusted": "true",
            "sort": "asc",
            "limit": 4,
            "apiKey": self.polygon_key,
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(url, params=params)
                data = r.json()

            bars = data.get("results", [])
            if len(bars) < 2:
                logger.warning(f"Not enough bars for {ticker}: {bars}")
                return {}

            current = bars[-1]["c"]   # close of latest bar
            previous = bars[-2]["c"]  # close of bar before
            change = current - previous
            change_pct = (change / previous) * 100 if previous else 0

            return {
                "ticker": ticker,
                "current": round(current, 2),
                "previous": round(previous, 2),
                "change": round(change, 2),
                "change_pct": round(change_pct, 2),
                "bar_time": bars[-1]["t"],
            }

        except Exception as e:
            logger.error(f"Polygon price error for {ticker}: {e}")
            return {}

    # ------------------------------------------------------------------ #
    #  SENTIMENT VIA CLAUDE                                                #
    # ------------------------------------------------------------------ #

    def get_sentiment(self, ticker: str) -> dict:
        """
        Calls Claude with web search to assess current news sentiment.
        Returns { sentiment: 'bullish'|'bearish'|'neutral', reason: str, confidence: int }
        """
        prompt = f"""Search the web for the latest news about {ticker} stock from the past 2 hours.

Respond with ONLY a JSON object, no markdown, no explanation:
{{
  "sentiment": "bullish" or "bearish" or "neutral",
  "confidence": number from 1-10 (how strongly the news points one way),
  "reason": "one sentence summarising the key news driving this sentiment",
  "top_headline": "the single most impactful headline you found"
}}

Be strict: only return bullish/bearish if there is clear, recent news. Otherwise return neutral."""

        try:
            response = self.claude.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{"role": "user", "content": prompt}]
            )
            text = "".join(
                b.text for b in response.content if hasattr(b, "text")
            ).strip()

            import json, re
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                return json.loads(match.group())
        except Exception as e:
            logger.error(f"Claude sentiment error for {ticker}: {e}")

        return {"sentiment": "neutral", "confidence": 0, "reason": "Could not retrieve news.", "top_headline": ""}

    # ------------------------------------------------------------------ #
    #  SIGNAL LOGIC                                                        #
    # ------------------------------------------------------------------ #

    def evaluate_signal(self, price_data: dict, sentiment: dict, threshold: float) -> str | None:
        """
        Returns 'BUY', 'SELL', or None.
        Signal fires only when price move AND sentiment agree.
        """
        if not price_data:
            return None

        pct = price_data["change_pct"]
        s = sentiment.get("sentiment", "neutral")
        confidence = sentiment.get("confidence", 0)

        # Require at least moderate confidence (5+) to avoid noise
        if confidence < 5:
            return None

        if pct >= threshold and s == "bullish":
            return "BUY"
        if pct <= -threshold and s == "bearish":
            return "SELL"

        return None

    # ------------------------------------------------------------------ #
    #  FULL PIPELINE                                                       #
    # ------------------------------------------------------------------ #

    async def run(self, ticker: str, threshold: float, force: bool = False) -> str | None:
        """
        Runs the full check. Returns a formatted Telegram message if a signal fires,
        or None if no signal (so the bot stays quiet).
        force=True bypasses threshold and market hours check (used for /check command).
        """
        if not force and not is_market_open():
            logger.info(f"{ticker}: skipped — market is closed")
            return None

        price_data = await self.get_price_change(ticker)
        sentiment = self.get_sentiment(ticker)
        signal = self.evaluate_signal(price_data, sentiment, threshold if not force else 0.0)

        if not signal and not force:
            logger.info(f"{ticker}: no signal (pct={price_data.get('change_pct', '?')}%, sentiment={sentiment.get('sentiment')})")
            return None

        return self._format_message(ticker, price_data, sentiment, signal, force)

    def _format_message(self, ticker: str, price_data: dict, sentiment: dict, signal: str | None, force: bool) -> str:
        now = datetime.now().strftime("%b %d, %H:%M")

        # Signal header
        if signal == "BUY":
            header = f"🚀 *BUY SIGNAL — {ticker}*"
        elif signal == "SELL":
            header = f"🔻 *SELL SIGNAL — {ticker}*"
        else:
            header = f"📊 *{ticker} Check — {now}*"

        # Price block
        if price_data:
            arrow = "📈" if price_data["change_pct"] >= 0 else "📉"
            sign = "+" if price_data["change_pct"] >= 0 else ""
            price_line = (
                f"{arrow} Price: *${price_data['current']}* "
                f"({sign}{price_data['change_pct']}% in last 30 min)"
            )
        else:
            price_line = "⚠️ Price data unavailable"

        # Sentiment block
        s = sentiment.get("sentiment", "neutral")
        emoji = {"bullish": "🟢", "bearish": "🔴", "neutral": "🟡"}.get(s, "🟡")
        sentiment_line = f"{emoji} News: *{s.capitalize()}* (confidence: {sentiment.get('confidence', '?')}/10)"
        reason_line = f"_{sentiment.get('reason', '')}_"
        headline_line = f"📰 {sentiment.get('top_headline', '')}" if sentiment.get("top_headline") else ""

        parts = [header, "", price_line, sentiment_line, reason_line]
        if headline_line:
            parts.append(headline_line)
        if signal:
            parts += ["", f"⏱ {now} · You decide whether to act on this."]

        return "\n".join(parts)
