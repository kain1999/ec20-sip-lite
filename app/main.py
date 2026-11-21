"""Entry point: wires config, database, modem, SIP server and the web panel."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

from . import util
from .config import Config
from .db import Db
from .ec20.modem import Modem
from .ec20.sim import SimModem
from .notify import Notifier
from .sip.server import SipServer
from .web.server import AppContext, WebServer

LOG = logging.getLogger("main")


def build_parser():
    parser = argparse.ArgumentParser(description="ec20-sip-lite: lean SIP + EC20 gateway")
    parser.add_argument("-c", "--config", default=os.environ.get("EC20SIP_CONFIG", "/etc/ec20-sip/config.json"))
    parser.add_argument("--sim", action="store_true", help="run without hardware (simulated modem)")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--sip-port", type=int, default=None)
    parser.add_argument("--web-port", type=int, default=None)
    parser.add_argument("--no-web", action="store_true")
    parser.add_argument("--print-password", action="store_true", help="reset admin password and print it")
    parser.add_argument("--version", action="version", version="ec20-sip-lite 1.0.0")
    return parser


async def run(args):
    config = Config(args.config)
    config.ensure_dirs()
    if args.sip_port:
        config.set("sip.port", args.sip_port)
    if args.web_port:
        config.set("web.port", args.web_port)
    debug = args.debug or bool(config.get("debug"))
    util.setup_logging("DEBUG" if debug else "INFO", config.get("paths.log_file"), debug=debug)

    data_dir = config.get("paths.data_dir")
    db = Db(os.path.join(data_dir, "ec20sip.db"))
    if args.print_password:
        from .db import hash_password, random_password
        password = random_password(10)
        db.set_setting("auth", {"username": (db.get_setting("auth", {}) or {}).get("username", "admin"),
                                "password_hash": hash_password(password)})
        print("新的管理密码: %s" % password)
        if not args.no_web and args.print_password:
            pass
    generated = getattr(db, "generated_admin_password", None)
    if generated:
        LOG.warning("首次启动，管理面板账号 admin / %s（请登录后修改）", generated)

    loop = asyncio.get_event_loop()

    if args.sim:
        modem = SimModem(loop, config, db)
    else:
        modem = Modem(loop, config, db)

    sip = SipServer(loop, config, db, modem)
    sip.sim_audio = args.sim
    notifier = Notifier(loop, db, db.all_settings)

    def on_sms(record):
        if not (db.get_setting("sms", {}) or {}).get("forward_enabled", True):
            return
        loop.create_task(notifier.forward(record))

    modem.on_sms = on_sms
    modem.on_call = sip.calls.on_modem_call
    modem.on_state = lambda: None

    ctx = AppContext(loop, config, db, sip, modem, notifier)
    ctx.sim = args.sim
    web = None
    if not args.no_web:
        web = WebServer(ctx, config.get("web.bind"), int(config.get("web.port", 8080))).start()

    await sip.start()
    await modem.start()

    stop_event = asyncio.Event()
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            pass

    LOG.info("ec20-sip-lite running (sim=%s, sip=%s:%s, web=%s)", args.sim,
             config.get("sip.bind"), config.get("sip.port"),
             "off" if web is None else config.get("web.port"))
    try:
        await stop_event.wait()
    finally:
        LOG.info("shutting down")
        if web:
            web.stop()
        await modem.stop()
        await sip.stop()
        db.close()


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())