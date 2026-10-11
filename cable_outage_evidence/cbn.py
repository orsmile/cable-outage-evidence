"""CBN /Request API, adapted to the channel/log schema used by the worker.

CG8673V2 firmware uses base64(encodeURIComponent(JSON)), session cookies and
a rotating CSRF header. One DOCSIS response contains all four channel groups.
"""

import base64
import binascii
import datetime as dt
import html
import http.cookiejar
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import evidence, hitron


def encode_payload(value):
    text = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    return base64.b64encode(urllib.parse.quote(text, safe="~()*!.'-").encode()).decode()


def decode_payload(raw):
    try:
        data = json.loads(urllib.parse.unquote(base64.b64decode(raw.strip(), validate=True).decode()))
    except (ValueError, UnicodeError, binascii.Error):
        raise hitron.ModemResponseError("CBN: invalid encoded response") from None
    if not isinstance(data, dict):
        raise hitron.ModemResponseError("CBN: expected an object")
    return data


def rows(value):
    if isinstance(value, dict):
        result = list(value.values())
    elif isinstance(value, list):
        result = value
    else:
        raise hitron.ModemResponseError("CBN: invalid channel/log collection")
    if any(not isinstance(c, dict) for c in result):
        raise hitron.ModemResponseError("CBN: invalid channel/log row")
    return result


def normalize_docsis(data):
    if not isinstance(data.get("DeviceInfo"), dict):
        raise hitron.ModemResponseError("CBN: missing device information")
    try:
        ds, us, of, dof = (rows(data[k]) for k in ("Downstream", "Upstream", "OFDMA", "OFDM"))
    except KeyError:
        raise hitron.ModemResponseError("CBN: missing DOCSIS channel groups") from None
    return {
        "DsInfo": {"Freq_List": [dict(portId=i, channelId=c.get("ChannelID"),
            lockStatus=c.get("LockStatus"), frequency=c.get("Frequency"),
            signalStrength=c.get("PowerLevel"), snr=c.get("SNRLevel"), modulation=c.get("Modulation"),
            dsoctets=c.get("Octets"), correcteds=c.get("Correcteds"),
            uncorrect=c.get("Uncorrectables")) for i, c in enumerate(ds)]},
        "UsInfo": {"Freq_List": [dict(portId=i, channelId=c.get("ChannelID"),
            lockStatus="OPERATE" if c.get("LockStatus") == "Locked" else c.get("LockStatus"),
            frequency=c.get("Frequency"), bandwidth=c.get("ChannelBandwidth"),
            signalStrength=c.get("PowerLevel"), modulationType=c.get("Modulation")) for i, c in enumerate(us)]},
        "UsOfdm": {"Freq_List": [dict(uschindex=c.get("CH", i),
            state="OPERATE" if str(c.get("STATE")) == "2" else "NOT_READY",
            channelBw=c.get("BW"), repPower=c.get("repPower"), repPower1_6=c.get("repPower1_6"),
            fftVal=c.get("FFTSIZE"), digAtten=c.get("DigitalAtt"), modulation=c.get("Modulation")) for i, c in enumerate(of)]},
        "DsOfdm": {"Freq_List": [dict(receive=c.get("ofdmId", i), plclock=c.get("PLCLocked"),
            ncplock=c.get("NCPLocked"), mdc1lock=c.get("MDC1Lock"),
            Subcarr0freqFreq=c.get("OFDMFrequency"), plcpower=c.get("rxPower"),
            ffttype=c.get("fftType"), modulation=c.get("Modulation")) for i, c in enumerate(dof)]},
    }


def event_type(message):
    # CBN omits Hitron's numeric type; map only the evidence already recognised
    # by this application. Other descriptions remain unclassified.
    if re.search(r"(?i)T4\s*time.?out", message):
        return evidence.T4
    if re.search(r"(?i)T3\s*time.?out", message):
        return evidence.T3
    if "CM-STATUS" in message.upper():
        return evidence.CM_STATUS
    return ""


def log_time(value):
    """Keep downstream CSV/analysis on Hitron's single local-clock format."""
    text = str(value or "")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
        try:
            return dt.datetime.strptime(text, fmt).strftime("%m/%d/%Y %H:%M:%S")
        except ValueError:
            pass
    # Preserve undated/malformed entries for diagnosis; never invent a time.
    return text


