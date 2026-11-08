"""tgsim from the command line: a fake Telegram for a bot that runs on its own.

    tgsim serve [--port 8081] [--token T] [--bot "python bot.py"] [--html out.html] [-c ACTION ...] [SCENARIO ...]

The bot points its Bot API base at http://127.0.0.1:8081 (with --bot it is started with TGSIM_API_URL set to that
address and TGSIM_TOKEN to the token, and stopped at the end). With actions or a scenario file, tgsim waits for
the bot to call, runs them, prints each step and exits 1 if one failed; without them it reads actions from the
terminal. The chats are at http://127.0.0.1:8081/ meanwhile. Actions: python -m tgsim.scenario's doc.
"""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import time

from . import render, scenario
from .model import Telegram
from .server import Server


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='tgsim', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    serve = commands.add_parser('serve', help='the Bot API over HTTP, and scenarios against it',
                                description=scenario.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    serve.add_argument('scenario', nargs='*', help='files of actions, - for stdin')
    serve.add_argument('-c', dest='actions', action='append', default=[], help='an action (repeatable)')
    serve.add_argument('--host', default='127.0.0.1')
    serve.add_argument('--port', type=int, default=8081, help='0: any free port')
    serve.add_argument('--token', help='the only token accepted (default: any)')
    serve.add_argument('--user', type=int, default=1001)
    serve.add_argument('--lang', default='en')
    serve.add_argument('--strict', action='store_true', help='refuse unknown chats, file ids and methods')
    serve.add_argument('--quiet', type=float, default=0.5, help='seconds without a call that end a step')
    serve.add_argument('--timeout', type=float, default=30.0, help='the longest a step waits for the bot')
    serve.add_argument('--bot', help='a command that starts the bot (TGSIM_API_URL and TGSIM_TOKEN are set)')
    serve.add_argument('--wait-bot', type=float, default=30.0, help='seconds to wait for the bot to call')
    serve.add_argument('--html', help='write the steps as a page here')
    args = parser.parse_args(argv)
    return _serve(args)


def _serve(args) -> int:
    tg = Telegram(token=args.token, strict=args.strict, quiet=args.quiet, settle_timeout=args.timeout)
    runner = scenario.Runner(tg, args.user, args.lang)
    server = Server(tg, args.host, args.port, runner).start()
    token = args.token or '123456:sim'
    print('tgsim: Bot API at %s/bot%s/<method>, chats at %s/' % (server.url, token, server.url), flush=True)
    bot = None
    if args.bot:
        env = dict(os.environ, TGSIM_API_URL=server.url, TGSIM_TOKEN=token)
        bot = subprocess.Popen(shlex.split(args.bot), env=env)
    try:
        lines = scenario.read_lines(args.scenario, args.actions)
        if not lines:
            return _interactive(runner, args.html, tg)
        if not _wait_for_bot(tg, args.wait_bot, bot):
            print('tgsim: the bot did not call in %g s' % args.wait_bot, file=sys.stderr)
            return 1
        ok = runner.run_all(lines)
        if args.html:
            _write(args.html, render.page(runner.steps, ' '.join(args.scenario) or 'tgsim', tg))
        return 0 if ok else 1
    finally:
        if bot is not None:
            bot.terminate()
            try:
                bot.wait(10)
            except subprocess.TimeoutExpired:
                bot.kill()
        server.stop()


def _wait_for_bot(tg: Telegram, seconds: float, bot) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if tg.polled or tg.webhook or tg.calls:
            return True
        if bot is not None and bot.poll() is not None:
            return False
        time.sleep(0.05)
    return False


def _interactive(runner, html_path, tg) -> int:
    print('tgsim: type actions (send /start, tap Menu, ...); empty line or Ctrl-D to stop', flush=True)
    while True:
        try:
            line = input('> ')
        except EOFError:
            break
        if not line.strip():
            break
        runner.run_all([line])
        if html_path:
            _write(html_path, render.page(runner.steps, 'tgsim', tg))
    return 0


def _write(path: str, text: str):
    with open(path, 'w', encoding='utf-8') as file:
        file.write(text)
    print('tgsim: wrote %s' % path)


if __name__ == '__main__':
    sys.exit(main())
