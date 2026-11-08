"""Scenarios: one action per line, as a user would act. A bot's own harness adds its actions (`runner.actions`).

    user ID [LANG] [USERNAME]  act as this user from now on (default 1001 en)
    send TEXT                  a message to the bot (\\n for a new line)
    start [PAYLOAD]            /start, with a deep-link payload
    tap TEXT | tap ROW,COL     a button of the bot's latest message that has it (exact text, else a part)
    data TEXT                  a tap with this callback data on the latest message
    press TEXT                 a button of the reply keyboard
    file PATH [MIME]           a document
    photo PATH                 a photo
    voice PATH SECONDS         a voice message
    pay [LINK]                 the latest invoice in the chat, or an invoice link
    inline QUERY               an inline query
    block | unblock            the bot, from this user
    group ID [TITLE]           a group with this user as its creator and the bot an admin
    channel ID [TITLE]         a channel, the same
    member ID STATUS           this user's status in a group (member, administrator, restricted, kicked, left)
    in ID TEXT                 says TEXT in a group
    reply ID MESSAGE TEXT      says TEXT in a group, as a reply to message MESSAGE there
    join ID | leave ID         this user joins or leaves a group
    addbot ID [STATUS]         this user adds the bot to a group (or changes its status)
    expect TEXT                the last step showed TEXT (in a message, a button or a toast), else fail
    screen [N]                 the last N messages of the chat
    wait SECONDS               nothing for a while
    # ...                      a comment
"""
from __future__ import annotations

import json
import os
import shlex
import time
from typing import Callable, Dict, List, Optional

from .model import Telegram
from .users import Step, User


class ScenarioError(AssertionError):
    pass