class CbnClient:
    def __init__(self, base_url, username, password):
        self.base = base_url.rstrip("/")
        self._username, self._password = username, password
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), urllib.request.ProxyHandler({}))
        self.logged_in = False
        self._token = ""
        self._lock = threading.Lock()
        self._docsis = None
        self._docsis_at = 0.0
        self._access = ""

    def _read(self, path, timeout, body=None, detect=False):
        req = urllib.request.Request(self.base + path, data=body, headers={
            "Referer": self.base + "/common_page/login.php", "CSRF_Token": self._token})
        try:
            with self.opener.open(req, timeout=timeout) as response:
                # Headers may arrive even if reading the body later times out.
                self._token = response.headers.get("CSRF_Token", self._token)
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise hitron.ModemResponseError("CBN: response too large")
                return raw, hitron.parse_http_date(response.headers.get("Date"))
        except urllib.error.HTTPError as e:
            if detect:
                raise hitron.ModemUnsupported(f"no CBN web API (HTTP {e.code})") from None
            raise hitron.ModemResponseError(f"CBN: HTTP {e.code}") from None
        except OSError as e:
            raise hitron.ModemUnreachable(f"CBN: no answer ({type(e).__name__})") from None

    def _api(self, kind, fun, timeout=4, **params):
        body = urllib.parse.urlencode({"Data": encode_payload(dict(fun=fun, **params))}).encode()
        raw, date = self._read(f"/Request/{kind}.php", timeout, body)
        return decode_payload(raw), date

    def login(self):
        with self._lock:
            self.logged_in = False
            self._token = ""
            self._docsis = None
            page, _ = self._read("/common_page/login.php", 4, detect=True)
            script = re.search(r'''src\s*=\s*["']([^"']*/js/CBN\.js\.php[^"']*)["']''', page.decode("utf-8", "replace"))
            if not script:
                raise hitron.ModemUnsupported("no CBN /Request web API")
            # Only the known local script may be fetched, never a page-supplied host.
            src = urllib.parse.urlsplit(html.unescape(script.group(1)))
            if src.scheme or src.netloc or src.path != "/js/CBN.js.php":
                raise hitron.ModemUnsupported("unrecognised CBN script URL")
            js, _ = self._read(src.path + ("?" + src.query if src.query else ""), 4, detect=True)
            token = re.search(r'''\bvar\s+CSRF_Token\s*=\s*["']([^"']+)["']''', js.decode("utf-8", "replace"))
            if not token:
                raise hitron.ModemResponseError("CBN: CSRF token missing")
            self._token = token.group(1)
            reply, _ = self._api("Set", 7, Username=self._username, Password=self._password)
            if reply.get("Status") != "success: login successfully.":
                # Do not echo the server body, which could contain credentials.
                raise hitron.ModemAuthError("CBN login rejected: check modem_username and modem_password")
            self.logged_in = True

    def logout(self):
        with self._lock:
            try:
                if self.logged_in:
                    self._api("Set", 8)
            except hitron.ModemError:
                pass
            finally:
                self.logged_in = False
                self._docsis = None

    def get(self, endpoint, timeout=None):
        timeout = timeout or hitron.TIMEOUTS.get(endpoint, 4)
        deadline = time.monotonic() + timeout
        if not self._lock.acquire(timeout=timeout):
            raise hitron.ModemUnreachable("CBN: request queue timeout")
        try:
            remaining = max(0.001, deadline - time.monotonic())
            if endpoint in ("UsInfo", "UsOfdm", "DsInfo", "DsOfdm"):
                if self._docsis is None or time.monotonic() - self._docsis_at >= 0.5:
                    data, date = self._api("Get", 108, remaining)
                    self._docsis = normalize_docsis(data), date
                    device = data["DeviceInfo"]
                    self._access = "Permitted" if str(device.get("NetworkAccess")) == "1" else device.get("NetworkAccess", "")
                    self._docsis_at = time.monotonic()
                data, date = self._docsis
                return data[endpoint], date
            if endpoint == "SysInfo":
                data, date = self._api("Get", 2, remaining)
                if not isinstance(data.get("IPv4"), dict) or not isinstance(data.get("SystemInfo"), dict):
                    raise hitron.ModemResponseError("CBN: missing system information")
                # NetworkAccess is delivered by Get 108, not Get 2. Read it
                # within the same deadline even when SysInfo is requested first.
                docsis, docsis_date = self._api("Get", 108, max(0.001, deadline - time.monotonic()))
                self._docsis = normalize_docsis(docsis), docsis_date
                self._docsis_at = time.monotonic()
                device = docsis["DeviceInfo"]
                self._access = "Permitted" if str(device.get("NetworkAccess")) == "1" else device.get("NetworkAccess", "")
                return {"gw": data["IPv4"].get("DefaultGateware"),
                        "ntAccess": self._access, "SystemUpTime": data["SystemInfo"].get("SystemUpTime")}, date
            if endpoint == "Log":
                data, date = self._api("Get", 11, remaining)
                if "DOCSIS_Log" not in data:
                    raise hitron.ModemResponseError("CBN: missing DOCSIS log")
                entries = rows(data["DOCSIS_Log"])
                return {"Log_List": [dict(time=log_time(c.get("Time")), priority=c.get("Priority"),
                    type=event_type(str(c.get("Description") or "")), event=c.get("Description")) for c in entries]}, date
            raise hitron.ModemResponseError("CBN: unknown endpoint")
        except hitron.ModemUnreachable:
            # A transport failure alone is not proof that the session expired.
            # Retry with the last received token. If a lost response rotated it,
            # the next rejected/invalid reply takes the reauthentication path.
            self._docsis = None
            raise
        except hitron.ModemError:
            self.logged_in = False
            self._docsis = None
            raise
        finally:
            self._lock.release()
