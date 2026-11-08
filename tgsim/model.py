"""Telegram, kept in memory: chats, messages, buttons, files, groups and their members, payments, inline queries.

`Telegram.call(method, params, files)` is the Bot API: it answers as Telegram would, refusals included (HTML it
cannot parse, a text over 4096 characters, an edit that changes nothing, a deleted message, a blocked user), and
records every call. What users do comes in as Bot API updates (`push`): to in-process listeners, to a webhook, or
to `getUpdates`.

    tg = Telegram()
    tg.listen(lambda update: ...)               # in process; or serve it over HTTP (server.py)
    tg.call('sendMessage', {'chat_id': 1, 'text': '<b>hi</b>', 'parse_mode': 'HTML'})
"""
from __future__ import annotations

import copy
import itertools
import json
import threading
import time
import traceback
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import html

TEXT_LIMIT = 4096
CAPTION_LIMIT = 1024
RICH_LIMIT = 32768
CALLBACK_DATA_LIMIT = 64  # bytes
TOAST_LIMIT = 200
DOWNLOAD_LIMIT = 20 * 1024 * 1024  # getFile
NOT_MODIFIED = ('message is not modified: specified new message content and reply markup are exactly the same '
                'as a current content and reply markup of the message')

MEDIA = {'sendPhoto': 'photo', 'sendAudio': 'audio', 'sendDocument': 'document', 'sendVideo': 'video',
         'sendVoice': 'voice', 'sendAnimation': 'animation', 'sendVideoNote': 'video_note',
         'sendSticker': 'sticker'}
MEDIA_KINDS = set(MEDIA.values())
# parameters the Bot API takes as JSON-serialized objects
JSON_PARAMS = ('reply_markup', 'media', 'rich_message', 'prices', 'results', 'entities', 'caption_entities',
               'reply_parameters', 'link_preview_options', 'permissions', 'allowed_updates', 'message_ids',
               'options', 'suggested_tip_amounts')
BUTTON_ACTIONS = ('callback_data', 'url', 'pay', 'switch_inline_query', 'switch_inline_query_current_chat',
                  'switch_inline_query_chosen_chat', 'web_app', 'login_url', 'copy_text', 'callback_game')
ADMIN_RIGHTS = ('can_manage_chat', 'can_delete_messages', 'can_manage_video_chats', 'can_restrict_members',
                'can_promote_members', 'can_change_info', 'can_invite_users', 'can_post_stories',
                'can_edit_stories', 'can_delete_stories', 'can_post_messages', 'can_edit_messages',
                'can_pin_messages', 'can_manage_topics')
SEND_RIGHTS = ('can_send_messages', 'can_send_audios', 'can_send_documents', 'can_send_photos', 'can_send_videos',
               'can_send_video_notes', 'can_send_voice_notes', 'can_send_polls', 'can_send_other_messages',
               'can_add_web_page_previews', 'can_change_info', 'can_invite_users', 'can_pin_messages',
               'can_manage_topics')


class ApiError(Exception):
    """A refusal, as the Bot API sends it: `{"ok": false, "error_code": 400, "description": "Bad Request: ..."}`."""

    def __init__(self, code: int, description: str, parameters: Optional[dict] = None):
        super().__init__(description)
        self.code, self.description, self.parameters = code, description, parameters

    def json(self) -> dict:
        data = {'ok': False, 'error_code': self.code, 'description': self.description}
        if self.parameters:
            data['parameters'] = self.parameters
        return data


def bad_request(text: str) -> ApiError:
    return ApiError(400, 'Bad Request: ' + text)


def flood(seconds: int) -> ApiError:
    return ApiError(429, 'Too Many Requests: retry after %d' % seconds, {'retry_after': seconds})


@dataclass
class Call:
    method: str
    params: dict
    files: list
    at: float
    result: Any = None
    error: Optional[ApiError] = None


@dataclass
class File:
    file_id: str
    unique_id: str
    data: Optional[bytes] = None
    size: Optional[int] = None
    name: Optional[str] = None
    mime: Optional[str] = None
    url: Optional[str] = None  # a file the bot sent by URL: Telegram fetched it
    path: Optional[str] = None


@dataclass
class Toast:
    chat_id: int
    text: Optional[str]
    alert: bool
    url: Optional[str] = None


@dataclass
class Message:
    chat_id: int
    message_id: int
    sender: dict
    kind: str  # text, rich, photo, audio, document, video, voice, animation, sticker, invoice, poll, payment, ...
    text: str = ''  # the source as sent (Telegram HTML when parse_mode is HTML): the text or the caption
    parse_mode: Optional[str] = None
    buttons: list = field(default_factory=list)  # rows of inline keyboard buttons, as the bot sent them
    keyboard: Optional[dict] = None  # a reply keyboard that came with it
    reply_to: Optional[int] = None
    media: dict = field(default_factory=dict)  # file_id, source (an URL or "upload"), title, performer, ...
    params: dict = field(default_factory=dict)  # the call's other parameters (link preview, protect content...)
    date: int = 0
    edit_date: Optional[int] = None
    deleted: bool = False
    history: list = field(default_factory=list)  # earlier versions, oldest first (see version())

    @property
    def from_bot(self) -> bool:
        return bool(self.sender.get('is_bot'))

    @property
    def tree(self) -> html.Node:
        mode = str(self.parse_mode or '').lower()
        if mode in ('html', 'rich'):
            try:
                return html.parse(self.text, lenient=mode == 'rich')
            except html.ParseError:
                return html.text_node(self.text)
        return html.text_node(self.text)

    @property
    def plain(self) -> str:
        return html.flatten(self.tree)[0]

    @property
    def entities(self) -> list:
        return html.flatten(self.tree)[1]

    def version(self) -> dict:
        return {'text': self.text, 'parse_mode': self.parse_mode, 'buttons': copy.deepcopy(self.buttons),
                'kind': self.kind, 'media': dict(self.media), 'edit_date': self.edit_date}

    def snapshot(self) -> 'Message':
        return copy.deepcopy(self)

    def button_texts(self) -> list:
        return [button.get('text', '') for row in self.buttons for button in row]


