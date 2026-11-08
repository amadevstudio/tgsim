"""Layout mistakes Telegram lets through: they reach the user, so a person or a test should see them.

What Telegram itself refuses (HTML it cannot parse, a text over the limit, callback data over 64 bytes) is not
here: the model refuses it, and the bot gets the error (`Step.refusals`). Lint is what goes out and looks wrong:
a `%s` left in, an `&nbsp;` shown as text, tags shown because parse_mode was forgotten, a word glued to a
number, a button label a phone cuts, text in another language than the user's.

    for issue in check_step(step):
        print(issue)
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import List, Optional

from .model import Message

PHONE_WIDTH = 390  # px, an ordinary phone in portrait
_KEYBOARD_SIDE = 16  # px the chat leaves on the sides
_BUTTON_GAP = 6
_BUTTON_PADDING = 16

_PLACEHOLDER = re.compile(r'%(\([a-zA-Z_]+\))?[sdif]\b|\{[a-zA-Z_][a-zA-Z0-9_]*\}|\{\d+\}')
_ENTITY = re.compile(r'&(#[0-9]+|#[xX][0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]*);')
_RAW_TAG = re.compile(r'</?(b|i|u|s|a|code|pre|blockquote|strong|em|tg-spoiler)\b[^>]*>')
_DOUBLE_SPACE = re.compile(r'(?<=\S) {2,}(?=\S)')
_GLUED = re.compile(r'(?<![\w/])[a-zа-яё]{3,}\d|\d[а-яё]{2,}')
_SPACE_PUNCT = re.compile(r'(?<=\w) +[,.:;!?](?=\s|$)')
_NOT_PROSE = re.compile(r'https?://\S+|tg://\S+|[@#/]\w+|\S*[_\\]\S*|\b[a-z0-9]+(\.[a-z0-9]+)+\b', re.I)

CYRILLIC = {'ru', 'uk', 'be', 'bg', 'kk', 'ky', 'mn', 'sr', 'tg', 'mk'}
ARABIC = {'fa', 'ar', 'ur', 'ps'}
HEBREW = {'he', 'yi'}
SPACE_BEFORE_PUNCT = {'fr'}


@dataclass
class Issue:
    level: str  # error (Telegram refused it), warn (the user sees a mistake), note (worth a look)
    code: str
    text: str
    chat_id: Optional[int] = None
    message_id: Optional[int] = None

    def __str__(self):
        where = '#%s ' % self.message_id if self.message_id is not None else ''
        return '%s %s%s: %s' % (self.level.upper(), where, self.code, self.text)


def char_width(char: str) -> float:
    """About how wide a character is in Telegram's 15 px font, in px."""
    code = ord(char)
    if char in '‍️︎' or unicodedata.category(char) in ('Mn', 'Me', 'Cf'):
        return 0.0
    if code >= 0x1F000 or 0x2600 <= code <= 0x27BF or 0x2B00 <= code <= 0x2BFF:
        return 19.0  # emoji
    if 0x2E80 <= code <= 0x9FFF or 0xAC00 <= code <= 0xD7AF or 0xFF00 <= code <= 0xFFEF:
        return 15.0
    if char == ' ':
        return 4.2
    if char.isdigit():
        return 8.6
    if 'a' <= char <= 'z':
        return 7.6 if char not in 'mw' else 11.5
    if 'A' <= char <= 'Z':
        return 9.8 if char not in 'MW' else 12.5
    if 'а' <= char <= 'я' or char == 'ё':
        return 8.2 if char not in 'мшщжю' else 11.0
    if 'А' <= char <= 'Я' or char == 'Ё':
        return 10.2
    if '֐' <= char <= 'ۿ':
        return 7.6
    if unicodedata.category(char).startswith('P') or char in '|+-=*':
        return 4.8
    return 8.5


def text_width(text: str) -> float:
    return sum(char_width(char) for char in text)


def button_room(buttons_in_row: int, width: int = PHONE_WIDTH) -> float:
    """The width a label has inside one of `buttons_in_row` buttons, in px."""
    row = width - _KEYBOARD_SIDE - _BUTTON_GAP * (buttons_in_row - 1)
    return row / buttons_in_row - _BUTTON_PADDING


def _letters(text: str) -> List[str]:
    return [char for char in text if char.isalpha()]


def _script(char: str) -> str:
    name = unicodedata.name(char, '')
    for script in ('LATIN', 'CYRILLIC', 'ARABIC', 'HEBREW', 'GREEK', 'CJK', 'HANGUL', 'HIRAGANA', 'KATAKANA'):
        if name.startswith(script):
            return script
    return 'OTHER'


