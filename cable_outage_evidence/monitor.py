"""The probe loop (spec §3, §5): one sample every interval_ms, events, files."""

import concurrent.futures as cf
import datetime as dt
import logging
import queue
import threading
import time
from typing import Dict, Optional

from . import classify, config, events, modem_worker, platform_net, storage

log = logging.getLogger("cable-outage-evidence")


class PathState:
    """Default gateway and ISP first hop, refreshed off the probe loop.

    Reading the routing table means a subprocess on Windows and macOS, too slow
    for every 500 ms sample, so the gateway is checked every few seconds and the
    first hop on the discovery interval or when the gateway changes.
    """

    GATEWAY_CHECK_SEC = 5

    def __init__(self, cfg: config.Config, pinger: platform_net.Pinger):
        self.cfg = cfg
        self.pinger = pinger
        self.lock = threading.Lock()
        self.gateway: Optional[str] = platform_net.default_gateway()
        self.first_hop: Optional[str] = None
        # Near-end targets actually probed: only those that answer ping. Some
        # routers never answer it, and probing one would turn every outage
        # into a false "home router unreachable" (spec §3).
        self.lan_probe: Optional[str] = None
        self.modem_probe: Optional[str] = None
        self._discover_now = threading.Event()
        self._discover_now.set()
        self._stop = threading.Event()

    def snapshot(self):
        with self.lock:
            return self.gateway, self.first_hop

    def near_targets(self):
        with self.lock:
            return self.lan_probe, self.modem_probe

    def _answers(self, address: Optional[str]) -> bool:
        return bool(address) and platform_net.ping_with_retries(
            self.pinger, address, 2, self.cfg.ping_timeout_sec) is not None

    def check_near_targets(self) -> None:
        """Adopts the home router and the modem as probe targets if they answer
        ping. One that stops answering is dropped only while the Internet is
        reachable: during an outage its silence is evidence, not a setting."""
        gw, _ = self.snapshot()
        # Pinged whenever an address is configured, even with reading off.
        modem = self.cfg.modem_address if self.cfg.modem_url else None
        lan = gw
        if modem == gw:
            # The cable modem is the router as well (gateway mode, common for
            # ISP-supplied units): one device, probed as the modem.
            lan = None
        online = None
        modem_label = "modem (also the default gateway)" if modem and modem == gw else "modem"
        for attr, address, label in (("lan_probe", lan, "home router"), ("modem_probe", modem, modem_label)):
            current = getattr(self, attr)
            if self._answers(address):
                if current != address:
                    log.info("%s %s answers ping; probed", label, address)
                value = address
            elif current is not None and current == address:
                if online is None:
                    online = self._answers(self.cfg.path_discovery_target)
                value = None if online else current
                if online:
                    log.info("%s %s stopped answering ping while online; not probed", label, address)
            else:
                if address and current is None:
                    log.info("%s %s does not answer ping; not probed", label, address)
                value = None
            with self.lock:
                setattr(self, attr, value)

    def start(self):
        threading.Thread(target=self._gateway_loop, name="gateway", daemon=True).start()
        threading.Thread(target=self._discovery_loop, name="discovery", daemon=True).start()

    def stop(self):
        self._stop.set()
        self._discover_now.set()

    def _gateway_loop(self):
        while not self._stop.wait(self.GATEWAY_CHECK_SEC):
            gw = platform_net.default_gateway()
            with self.lock:
                changed = gw != self.gateway
                self.gateway = gw
                if changed:
                    # Never report the old topology against a new gateway.
                    self.first_hop = None
                    self.lan_probe = None
            if changed:
                log.info("default gateway changed to %s", gw or "none")
                self._discover_now.set()

    def _discovery_loop(self):
        while not self._stop.is_set():
            self._discover_now.wait(self.cfg.path_discovery_interval_sec)
            self._discover_now.clear()
            if self._stop.is_set():
                return
            self.discover()

    def discover(self) -> Optional[str]:
        self.check_near_targets()
        gw, _ = self.snapshot()
        if gw is None:
            return None
        hops = platform_net.traceroute(self.cfg.path_discovery_target)
        skip = [gw]
        if self.cfg.modem_type != "none" or self.cfg.modem_url:
            skip.append(self.cfg.modem_address)
        hop = platform_net.first_isp_hop(hops, skip)
        # Adopted only if it answers ping; some ISP routers answer only with
        # TTL-exceeded, and probing those would cost every sample a timeout.
        if hop and platform_net.ping_with_retries(self.pinger, hop, 2, self.cfg.ping_timeout_sec) is None:
            log.info("first hop %s does not answer ping; not probed", hop)
            hop = None
        with self.lock:
            if hop or not hops:
                # A failed traceroute keeps the last valid address.
                self.first_hop = hop if hop else self.first_hop
            else:
                self.first_hop = None
        log.info("ISP first hop: %s", hop or "unknown")
        return hop


