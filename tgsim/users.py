"""A person on Telegram's side: what they do becomes Bot API updates, what the bot did about it is a Step.

    tg = Telegram()
    alice = tg.user(1001, lang='ru')
    step = alice.send('/start')          # waits until the bot is done, then says what changed
    step = alice.tap('Settings')         # a button of the bot's latest message that has it
    step = alice.tap((0, 1))             # by row and column
    step = alice.pay()                   # the latest invoice: pre-checkout, then the payment
    print(step)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple, Union

from .model import Message, Telegram, Toast, member


@dataclass
class Step:
    """What one action changed: the bot's new and edited messages in the user's chat (as they were right after
    it), the deleted ones, toasts, messages to other chats, every Bot API call and the refusals among them."""
    action: str
    chat_id: int
    lang: Optional[str] = None
    new: List[Message] = field(default_factory=list)
    edited: List[Message] = field(default_factory=list)
    deleted: List[int] = field(default_factory=list)
    toasts: List[Toast] = field(default_factory=list)
    other_chats: List[Message] = field(default_factory=list)
    calls: list = field(default_factory=list)
    handler_errors: List[str] = field(default_factory=list)
    inline_results: Optional[list] = None
    unanswered_tap: bool = False
    notes: List[str] = field(default_factory=list)
    timed_out: bool = False
    seconds: float = 0.0
    sent: Optional[Message] = None  # the user's own message, when the action was one

    @property
    def refusals(self) -> list:
        """Calls Telegram refused: the bot got an error back."""
        return [call for call in self.calls if call.error is not None]

    @property
    def messages(self) -> List[Message]:
        return self.new + self.edited

    @property
    def texts(self) -> List[str]:
        return [message.plain for message in self.messages]

    @property
    def failed(self) -> bool:
        return bool(self.handler_errors or self.timed_out or
                    any(call.error.code != 429 for call in self.refusals))

    def __str__(self):
        from .render import text_step
        return text_step(self)


class User:
    """A user's handle; `tg.user(id)` makes one."""

    def __init__(self, tg: Telegram, user_id: int):
        self.tg, self.id = tg, int(user_id)

    @property
    def data(self) -> dict:
        return self.tg.users[self.id]

    @property
    def lang(self) -> str:
        return self.tg.langs.get(self.id, 'en')

    # --- what the user sees --------------------------------------------------------------
    def chat(self) -> List[Message]:
        return self.tg.history(self.id)

    def last(self, with_buttons: bool = False) -> Optional[Message]:
        for message in reversed(self.chat()):
            if message.from_bot and (message.buttons or not with_buttons):
                return message
        return None

    def screen(self, count: int = 3) -> str:
        from .render import text_message
        return '\n'.join(text_message(m, 'bot' if m.from_bot else 'you') for m in self.chat()[-count:])

    # --- private chat --------------------------------------------------------------------------
    def send(self, text: str, reply_to: Optional[int] = None) -> Step:
        """A text message to the bot (a command too). A user who blocked the bot unblocks it first."""
        return self._message('send %r' % text, self.id, 'text', text, reply_to=reply_to)

    def start(self, payload: Optional[str] = None) -> Step:
        return self.send('/start' + (' ' + payload if payload else ''))

    def press(self, text: str) -> Step:
        """A button of the reply keyboard the chat shows: its text is sent."""
        keyboard = self.tg.keyboards.get(self.id) or {}
        labels = [str(b.get('text', b) if isinstance(b, dict) else b) for row in keyboard.get('keyboard', [])
                  for b in row]
        chosen = next((label for label in labels if label == text), None) or next(
            (label for label in labels if text.lower() in label.lower()), None)
        if chosen is None:
            raise LookupError('no keyboard button %r; the keyboard: %s' % (text, labels))
        return self._message('press %r' % chosen, self.id, 'text', chosen)

    def send_document(self, name: str, data: bytes, mime: str = 'application/octet-stream',
                      caption: str = '', chat_id: Optional[int] = None) -> Step:
        file = self.tg._store(data=data, name=name, mime=mime)
        media = {'file_id': file.file_id, 'file_name': name, 'mime_type': mime, 'file_size': len(data)}
        return self._message('file %s (%d bytes)' % (name, len(data)), chat_id or self.id, 'document', caption,
                             media=media)

    def send_photo(self, data: bytes, caption: str = '', chat_id: Optional[int] = None) -> Step:
        file = self.tg._store(data=data, name='photo.jpg', mime='image/jpeg')
        return self._message('photo (%d bytes)' % len(data), chat_id or self.id, 'photo', caption,
                             media={'file_id': file.file_id, 'file_size': len(data)})

    def send_voice(self, data: bytes, duration: int, chat_id: Optional[int] = None) -> Step:
        file = self.tg._store(data=data, name='voice.ogg', mime='audio/ogg')
        media = {'file_id': file.file_id, 'duration': duration, 'mime_type': 'audio/ogg', 'file_size': len(data)}
        return self._message('voice (%d s)' % duration, chat_id or self.id, 'voice', '', media=media)

    def send_audio(self, data: bytes, duration: int, title: str = '', performer: str = '',
                   name: str = 'audio.mp3', chat_id: Optional[int] = None) -> Step:
        file = self.tg._store(data=data, name=name, mime='audio/mpeg')
        media = {'file_id': file.file_id, 'duration': duration, 'title': title, 'performer': performer,
                 'file_name': name, 'mime_type': 'audio/mpeg', 'file_size': len(data)}
        return self._message('audio %s' % name, chat_id or self.id, 'audio', '', media=media)

    # --- buttons -----------------------------------------------------------------------------
    def tap(self, button: Union[str, Tuple[int, int]], message: Optional[Message] = None) -> Step:
        """A tap on an inline button: its text (exact, else a part of it) or (row, column), on `message` or on
        the bot's latest message in this chat that has it. A URL button only says where it leads; a pay button
        pays."""
        target, chosen = self.find_button(button, message)
        if chosen.get('pay'):
            return self.pay(target)
        if chosen.get('url'):
            step = Step('tap %r on #%d' % (chosen.get('text'), target.message_id), self.id, self.lang)
            step.notes.append('a link: %s' % chosen['url'])
            return step
        if 'callback_data' not in chosen:
            raise ValueError('%r is not a callback button: %r' % (button, chosen))
        return self.tap_data(str(chosen['callback_data']), target,
                             label='tap %r on #%d' % (chosen.get('text'), target.message_id))

    def tap_data(self, data: str, message: Optional[Message] = None, label: Optional[str] = None) -> Step:
        """A tap with this callback data (an old button, a crafted one) on `message`, else the latest one."""
        tg = self.tg
        target = message or self.last()
        with tg.lock:
            query_id = str(next(tg._query_ids))
            tg.queries[query_id] = {'answered': False, 'chat_id': target.chat_id if target else self.id,
                                    'user_id': self.id, 'data': data}
            payload = {'id': query_id, 'from': dict(self.data), 'chat_instance': str(self.id), 'data': data}
            if target is not None:
                payload['message'] = tg.message_json(target)
        step = self._observe(label or 'tap data %s' % data, lambda: tg.push('callback_query', payload))
        step.unanswered_tap = not tg.queries[query_id]['answered']
        if step.unanswered_tap:
            step.notes.append('the tap was not answered: the button keeps spinning (answerCallbackQuery)')
        return step

    def find_button(self, button, message: Optional[Message] = None) -> Tuple[Message, dict]:
        candidates = [message] if message is not None else [m for m in reversed(self.tg.history(self.id))
                                                            if m.from_bot and m.buttons]
        for candidate in candidates:
            if isinstance(button, tuple):
                row, column = button
                try:
                    return candidate, candidate.buttons[row][column]
                except IndexError:
                    continue
            for row in candidate.buttons:
                for chosen in row:
                    if chosen.get('text') == button:
                        return candidate, chosen
            for row in candidate.buttons:
                for chosen in row:
                    if str(button).lower() in str(chosen.get('text', '')).lower():
                        return candidate, chosen
        raise LookupError('no button %r in the chat; the latest: %s' % (
            button, candidates[0].button_texts() if candidates else []))

    # --- payments ------------------------------------------------------------------------------
    def pay(self, invoice: Union[Message, str, None] = None, wait: float = 10.0) -> Step:
        """Pays an invoice: a message, an invoice link, or the latest invoice in the chat. The bot gets a
        pre-checkout query; if it says yes within `wait` seconds, the payment comes as a message."""
        tg = self.tg
        if isinstance(invoice, str):
            data, label = tg.invoice_links.get(invoice), 'pay %s' % invoice
            if data is None:
                raise LookupError('no invoice link %s' % invoice)
        else:
            message = invoice or next((m for m in reversed(self.chat()) if m.kind == 'invoice'), None)
            if message is None:
                raise LookupError('no invoice in the chat')
            data, label = message.media['invoice'], 'pay invoice #%d' % message.message_id

        def work():
            with tg.lock:
                query_id = 'pcq-%d' % next(tg._query_ids)
                tg.checkouts[query_id] = {'answered': False}
            tg.push('pre_checkout_query', {'id': query_id, 'from': dict(self.data), 'currency': data['currency'],
                                           'total_amount': data['total_amount'],
                                           'invoice_payload': data['payload']})
            deadline = time.monotonic() + wait
            while not tg.checkouts[query_id]['answered'] and time.monotonic() < deadline:
                time.sleep(0.01)
            checkout = tg.checkouts[query_id]
            if not checkout['answered']:
                notes.append('the bot did not answer the pre-checkout in %g s: Telegram cancels the payment' % wait)
                return
            if not checkout['ok']:
                notes.append('the bot refused the payment: %s' % checkout.get('error_message'))
                return
            tg.settle()
            with tg.lock:
                charge = 'sim-charge-%d' % (len(tg.payments) + 1)
                payment = {'currency': data['currency'], 'total_amount': data['total_amount'],
                           'invoice_payload': data['payload'], 'telegram_payment_charge_id': charge,
                           'provider_payment_charge_id': charge}
                if data.get('subscription_period'):
                    payment.update(subscription_expiration_date=tg._now() + data['subscription_period'],
                                   is_recurring=True, is_first_recurring=True)
                tg.payments.append(dict(payment, user_id=self.id, date=tg._now()))
                message = tg._new(self.id, 'payment', dict(self.data), media={'payment': payment})
                payload = tg.message_json(message)
            tg.push('message', payload)

        notes: List[str] = []
        step = self._observe(label, work)
        step.notes.extend(notes)
        return step

    def renew(self, charge_id: str) -> Step:
        """The next charge of a Stars subscription paid with `charge_id`."""
        tg = self.tg
        first = next(p for p in tg.payments if p['telegram_payment_charge_id'] == charge_id)

        def work():
            with tg.lock:
                charge = 'sim-charge-%d' % (len(tg.payments) + 1)
                payment = {key: first[key] for key in ('currency', 'total_amount', 'invoice_payload')}
                payment.update(telegram_payment_charge_id=charge, provider_payment_charge_id=charge,
                               is_recurring=True, subscription_expiration_date=tg._now() + 30 * 86400)
                tg.payments.append(dict(payment, user_id=self.id, date=tg._now()))
                message = tg._new(self.id, 'payment', dict(self.data), media={'payment': payment})
                payload = tg.message_json(message)
            tg.push('message', payload)
        return self._observe('renew %s' % charge_id, work)

    # --- inline mode ------------------------------------------------------------------------------
    def inline(self, query: str) -> Step:
        """Types `@bot query` in some chat: the bot's results are `step.inline_results`."""
        tg = self.tg
        with tg.lock:
            query_id = 'iq-%d' % next(tg._query_ids)
            tg.inline_answers[query_id] = {'results': None, 'answered': False}
        step = self._observe('inline %r' % query, lambda: tg.push('inline_query', {
            'id': query_id, 'from': dict(self.data), 'query': query, 'offset': '', 'chat_type': 'sender'}))
        step.inline_results = tg.inline_answers[query_id]['results']
        if step.inline_results is None:
            step.notes.append('the inline query was not answered')
        return step

    # --- the bot as a contact ------------------------------------------------------------------------
    def block(self) -> Step:
        tg = self.tg
        tg.blocked.add(self.id)
        return self._observe('block the bot', lambda: tg.push('my_chat_member', self._bot_status('member',
                                                                                                'kicked')))

    def unblock(self) -> Step:
        tg = self.tg
        tg.blocked.discard(self.id)
        return self._observe('unblock the bot', lambda: tg.push('my_chat_member', self._bot_status('kicked',
                                                                                                  'member')))

    def _bot_status(self, old: str, new: str, chat_id: Optional[int] = None) -> dict:
        tg = self.tg
        return {'chat': tg.chat_json(chat_id or self.id), 'from': dict(self.data), 'date': tg._now(),
                'old_chat_member': member(tg._public_bot(), old), 'new_chat_member': member(tg._public_bot(), new)}

    # --- groups -------------------------------------------------------------------------------------
    def say(self, chat_id: int, text: str, reply_to: Optional[int] = None) -> Step:
        """A message in a group; the user becomes its member if they were not."""
        tg = self.tg
        if (int(chat_id), self.id) not in tg.members:
            tg.set_member(chat_id, self.id, 'member')
        status = tg.members[(int(chat_id), self.id)]['status']
        if status in ('kicked', 'left') or (status == 'restricted' and not tg.members[(int(chat_id), self.id)].get(
                'can_send_messages')):
            step = Step('say in %s %r' % (chat_id, text), int(chat_id), self.lang)
            step.notes.append('Telegram does not let this user write there (%s)' % status)
            return step
        return self._message('say in %s %r' % (chat_id, text), int(chat_id), 'text', text, reply_to=reply_to)

    def join(self, chat_id: int) -> Step:
        tg = self.tg
        old = tg.members.get((int(chat_id), self.id), {}).get('status', 'left')
        new = tg.set_member(chat_id, self.id, 'member')

        def work():
            tg.push('chat_member', {'chat': tg.chat_json(chat_id), 'from': dict(self.data), 'date': tg._now(),
                                    'old_chat_member': member(dict(self.data), old), 'new_chat_member': new})
        return self._observe('join %s' % chat_id, work, chat_id=int(chat_id))

    def leave(self, chat_id: int) -> Step:
        tg = self.tg
        old = tg.members.get((int(chat_id), self.id), {}).get('status', 'member')
        new = tg.set_member(chat_id, self.id, 'left')

        def work():
            tg.push('chat_member', {'chat': tg.chat_json(chat_id), 'from': dict(self.data), 'date': tg._now(),
                                    'old_chat_member': member(dict(self.data), old), 'new_chat_member': new})
        return self._observe('leave %s' % chat_id, work, chat_id=int(chat_id))

    def add_bot(self, chat_id: int, status: str = 'administrator') -> Step:
        """Adds the bot to a group (or makes it an admin there): the bot gets my_chat_member."""
        tg = self.tg
        old = tg.members.get((int(chat_id), tg.bot['id']), {}).get('status', 'left')
        tg.set_member(chat_id, tg.bot['id'], status)
        return self._observe('bot is %s in %s' % (status, chat_id),
                             lambda: tg.push('my_chat_member', self._bot_status(old, status, int(chat_id))),
                             chat_id=int(chat_id))

    # --- plumbing ----------------------------------------------------------------------------------
    def _message(self, label: str, chat_id: int, kind: str, text: str, reply_to: Optional[int] = None,
                 media: Optional[dict] = None) -> Step:
        tg = self.tg
        if chat_id == self.id and self.id in tg.blocked:
            self.unblock()
        holder: List[Message] = []

        def work():
            with tg.lock:
                tg.chat(chat_id)
                message = tg._new(chat_id, kind, dict(self.data), text, None, reply_to=reply_to, media=media)
                holder.append(message)
                payload = tg.message_json(message)
            if self._delivered(chat_id, message):
                tg.push('message', payload)
        step = self._observe(label, work, chat_id=chat_id)
        step.sent = holder[0] if holder else None
        return step

    def _delivered(self, chat_id: int, message: Message) -> bool:
        """Privacy mode: in a group where it is not an admin the bot sees commands and replies to itself."""
        tg = self.tg
        if not tg.privacy or chat_id == self.id:
            return True
        status = tg.members.get((chat_id, tg.bot['id']), {}).get('status')
        if status in ('administrator', 'creator'):
            return True
        if message.text.startswith('/'):
            return True
        target = tg.messages.get((chat_id, message.reply_to)) if message.reply_to else None
        return bool(target and target.from_bot)

    def _observe(self, label: str, work: Callable[[], None], chat_id: Optional[int] = None) -> Step:
        return observe(self.tg, label, chat_id or self.id, work, self.lang)


