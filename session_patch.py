# ============================================================
# ⚡ SESSION CHURN PATCH — CPU 100% ফিক্স (PyroFork 2.3.48)
# ============================================================
# PyroFork-এর Session-এ তিনটা দুর্বলতা আছে, যেগুলো একসাথে মিলে
# কোনো কাজ না করার অবস্থাতেও CPU 100% পর্যন্ত তুলে দেয়:
#
# ১) start() বারবার ফেল করলে কোনো বিরতি ছাড়াই অজস্রবার চেষ্টা চালিয়ে
#    যায় (tight loop) — সেকেন্ডে হাজারবার connect চেষ্টা → CPU পুড়ে যায়।
# ২) একই সেশনে একসাথে দুটো restart চললে দুটো recv worker একই
#    connection-এ চাপা দেয় → "RuntimeError: read() called while another
#    coroutine is already waiting for incoming data" → এক reader মরে
#    গেলে সেশন জম্বি, সব রিকোয়েস্ট ২০ সেকেন্ড টাইমআউট।
# ৩) stop() পুরনো recv worker-কে বন্ধ করে না — সে নতুন connection-এ
#    চুরি করে পড়তে বসে যায় (আবার সেই RuntimeError)।
#
# এই ফাইল PyroFork-এর Session.stop / Session.restart বদলে দেয়:
#   - ব্যর্থ চেষ্টার পরে বিরতি (1s → 2s → 4s → 8s)
#   - এক সেশনে একসাথে একটাই restart
#   - stop-এর সময় আগে recv worker বন্ধ, তারপর connection
# ============================================================

import asyncio
import logging

from pyrogram.session import Session

_log = logging.getLogger(__name__)


async def _patched_stop(self):
    # আগে থেকে started কি না — start()-এর ব্যর্থ চক্রে বিরতি দেওয়ার
    # সিদ্ধান্ত এর উপর নির্ভর করে
    started = self.is_started.is_set()

    # ১) আগে ping worker থামাও — ও চলতে চলতে connection বন্ধ হলে
    #    ও নিজে থেকে আরেকটা restart ছাড়ে, সেটা এড়াতে আগে থামাও
    self.ping_task_event.set()
    ping = getattr(self, "ping_task", None)
    if ping is not None and not ping.done():
        try:
            await asyncio.wait_for(ping, 3)
        except (asyncio.CancelledError, Exception):
            pass
    self.ping_task = None
    self.ping_task_event.clear()

    # ২) recv worker বন্ধ — স্টেল reader নতুন connection চুরি করতে
    #    পারবে না (RuntimeError-এর আসল উৎস এটাই)
    recv = getattr(self, "recv_task", None)
    if recv is not None and not recv.done():
        recv.cancel()
        try:
            await recv
        except (asyncio.CancelledError, Exception):
            pass
    self.recv_task = None

    # ৩) এবার connection বন্ধ
    try:
        if getattr(self, "connection", None) is not None:
            await self.connection.close()
    except Exception:
        pass

    self.stored_msg_ids.clear()
    self.is_started.clear()

    # ৪) start()-এর ব্যর্থ চক্রের বিরতি — এটাই CPU 100%-এর প্রধান দফা
    if not started:
        self._start_fails = getattr(self, "_start_fails", 0) + 1
        delay = min(0.5 * (2 ** min(self._start_fails, 4)), 8.0)
        _log.warning(
            f"Session start retry #{self._start_fails} failed — waiting {delay}s before next attempt"
        )
        await asyncio.sleep(delay)
    else:
        self._start_fails = 0


async def _patched_restart(self):
    # একই সেশনে একসাথে একাধিক restart চললে দুটো reader একই
    # connection-এ চাপা দেয় — তাই চলমান restart থাকলে নতুনটা কিছু না
    # করে চুপচাপ ফিরে যাবে
    if getattr(self, "_restarting", False):
        return
    self._restarting = True
    try:
        await self.stop()
        await self.start()
    except Exception as e:
        _log.warning(f"Session restart failed (will retry when needed): {type(e).__name__}: {e}")
    finally:
        self._restarting = False


Session.stop = _patched_stop
Session.restart = _patched_restart

_log.info("Session churn patch applied — backoff + single-restart + safe recv shutdown")
