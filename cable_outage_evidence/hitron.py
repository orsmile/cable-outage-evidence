"""Hitron CODA modem API (spec §8.1) and parsing of its responses.

The client is synchronous and bounded: every request has a hard timeout so a
slow or hung modem can only delay the modem worker, never the probe loop.
"""

import email.utils
import http.cookiejar
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

# Endpoint -> timeout in seconds (spec §8.2).
FAST_ENDPOINTS = ("UsInfo", "UsOfdm", "DsOfdm")
TIMEOUTS = {"UsInfo": 2, "UsOfdm": 2, "DsOfdm": 2, "DsInfo": 4, "SysInfo": 4, "Log": 4}


class ModemError(Exception):
    """Any failed modem read. The subclass says which kind (spec §8.2)."""


class ModemUnreachable(ModemError):
    """No HTTP answer: wrong address, modem off, or a timeout."""


class ModemAuthError(ModemError):
    """The modem answered but rejected the username or password."""


class ModemUnsupported(ModemError):
    """The device answers HTTP but is not a Hitron CODA web API (another brand
    or firmware). Retrying with other credentials cannot help."""


class ModemResponseError(ModemError):
    """The modem answered, but not with usable data (errCode other than 000,
    or not JSON). An expired session looks like this; the next read logs in."""


class HitronClient:
    def __init__(self, base_url: str, username: str, password: str):
        self.base = base_url.rstrip("/")
        self._username = username
        self._password = password
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar),
                                                  urllib.request.ProxyHandler({}))
        self.logged_in = False

    def login(self) -> None:
        body = urllib.parse.urlencode({"model": json.dumps(
            {"username": self._username, "password": self._password})}).encode()
        self.logged_in = False
        try:
            with self.opener.open(self.base + "/1/Device/Users/Login", data=body, timeout=4) as r:
                text = r.read(65536).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            # An HTTP error page (404, 405...) means there is no such API here.
            raise ModemUnsupported(f"login page answered HTTP {e.code}{_model_note(self.base)}") from None
        except OSError as e:
            raise ModemUnreachable(f"no answer from {self.base} ({type(e).__name__})") from None
        # A Hitron answers JSON with a "result" field. Anything else (another
        # brand's login page, a redirect to HTML) is not this API at all, and
        # must not be reported as a wrong password.
        try:
            reply = json.loads(text)
        except ValueError:
            reply = None
        if not isinstance(reply, dict) or "result" not in reply:
            raise ModemUnsupported(f"not a Hitron CODA web API{_model_note(self.base)}")
        # The response never echoes the credentials; only the result is checked.
        self.logged_in = str(reply.get("result")).lower() == "success"
        if not self.logged_in:
            raise ModemAuthError("login rejected: check modem_username and modem_password")

    def logout(self) -> None:
        body = urllib.parse.urlencode({"model": "{}"}).encode()
        try:
            self.opener.open(self.base + "/1/Device/Users/Logout", data=body, timeout=4).close()
        except OSError:
            pass
        self.logged_in = False

    def get(self, endpoint: str, timeout: Optional[float] = None) -> Tuple[Dict, Optional[float]]:
        """Returns (response, epoch of the modem's Date header). Raises
        ModemError on a timeout or any errCode other than 000; the caller logs
        in again then."""
        url = f"{self.base}/1/Device/CM/{endpoint}?_={int(time.time())}"
        try:
            with self.opener.open(url, timeout=timeout or TIMEOUTS.get(endpoint, 4)) as r:
                raw = r.read()
                date = r.headers.get("Date")
        except urllib.error.HTTPError as e:
            raise ModemResponseError(f"{endpoint}: HTTP {e.code}") from None
        except OSError as e:
            raise ModemUnreachable(f"{endpoint}: no answer ({type(e).__name__})") from None
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise ModemResponseError(f"{endpoint}: not JSON") from None
        if str(data.get("errCode", "")) != "000":
            self.logged_in = False
            raise ModemResponseError(f"{endpoint}: errCode {data.get('errCode')!r}")
        return data, parse_http_date(date)


VENDORS = ("Hitron", "CODA", "Compal", "CBN", "Connect Box", "Technicolor", "Arris", "ARRIS", "Sagemcom",
           "Ubee", "Netgear", "NETGEAR", "Askey", "Sercomm", "Huawei", "ZTE", "Motorola", "Castlenet", "Casa")


