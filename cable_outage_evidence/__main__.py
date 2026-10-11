"""Command line: python -m cable_outage_evidence <command>."""

import argparse
import logging
import signal
import sys
from pathlib import Path

from . import __version__, analysis, config, hitron, monitor, platform_net, report, setup, storage, ucid


def cmd_run(cfg, args):
    m = monitor.Monitor(cfg)
    signal.signal(signal.SIGINT, lambda *_: m.stop())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: m.stop())
    m.run()


def cmd_once(cfg, args):
    m = monitor.Monitor(cfg)
    m.path.discover()
    s = m.take_sample()
    from . import classify
    s.classification = classify.classify_sample(s)
    width = max(len(n) for n in ["LanGateway", "Modem", "FirstHop"] + m.names)
    for label, p in [("LanGateway", s.lan_gateway), ("Modem", s.modem), ("FirstHop", s.first_hop)] + list(s.public.items()):
        state = "not probed" if not p.probed else (f"{p.rtt:.1f} ms" if p.ok else "FAILED")
        print(f"{label:<{width}}  {p.address or '-':<16}  {state}")
    print(f"classification: {s.classification}  (ping via {m.pinger.method})")


def cmd_discover_path(cfg, args):
    pinger = platform_net.Pinger()
    gw = platform_net.default_gateway()
    print(f"default gateway: {gw or 'none'}")
    hops = platform_net.traceroute(cfg.path_discovery_target)
    for i, h in enumerate(hops, 1):
        print(f"  hop {i}: {h or '*'}")
    print(f"ISP first hop: {platform_net.first_isp_hop(hops, [gw, cfg.modem_address]) or 'unknown'}")


def _config_path(args) -> Path:
    return Path(args.config) if args.config else config.default_config_path()


def cmd_init(cfg, args):
    path = _config_path(args)
    if path.exists() and not args.force:
        print(f"{path} already exists (use --force to overwrite)")
        return 1
    if sys.stdin.isatty():
        setup.interactive(cfg)
    print(f"wrote {config.save(cfg, path)}")


def cmd_modem_check(cfg, args):
    """Logs in once and prints what the modem reports; never prints credentials."""
    if cfg.modem_type == "none":
        print("modem reading is off (modem_type = none)")
        return 1
    status, lines = setup.check(cfg)
    for line in lines:
        print(line)
    return 0 if status == setup.OK else 1


def _emit(text, args):
    if getattr(args, "out", None):
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(text)


def cmd_report(cfg, args):
    _emit(report.summary(cfg.data_path, args.days, not args.no_redact), args)


def cmd_evidence(cfg, args):
    _emit(report.evidence(cfg.data_path, args.time, not args.no_redact), args)


def cmd_timeline(cfg, args):
    _emit(report.timeline(cfg.data_path, args.time, not args.no_redact), args)


def cmd_docsis_log(cfg, args):
    _emit(report.docsis_log(cfg.data_path, args.days, not args.no_redact), args)


def cmd_ucid_report(cfg, args):
    _emit(report.Redactor(not args.no_redact)(ucid.report(cfg.data_path, args.ucid, args.days)), args)


def cmd_classify(cfg, args):
    events = analysis.all_events(cfg.data_path)
    if args.time:
        events = [e for e in events if e["StartTime"].startswith(args.time)]
    for e in events:
        c = analysis.classify_and_save(cfg.data_path, e)
        print(f"{e['StartTime'][:19]}  {e['Grade']}  {c.cls:<12}  {'; '.join(c.reasons)[:120]}")
    if not events:
        print("no matching event")


def cmd_paths(cfg, args):
    print(f"config: {Path(args.config) if args.config else config.default_config_path()}")
    print(f"data:   {cfg.data_path}")