class Runner:
    def __init__(self, tg: Telegram, user_id: int = 1001, lang: str = 'en', base_dir: str = '.'):
        self.tg, self.base_dir = tg, base_dir
        self.user: User = tg.user(user_id, lang)
        self.steps: List[Step] = []
        self.last: Optional[Step] = None
        self.actions: Dict[str, Callable[['Runner', str], object]] = {}
        for name in dir(self):
            if name.startswith('do_'):
                self.actions[name[3:]] = getattr(type(self), name)

    def action(self, name: str):
        """Registers `fn(runner, rest)` as action `name`; it returns a Step, a text to print, or None."""
        def register(fn):
            self.actions[name] = fn
            return fn
        return register

    def run(self, line: str):
        line = line.strip()
        if not line or line.startswith('#'):
            return None
        verb, _, rest = line.partition(' ')
        handler = self.actions.get(verb)
        if handler is None:
            raise ScenarioError('unknown action %r; known: %s' % (verb, ', '.join(sorted(self.actions))))
        result = handler(self, rest.strip())
        if isinstance(result, Step):
            self.steps.append(result)
            self.last = result
        return result

    def run_all(self, lines, out=print, stop_on_failure: bool = False) -> bool:
        """Runs every line, printing what happened; False if an action failed or a step did."""
        ok = True
        for line in lines:
            try:
                result = self.run(line)
            except Exception as e:
                out('!!! %s: %s: %s' % (line.strip(), type(e).__name__, e))
                ok = False
                if stop_on_failure:
                    return False
                continue
            if result is not None:
                out(str(result))
            if isinstance(result, Step) and result.failed:
                ok = False
                if stop_on_failure:
                    return False
        return ok

    def _path(self, path: str) -> str:
        return path if os.path.isabs(path) else os.path.join(self.base_dir, path)

    # --- actions --------------------------------------------------------------------------
    def do_user(self, rest):
        parts = rest.split()
        if not parts:
            raise ScenarioError('user ID [LANG] [USERNAME]')
        self.user = self.tg.user(int(parts[0]), parts[1] if len(parts) > 1 else self.user.lang,
                                 username=parts[2] if len(parts) > 2 else None)
        return '>>> now user %d (%s)' % (self.user.id, self.user.lang)

    def do_send(self, rest):
        return self.user.send(rest.replace('\\n', '\n'))

    def do_start(self, rest):
        return self.user.start(rest or None)

    def do_tap(self, rest):
        if ',' in rest and all(part.strip().isdigit() for part in rest.split(',', 1)):
            row, column = (int(part) for part in rest.split(',', 1))
            return self.user.tap((row, column))
        return self.user.tap(rest)

    def do_data(self, rest):
        return self.user.tap_data(rest)

    def do_press(self, rest):
        return self.user.press(rest)

    def do_file(self, rest):
        path, _, mime = rest.partition(' ')
        with open(self._path(path), 'rb') as file:
            data = file.read()
        return self.user.send_document(os.path.basename(path), data, mime.strip() or 'application/octet-stream')

    def do_photo(self, rest):
        with open(self._path(rest), 'rb') as file:
            return self.user.send_photo(file.read())

    def do_voice(self, rest):
        path, _, seconds = rest.rpartition(' ')
        with open(self._path(path), 'rb') as file:
            return self.user.send_voice(file.read(), int(seconds))

    def do_pay(self, rest):
        return self.user.pay(rest or None)

    def do_inline(self, rest):
        return self.user.inline(rest)

    def do_block(self, rest):
        return self.user.block()

    def do_unblock(self, rest):
        return self.user.unblock()

    def do_group(self, rest):
        chat_id, _, title = rest.partition(' ')
        self.tg.group(int(chat_id), title or None, creator=self.user.id)
        return '>>> group %s, %d its creator, the bot an admin' % (chat_id, self.user.id)

    def do_channel(self, rest):
        chat_id, _, title = rest.partition(' ')
        self.tg.channel(int(chat_id), title or None, creator=self.user.id)
        return '>>> channel %s, %d its creator, the bot an admin' % (chat_id, self.user.id)

    def do_member(self, rest):
        chat_id, status = rest.split()
        self.tg.set_member(int(chat_id), self.user.id, status)
        return '>>> %d is %s in %s' % (self.user.id, status, chat_id)

    def do_in(self, rest):
        chat_id, _, text = rest.partition(' ')
        return self.user.say(int(chat_id), text.replace('\\n', '\n'))

    def do_reply(self, rest):
        parts = shlex.split(rest)
        if len(parts) < 3:
            raise ScenarioError('reply CHAT MESSAGE TEXT')
        return self.user.say(int(parts[0]), ' '.join(parts[2:]), reply_to=int(parts[1]))

    def do_join(self, rest):
        return self.user.join(int(rest))

    def do_leave(self, rest):
        return self.user.leave(int(rest))

    def do_addbot(self, rest):
        chat_id, _, status = rest.partition(' ')
        return self.user.add_bot(int(chat_id), status.strip() or 'administrator')

    def do_expect(self, rest):
        step = self.last
        if step is None:
            raise ScenarioError('expect: no step yet')
        seen = list(step.texts) + [label for m in step.messages for label in m.button_texts()]
        seen += [toast.text for toast in step.toasts if toast.text]
        if not any(rest.lower() in text.lower() for text in seen):
            raise ScenarioError('expected %r in the last step; it showed: %s' % (rest, json.dumps(
                [text[:80] for text in seen], ensure_ascii=False)))
        return '    ok: %r' % rest

    def do_screen(self, rest):
        return self.user.screen(int(rest or 3))

    def do_wait(self, rest):
        time.sleep(float(rest or 1))
        return None


def read_lines(paths: List[str], actions: List[str]) -> List[str]:
    """Scenario lines from files (or - for stdin) and -c actions, in that order."""
    import sys
    lines = list(actions)
    for path in paths:
        if path == '-':
            lines += sys.stdin.read().splitlines()
        else:
            with open(path, encoding='utf-8') as file:
                lines += file.read().splitlines()
    return lines
