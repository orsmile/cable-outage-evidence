"""Interactive modem setup: ask, verify by logging in, and only then keep it.

A wrong password is never saved silently: the user is asked again, or chooses
to turn modem reading off, or explicitly keeps the settings (for a modem that
is switched off right now). Input functions are parameters so tests can drive
the dialogue.
"""

import getpass
from typing import Callable, List, Tuple

from . import config, hitron, modem
from .modem_worker import AUTH_FAILED, BAD_RESPONSE, OK, UNREACHABLE, UNSUPPORTED, status_message

Ask = Callable[[str], str]


def check(cfg: config.Config, context: str = "check") -> Tuple[str, List[str]]:
    """Logs in once and reads the channel state. Returns (status, lines to show);
    the lines never contain the password."""
    client = modem.create_client(cfg)
    try:
        client.login()
        data = {ep: client.get(ep)[0] for ep in ("SysInfo", "UsInfo", "UsOfdm", "DsInfo", "DsOfdm")}
        c = hitron.counts(data["UsInfo"], data["UsOfdm"], data["DsInfo"], data["DsOfdm"])
        entries = hitron.log_entries(client.get("Log")[0])
        return OK, [
            "數據機連線成功 / modem OK",
            f"  網路存取 network access: {hitron.clean(data['SysInfo'].get('ntAccess'))}",
            f"  上行 upstream: SC-QAM {c['UsScQamOperate']}/{c['UsScQamCount']} OPERATE，"
            f"OFDMA {c['UsOfdmaOperate']}/{c['UsOfdmaCount']} OPERATE",
            f"  下行 downstream: SC-QAM {c['DsScQamLocked']}/{c['DsScQamCount']} locked，"
            f"OFDM {c['DsOfdmLocked']}/{c['DsOfdmCount']} locked",
            f"  數據機日誌 event log: {len(entries)} 筆",
        ]
    except hitron.ModemError as e:
        kind = (AUTH_FAILED if isinstance(e, hitron.ModemAuthError) else
                UNSUPPORTED if isinstance(e, hitron.ModemUnsupported) else
                UNREACHABLE if isinstance(e, hitron.ModemUnreachable) else BAD_RESPONSE)
        return kind, [status_message(kind, str(e), cfg, context)]
    finally:
        client.logout()


def ask_settings(cfg: config.Config, ask: Ask, secret: Ask) -> None:
    """Asks for the modem address and login; Enter keeps the value shown."""
    url = ask(f"數據機位址 modem address [{cfg.modem_url}]（輸入 none 表示不讀數據機）: ").strip()
    if url.lower() == "none":
        # No cable modem at all: nothing to read and nothing to ping.
        cfg.modem_type = "none"
        cfg.modem_url = ""
        return
    cfg.modem_type = "auto"
    if url:
        cfg.modem_url = url if "://" in url else "http://" + url
    cfg.modem_username = ask(f"帳號 username [{cfg.modem_username}]: ").strip() or cfg.modem_username
    cfg.modem_password = secret("密碼 password（不會顯示；直接按 Enter 沿用目前的）: ") or cfg.modem_password


OFF = "off"
KEPT = "kept"


def interactive(cfg: config.Config, ask: Ask = None, secret: Ask = None,
                out: Callable[[str], None] = None) -> str:
    """Runs the dialogue until the modem answers or the user decides.

    Returns OK; OFF (modem reading turned off: the login entered in this
    dialogue is not kept, since it never worked); or KEPT (the user chose to
    keep settings the modem did not accept, e.g. while it is switched off).
    """
    # Looked up at call time, so a replaced input() or getpass() is honoured.
    ask = ask or (lambda prompt: input(prompt))
    secret = secret or (lambda prompt: getpass.getpass(prompt))
    out = out or print
    original = (cfg.modem_username, cfg.modem_password)

    def off():
        # Reading off, but the address stays: the modem is still pinged as a layer.
        cfg.modem_type = "none"
        cfg.modem_username, cfg.modem_password = original
        out("不讀數據機：只記錄斷線，報告裡不會有 DOCSIS 日誌與頻道狀態。")
        return OFF

    while True:
        ask_settings(cfg, ask, secret)
        if cfg.modem_type == "none":
            return off()
        out("正在連線數據機…")
        status, lines = check(cfg, "dialog")
        for line in lines:
            out(line)
        if status == OK:
            return OK
        if status == UNSUPPORTED:
            # Other credentials cannot help, so not reading is the default.
            choice = ask("1 換一個數據機位址重試  2 不讀數據機，只記錄斷線（預設）: ").strip() or "2"
        else:
            choice = ask("1 重新輸入（預設）  2 不讀數據機，只記錄斷線  3 先照這樣存（例如數據機暫時關機）: ").strip()
        if choice == "2":
            return off()
        if choice == "3":
            return KEPT
        out("")