def identify(base_url: str, timeout: float = 4) -> str:
    """Brand words and the page title found on the modem's home page, e.g.
    "CBN; title: CBN CH8568". Empty if nothing could be read."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(base_url.rstrip("/") + "/", timeout=timeout) as r:
            html = r.read(262144).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            html = e.read(262144).decode("utf-8", "replace")
        except OSError:
            return ""
    except OSError:
        return ""
    found = []
    for v in VENDORS:
        if v.lower() in html.lower() and v.lower() not in (f.lower() for f in found):
            found.append(v)
    m = re.search(r"<title[^>]*>\s*([^<]{1,80}?)\s*</title>", html, re.IGNORECASE)
    parts = found[:3] + ([f"title: {m.group(1)}"] if m and m.group(1).strip() else [])
    return "; ".join(parts)


def _model_note(base_url: str) -> str:
    who = identify(base_url)
    return f" (found: {who})" if who else ""


def parse_http_date(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return email.utils.parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError):
        return None


# --- parsing --------------------------------------------------------------------

def clean(value) -> str:
    """The modem pads values with spaces ("   OPERATE", "   70.8000")."""
    return "" if value is None else str(value).replace(" ", "")


def num(value) -> Optional[float]:
    try:
        return float(clean(value))
    except ValueError:
        return None


def freq_list(resp: Optional[Dict]) -> List[Dict]:
    return (resp or {}).get("Freq_List") or []


CHANNEL_HEADER = ["Timestamp", "Reason", "Kind", "Index", "ChannelId", "State", "FrequencyHz", "Width",
                  "Modulation", "PowerDbmv", "SnrDb", "Fft", "DigAtten", "DigAttenBo", "RepPower1_6",
                  "PlcLock", "NcpLock", "Mdc1Lock", "Octets", "Corrected", "Uncorrectable"]


def channel_rows(ts: str, reason: str, us=None, of=None, ds=None, dof=None) -> List[List[str]]:
    rows = []

    def row(kind, **v):
        rows.append([ts, reason, kind] + [clean(v.get(k.lower(), "")) for k in CHANNEL_HEADER[3:]])

    for c in freq_list(us):
        row("us_scqam", index=c.get("portId"), channelid=c.get("channelId"), state=c.get("lockStatus"),
            frequencyhz=c.get("frequency"), width=c.get("bandwidth"), modulation=c.get("modulationType"),
            powerdbmv=c.get("signalStrength"))
    for c in freq_list(of):
        row("us_ofdma", index=c.get("uschindex"), state=c.get("state"), width=c.get("channelBw"),
            powerdbmv=c.get("repPower"), fft=c.get("fftVal"), digatten=c.get("digAtten"),
            digattenbo=c.get("digAttenBo"), reppower1_6=c.get("repPower1_6"), modulation=c.get("modulation"))
    for c in freq_list(ds):
        row("ds_scqam", index=c.get("portId"), channelid=c.get("channelId"), state=c.get("lockStatus"),
            frequencyhz=c.get("frequency"), modulation=c.get("modulation"), powerdbmv=c.get("signalStrength"),
            snrdb=c.get("snr"), octets=c.get("dsoctets"), corrected=c.get("correcteds"),
            uncorrectable=c.get("uncorrect"))
    for c in freq_list(dof):
        row("ds_ofdm", index=c.get("receive"), state=c.get("plclock"), frequencyhz=c.get("Subcarr0freqFreq"),
            powerdbmv=c.get("plcpower"), fft=c.get("ffttype"), plclock=c.get("plclock"),
            ncplock=c.get("ncplock"), mdc1lock=c.get("mdc1lock"), modulation=c.get("modulation"))
    return rows


def upstream_state(us=None, of=None) -> Tuple[str, ...]:
    """Which upstream channels exist and whether they operate, on which
    frequency, width and modulation (spec §8.3). Power and attenuation drift
    continuously and are deliberately left out."""
    parts = []
    for c in freq_list(us):
        parts.append(":".join(["sc", clean(c.get("portId")), clean(c.get("channelId")), clean(c.get("lockStatus")),
                               clean(c.get("frequency")), clean(c.get("bandwidth")), clean(c.get("modulationType"))]))
    for c in freq_list(of):
        parts.append(":".join(["of", clean(c.get("uschindex")), clean(c.get("state")),
                               clean(c.get("channelBw")), clean(c.get("fftVal"))]))
    return tuple(sorted(parts))


def counts(us=None, of=None, ds=None, dof=None) -> Dict[str, Optional[int]]:
    def n(lst, ok):
        return (sum(1 for c in lst if ok(c)), len(lst))
    out = {}
    if us is not None:
        out["UsScQamOperate"], out["UsScQamCount"] = n(freq_list(us), lambda c: clean(c.get("lockStatus")) == "OPERATE")
    if of is not None:
        out["UsOfdmaOperate"], out["UsOfdmaCount"] = n(freq_list(of), lambda c: "OPERATE" in clean(c.get("state")))
    if dof is not None:
        out["DsOfdmLocked"], out["DsOfdmCount"] = n(freq_list(dof), lambda c: clean(c.get("plclock")) == "YES")
    if ds is not None:
        out["DsScQamLocked"], out["DsScQamCount"] = n(freq_list(ds), lambda c: clean(c.get("lockStatus")) == "Locked")
    return out


SUMMARY_HEADER = ["Timestamp", "Reason", "ReadOK", "Error", "NetworkAccess", "CmGateway",
                  "DsChannelCount", "DsLockedCount", "DsPowerMin", "DsPowerMax", "DsSnrMin",
                  "DsCorrectedTotal", "DsUncorrectableTotal",
                  "DsOfdmCount", "DsOfdmLockedCount", "DsOfdmPowerMin", "DsOfdmPowerMax",
                  "UsScQamCount", "UsScQamOperateCount", "UsPowerMin", "UsPowerMax",
                  "UsOfdmaCount", "UsOfdmaOperateCount", "UsOfdmaReportPowerMin", "UsOfdmaReportPowerMax",
                  "UsOfdmaBwMin", "UsOfdmaBwMax", "UsOfdmaPower1_6Min", "UsOfdmaPower1_6Max"]


def summary_row(ts: str, reason: str, data: Dict[str, Dict]) -> List[str]:
    def vals(resp, key, skip_na=True):
        out = []
        for c in freq_list(resp):
            v = num(c.get(key))
            if v is not None:
                out.append(v)
        return out

    def mn(v):
        return f"{min(v):g}" if v else ""

    def mx(v):
        return f"{max(v):g}" if v else ""

    def tot(v):
        return f"{sum(v):g}" if v else "0"

    sysinfo, ds, us = data.get("SysInfo") or {}, data.get("DsInfo"), data.get("UsInfo")
    dof, of = data.get("DsOfdm"), data.get("UsOfdm")
    c = counts(us, of, ds, dof)
    dsp, dssnr = vals(ds, "signalStrength"), vals(ds, "snr")
    return [ts, reason, "1", "", clean(sysinfo.get("ntAccess")), clean(sysinfo.get("gw")),
            str(len(freq_list(ds))), str(c.get("DsScQamLocked", "")), mn(dsp), mx(dsp), mn(dssnr),
            tot(vals(ds, "correcteds")), tot(vals(ds, "uncorrect")),
            str(len(freq_list(dof))), str(c.get("DsOfdmLocked", "")), mn(vals(dof, "plcpower")), mx(vals(dof, "plcpower")),
            str(len(freq_list(us))), str(c.get("UsScQamOperate", "")), mn(vals(us, "signalStrength")), mx(vals(us, "signalStrength")),
            str(len(freq_list(of))), str(c.get("UsOfdmaOperate", "")), mn(vals(of, "repPower")), mx(vals(of, "repPower")),
            mn(vals(of, "channelBw")), mx(vals(of, "channelBw")), mn(vals(of, "repPower1_6")), mx(vals(of, "repPower1_6"))]


def failure_row(ts: str, reason: str, error: str) -> List[str]:
    return [ts, reason, "0", error] + [""] * (len(SUMMARY_HEADER) - 4)


LOG_HEADER = ["RecordedAt", "LogTime", "Priority", "EventType", "Message", "CmtsMac", "CmMac"]


def _tag(event: str, name: str) -> str:
    return event.split(name + "=", 1)[1].split(";", 1)[0] if name + "=" in event else ""


def log_entries(resp: Optional[Dict]) -> List[Tuple[str, str, str, str, str, str]]:
    """(LogTime, Priority, EventType, Message, CmtsMac, CmMac) per entry. The
    trailing ;CM-MAC=..;CMTS-MAC=..;CM-QOS=..;CM-VER=.. tags are split off the
    message. Both MACs are kept: the ISP finds the subscriber by the modem's
    MAC and the node by the CMTS's."""
    out = []
    for e in (resp or {}).get("Log_List") or []:
        event = str(e.get("event") or "")
        message = event.split(";CM-MAC=")[0]
        out.append((str(e.get("time") or ""), str(e.get("priority") or ""), str(e.get("type") or ""),
                    message, _tag(event, "CMTS-MAC"), _tag(event, ";CM-MAC")))
    return out


class LogDeduper:
    """Identifies entries by content, never by index: index is a position in
    the 40-entry ring that shifts as new entries arrive (spec §8.5)."""

    KEEP = 240

    def __init__(self, path):
        self.path = path
        self.seen: List[str] = []
        try:
            with open(path, encoding="utf-8") as fh:
                self.seen = [line.rstrip("\n") for line in fh if line.strip()]
        except OSError:
            pass
        self._set = set(self.seen)

    def new(self, entries) -> List[Tuple]:
        fresh = []
        for e in entries:
            # The first five fields only, so keys written before CmMac existed still match.
            key = json.dumps(list(e[:5]), ensure_ascii=False)
            if key not in self._set:
                self._set.add(key)
                self.seen.append(key)
                fresh.append(e)
        if fresh:
            self.seen = self.seen[-self.KEEP:]
            self._set = set(self.seen)
            with open(self.path, "w", encoding="utf-8") as fh:
                fh.write("\n".join(self.seen) + "\n")
        return fresh
