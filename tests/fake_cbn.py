"""Local CBN API with synthetic channel data and rotating CSRF tokens."""

import base64
import copy
import http.server
import json
import threading
import time
import urllib.parse

DOCSIS = {
    "DeviceInfo": {"NetworkAccess": "1"},
    "Downstream": {"0": {"ChannelID": "29", "Frequency": "699000000", "PowerLevel": "7.199",
        "SNRLevel": "40.946", "Modulation": "256qam", "Octets": "123456", "Correcteds": "2",
        "Uncorrectables": "3", "LockStatus": "Locked"}},
    "Upstream": {"0": {"ChannelID": "11", "Frequency": "28200000", "PowerLevel": "38",
        "Modulation": "64qam", "LockStatus": "Locked", "ChannelBandwidth": "6400000"}},
    "OFDM": [{"ofdmId": 0, "PLCLocked": "NO", "rxPower": "NA"},
        {"ofdmId": 1, "PLCLocked": "YES", "NCPLocked": "YES", "MDC1Lock": "YES",
         "rxPower": "7.2", "fftType": "4K", "OFDMFrequency": "692600000", "Modulation": "4096QAM"}],
    "OFDMA": [{"CH": 0, "STATE": 2, "BW": "72.8", "repPower": "55.58",
        "repPower1_6": "39", "FFTSIZE": "2K", "DigitalAtt": "3.57", "Modulation": "2048QAM"},
        {"CH": 1, "STATE": 1, "BW": "39.6", "repPower": "47.94", "repPower1_6": "34"}],
}


class FakeCbn:
    def __init__(self):
        self.token = "token0"
        self.sequence = 0
        self.logged_in = False
        self.bad_response = False
        self.reads = {}
        self.logins = 0
        self.docsis = copy.deepcopy(DOCSIS)
        self.body_delay = {}
        self.logs = [{"Time": "2026-10-01 12:00:00", "Priority": "critical",
            "Description": "Received Response to Broadcast Maintenance Request, But no Unicast Maintenance opportunities received - T4 time out;CM-MAC=00:00:00:00:00:01;CMTS-MAC=00:00:00:00:00:02;"}]
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, body, cookie=False, rotate=False, body_delay=0):
                self.send_response(200)
                if cookie:
                    self.send_header("Set-Cookie", "session=cbn; Path=/")
                if rotate:
                    fake.sequence += 1
                    fake.token = f"token{fake.sequence}"
                    self.send_header("CSRF_Token", fake.token)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body_delay:
                    self.wfile.flush()
                    time.sleep(body_delay)
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass  # Expected when testing a client's body-read timeout.

            def do_GET(self):
                if self.path == "/common_page/login.php":
                    return self.send(b'<script src="/js/CBN.js.php?v=test-version"></script>', cookie=True)
                if self.path == "/js/CBN.js.php?v=test-version" and self.headers.get("Cookie") == "session=cbn":
                    return self.send(f'var CSRF_Token="{fake.token}";'.encode())
                self.send(b"<html>CBN</html>")

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path.startswith("/1/"):
                    return self.send(b"<html>CBN</html>")
                form = urllib.parse.parse_qs(body.decode())
                value = json.loads(urllib.parse.unquote(base64.b64decode(form["Data"][0]).decode()))
                fun = value["fun"]
                fake.reads[fun] = fake.reads.get(fun, 0) + 1
                ok = (self.headers.get("CSRF_Token") == fake.token and
                      self.headers.get("Cookie") == "session=cbn" and
                      self.headers.get("Referer") == fake.url + "/common_page/login.php")
                if not ok:
                    data = {"Status": "error: invalid session or CSRF"}
                elif fun == 7:
                    fake.logins += 1
                    fake.logged_in = value.get("Username") == "admin" and value.get("Password") == "password"
                    data = {"Status": "success: login successfully." if fake.logged_in else "error: login"}
                elif fun == 8:
                    fake.logged_in = False
                    data = {"Status": "success: logout"}
                elif not fake.logged_in:
                    data = {"Status": "error: expired session"}
                elif fun == 108:
                    data = fake.docsis
                elif fun == 2:
                    data = {"SystemInfo": {"SystemUpTime": "1 day(s) 02h:03m:04s"},
                            "IPv4": {"DefaultGateware": "10.0.0.1"}}
                elif fun == 11:
                    data = {"DOCSIS_Log": fake.logs}
                else:
                    data = {}
                text = urllib.parse.quote(json.dumps(data), safe="~()*!.'-")
                self.send(b"bad payload" if fake.bad_response else base64.b64encode(text.encode()),
                          rotate=True, body_delay=fake.body_delay.get(fun, 0))

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