def observe(tg: Telegram, label: str, chat_id: int, work: Callable[[], None], lang: Optional[str] = None) -> Step:
    """Runs `work`, waits for the bot to settle, and says what changed in `chat_id` and elsewhere."""
    started = time.monotonic()
    with tg.lock:
        before = {key: (m.version(), m.deleted) for key, m in tg.messages.items()}
        calls, toasts, errors = len(tg.calls), len(tg.toasts), len(tg.handler_errors)
    work()
    settled = tg.settle()
    step = Step(label, chat_id, lang, timed_out=not settled, seconds=time.monotonic() - started)
    with tg.lock:
        for key, message in sorted(tg.messages.items(), key=lambda kv: kv[0][1]):
            old = before.get(key)
            if old is None:
                if not message.from_bot or message.deleted:
                    continue
                (step.new if message.chat_id == chat_id else step.other_chats).append(message.snapshot())
            elif message.deleted and not old[1]:
                if message.chat_id == chat_id:
                    step.deleted.append(message.message_id)
            elif message.version() != old[0] and not message.deleted:
                (step.edited if message.chat_id == chat_id else step.other_chats).append(message.snapshot())
        step.calls = tg.calls[calls:]
        step.toasts = tg.toasts[toasts:]
        step.handler_errors = tg.handler_errors[errors:]
    if not settled:
        step.notes.append('the bot was still busy after %g s' % tg.settle_timeout)
    return step