def cmd_start(cfg, args):
    """What a double-click does: set up on first use, check the modem, monitor.

    Settings are saved only once the modem accepted them or the user decided;
    a wrong password found later is offered for re-entry right here.
    """
    path = _config_path(args)
    interactive = sys.stdin.isatty()
    print(f"cable-outage-evidence {__version__}")
    if not path.exists():
        print("第一次使用，先設定數據機。自動辨識 Hitron / CBN；直接按 Enter 會使用括號裡的值。")
        if interactive:
            setup.interactive(cfg)
        config.save(cfg, path)
        print(f"已儲存設定 / saved {path}")
    elif cfg.modem_type != "none":
        status, lines = setup.check(cfg, "check")
        for line in lines:
            print(line)
        if status == setup.UNSUPPORTED and interactive:
            answer = input("要改成不讀數據機（只記錄斷線），以後不再檢查嗎？[Y/n] ").strip().lower()
            if answer in ("", "y", "yes"):
                cfg.modem_type = "none"     # the address stays: the modem is still pinged
                config.save(cfg, path)
                print(f"已更新設定 / saved {path}")
        elif status == setup.AUTH_FAILED and interactive:
            answer = input("要現在重新輸入數據機帳密嗎？[Y/n] ").strip().lower()
            if answer in ("", "y", "yes"):
                # Every outcome is a decision (verified, kept on purpose, or
                # modem reading off with the old login), so it is saved.
                setup.interactive(cfg)
                config.save(cfg, path)
                print(f"已更新設定 / saved {path}")
    print(f"資料夾 data: {cfg.data_path}")
    print("開始監控。這個視窗要保持開著；要停止請按 Ctrl+C。")
    print("事件報告：在命令列執行 cable-outage-evidence report（或 evidence / timeline）。")
    cmd_run(cfg, args)


COMMANDS = {
    "start": (cmd_start, "first-time setup if needed, modem check, then monitor (the default)"),
    "run": (cmd_run, "monitor until stopped (Ctrl+C)"),
    "once": (cmd_once, "take one sample and print it"),
    "discover-path": (cmd_discover_path, "show the default gateway and ISP first hop"),
    "modem-check": (cmd_modem_check, "log in to the modem once and show its channel state"),
    "report": (cmd_report, "summary report for the ISP"),
    "evidence": (cmd_evidence, "full evidence for one event (latest, or TIME)"),
    "timeline": (cmd_timeline, "timeline of one event (latest, or TIME)"),
    "docsis-log": (cmd_docsis_log, "the collected DOCSIS event log"),
    "ucid-report": (cmd_ucid_report, "one upstream channel's T4 / CM-STATUS / channel history"),
    "classify": (cmd_classify, "classify events again (all, or those starting with TIME)"),
    "init": (cmd_init, "write a config file with every default"),
    "paths": (cmd_paths, "print the config and data locations"),
}


def main(argv=None):
    p = argparse.ArgumentParser(prog="cable-outage-evidence",
                                description="Records cable outages with evidence for ISP reports.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config", help="config file (default: per-user location)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command")
    for name, (_, text) in COMMANDS.items():
        sp = sub.add_parser(name, help=text)
        if name == "init":
            sp.add_argument("--force", action="store_true")
        if name in ("evidence", "timeline", "classify"):
            sp.add_argument("time", nargs="?", default="", help="start time prefix, e.g. '2026-09-03 23:26'")
        if name == "ucid-report":
            sp.add_argument("ucid", help="upstream channel ID as the CM-STATUS entries name it, e.g. 7")
        if name in ("report", "docsis-log", "ucid-report"):
            sp.add_argument("--days", type=int, default={"report": 30, "docsis-log": 7}.get(name, 14))
        if name in ("report", "evidence", "timeline", "docsis-log", "ucid-report"):
            sp.add_argument("--no-redact", action="store_true", help="do not mask home addresses and MACs")
            sp.add_argument("--out", help="write to this file (UTF-8) instead of the console")
    argv = sys.argv[1:] if argv is None else argv
    # No command (a double-click on the exe): start. Options alone still apply.
    if not any(a in COMMANDS for a in argv) and not any(a in ("-h", "--help", "--version") for a in argv):
        argv = list(argv) + ["start"]
    args = p.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        # Reports are Chinese; a legacy console code page must not crash them.
        sys.stdout.reconfigure(errors="replace", line_buffering=True)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = config.load(Path(args.config) if args.config else None)
    try:
        return COMMANDS[args.command][0](cfg, args) or 0
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        if getattr(sys, "frozen", False) and args.command == "start":
            # A double-clicked window would close before the message is read.
            print(f"錯誤 / error: {e}")
            input("按 Enter 結束 / press Enter to exit")
            return 1
        raise


if __name__ == "__main__":
    sys.exit(main())
