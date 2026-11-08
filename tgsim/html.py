"""Telegram's HTML, read the way the Bot API reads `parse_mode=HTML`.

The Bot API knows a few tags and four named entities (`&lt; &gt; &amp; &quot;`) plus numeric ones. Any other
`&name;` stays in the text as it is (a pasted `&nbsp;` shows as "&nbsp;"), and any `<` that does not open a known
tag, an end tag that does not match, or a tag left open refuses the whole message with "can't parse entities".

    root = parse('<b>Hi</b> there')       # a tree of Node and str
    text, entities = flatten(root)         # 'Hi there', [{'type': 'bold', 'offset': 0, 'length': 2}]
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Union

# tag -> entity type
TAGS = {
    'b': 'bold', 'strong': 'bold',
    'i': 'italic', 'em': 'italic',
    'u': 'underline', 'ins': 'underline',
    's': 'strikethrough', 'strike': 'strikethrough', 'del': 'strikethrough',
    'a': 'text_link',
    'code': 'code',
    'pre': 'pre',
    'blockquote': 'blockquote',
    'tg-spoiler': 'spoiler', 'span': 'spoiler',
    'tg-emoji': 'custom_emoji',
}
NAMED = {'lt': '<', 'gt': '>', 'amp': '&', 'quot': '"'}

_ENTITY = re.compile(r'&(#[0-9]+|#[xX][0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]*);')
_NAME = re.compile(r'[a-zA-Z][a-zA-Z0-9-]*')
_ATTRIBUTE = re.compile(r'[a-zA-Z_][a-zA-Z0-9_-]*')
VOID = {'br', 'hr', 'img', 'source', 'wbr'}  # tags without an end tag (lenient mode only)


class ParseError(ValueError):
    """What the Bot API says after "Bad Request: can't parse entities: "."""


@dataclass
class Node:
    tag: str  # '' for the root
    attrs: dict = field(default_factory=dict)
    children: List[Union['Node', str]] = field(default_factory=list)

    @property
    def entity(self) -> Optional[str]:
        if self.tag == 'blockquote' and 'expandable' in self.attrs:
            return 'expandable_blockquote'
        if self.tag == 'a' and not self.attrs.get('href'):
            return None  # an <a> without a link is plain text
        return TAGS.get(self.tag)


def utf16_len(text: str) -> int:
    """Telegram counts text and entity offsets in UTF-16 code units: an emoji is often 2."""
    return len(text.encode('utf-16-le')) // 2


def _decode(match) -> Optional[str]:
    name = match.group(1)
    if name[0] == '#':
        try:
            code = int(name[2:], 16) if name[1] in 'xX' else int(name[1:])
            return chr(code)
        except (ValueError, OverflowError):
            return None
    return NAMED.get(name)


def unescape(text: str) -> str:
    """Entities decoded as Telegram decodes them: unknown named ones stay."""
    def one(match):
        decoded = _decode(match)
        return match.group(0) if decoded is None else decoded
    return _ENTITY.sub(one, text)


def _offset(source: str, index: int) -> int:
    return len(source[:index].encode('utf-8'))


def parse(source: str, lenient: bool = False) -> Node:
    """The tree of `source`, or ParseError worded as the Bot API words it. `lenient`: tags a message does not
    know are kept (a rich message has its own, `<audio>`), void ones need no end tag; the rest is as strict."""
    root = Node('')
    stack = [root]
    buffer: List[str] = []

    def flush():
        if buffer:
            stack[-1].children.append(''.join(buffer))
            buffer.clear()

    i, n = 0, len(source)
    while i < n:
        char = source[i]
        if char == '&':
            match = _ENTITY.match(source, i)
            if match:
                decoded = _decode(match)
                buffer.append(match.group(0) if decoded is None else decoded)
                i = match.end()
            else:
                buffer.append('&')
                i += 1
            continue
        if char != '<':
            buffer.append(char)
            i += 1
            continue

        offset = _offset(source, i)
        if source.startswith('</', i):
            end = source.find('>', i)
            if end < 0:
                raise ParseError('Unclosed end tag at byte offset %d' % offset)
            name = source[i + 2:end].strip().lower()
            if len(stack) == 1:
                raise ParseError('Unexpected end tag at byte offset %d' % offset)
            if name != stack[-1].tag:
                raise ParseError('Unmatched end tag at byte offset %d, expected "</%s>", found "</%s>"'
                                 % (offset, stack[-1].tag, name))
            flush()
            stack.pop()
            i = end + 1
            continue

        match = _NAME.match(source, i + 1)
        name = match.group(0).lower() if match else ''
        if name not in TAGS and not (lenient and name):
            raise ParseError('Unsupported start tag "%s" at byte offset %d' % (name, offset))
        j, attrs = match.end(), {}
        while True:
            while j < n and source[j] in ' \t\r\n':
                j += 1
            if j >= n:
                raise ParseError('Unclosed start tag at byte offset %d' % offset)
            if source[j] == '>':
                break
            if lenient and source.startswith('/>', j):
                j += 1
                break
            attribute = _ATTRIBUTE.match(source, j)
            if not attribute:
                raise ParseError('Expected equal sign in declaration of an attribute of the tag at byte offset %d'
                                 % offset)
            key, j = attribute.group(0).lower(), attribute.end()
            value = ''
            if j < n and source[j] == '=':
                j += 1
                if j < n and source[j] in '"\'':
                    quote = source[j]
                    end = source.find(quote, j + 1)
                    if end < 0:
                        raise ParseError('Unclosed start tag at byte offset %d' % offset)
                    value, j = source[j + 1:end], end + 1
                else:
                    start = j
                    while j < n and source[j] not in ' \t\r\n>':
                        j += 1
                    value = source[start:j]
            attrs[key] = unescape(value)
        if name == 'span' and attrs.get('class') != 'tg-spoiler':
            raise ParseError('Tag "span" must have class "tg-spoiler" at byte offset %d' % offset)
        if name == 'tg-emoji' and not attrs.get('emoji-id'):
            raise ParseError('Custom emoji entity must contain a tag "emoji-id" at byte offset %d' % offset)
        flush()
        node = Node(name, attrs)
        stack[-1].children.append(node)
        if not (lenient and name in VOID):
            stack.append(node)
        i = j + 1

    flush()
    if len(stack) > 1:
        raise ParseError('Can\'t find end tag corresponding to start tag "%s"' % stack[-1].tag)
    return root


def text_node(text: str) -> Node:
    """A tree for a message sent without parse_mode: the text as it is."""
    return Node('', children=[text] if text else [])


def flatten(root: Node) -> Tuple[str, list]:
    """The plain text Telegram shows and its entities (UTF-16 offsets, as the Bot API gives them)."""
    parts: List[str] = []
    entities: list = []
    position = [0]

    def walk(node: Node, depth: int):
        start = position[0]
        for child in node.children:
            if isinstance(child, str):
                parts.append(child)
                position[0] += utf16_len(child)
            elif child.tag == 'br':
                parts.append('\n')
                position[0] += 1
            else:
                walk(child, depth + 1)
        kind = node.entity if node.tag else None
        if kind and position[0] > start:
            entity = {'type': kind, 'offset': start, 'length': position[0] - start, '_depth': depth}
            if kind == 'text_link':
                entity['url'] = node.attrs.get('href')
            if kind == 'custom_emoji':
                entity['custom_emoji_id'] = node.attrs.get('emoji-id')
            if kind == 'pre':
                language = _pre_language(node)
                if language:
                    entity['language'] = language
            entities.append(entity)

    walk(root, 0)
    entities.sort(key=lambda e: (e['offset'], -e['length'], e['_depth']))  # the outer one first
    for entity in entities:
        del entity['_depth']
    return ''.join(parts), entities


def _pre_language(node: Node) -> Optional[str]:
    for child in node.children:
        if isinstance(child, Node) and child.tag == 'code':
            css = child.attrs.get('class', '')
            if css.startswith('language-'):
                return css[len('language-'):]
    return None


def plain(source: str, parse_mode: Optional[str]) -> str:
    """What the user reads: `source` without tags for HTML, as it is otherwise."""
    if str(parse_mode or '').lower() != 'html':
        return source
    return flatten(parse(source))[0]