def _expected_script(lang: Optional[str]) -> Optional[str]:
    base = (lang or '').lower().split('-')[0].split('_')[0]
    if base in CYRILLIC:
        return 'CYRILLIC'
    if base in ARABIC:
        return 'ARABIC'
    if base in HEBREW:
        return 'HEBREW'
    if base:
        return 'LATIN'
    return None


def _wrong_language(text: str, lang: Optional[str]) -> bool:
    expected = _expected_script(lang)
    words = [word for word in re.findall(r'[^\W\d_]{3,}', _NOT_PROSE.sub(' ', text))]
    if expected is None or not words:
        return False
    scripts = {_script(char) for char in _letters(''.join(words))}
    return expected not in scripts and bool(scripts & {'LATIN', 'CYRILLIC', 'ARABIC', 'HEBREW'})


def _prose(text: str) -> str:
    return _NOT_PROSE.sub(' ', text)


def check_text(text: str, lang: Optional[str] = None) -> List[tuple]:
    """(level, code, text) for what reads wrong in a text the user sees."""
    found = []
    match = _PLACEHOLDER.search(text)
    if match:
        found.append(('warn', 'placeholder', 'a placeholder left in the text: %r' % match.group(0)))
    match = _ENTITY.search(text)
    if match:
        found.append(('warn', 'entity', 'shown as text: %r (Telegram knows only &lt; &gt; &amp; &quot;)'
                      % match.group(0)))
    prose = _prose(text)
    for line in prose.splitlines():
        match = _DOUBLE_SPACE.search(line)
        if match:
            found.append(('warn', 'double_space', 'two spaces in %r' % _around(line, match)))
            break
    match = _GLUED.search(prose)
    if match:
        found.append(('warn', 'glued', 'a word glued to a number: %r' % _around(prose, match)))
    base = (lang or '').lower().split('-')[0]
    if base not in SPACE_BEFORE_PUNCT:
        match = _SPACE_PUNCT.search(prose)
        if match:
            found.append(('warn', 'space_punct', 'a space before punctuation: %r' % _around(prose, match)))
    return found


def _around(text: str, match) -> str:
    return text[max(0, match.start() - 15):match.end() + 15].strip()


def check_message(message: Message, lang: Optional[str] = None, width: int = PHONE_WIDTH) -> List[Issue]:
    issues: List[Issue] = []

    def add(level, code, text):
        issues.append(Issue(level, code, text, message.chat_id, message.message_id))

    if not str(message.parse_mode or '') and _RAW_TAG.search(message.text):
        add('warn', 'raw_tags', 'tags shown as text: parse_mode is not set (%r)'
            % _RAW_TAG.search(message.text).group(0))
    if message.parse_mode and str(message.parse_mode).lower() not in ('html',):
        add('note', 'parse_mode', 'parse_mode %s is not modeled: the text is shown as sent' % message.parse_mode)
    for level, code, text in check_text(message.plain, lang):
        add(level, code, text)

    for r, row in enumerate(message.buttons):
        if len(row) > 3:
            add('note', 'crowded_row', 'row %d has %d buttons' % (r, len(row)))
        room = button_room(len(row), width)
        for c, button in enumerate(row):
            label = str(button.get('text', ''))
            if not label.strip():
                add('warn', 'empty_label', 'button %d,%d has no text' % (r, c))
                continue
            if text_width(label) > room:
                add('warn', 'cut_label', 'button %d,%d %r is cut on a %d px phone (%d buttons in the row)'
                    % (r, c, label, width, len(row)))
            for level, code, text in check_text(label, lang):
                if code != 'double_space':
                    add(level, code, 'button %d,%d: %s' % (r, c, text))
            if _wrong_language(label, lang):
                add('note', 'language', 'button %d,%d %r is not in %s' % (r, c, label, lang))
    return issues


def check_step(step) -> List[Issue]:
    """Lint of every message the step showed, its refusals and the bot's own failures."""
    issues: List[Issue] = []
    for call in step.refusals:
        level = 'note' if call.error.code == 429 else 'error'
        issues.append(Issue(level, 'refused', '%s: %d %s' % (call.method, call.error.code, call.error.description),
                            step.chat_id))
    for error in step.handler_errors:
        issues.append(Issue('error', 'handler', error.strip().splitlines()[-1], step.chat_id))
    if step.unanswered_tap:
        issues.append(Issue('warn', 'unanswered_tap', 'the tap was not answered (answerCallbackQuery)',
                            step.chat_id))
    if step.timed_out:
        issues.append(Issue('error', 'timeout', 'the bot was still busy when the step ended', step.chat_id))
    for toast in step.toasts:
        if toast.text:
            for level, code, text in check_text(toast.text, step.lang):
                issues.append(Issue(level, code, 'toast: ' + text, step.chat_id))
    for message in step.messages:
        issues.extend(check_message(message, step.lang))
    for message in step.other_chats:
        issues.extend(check_message(message, None))
    return issues