class Monitor:
    def __init__(self, cfg: config.Config):
        self.cfg = cfg
        self.names = [n for n, _ in cfg.public_targets]
        self.store = storage.Store(cfg.data_path)
        self.pinger = platform_net.Pinger()
        self.path = PathState(cfg, self.pinger)
        self.tracker = events.Tracker(cfg.interval_ms, cfg.sustained_samples, cfg.recovery_rounds,
                                      cfg.pre_event_seconds, cfg.post_recovery_seconds)
        self.minutes = storage.MinuteSummary(self.names)
        # A failing ping takes retries x timeout, so during an outage several
        # samples are in flight at once; this many keeps the 500 ms cadence.
        self.max_in_flight = max(2, int(cfg.ping_retries * cfg.ping_timeout_sec * 1000 / cfg.interval_ms) + 2)
        self.sampler = cf.ThreadPoolExecutor(max_workers=self.max_in_flight, thread_name_prefix="sample")
        per_sample = 3 + len(self.names)
        self.pool = cf.ThreadPoolExecutor(max_workers=self.max_in_flight * per_sample + len(self.names),
                                          thread_name_prefix="probe")
        self.tcp_results: "queue.Queue" = queue.Queue()
        self.incident_paths: Dict[int, object] = {}
        self.awaiting_tcp = set()
        self.sequence = 0
        self._stop = threading.Event()
        self.on_event_start = None
        self.on_event_recovered = None
        self.modem: Optional[modem_worker.ModemWorker] = None
        if cfg.modem_type != "none" and cfg.modem_url:
            self.modem = modem_worker.ModemWorker(cfg, self.store)

    # --- probing -------------------------------------------------------------

    def _ping(self, address: Optional[str]) -> classify.Probe:
        if not address:
            return classify.Probe(None)
        rtt = platform_net.ping_with_retries(self.pinger, address, self.cfg.ping_retries,
                                             self.cfg.ping_timeout_sec)
        return classify.Probe(address, rtt)

    def take_sample(self, sequence: int = 0, timestamp: str = "", late_ms: int = 0) -> classify.Sample:
        """Probes every layer at once. The timestamp is when the sample started:
        during an outage the pings take retries x timeout to give up."""
        timestamp = timestamp or storage.now_text()
        gw, hop = self.path.snapshot()
        lan, modem = self.path.near_targets()
        futures = {
            "lan": self.pool.submit(self._ping, lan),
            "modem": self.pool.submit(self._ping, modem),
            "hop": self.pool.submit(self._ping, hop),
        }
        for name, address in self.cfg.public_targets:
            futures[name] = self.pool.submit(self._ping, address)
        return classify.Sample(
            timestamp=timestamp, sequence=sequence, link=gw is not None,
            lan_gateway=futures["lan"].result(), modem=futures["modem"].result(),
            first_hop=futures["hop"].result(),
            public={n: futures[n].result() for n in self.names}, late_ms=late_ms)

    def _tcp_checks(self, ev: events.Event):
        results = {}
        checks = {n: self.pool.submit(platform_net.tcp_connect, a, 443, self.cfg.tcp_timeout_sec)
                  for n, a in self.cfg.public_targets}
        for n, fut in checks.items():
            results[n] = fut.result()
        self.tcp_results.put((ev, results))

    def _apply_tcp(self, ev: events.Event, results):
        ev.tcp = results
        if self.tracker.regrade(ev):
            log.warning("outage grade now %s (TCP/443)", ev.grade)
        if id(ev) in self.awaiting_tcp:
            self.awaiting_tcp.discard(id(ev))
            self._finish(ev)

    def _finish(self, ev: events.Event):
        self.store.append_csv("events.csv", storage.event_header(self.names),
                              [storage.event_row(ev, self.names)])
        self.store.append_jsonl("events-journal.jsonl", {
            "type": "recovery", "start": ev.start.timestamp, "end": ev.end.timestamp,
            "grade": ev.grade, "classification": ev.worst})
        log.warning("outage recovered %s, grade %s (%s), %d bad samples",
                    ev.end.timestamp, ev.grade, ev.worst, ev.bad_samples)

    # --- files -----------------------------------------------------------------

    def _incident_path(self, ev: events.Event):
        key = id(ev)
        if key not in self.incident_paths:
            d = self.store.day_dir(dt.date.fromisoformat(ev.start.timestamp[:10])) / "incidents"
            d.mkdir(exist_ok=True)
            stamp = ev.start.timestamp[:19].replace("-", "").replace(":", "").replace(" ", "-")
            self.incident_paths[key] = d / f"incident-{stamp}.csv"
        return self.incident_paths[key]

    def handle(self, up: events.Update):
        header = storage.sample_header(self.names)
        by_event = {}
        for ev, s in up.writes:
            by_event.setdefault(id(ev), (ev, []))[1].append(storage.sample_row(s))
        for ev, rows in by_event.values():
            self.store.append_csv("", header, rows, path=self._incident_path(ev))
        if up.started:
            ev = up.started
            log.warning("outage started %s: %s", ev.start.timestamp, ev.start.classification)
            self.store.append_jsonl("events-journal.jsonl", {
                "type": "start", "time": ev.start.timestamp, "classification": ev.start.classification,
                "first_failing_layer": ev.first_failing_layer})
            # Fast mode first, before the TCP checks: the first seconds matter most.
            if self.modem:
                self.modem.request_fast(self.cfg.modem_fast_min_sec, "IncidentStart")
            threading.Thread(target=self._tcp_checks, args=(ev,), daemon=True).start()
            if self.on_event_start:
                self.on_event_start(ev)
        if up.grade_raised:
            log.warning("outage grade now %s", up.grade_raised.grade)
        if up.recovered:
            ev = up.recovered
            if ev.tcp:
                self._finish(ev)
            else:
                # The TCP/443 checks can outlast a short outage; the row waits for them.
                self.awaiting_tcp.add(id(ev))
            if self.modem:
                self.modem.request_fast(self.cfg.modem_fast_post_sec, "Recovery")
            if self.on_event_recovered:
                self.on_event_recovered(ev)
        if up.closed:
            self.incident_paths.pop(id(up.closed), None)
            # The post-recovery window is complete: classify it (spec §7).
            threading.Thread(target=self._classify, args=(up.closed.start.timestamp,),
                             name="classify", daemon=True).start()

    def _classify(self, start_ts: str):
        from . import analysis
        try:
            for _ in range(10):
                ev = analysis.find_event(self.cfg.data_path, start_ts)
                if ev is not None and ev["StartTime"] == start_ts:
                    c = analysis.classify_and_save(self.cfg.data_path, ev)
                    log.info("event %s classified %s; timeline %s", start_ts[:19], c.cls, c.timeline_path)
                    return
                time.sleep(1)     # the events.csv row may still wait for TCP results
        except Exception:
            log.exception("classifying %s failed", start_ts)

    # --- loop ---------------------------------------------------------------------

    def stop(self):
        self._stop.set()

    def _process(self, sample: classify.Sample):
        while not self.tcp_results.empty():
            self._apply_tcp(*self.tcp_results.get())
        self.handle(self.tracker.feed(sample))
        row = self.minutes.add(sample)
        if row:
            self.store.append_csv("minute-summary.csv", self.minutes.header(), [row])

    def run(self):
        self.path.start()
        if self.modem:
            self.modem.request_snapshot("Startup")
            self.modem.start()
        self.store.append_jsonl("sessions.jsonl", {"type": "start", "time": storage.now_text(),
                                                   "ping_method": self.pinger.method})
        log.info("monitoring every %d ms; ping via %s; data in %s",
                 self.cfg.interval_ms, self.pinger.method, self.cfg.data_path)
        interval = self.cfg.interval_ms / 1000
        next_start = time.monotonic()
        last_cleanup = None
        pending: Dict[int, cf.Future] = {}
        next_feed = 1
        skipped = 0
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if now >= next_start:
                    if now - next_start > 5:
                        # After sleep or a long stall, restart the schedule instead
                        # of firing a burst of catch-up samples.
                        next_start = now
                    if len(pending) < self.max_in_flight:
                        self.sequence += 1
                        # Lateness is recorded, never absorbed (spec 3.2); a tick
                        # skipped because too many samples were in flight counts too.
                        late_ms = int((now - next_start) * 1000) + int(skipped * interval * 1000)
                        skipped = 0
                        pending[self.sequence] = self.sampler.submit(
                            self.take_sample, self.sequence, storage.now_text(), late_ms)
                    else:
                        skipped += 1
                    next_start += interval
                # Samples finish out of order during an outage; events need them in order.
                while next_feed in pending and pending[next_feed].done():
                    self._process(pending.pop(next_feed).result())
                    next_feed += 1
                today = dt.date.today()
                if last_cleanup != today:
                    self.store.cleanup(self.cfg.retention_days)
                    last_cleanup = today
                self._stop.wait(min(0.02, max(0.0, next_start - time.monotonic())))
            for seq in sorted(pending):
                self._process(pending[seq].result())
            if self.minutes.samples:
                self.store.append_csv("minute-summary.csv", self.minutes.header(), [self.minutes.row()])
        finally:
            self.path.stop()
            if self.modem:
                self.modem.stop()
            self.sampler.shutdown(wait=False)
            self.pool.shutdown(wait=False)
            self.store.append_jsonl("sessions.jsonl", {"type": "stop", "time": storage.now_text(),
                                                       "open_event": self.tracker.event.start.timestamp
                                                       if self.tracker.event else None})
