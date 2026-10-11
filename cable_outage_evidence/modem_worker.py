"""The modem worker (spec §8.2-8.4): the only client of the modem API.

Normal cadence: upstream state every 5 s, a full read every 30 s, downstream
channel rows every 300 s, the event log every 300 s. Fast mode reads the
upstream and OFDM downstream state once a second; DsInfo, the log and SysInfo
run in a single background lane that is skipped when busy, so a slow modem
never stretches a tick.
"""

import collections
import concurrent.futures as cf
import logging
import threading
import time
from typing import Dict, List, Optional, Tuple

from . import config, hitron, modem, storage

log = logging.getLogger("cable-outage-evidence")

SIGNALS_HEADER = hitron.SUMMARY_HEADER + ["SkewSec"]

# Modem reading status. Probing and outage detection never depend on it.
OK, UNREACHABLE, AUTH_FAILED, BAD_RESPONSE = "ok", "unreachable", "auth_failed", "bad_response"
UNSUPPORTED = "unsupported"
# A wrong password is retried this rarely, so the modem never locks the account.
AUTH_RETRY_SEC = 300
# Another brand of modem will not turn into a Hitron; checked again hourly in
# case the address was briefly answered by something else.
UNSUPPORTED_RETRY_SEC = 3600
# Timeouts and odd answers happen (an expired session looks like one), so a
# status change is announced only after this many reads in a row fail.
FAILURES_BEFORE_NOTICE = 3


def status_message(status: str, detail: str, cfg, context: str = "monitor") -> str:
    """The notice shown when reading changes status.

    context: "monitor" while monitoring, "check" for the one-off modem-check
    command, "dialog" inside the setup dialogue (which is where credentials
    are being entered, so it does not send the user to init).
    """
    running = "探測與斷線偵測照常進行。" if context == "monitor" else ""
    if status == OK:
        return "數據機讀取正常 / modem readings OK"
    if status == UNREACHABLE:
        return (f"數據機連不上 / modem unreachable ({detail})。請確認數據機位址（目前 {cfg.modem_url}）；"
                "常見位址是 http://192.168.0.1 或 http://192.168.100.1。" + running)
    if status == AUTH_FAILED:
        where = "請重新輸入帳密" if context == "dialog" else "請執行 init 重新設定 modem_username 和 modem_password"
        retry = f"{AUTH_RETRY_SEC // 60} 分鐘後再試一次。" if context == "monitor" else ""
        return (f"數據機帳密錯誤 / modem login rejected。{where}（預設 admin / password，有些 ISP 會改掉或"
                f"印在數據機貼紙上）。" + retry + running)
    if status == UNSUPPORTED:
        return (f"這台數據機不是 Hitron CODA 或支援的 CBN 韌體，目前不支援讀取 / unsupported modem ({detail})。"
                "換帳密也不會成功。斷線偵測仍然可以用，只是報告裡不會有 DOCSIS 日誌與頻道狀態。"
                "歡迎到 https://github.com/orsmile/cable-outage-evidence/issues 提供數據機型號，協助支援。"
                + running)
    return (f"數據機回應異常 / unexpected modem response ({detail})。可能不是支援的 Hitron CODA 或 CBN 韌體。"
            + running)


FAST_HEADER = ["Timestamp", "Trigger", "Tick", "TickLateMs", "CoreOK", "CoreMs", "SlowRead", "SkewSec",
               "UsScQamOperate", "UsScQamCount", "UsOfdmaOperate", "UsOfdmaCount",
               "DsOfdmLocked", "DsOfdmCount", "DsScQamLocked", "DsScQamCount"]