def member(user: dict, status: str = 'member', **fields) -> dict:
    """A ChatMember object."""
    data = {'status': status, 'user': user}
    if status == 'administrator':
        data.update({'can_be_edited': False, 'is_anonymous': False})
        data.update({right: True for right in ADMIN_RIGHTS})
    elif status == 'creator':
        data['is_anonymous'] = False
    elif status == 'restricted':
        data.update({'is_member': True, 'until_date': 0})
        data.update({right: False for right in SEND_RIGHTS})
    elif status == 'kicked':
        data['until_date'] = 0
    data.update(fields)
    return data


def _json_param(value):
    if isinstance(value, (bytes, bytearray)):
        value = value.decode('utf-8')
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _flag(value) -> bool:
    return value is True or str(value).lower() in ('true', '1')


def _int(value, default=None):
    if value is None or value == '':
        return default
    return int(value)


class Telegram:
    """One bot and the people around it."""

    def __init__(self, bot: Optional[dict] = None, token: Optional[str] = None, strict: bool = False,
                 clock: Optional[Callable[[], float]] = None, quiet: Optional[float] = None,
                 settle_timeout: float = 30.0, privacy: bool = False):
        """`token`: the one the bot must use (None: any). `strict`: refuse what lenient mode lets through for a
        quick start (a chat nobody created, a file_id this Telegram never issued, a method it does not know).
        `clock`: seconds since the epoch for message dates (a fixed one makes runs reproducible). `quiet`: seconds
        without a Bot API call after which an action is over (None: 0.02 in process, 0.5 over HTTP). `privacy`:
        the bot sees only commands and replies to it in groups where it is not an admin, as Telegram's default."""
        self.bot = {'id': 100000001, 'is_bot': True, 'first_name': 'Bot', 'username': 'sim_bot',
                    'can_join_groups': True, 'can_read_all_group_messages': False, 'supports_inline_queries': True}
        self.bot.update(bot or {})
        self.token, self.strict, self.privacy = token, strict, privacy
        self.clock = clock or time.time
        self.quiet, self.settle_timeout = quiet, settle_timeout

        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.users: Dict[int, dict] = {}
        self.langs: Dict[int, str] = {}
        self.chats: Dict[int, dict] = {}
        self.blocked: set = set()
        self.members: Dict[Tuple[int, int], dict] = {}
        self.messages: Dict[Tuple[int, int], Message] = {}
        self.keyboards: Dict[int, dict] = {}  # chat id -> the reply keyboard it shows
        self.files: Dict[str, File] = {}
        self.calls: List[Call] = []
        self.toasts: List[Toast] = []
        self.queries: Dict[str, dict] = {}  # callback query id -> who tapped what, answered
        self.inline_answers: Dict[str, dict] = {}
        self.checkouts: Dict[str, dict] = {}  # pre-checkout query id -> the bot's answer
        self.payments: List[dict] = []
        self.invoice_links: Dict[str, dict] = {}
        self.handler_errors: List[str] = []  # what in-process listeners raised
        self.webhook_errors: List[str] = []
        self.answers: Dict[str, Any] = {}  # method -> a result, or a function of (params, files) returning one
        self.faults: List[dict] = []
        self.idle_checks: List[Callable[[], bool]] = []  # more conditions for "the bot is done" (its threads)

        self.updates: List[dict] = []
        self.listeners: List[Callable[[dict], Any]] = []
        self.webhook: Optional[dict] = None
        self.allowed_updates: Optional[list] = None
        self.fetched = 0  # the last update id getUpdates handed out
        self.polled = False
        self.in_flight = 0
        self.last_activity = time.monotonic()
        self.closed = False

        self._update_ids = itertools.count(1)
        self._query_ids = itertools.count(1)
        self._file_ids = itertools.count(1)
        self._message_ids: Dict[int, Any] = {}
        self._handlers = {name[4:].lower(): getattr(self, name) for name in dir(self) if name.startswith('api_')}

    # --- people and chats ---------------------------------------------------------------
    def user(self, user_id: int, lang: str = 'en', first_name: Optional[str] = None,
             username: Optional[str] = None, **fields):
        """A person who uses the bot; their actions are a `User` (users.py)."""
        from .users import User
        with self.lock:
            data = self.users.get(int(user_id)) or {'id': int(user_id), 'is_bot': False,
                                                    'first_name': first_name or 'User %d' % int(user_id)}
            if first_name:
                data['first_name'] = first_name
            if username:
                data['username'] = username
            data['language_code'] = lang
            data.update(fields)
            self.users[int(user_id)] = data
            self.langs[int(user_id)] = lang
            self.chats.setdefault(int(user_id), self._private_chat(data))
        return User(self, int(user_id))

    def _private_chat(self, user: dict) -> dict:
        chat = {'id': user['id'], 'type': 'private', 'first_name': user.get('first_name', '')}
        if user.get('username'):
            chat['username'] = user['username']
        return chat

    def group(self, chat_id: int, title: Optional[str] = None, type: str = 'supergroup',
              username: Optional[str] = None, creator: Optional[int] = None, bot: Optional[str] = 'administrator',
              **fields) -> dict:
        """A group (or a channel, `type='channel'`) with the bot in it as `bot` (None: not in it)."""
        with self.lock:
            chat = {'id': int(chat_id), 'type': type, 'title': title or '%s %d' % (type.title(), int(chat_id))}
            if username:
                chat['username'] = username.lstrip('@')
            chat.update(fields)
            self.chats[int(chat_id)] = chat
            if bot:
                self.members[(int(chat_id), self.bot['id'])] = member(self._public_bot(), bot)
            if creator is not None:
                self.members[(int(chat_id), int(creator))] = member(self._person(creator), 'creator')
            return chat

    def channel(self, chat_id: int, title: Optional[str] = None, **fields) -> dict:
        return self.group(chat_id, title, type='channel', **fields)

    def set_member(self, chat_id: int, user_id: int, status: str = 'member', **fields) -> dict:
        with self.lock:
            target = self._public_bot() if int(user_id) == self.bot['id'] else self._person(user_id)
            data = member(target, status, **fields)
            self.members[(int(chat_id), int(user_id))] = data
            return data

    def _person(self, user_id) -> dict:
        return self.users.get(int(user_id)) or {'id': int(user_id), 'is_bot': False,
                                                'first_name': 'User %d' % int(user_id)}

    def _public_bot(self) -> dict:
        return {key: self.bot[key] for key in ('id', 'is_bot', 'first_name', 'username') if key in self.bot}

    def chat(self, chat_id) -> dict:
        """A chat by id or @username; lenient mode makes one up for an id it has not seen."""
        with self.lock:
            if isinstance(chat_id, str) and chat_id.startswith('@'):
                for chat in self.chats.values():
                    if chat.get('username', '').lower() == chat_id[1:].lower():
                        return chat
                raise bad_request('chat not found')
            chat_id = int(chat_id)
            if chat_id in self.chats:
                return self.chats[chat_id]
            if self.strict:
                raise bad_request('chat not found')
            if chat_id > 0:
                return self.chats.setdefault(chat_id, self._private_chat(self._person(chat_id)))
            return self.group(chat_id, bot='administrator')

    def history(self, chat_id: int, deleted: bool = False) -> List[Message]:
        """The chat's messages in order."""
        with self.lock:
            return [m for (c, _), m in sorted(self.messages.items(), key=lambda kv: kv[0][1])
                    if c == int(chat_id) and (deleted or not m.deleted)]

    def file_bytes(self, file_path: str) -> Optional[bytes]:
        with self.lock:
            for file in self.files.values():
                if file.path == file_path:
                    return file.data
        return None

    # --- shaping the answers ------------------------------------------------------------------
    def answer(self, method: str, value: Any):
        """`method` answers with `value` (or what `value(params, files)` returns; an ApiError it returns is a
        refusal) instead of the model."""
        self.answers[method.lower()] = value

    def fail(self, method: str, error: ApiError, when: Optional[Callable[[dict], bool]] = None,
             times: Optional[int] = 1):
        """`method` refuses with `error` the next `times` calls (None: always) for which `when(params)` holds."""
        self.faults.append({'method': method.lower(), 'error': error, 'when': when, 'times': times})

    def _fault(self, method: str, params: dict) -> Optional[ApiError]:
        for fault in self.faults:
            if fault['method'] != method or fault['times'] == 0:
                continue
            if fault['when'] is not None and not fault['when'](params):
                continue
            if fault['times'] is not None:
                fault['times'] -= 1
            return fault['error']
        return None

    # --- the Bot API ------------------------------------------------------------------------
    def call(self, method: str, params: Optional[dict] = None, files: Optional[dict] = None,
             token: Optional[str] = None):
        """One Bot API request. `files`: uploads by field name, each bytes or (filename, bytes[, mime])."""
        if self.token is not None and token is not None and token != self.token:
            raise ApiError(401, 'Unauthorized')
        params = {key: (_json_param(value) if key in JSON_PARAMS else value)
                  for key, value in (params or {}).items() if value is not None}
        files = dict(files or {})
        name = method.lower()
        if name == 'getupdates':
            return self.api_getUpdates(params, files)
        with self.lock:
            record = Call(method, params, sorted(files), time.time())
            self.calls.append(record)
            self.in_flight += 1
            self.last_activity = time.monotonic()
        try:
            with self.lock:
                error = self._fault(name, params)
                if error is not None:
                    raise error
                if name in self.answers:
                    answer = self.answers[name]
                    result = answer(params, files) if callable(answer) else copy.deepcopy(answer)
                    if isinstance(result, ApiError):
                        raise result
                else:
                    handler = self._handlers.get(name)
                    result = handler(params, files) if handler else self._unknown(method, params, files)
                record.result = result
                return result
        except ApiError as e:
            record.error = e
            raise
        finally:
            with self.changed:
                self.in_flight -= 1
                self.last_activity = time.monotonic()
                self.changed.notify_all()

    def _unknown(self, method: str, params: dict, files: dict):
        if method.lower().startswith('send'):
            kind = method[4:].lower()
            text = str(params.get('text') or params.get('caption') or params.get('question') or '')
            return self._send(kind, params, text, params.get('parse_mode') if 'question' not in params else None,
                              limit=TEXT_LIMIT, empty_ok=True)
        if self.strict:
            raise ApiError(404, 'Not Found')
        return True

    # updates -------------------------------------------------------------------------------
    def api_getUpdates(self, params, files):
        offset = _int(params.get('offset'), 0)
        limit = _int(params.get('limit'), 100)
        timeout = min(float(params.get('timeout') or 0), 50.0)
        deadline = time.monotonic() + timeout
        with self.changed:
            if self.webhook:
                raise ApiError(409, "Conflict: can't use getUpdates method while webhook is active; use "
                                    "deleteWebhook to delete the webhook first")
            if params.get('allowed_updates') is not None:
                self.allowed_updates = list(params['allowed_updates']) or None
            self.polled = True
            while True:
                if offset:
                    self.updates = [u for u in self.updates if u['update_id'] >= offset]
                if self.allowed_updates:
                    self.updates = [u for u in self.updates if self._kind(u) in self.allowed_updates]
                remaining = deadline - time.monotonic()
                if self.updates or remaining <= 0 or self.closed:
                    break
                self.changed.wait(remaining)
            batch = copy.deepcopy(self.updates[:limit])
            if batch:
                self.fetched = max(self.fetched, batch[-1]['update_id'])
                self.last_activity = time.monotonic()
                self.changed.notify_all()
            return batch

    @staticmethod
    def _kind(update: dict) -> str:
        return next(key for key in update if key != 'update_id')

    def api_setWebhook(self, params, files):
        url = str(params.get('url') or '')
        with self.lock:
            if not url:
                self.webhook = None
                return True
            if not url.startswith('https://') and not url.startswith('http://'):
                raise bad_request('bad webhook: An HTTPS URL must be provided for webhook')
            self.webhook = {'url': url, 'secret_token': params.get('secret_token')}
            if params.get('allowed_updates') is not None:
                self.allowed_updates = list(params['allowed_updates']) or None
            if _flag(params.get('drop_pending_updates')):
                self.updates.clear()
            return True

    def api_deleteWebhook(self, params, files):
        with self.lock:
            self.webhook = None
            if _flag(params.get('drop_pending_updates')):
                self.updates.clear()
            return True

    def api_getWebhookInfo(self, params, files):
        return {'url': (self.webhook or {}).get('url', ''), 'has_custom_certificate': False,
                'pending_update_count': len(self.updates)}

    def listen(self, listener: Callable[[dict], Any]):
        """Updates go to `listener(update)` in the acting thread, not to getUpdates or a webhook."""
        self.listeners.append(listener)

    def push(self, kind: str, payload: dict) -> dict:
        """An update from Telegram's side: to the listeners, else the webhook, else getUpdates."""
        with self.changed:
            update = {'update_id': next(self._update_ids), kind: payload}
            listeners, hook = list(self.listeners), self.webhook
            if not listeners and not hook:
                if self.allowed_updates and kind not in self.allowed_updates:
                    return update
                self.updates.append(update)
                self.changed.notify_all()
                return update
            self.last_activity = time.monotonic()
        if listeners:
            for listener in listeners:
                try:
                    listener(copy.deepcopy(update))
                except Exception:
                    self.handler_errors.append(traceback.format_exc())
        else:
            self._post_webhook(hook, update)
        with self.lock:
            self.fetched = max(self.fetched, update['update_id'])
            self.last_activity = time.monotonic()
        return update

    def _post_webhook(self, hook: dict, update: dict):
        headers = {'Content-Type': 'application/json'}
        if hook.get('secret_token'):
            headers['X-Telegram-Bot-Api-Secret-Token'] = hook['secret_token']
        request = urllib.request.Request(hook['url'], json.dumps(update).encode('utf-8'), headers)
        try:
            with urllib.request.urlopen(request, timeout=self.settle_timeout) as response:
                body = response.read()
        except Exception as e:
            self.webhook_errors.append('%s: %s' % (type(e).__name__, e))
            return
        try:  # a webhook may answer with one method call of its own
            reply = json.loads(body) if body else None
        except ValueError:
            return
        if isinstance(reply, dict) and reply.get('method'):
            method = reply.pop('method')
            try:
                self.call(method, reply)
            except ApiError:
                pass

    def settle(self, timeout: Optional[float] = None, quiet: Optional[float] = None) -> bool:
        """Wait until the bot is done: every update handed out, no call running, none for `quiet` seconds, and
        every idle check holds. False when `timeout` ran out first."""
        if quiet is None:
            quiet = self.quiet if self.quiet is not None else (0.02 if self.listeners else 0.5)
        deadline = time.monotonic() + (self.settle_timeout if timeout is None else timeout)
        while True:
            with self.lock:
                pending = any(u['update_id'] > self.fetched for u in self.updates)
                busy = self.in_flight > 0
                still = time.monotonic() - self.last_activity
            if not pending and not busy and still >= quiet and all(check() for check in self.idle_checks):
                return True
            if time.monotonic() > deadline:
                return False
            time.sleep(min(0.01, max(quiet / 4, 0.001)))

    def close(self):
        with self.changed:
            self.closed = True
            self.changed.notify_all()

    # helpers of the send methods -------------------------------------------------------------
    def _now(self) -> int:
        return int(self.clock())

    def _next_message_id(self, chat_id: int) -> int:
        counter = self._message_ids.setdefault(chat_id, itertools.count(1))
        return next(counter)

    def _target_chat(self, chat_id) -> dict:
        if chat_id is None or chat_id == '':
            raise bad_request('chat_id is empty')
        chat = self.chat(chat_id)
        if chat['type'] == 'private':
            if chat['id'] in self.blocked:
                raise ApiError(403, 'Forbidden: bot was blocked by the user')
            if self.strict and chat['id'] not in self.users:
                raise ApiError(403, "Forbidden: bot can't initiate conversation with a user")
        else:
            status = self.members.get((chat['id'], self.bot['id']), {}).get('status')
            if status == 'kicked':
                raise ApiError(403, 'Forbidden: bot was kicked from the %s chat' % chat['type'])
            if status == 'left':
                raise ApiError(403, 'Forbidden: bot is not a member of the %s chat' % chat['type'])
        return chat

    def _check_text(self, text: str, parse_mode, limit: int, what: str = 'message', empty_ok: bool = False):
        mode = str(parse_mode or '').lower()
        if mode in ('html', 'rich'):
            try:
                root = html.parse(text, lenient=mode == 'rich')
            except html.ParseError as e:
                raise bad_request("can't parse entities: %s" % e)
            shown = html.flatten(root)[0]
        else:
            shown = text
        if not shown.strip() and not empty_ok:
            raise bad_request('message text is empty')
        if html.utf16_len(shown) > limit:
            raise bad_request('message caption is too long' if what == 'caption' else 'message is too long')

    def _markup(self, params) -> Tuple[list, Optional[dict]]:
        markup = params.get('reply_markup')
        if not markup:
            return [], None
        if not isinstance(markup, dict):
            raise bad_request("can't parse reply keyboard markup JSON object")
        rows = markup.get('inline_keyboard')
        if rows is not None:
            for row in rows:
                for button in row:
                    self._check_button(button)
            return [list(row) for row in rows], None
        if 'keyboard' in markup or 'remove_keyboard' in markup or 'force_reply' in markup:
            return [], markup
        return [], None

    @staticmethod
    def _check_button(button: dict):
        if not isinstance(button, dict) or 'text' not in button:
            raise bad_request("can't parse inline keyboard button: Field \"text\" must be of type String")
        if not any(key in button for key in BUTTON_ACTIONS):
            raise bad_request("can't parse inline keyboard button: Text buttons are unallowed in the inline "
                              "keyboard")
        data = button.get('callback_data')
        if data is not None and not 1 <= len(str(data).encode('utf-8')) <= CALLBACK_DATA_LIMIT:
            raise bad_request('BUTTON_DATA_INVALID')
        url = button.get('url')
        if url is not None and not str(url).startswith(('http://', 'https://', 'tg://')):
            raise bad_request("inline keyboard button URL '%s' is invalid: Wrong HTTP URL" % url)

    def _reply_to(self, chat_id: int, params) -> Optional[int]:
        reply = params.get('reply_parameters')
        if isinstance(reply, dict):
            message_id, allow = reply.get('message_id'), reply.get('allow_sending_without_reply')
            if reply.get('chat_id') not in (None, chat_id, str(chat_id)):
                return _int(message_id)
        else:
            message_id, allow = params.get('reply_to_message_id'), params.get('allow_sending_without_reply')
        message_id = _int(message_id)
        if message_id is None:
            return None
        target = self.messages.get((chat_id, message_id))
        if target is None or target.deleted:
            if _flag(allow):
                return None
            raise bad_request('message to be replied not found')
        return message_id

    def _new(self, chat_id: int, kind: str, sender: dict, text: str = '', parse_mode=None, buttons=None,
             keyboard=None, reply_to=None, media=None, params=None) -> Message:
        message = Message(chat_id, self._next_message_id(chat_id), sender, kind, text, parse_mode,
                          list(buttons or []), keyboard, reply_to, dict(media or {}), dict(params or {}),
                          self._now())
        self.messages[(chat_id, message.message_id)] = message
        if keyboard:
            if keyboard.get('remove_keyboard'):
                self.keyboards.pop(chat_id, None)
            elif 'keyboard' in keyboard:
                self.keyboards[chat_id] = keyboard
        self.changed.notify_all()
        return message

    def _input_file(self, value, files: dict, kind: str) -> dict:
        """What a send method's file parameter names: an upload, an URL Telegram fetches, or a file_id."""
        upload = None
        if isinstance(value, str) and value.startswith('attach://'):
            upload = files.get(value[len('attach://'):])
        elif kind in files and not value:
            upload = files[kind]
        if upload is not None:
            if isinstance(upload, (bytes, bytearray)):
                name, data, mime = None, bytes(upload), None
            else:
                name, data = upload[0], bytes(upload[1])
                mime = upload[2] if len(upload) > 2 else None
            file = self._store(data=data, name=name, mime=mime)
            return {'file_id': file.file_id, 'source': 'upload', 'file_name': name, 'file_size': len(data),
                    'mime_type': mime}
        if not value:
            raise bad_request('there is no %s in the request' % kind.replace('_', ' '))
        value = str(value)
        if value.startswith(('http://', 'https://')):
            file = self._store(url=value)
            return {'file_id': file.file_id, 'source': value}
        file = self.files.get(value)
        if file is None:
            if self.strict:
                raise bad_request('wrong file identifier/HTTP URL specified')
            file = self.files[value] = File(value, 'u' + value[-16:])
        return {'file_id': file.file_id, 'source': 'file_id', 'file_name': file.name, 'file_size': file.size,
                'mime_type': file.mime}

    def _store(self, data: Optional[bytes] = None, name=None, mime=None, url=None) -> File:
        number = next(self._file_ids)
        file = File('sim-file-%d' % number, 'sim-u%d' % number, data, len(data) if data is not None else None,
                    name, mime, url, 'files/%d%s' % (number, '_' + name if name else ''))
        self.files[file.file_id] = file
        return file

    def _send(self, kind: str, params: dict, text: str = '', parse_mode=None, media=None, limit=TEXT_LIMIT,
              what='message', empty_ok=False) -> dict:
        chat = self._target_chat(params.get('chat_id'))
        if text or not empty_ok:
            self._check_text(text, parse_mode, limit, what, empty_ok)
        buttons, keyboard = self._markup(params)
        reply_to = self._reply_to(chat['id'], params)
        others = {key: value for key, value in params.items()
                  if key not in ('chat_id', 'text', 'caption', 'parse_mode', 'reply_markup', kind)}
        message = self._new(chat['id'], kind, self._public_bot(), text, parse_mode, buttons, keyboard, reply_to,
                            media, others)
        return self.message_json(message)

    # sending ---------------------------------------------------------------------------------
    def api_getMe(self, params, files):
        return dict(self.bot)

    def api_sendMessage(self, params, files):
        return self._send('text', params, str(params.get('text', '')), params.get('parse_mode'))

    def api_sendRichMessage(self, params, files):
        """A message with a player and a long text (`rich_message`: {html, media: [...]}), up to 32768
        characters."""
        rich = params.get('rich_message') if isinstance(params.get('rich_message'), dict) else {}
        source = str(rich.get('html', ''))
        try:  # a rich message has tags of its own (<audio>): kept, not refused
            shown = html.flatten(html.parse(source, lenient=True))[0]
        except html.ParseError as e:
            raise bad_request("can't parse entities: %s" % e)
        if html.utf16_len(shown) > RICH_LIMIT:
            raise bad_request('message is too long')
        media = {}
        for item in rich.get('media') or []:
            # {type, media, ...}, or {id, media: {type, media, ...}}
            inner = item.get('media') if isinstance(item, dict) and isinstance(item.get('media'), dict) else item
            if isinstance(inner, dict) and inner.get('type') == 'audio':
                media = self._input_file(inner.get('media'), files, 'audio')
                media.update({key: inner.get(key) for key in ('title', 'performer', 'duration') if inner.get(key)})
        return self._send('rich', params, source, 'rich', media, limit=RICH_LIMIT)

    def _media(self, method: str, params: dict, files: dict) -> dict:
        kind = MEDIA[method]
        media = self._input_file(params.get(kind), files, kind)
        for key in ('title', 'performer', 'duration', 'width', 'height'):
            if params.get(key) not in (None, ''):
                media[key] = params[key]
        return self._send(kind, params, str(params.get('caption', '')), params.get('parse_mode'), media,
                          CAPTION_LIMIT, 'caption', empty_ok=True)

    def api_sendPhoto(self, params, files):
        return self._media('sendPhoto', params, files)

    def api_sendAudio(self, params, files):
        return self._media('sendAudio', params, files)

    def api_sendDocument(self, params, files):
        return self._media('sendDocument', params, files)

    def api_sendVideo(self, params, files):
        return self._media('sendVideo', params, files)

    def api_sendVoice(self, params, files):
        return self._media('sendVoice', params, files)

    def api_sendAnimation(self, params, files):
        return self._media('sendAnimation', params, files)

    def api_sendVideoNote(self, params, files):
        return self._media('sendVideoNote', params, files)

    def api_sendSticker(self, params, files):
        return self._media('sendSticker', params, files)

    def api_sendMediaGroup(self, params, files):
        items = params.get('media')
        if not isinstance(items, list) or not 2 <= len(items) <= 10:
            raise bad_request('wrong number of media in the album: 2-10 are allowed')
        chat = self._target_chat(params.get('chat_id'))
        reply_to = self._reply_to(chat['id'], params)
        sent = []
        for item in items:
            kind = item.get('type', 'photo')
            caption = str(item.get('caption', ''))
            if caption:
                self._check_text(caption, item.get('parse_mode'), CAPTION_LIMIT, 'caption', True)
            media = self._input_file(item.get('media'), files, kind)
            message = self._new(chat['id'], kind, self._public_bot(), caption, item.get('parse_mode'),
                                reply_to=reply_to, media=media, params={'media_group': True})
            sent.append(self.message_json(message))
        return sent

    def api_sendInvoice(self, params, files):
        invoice = self._invoice(params)
        text = '%s\n%s' % (invoice['title'], invoice['description'])
        chat = self._target_chat(params.get('chat_id'))
        buttons, _ = self._markup(params)
        if buttons and not buttons[0][0].get('pay'):
            raise bad_request('REPLY_MARKUP_BUY_EMPTY')
        if not buttons:
            buttons = [[{'text': 'Pay %s' % self._price(invoice), 'pay': True}]]
        reply_to = self._reply_to(chat['id'], params)
        message = self._new(chat['id'], 'invoice', self._public_bot(), text, None, buttons, None, reply_to,
                            {'invoice': invoice}, {})
        return self.message_json(message)

    def api_createInvoiceLink(self, params, files):
        invoice = self._invoice(params)
        with self.lock:
            link = 'https://t.me/$sim-invoice-%d' % (len(self.invoice_links) + 1)
            self.invoice_links[link] = invoice
        return link

    def _invoice(self, params) -> dict:
        title, description = str(params.get('title', '')), str(params.get('description', ''))
        payload, currency = str(params.get('payload', '')), str(params.get('currency', ''))
        prices = params.get('prices')
        if not 1 <= len(title) <= 32:
            raise bad_request('INVOICE_TITLE_INVALID' if title else 'title is empty')
        if not 1 <= len(description) <= 255:
            raise bad_request('INVOICE_DESCRIPTION_INVALID' if description else 'description is empty')
        if not 1 <= len(payload.encode('utf-8')) <= 128:
            raise bad_request('INVOICE_PAYLOAD_INVALID' if payload else 'payload is empty')
        if not isinstance(prices, list) or not prices:
            raise bad_request('prices are empty')
        if currency == 'XTR' and len(prices) != 1:
            raise bad_request('STARS_INVOICE_INVALID')
        total = sum(int(price.get('amount', 0)) for price in prices)
        invoice = {'title': title, 'description': description, 'payload': payload, 'currency': currency,
                   'total_amount': total, 'prices': prices, 'start_parameter': str(params.get('start_parameter',
                                                                                             ''))}
        if params.get('subscription_period'):
            invoice['subscription_period'] = int(params['subscription_period'])
        return invoice

    @staticmethod
    def _price(invoice: dict) -> str:
        if invoice['currency'] == 'XTR':
            return '⭐️ %d' % invoice['total_amount']
        return '%.2f %s' % (invoice['total_amount'] / 100, invoice['currency'])

    def api_copyMessage(self, params, files):
        source = self.messages.get((int(self.chat(params.get('from_chat_id'))['id']),
                                    _int(params.get('message_id'), 0)))
        if source is None or source.deleted:
            raise bad_request('message to copy not found')
        chat = self._target_chat(params.get('chat_id'))
        buttons, keyboard = self._markup(params)
        text = str(params['caption']) if 'caption' in params else source.text
        mode = params.get('parse_mode') if 'caption' in params else source.parse_mode
        message = self._new(chat['id'], source.kind, self._public_bot(), text, mode, buttons or source.buttons,
                            keyboard, self._reply_to(chat['id'], params), source.media, {'copy_of': source.message_id})
        return {'message_id': message.message_id}

    def api_forwardMessage(self, params, files):
        source = self.messages.get((int(self.chat(params.get('from_chat_id'))['id']),
                                    _int(params.get('message_id'), 0)))
        if source is None or source.deleted:
            raise bad_request('message to forward not found')
        chat = self._target_chat(params.get('chat_id'))
        message = self._new(chat['id'], source.kind, self._public_bot(), source.text, source.parse_mode,
                            source.buttons, None, None, source.media,
                            {'forward_from': source.sender, 'forward_of': source.message_id})
        return self.message_json(message)

    def api_sendChatAction(self, params, files):
        self._target_chat(params.get('chat_id'))
        return True

    # editing -----------------------------------------------------------------------------------
    def _edit_target(self, params) -> Optional[Message]:
        if params.get('inline_message_id'):
            return None
        chat = self.chat(params.get('chat_id'))
        message = self.messages.get((chat['id'], _int(params.get('message_id'), 0)))
        if message is None or message.deleted:
            raise bad_request('message to edit not found')
        if not message.from_bot:
            raise bad_request("message can't be edited")
        return message

    @staticmethod
    def _content(message: Message):
        return html.flatten(message.tree), message.buttons, message.kind, message.media.get('file_id')

    def _edit(self, message: Message, **changes) -> dict:
        before = self._content(message)
        version = message.version()
        for key, value in changes.items():
            setattr(message, key, value)
        if self._content(message) == before:
            for key, value in version.items():
                if key in changes:
                    setattr(message, key, value)
            raise bad_request(NOT_MODIFIED)
        message.history.append(version)
        message.edit_date = self._now()
        self.changed.notify_all()
        return self.message_json(message)

    def api_editMessageText(self, params, files):
        message = self._edit_target(params)
        text, mode = str(params.get('text', '')), params.get('parse_mode')
        self._check_text(text, mode, TEXT_LIMIT)
        if message is None:
            return True
        if message.kind not in ('text', 'rich'):
            raise bad_request('there is no text in the message to edit')
        buttons, _ = self._markup(params)
        return self._edit(message, text=text, parse_mode=mode, buttons=buttons, kind='text')

    def api_editMessageCaption(self, params, files):
        message = self._edit_target(params)
        caption, mode = str(params.get('caption', '')), params.get('parse_mode')
        if caption:
            self._check_text(caption, mode, CAPTION_LIMIT, 'caption', True)
        if message is None:
            return True
        if message.kind not in MEDIA_KINDS:
            raise bad_request('there is no caption in the message to edit')
        buttons, _ = self._markup(params)
        return self._edit(message, text=caption, parse_mode=mode, buttons=buttons)

    def api_editMessageReplyMarkup(self, params, files):
        message = self._edit_target(params)
        buttons, _ = self._markup(params)
        if message is None:
            return True
        return self._edit(message, buttons=buttons)

    def api_editMessageMedia(self, params, files):
        message = self._edit_target(params)
        item = params.get('media') if isinstance(params.get('media'), dict) else {}
        kind = item.get('type') or 'photo'
        caption, mode = str(item.get('caption', '')), item.get('parse_mode')
        if caption:
            self._check_text(caption, mode, CAPTION_LIMIT, 'caption', True)
        media = self._input_file(item.get('media'), files, kind)
        buttons, _ = self._markup(params)
        if message is None:
            return True
        if message.kind not in MEDIA_KINDS:  # Telegram turns a text message into media here
            message.params['was_text'] = True
        return self._edit(message, kind=kind, media=media, text=caption, parse_mode=mode, buttons=buttons)

    def api_deleteMessage(self, params, files):
        chat = self.chat(params.get('chat_id'))
        message = self.messages.get((chat['id'], _int(params.get('message_id'), 0)))
        if message is None or message.deleted:
            raise bad_request('message to delete not found')
        message.deleted = True
        self.changed.notify_all()
        return True

    def api_deleteMessages(self, params, files):
        chat = self.chat(params.get('chat_id'))
        for message_id in params.get('message_ids') or []:
            message = self.messages.get((chat['id'], int(message_id)))
            if message is not None:
                message.deleted = True
        return True

    # answers -------------------------------------------------------------------------------------
    def api_answerCallbackQuery(self, params, files):
        query = self.queries.get(str(params.get('callback_query_id')))
        if query is None or query['answered']:
            raise bad_request('query is too old and response timeout expired or query ID is invalid')
        text = params.get('text')
        if text is not None and len(str(text)) > TOAST_LIMIT:
            raise bad_request('MESSAGE_TOO_LONG')
        query['answered'] = True
        self.toasts.append(Toast(query['chat_id'], str(text) if text else None, _flag(params.get('show_alert')),
                                 params.get('url')))
        return True

    def api_answerInlineQuery(self, params, files):
        query_id = str(params.get('inline_query_id'))
        if query_id not in self.inline_answers:
            raise bad_request('query is too old and response timeout expired or query ID is invalid')
        results = params.get('results')
        if not isinstance(results, list):
            raise bad_request("can't parse inline query results")
        if len(results) > 50:
            raise bad_request('RESULTS_TOO_MUCH')
        for result in results:
            if not 1 <= len(str(result.get('id', '')).encode('utf-8')) <= 64:
                raise bad_request('RESULT_ID_INVALID')
            content = result.get('input_message_content') or {}
            if content.get('message_text') is not None:
                self._check_text(str(content['message_text']), content.get('parse_mode'), TEXT_LIMIT)
            for row in (result.get('reply_markup') or {}).get('inline_keyboard') or []:
                for button in row:
                    self._check_button(button)
        self.inline_answers[query_id] = {'results': results, 'answered': True}
        return True

    def api_answerPreCheckoutQuery(self, params, files):
        query_id = str(params.get('pre_checkout_query_id'))
        checkout = self.checkouts.get(query_id)
        if checkout is None or checkout.get('answered'):
            raise bad_request('QUERY_ID_INVALID')
        ok = _flag(params.get('ok'))
        if not ok and not params.get('error_message'):
            raise bad_request('error_message is empty')
        checkout.update(answered=True, ok=ok, error_message=params.get('error_message'))
        self.changed.notify_all()
        return True

    def api_refundStarPayment(self, params, files):
        charge = str(params.get('telegram_payment_charge_id'))
        for payment in self.payments:
            if payment['telegram_payment_charge_id'] == charge and payment['user_id'] == _int(params.get('user_id')):
                if payment.get('refunded'):
                    raise bad_request('CHARGE_ALREADY_REFUNDED')
                payment['refunded'] = True
                return True
        raise bad_request('CHARGE_NOT_FOUND')

    def api_getStarTransactions(self, params, files):
        transactions = []
        for payment in self.payments:
            if payment['currency'] != 'XTR':
                continue
            transactions.append({'id': payment['telegram_payment_charge_id'], 'amount': payment['total_amount'],
                                 'date': payment['date'],
                                 'source': {'type': 'user', 'user': self._person(payment['user_id'])}})
            if payment.get('refunded'):
                transactions.append({'id': payment['telegram_payment_charge_id'],
                                     'amount': payment['total_amount'], 'date': payment['date'],
                                     'receiver': {'type': 'user', 'user': self._person(payment['user_id'])}})
        offset, limit = _int(params.get('offset'), 0), _int(params.get('limit'), 100)
        return {'transactions': transactions[offset:offset + limit]}

    def api_editUserStarSubscription(self, params, files):
        charge = str(params.get('telegram_payment_charge_id'))
        for payment in self.payments:
            if payment['telegram_payment_charge_id'] == charge:
                payment['canceled'] = _flag(params.get('is_canceled'))
                return True
        raise bad_request('CHARGE_ID_INVALID')

    # chats and members ---------------------------------------------------------------------------
    def api_getChat(self, params, files):
        chat = self.chat(params.get('chat_id'))
        return dict({'accent_color_id': 0, 'max_reaction_count': 11, 'accepted_gift_types': {
            'unlimited_gifts': False, 'limited_gifts': False, 'unique_gifts': False,
            'premium_subscription': False, 'gifts_from_channels': False}}, **chat)

    def api_getChatMember(self, params, files):
        chat = self.chat(params.get('chat_id'))
        user_id = _int(params.get('user_id'))
        found = self.members.get((chat['id'], user_id))
        if found is not None:
            return copy.deepcopy(found)
        if chat['type'] == 'private' and chat['id'] == user_id:
            return member(self._person(user_id))
        if self.strict and user_id not in self.users:
            raise bad_request('user not found')
        return member(self._person(user_id), 'left')

    def api_getChatAdministrators(self, params, files):
        chat = self.chat(params.get('chat_id'))
        return [copy.deepcopy(m) for (chat_id, _), m in self.members.items()
                if chat_id == chat['id'] and m['status'] in ('administrator', 'creator')]

    def api_getChatMemberCount(self, params, files):
        chat = self.chat(params.get('chat_id'))
        return sum(1 for (chat_id, _), m in self.members.items()
                   if chat_id == chat['id'] and m['status'] in ('member', 'administrator', 'creator', 'restricted'))

    def _admin_only(self, chat: dict):
        status = self.members.get((chat['id'], self.bot['id']), {}).get('status')
        if chat['type'] != 'private' and status not in ('administrator', 'creator'):
            raise bad_request('not enough rights to restrict/unrestrict chat member')

    def api_restrictChatMember(self, params, files):
        chat = self.chat(params.get('chat_id'))
        self._admin_only(chat)
        user_id = _int(params.get('user_id'))
        permissions = params.get('permissions') if isinstance(params.get('permissions'), dict) else {}
        if all(permissions.get(right) for right in ('can_send_messages',)) and \
                all(permissions.get(right, True) for right in SEND_RIGHTS):
            self.set_member(chat['id'], user_id, 'member')
        else:
            fields = {right: bool(permissions.get(right, False)) for right in SEND_RIGHTS}
            self.set_member(chat['id'], user_id, 'restricted', until_date=_int(params.get('until_date'), 0),
                            **fields)
        return True

    def api_banChatMember(self, params, files):
        chat = self.chat(params.get('chat_id'))
        self._admin_only(chat)
        self.set_member(chat['id'], _int(params.get('user_id')), 'kicked',
                        until_date=_int(params.get('until_date'), 0))
        return True

    def api_unbanChatMember(self, params, files):
        chat = self.chat(params.get('chat_id'))
        key = (chat['id'], _int(params.get('user_id')))
        current = self.members.get(key, {}).get('status')
        if _flag(params.get('only_if_banned')) and current != 'kicked':
            return True
        self.set_member(*key, status='left')
        return True

    def api_promoteChatMember(self, params, files):
        chat = self.chat(params.get('chat_id'))
        user_id = _int(params.get('user_id'))
        rights = {right: _flag(params.get(right)) for right in ADMIN_RIGHTS if right in params}
        if rights and not any(rights.values()):
            self.set_member(chat['id'], user_id, 'member')
        else:
            self.set_member(chat['id'], user_id, 'administrator', **rights)
        return True

    def api_leaveChat(self, params, files):
        chat = self.chat(params.get('chat_id'))
        self.set_member(chat['id'], self.bot['id'], 'left')
        return True

    def api_exportChatInviteLink(self, params, files):
        chat = self.chat(params.get('chat_id'))
        return 'https://t.me/+sim%d' % abs(chat['id'])

    def api_createChatInviteLink(self, params, files):
        chat = self.chat(params.get('chat_id'))
        return {'invite_link': 'https://t.me/+sim%d_%d' % (abs(chat['id']), len(self.calls)),
                'creator': self._public_bot(), 'creates_join_request': _flag(params.get('creates_join_request')),
                'is_primary': False, 'is_revoked': False, 'name': params.get('name')}

    # files -----------------------------------------------------------------------------------------
    def api_getFile(self, params, files):
        file = self.files.get(str(params.get('file_id')))
        if file is None:
            raise bad_request('invalid file_id')
        if file.size is not None and file.size > DOWNLOAD_LIMIT:
            raise bad_request('file is too big')
        return {'file_id': file.file_id, 'file_unique_id': file.unique_id, 'file_size': file.size,
                'file_path': file.path}

    # --- Bot API objects ----------------------------------------------------------------------------
    def chat_json(self, chat_id: int) -> dict:
        return dict(self.chat(chat_id))

    def message_json(self, message: Message, with_reply: bool = True) -> dict:
        data = {'message_id': message.message_id, 'date': message.date, 'chat': self.chat_json(message.chat_id)}
        if message.sender:
            data['from'] = dict(message.sender)
        if message.edit_date:
            data['edit_date'] = message.edit_date
        try:
            text, entities = html.flatten(message.tree)
        except html.ParseError:
            text, entities = message.text, []
        if message.kind in ('text', 'rich'):
            data['text'] = text
            if entities:
                data['entities'] = entities
            if text.startswith('/') and not message.from_bot:
                data['entities'] = [{'type': 'bot_command', 'offset': 0, 'length': len(text.split()[0])}]
        elif text and message.kind != 'invoice':
            data['caption'] = text
            if entities:
                data['caption_entities'] = entities
        media = message.media
        body = {'file_id': media.get('file_id'), 'file_unique_id': self._unique(media.get('file_id'))}
        if media.get('file_size'):
            body['file_size'] = media['file_size']
        if message.kind == 'photo':
            data['photo'] = [dict(body, width=90, height=90), dict(body, width=1280, height=1280)]
        elif message.kind in ('audio', 'voice', 'video', 'animation', 'video_note'):
            body['duration'] = int(media.get('duration') or 0)
            for key in ('title', 'performer', 'file_name', 'mime_type'):
                if media.get(key):
                    body[key] = media[key]
            if message.kind in ('video', 'animation'):
                body.update(width=int(media.get('width') or 1), height=int(media.get('height') or 1))
            if message.kind == 'video_note':
                body['length'] = 240
            data[message.kind] = body
        elif message.kind == 'document':
            for key in ('file_name', 'mime_type'):
                if media.get(key):
                    body[key] = media[key]
            data['document'] = body
        elif message.kind == 'sticker':
            data['sticker'] = dict(body, type='regular', width=512, height=512, is_animated=False,
                                   is_video=False)
        elif message.kind == 'invoice':
            invoice = media['invoice']
            data['invoice'] = {key: invoice[key] for key in ('title', 'description', 'start_parameter',
                                                             'currency', 'total_amount')}
        elif message.kind == 'payment':
            data['successful_payment'] = dict(media['payment'])
        elif message.kind == 'rich' and media.get('file_id'):
            data['rich_message'] = {'blocks': [{'type': 'audio', 'audio': dict(body)}]}
        if message.buttons:
            data['reply_markup'] = {'inline_keyboard': message.buttons}
        if with_reply and message.reply_to:
            target = self.messages.get((message.chat_id, message.reply_to))
            if target is not None and not target.deleted:
                data['reply_to_message'] = self.message_json(target, with_reply=False)
        return data

    def _unique(self, file_id) -> Optional[str]:
        file = self.files.get(file_id) if file_id else None
        return file.unique_id if file else file_id