class ModemWorker:
    def __init__(self, cfg: config.Config, store: storage.Store, client: hitron.HitronClient = None):
        self.cfg = cfg
        self.store = store
        self.client = client or modem.create_client(cfg)
        self.lock = threading.Lock()
        self.login_lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.fast_until = 0.0
        self.fast_started = 0.0
        self.fast_trigger = ""
        self.markers: List[str] = []          # aggregate rows requested by the monitor
        self.snapshot_requests: List[str] = []
        self.last_obs: Optional[Tuple[str, Dict, Dict, Tuple]] = None   # ts, us, of, state
        self.latest: Dict[str, Dict] = {}     # endpoint -> last good response
        self.skews: collections.deque = collections.deque(maxlen=30)
        self.dedupe = hitron.LogDeduper(cfg.data_path / "docsis-log-seen")
        self.slow = cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="modem-slow")
        self.slow_future: Optional[cf.Future] = None
        self.core = cf.ThreadPoolExecutor(max_workers=len(hitron.FAST_ENDPOINTS), thread_name_prefix="modem-core")
        self.failures = 0
        self.status = "unknown"
        self.next_login = 0.0
        self.on_status = None             # callback(status, message), e.g. notifications
        # Not 0.0: monotonic time counts from boot on Linux, so on a machine up
        # for under a poll interval the first failure would look already written.
        self.last_failure_row = float("-inf")
        self.obs_lock = threading.Lock()
        self.thread = threading.Thread(target=self._run, name="modem-worker", daemon=True)

    # --- requests from the monitor (never block) -----------------------------------

    def request_fast(self, seconds: int, trigger: str) -> None:
        now = time.monotonic()
        with self.lock:
            if self.fast_until <= now:
                self.fast_started = now
                self.fast_trigger = trigger
            cap = self.fast_started + self.cfg.modem_fast_max_sec
            self.fast_until = min(max(self.fast_until, now + seconds), cap)
            self.markers.append(trigger)
        self._wake.set()

    def request_snapshot(self, reason: str) -> None:
        with self.lock:
            self.snapshot_requests.append(reason)
        self._wake.set()

    def start(self):
        self.thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        self.thread.join(timeout=10)
        self.client.logout()

    def skew(self) -> Optional[int]:
        """Median of recent (computer - modem) clock differences, 1 s resolution."""
        if not self.skews:
            return None
        values = sorted(self.skews)
        return values[len(values) // 2]

    # --- modem access --------------------------------------------------------------------

    def _get(self, endpoint: str, timeout: float = None) -> Dict:
        try:
            with self.login_lock:
                if not self.client.logged_in:
                    if self.status in (AUTH_FAILED, UNSUPPORTED) and time.monotonic() < self.next_login:
                        # Not retried yet: no request reaches the modem.
                        if self.status == UNSUPPORTED:
                            raise hitron.ModemUnsupported("unsupported modem API (waiting before the next try)")
                        raise hitron.ModemAuthError("login rejected (waiting before the next try)")
                    try:
                        self.client.login()
                    except hitron.ModemAuthError:
                        self.next_login = time.monotonic() + AUTH_RETRY_SEC
                        raise
                    except hitron.ModemUnsupported:
                        self.next_login = time.monotonic() + UNSUPPORTED_RETRY_SEC
                        raise
            data, modem_epoch = self.client.get(endpoint, timeout)
        except hitron.ModemError as e:
            self._read_failed(e)
            raise
        if modem_epoch is not None:
            self.skews.append(int(round(time.time() - modem_epoch)))
        self.latest[endpoint] = data
        self.failures = 0
        self._set_status(OK, "")
        return data

    def _read_failed(self, err: hitron.ModemError) -> None:
        self.failures += 1
        if isinstance(err, hitron.ModemAuthError):
            self._set_status(AUTH_FAILED, str(err))
        elif isinstance(err, hitron.ModemUnsupported):
            self._set_status(UNSUPPORTED, str(err))
        elif self.failures >= FAILURES_BEFORE_NOTICE:
            self._set_status(UNREACHABLE if isinstance(err, hitron.ModemUnreachable) else BAD_RESPONSE, str(err))

    def _set_status(self, status: str, detail: str) -> None:
        with self.lock:
            if status == self.status:
                return
            previous, self.status = self.status, status
        message = status_message(status, detail, self.cfg)
        # The first successful read is routine; every other change is news.
        routine = status == OK and previous == "unknown"
        (log.info if routine else log.warning)("%s", message)
        # Reports use these to show when modem data is missing and why.
        self.store.append_jsonl("events-journal.jsonl", {
            "type": "modem_status", "time": storage.now_text(), "status": status,
            "previous": previous, "detail": detail})
        if self.on_status and not routine:
            try:
                self.on_status(status, message)
            except Exception:   # a notifier must never stop the worker
                log.exception("modem status notifier failed")

    def _failed(self, reason: str, err: Exception) -> None:
        """Writes a failure row, at most one per poll interval. The status
        change itself is announced by _set_status."""
        log.debug("modem read failed (%s): %s", reason, err)
        now = time.monotonic()
        if now - self.last_failure_row < self.cfg.modem_poll_interval_sec:
            return
        self.last_failure_row = now
        self.store.append_csv("modem-signals.csv", SIGNALS_HEADER,
                              [hitron.failure_row(storage.now_text(), reason, str(err)) + [""]])

    def _skew_text(self) -> str:
        s = self.skew()
        return "" if s is None else str(s)

    # --- writing ---------------------------------------------------------------------------

    def _summary(self, reason: str) -> None:
        if not self.latest:
            return
        row = hitron.summary_row(storage.now_text(), reason, self.latest) + [self._skew_text()]
        self.store.append_csv("modem-signals.csv", SIGNALS_HEADER, [row])

    def _channels(self, ts: str, reason: str, **resp) -> None:
        rows = hitron.channel_rows(ts, reason, **resp)
        if rows:
            self.store.append_csv("modem-channels.csv", hitron.CHANNEL_HEADER, rows)

    def _observe_upstream(self, ts: str, us: Dict, of: Dict) -> bool:
        """Compares the upstream state with the previous observation. On a
        change, writes the preserved previous observation under its own time,
        then the new one, so the transition is bounded by two real
        observations (spec §8.3). Returns True on a change."""
        state = hitron.upstream_state(us, of)
        with self.obs_lock:
            prev = self.last_obs
            self.last_obs = (ts, us, of, state)
        if prev is None or prev[3] == state:
            return False
        self._channels(prev[0], "PrevObserved", us=prev[1], of=prev[2])
        self._channels(ts, "StateChange", us=us, of=of)
        log.info("upstream channel state changed")
        self.request_fast(self.cfg.modem_fast_statechange_sec, "StateChange")
        return True

    def _read_log(self, reason: str) -> None:
        entries = hitron.log_entries(self._get("Log"))
        fresh = self.dedupe.new(entries)
        if fresh:
            now = storage.now_text()
            self.store.append_csv("docsis-log.csv", hitron.LOG_HEADER, [[now] + list(e) for e in fresh])
            log.info("DOCSIS log: %d new entries (%s)", len(fresh), reason)

    def _full_read(self, reason: str, with_ds_rows: bool, with_log: bool) -> None:
        try:
            for ep in ("SysInfo", "DsInfo", "UsInfo", "UsOfdm", "DsOfdm"):
                self._get(ep)
            ts = storage.now_text()
            self._summary(reason)
            us, of = self.latest["UsInfo"], self.latest["UsOfdm"]
            self._channels(ts, reason, us=us, of=of)
            if with_ds_rows:
                self._channels(ts, reason, ds=self.latest["DsInfo"], dof=self.latest["DsOfdm"])
            self._observe_upstream(ts, us, of)
            if with_log:
                self._read_log(reason)
        except hitron.ModemError as e:
            self._failed(reason, e)

    def _slow_busy(self) -> bool:
        return self.slow_future is not None and not self.slow_future.done()

    def _slow_submit(self, fn, *args) -> bool:
        if self._slow_busy():
            return False
        self.slow_future = self.slow.submit(fn, *args)
        return True

    # --- loop ---------------------------------------------------------------------------------

    def _run(self):
        now = time.monotonic()
        next_state = now
        next_full = now
        next_ds = now
        next_log = now
        while not self._stop.is_set():
            now = time.monotonic()
            with self.lock:
                fast = self.fast_until > now
                requests, self.snapshot_requests = self.snapshot_requests, []
            if fast:
                self._fast_window()
                next_state = next_full = time.monotonic()
                continue
            for i, reason in enumerate(requests):
                if not self._slow_submit(self._full_read, reason, True, True):
                    # The lane is busy; keep the rest queued for the next pass.
                    with self.lock:
                        self.snapshot_requests[:0] = requests[i:]
                    break
                # A requested read is a full read; it stands in for this round's.
                next_full = now + self.cfg.modem_poll_interval_sec
                next_ds = now + self.cfg.modem_channel_ds_interval_sec
                next_log = now + self.cfg.modem_log_interval_sec
                next_state = now + self.cfg.modem_state_poll_interval_sec
            if now >= next_full and self._slow_submit(self._full_read, "Periodic", now >= next_ds, now >= next_log):
                if now >= next_ds:
                    next_ds = now + self.cfg.modem_channel_ds_interval_sec
                if now >= next_log:
                    next_log = now + self.cfg.modem_log_interval_sec
                next_full = now + self.cfg.modem_poll_interval_sec
                next_state = now + self.cfg.modem_state_poll_interval_sec
            elif now >= next_state:
                try:
                    us, of = self._get("UsInfo"), self._get("UsOfdm")
                    self._observe_upstream(storage.now_text(), us, of)
                except hitron.ModemError as e:
                    self._failed("StatePoll", e)
                next_state = now + self.cfg.modem_state_poll_interval_sec
            self._wake.wait(max(0.05, min(next_state, next_full) - time.monotonic()))
            self._wake.clear()

    def _fast_window(self):
        """One-second ticks until the deadline (spec §8.3)."""
        with self.lock:
            trigger = self.fast_trigger
        log.info("modem fast mode (%s)", trigger)
        tick_sec = 1.0
        deadline_sec = tick_sec * 0.85
        start = time.monotonic()
        next_tick = start
        tick = 0
        recent = collections.deque(maxlen=3)        # (core_ms, late_ms)
        pause = 0
        last_counts = None
        last_ds = start - 999
        last_ds_seen = None
        last_log = start - 999
        slow_entry = True
        self._summary(trigger)
        while not self._stop.is_set():
            now = time.monotonic()
            with self.lock:
                until = self.fast_until
                markers, self.markers = self.markers, []
            if now >= until:
                break
            for m in markers:
                if m == "Recovery":
                    self._summary("Recovery")
            if now < next_tick:
                self._stop.wait(next_tick - now)
                continue
            late_ms = int((now - next_tick) * 1000)
            tick += 1
            ts = storage.now_text()
            futures = {ep: self.core.submit(self._get, ep, deadline_sec) for ep in hitron.FAST_ENDPOINTS}
            done, _ = cf.wait(futures.values(), timeout=deadline_sec)
            core_ms = int((time.monotonic() - now) * 1000)
            results = {}
            for ep, fut in futures.items():
                if fut in done and fut.exception() is None:
                    results[ep] = fut.result()
            core_ok = len(results) == len(futures)
            recent.append((core_ms, late_ms))

            slow_read = ""
            if pause:
                pause -= 1
            elif any(c > 600 or l > 0 for c, l in recent) and tick > 1:
                # State-transition resolution comes before DS/log freshness.
                pause = 5
            else:
                if slow_entry and self._slow_submit(self._slow_entry):
                    slow_read, slow_entry = "Entry", False
                elif now - last_log >= self.cfg.modem_fast_log_interval_sec and self._slow_submit(self._read_log_safe, "Fast"):
                    slow_read, last_log = "Log", now
                elif now - last_ds >= self.cfg.modem_fast_dsinfo_interval_sec and self._slow_submit(self._get_safe, "DsInfo"):
                    slow_read, last_ds = "DsInfo", now

            us, of, dof = results.get("UsInfo"), results.get("UsOfdm"), results.get("DsOfdm")
            if us is not None and of is not None:
                self._channels(ts, "Fast", us=us, of=of)
                self._observe_upstream(ts, us, of)
            # DsInfo comes from the slow lane; count it only on the tick it arrives.
            ds = self.latest.get("DsInfo")
            if ds is last_ds_seen:
                ds = None
            else:
                last_ds_seen = ds
            c = hitron.counts(us, of, ds, dof)
            row = [ts, trigger, str(tick), str(late_ms), "1" if core_ok else "0", str(core_ms), slow_read,
                   self._skew_text()] + [str(c.get(k, "")) for k in FAST_HEADER[8:]]
            self.store.append_csv("fast-poll.csv", FAST_HEADER, [row])
            key = tuple(c.get(k) for k in FAST_HEADER[8:14])
            if last_counts is not None and core_ok and key != last_counts:
                self._summary("ChannelChange")
            if core_ok:
                last_counts = key
            next_tick += tick_sec
            if time.monotonic() > next_tick:
                next_tick = time.monotonic()   # overruns are recorded as lateness, not absorbed

        self._slow_submit(self._slow_exit)
        self._summary("FastEnd")
        log.info("modem fast mode ended after %d ticks", tick)

    def _get_safe(self, endpoint: str):
        try:
            self._get(endpoint)
        except hitron.ModemError:
            pass

    def _read_log_safe(self, reason: str):
        try:
            self._read_log(reason)
        except hitron.ModemError:
            pass

    def _slow_entry(self):
        for ep in ("SysInfo", "DsInfo"):
            self._get_safe(ep)
        self._read_log_safe("FastEntry")

    def _slow_exit(self):
        self._get_safe("SysInfo")
        self._read_log_safe("FastExit")
