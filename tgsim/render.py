"""What the user sees: steps as terminal text, and chats or steps as one self-contained HTML page.

    print(text_step(step))
    open('out.html', 'w').write(page(steps, title='Onboarding'))
    open('chat.html', 'w').write(chat_page(tg, [1001]))

The page has no scripts or styles from the network: bubbles as Telegram draws them (formatting, spoilers,
expandable quotes, replies, edits with their history, media as placeholders), inline keyboards cut at a phone's
width, toasts and refusals at their step, lint under each message, light and dark themes.
"""
from __future__ import annotations

import html as _html
import re
from typing import Iterable, List, Optional

from . import html, lint
from .model import Message, Telegram

# --- terminal -----------------------------------------------------------------------------------

def _plain(message: Message) -> str:
    try:
        return message.plain
    except html.ParseError:
        return message.text


def _media_line(message: Message) -> Optional[str]:
    media = message.media
    if message.kind == 'invoice':
        invoice = media['invoice']
        return '(invoice: %s %s, payload %s)' % (invoice['total_amount'], invoice['currency'], invoice['payload'])
    if message.kind == 'payment':
        payment = media['payment']
        return '(paid %s %s, payload %s)' % (payment['total_amount'], payment['currency'],
                                             payment['invoice_payload'])
    if not media.get('file_id') and not media.get('source'):
        return None
    parts = [message.kind]
    for key in ('title', 'performer', 'file_name'):
        if media.get(key):
            parts.append(str(media[key]))
    if media.get('duration'):
        parts.append(_duration(media['duration']))
    if media.get('source') and media['source'] not in ('upload', 'file_id'):
        parts.append(str(media['source'])[:120])
    elif media.get('source'):
        parts.append(media['source'])
    return '(%s)' % ', '.join(parts)


def _duration(value) -> str:
    seconds = int(float(value or 0))
    if seconds >= 3600:
        return '%d:%02d:%02d' % (seconds // 3600, seconds % 3600 // 60, seconds % 60)
    return '%d:%02d' % (seconds // 60, seconds % 60)


def text_message(message: Message, label: str = '') -> str:
    head = '  [%s #%d %s%s]' % (label, message.message_id, message.kind,
                                ' reply to #%d' % message.reply_to if message.reply_to else '')
    lines = [head]
    text = re.sub(r'\n{3,}', '\n\n', _plain(message))
    lines += ['    ' + line for line in text.splitlines()]
    media = _media_line(message)
    if media and message.kind not in ('text',):
        lines.append('    ' + media)
    for row in message.buttons:
        lines.append('    ' + '  '.join('[%s%s]' % (button.get('text'), ' -> ' + button['url'] if button.get('url')
                                                       else '') for button in row))
    if message.keyboard and message.keyboard.get('keyboard'):
        for row in message.keyboard['keyboard']:
            lines.append('    ' + '  '.join('<%s>' % (b.get('text') if isinstance(b, dict) else b) for b in row))
    return '\n'.join(lines)


def text_step(step) -> str:
    lines = ['>>> %s  (%.1f s)' % (step.action, step.seconds)]
    for label, messages in (('new', step.new), ('edited', step.edited)):
        for message in messages:
            lines.append(text_message(message, label))
    for message_id in step.deleted:
        lines.append('  [deleted #%s]' % message_id)
    for toast in step.toasts:
        if toast.text:  # a tap answered without a word only stops the spinner
            lines.append('  [%s] %s' % ('alert' if toast.alert else 'toast', toast.text))
    for message in step.other_chats:
        lines.append(text_message(message, 'to chat %s' % message.chat_id))
    if step.inline_results is not None:
        for result in step.inline_results:
            lines.append('  [inline %s] %s' % (result.get('type'), result.get('title') or result.get('id')))
    for call in step.refusals:
        lines.append('  !! %s refused: %d %s' % (call.method, call.error.code, call.error.description))
    for error in step.handler_errors:
        lines.append('  !! the bot raised: %s' % error.strip().splitlines()[-1])
    for note in step.notes:
        lines.append('  note: %s' % note)
    return '\n'.join(lines)


# --- HTML ---------------------------------------------------------------------------------------

def _e(text) -> str:
    return _html.escape(str(text), quote=True)


_SAFE_HREF = re.compile(r'^(https?|tg)://', re.I)


def _nodes(node: html.Node) -> str:
    out = []
    for child in node.children:
        if isinstance(child, str):
            out.append(_e(child))
            continue
        inner = _nodes(child)
        tag = child.tag
        if tag in ('b', 'strong'):
            out.append('<b>%s</b>' % inner)
        elif tag in ('i', 'em'):
            out.append('<i>%s</i>' % inner)
        elif tag in ('u', 'ins'):
            out.append('<u>%s</u>' % inner)
        elif tag in ('s', 'strike', 'del'):
            out.append('<s>%s</s>' % inner)
        elif tag == 'a':
            href = child.attrs.get('href', '')
            if href and _SAFE_HREF.match(href):
                out.append('<a href="%s" title="%s" target="_blank" rel="noopener noreferrer">%s</a>'
                           % (_e(href), _e(href), inner))
            else:
                out.append(inner)
        elif tag == 'code':
            out.append('<code>%s</code>' % inner)
        elif tag == 'pre':
            out.append('<pre>%s</pre>' % inner)
        elif tag == 'blockquote':
            if 'expandable' in child.attrs:
                out.append('<blockquote class="exp" onclick="this.classList.toggle(\'open\')">%s</blockquote>'
                           % inner)
            else:
                out.append('<blockquote>%s</blockquote>' % inner)
        elif tag in ('tg-spoiler', 'span'):
            out.append('<span class="spoiler" onclick="this.classList.add(\'shown\')">%s</span>' % inner)
        else:  # tg-emoji: its fallback character
            out.append(inner)
    return ''.join(out)


def _body(message: Message) -> str:
    try:
        return _nodes(message.tree)
    except html.ParseError:
        return _e(message.text)


def _media(message: Message) -> str:
    media, kind = message.media, message.kind
    source = media.get('source')
    where = '' if source in (None, 'upload', 'file_id') else source
    if kind in ('audio', 'rich') and (media.get('file_id') or source):
        title = media.get('title') or media.get('file_name') or 'Audio'
        sub = ' · '.join(str(x) for x in (media.get('performer'), _duration(media['duration'])
                                          if media.get('duration') else None) if x)
        return ('<div class="audio"><span class="play">▶</span><span class="meta2"><b>%s</b><small>%s</small>'
                '%s</span></div>' % (_e(title), _e(sub), '<small class="src" title="%s">%s</small>'
                                     % (_e(where), _e(where)) if where else ''))
    if kind == 'voice':
        return ('<div class="audio"><span class="play">▶</span><span class="wave"></span><small>%s</small></div>'
                % _e(_duration(media.get('duration'))))
    if kind in ('photo', 'video', 'animation', 'video_note', 'sticker'):
        label = {'photo': '🖼 photo', 'video': '🎬 video', 'animation': 'GIF', 'video_note': '⏺ video note',
                 'sticker': 'sticker'}[kind]
        return '<div class="pic %s"><span>%s</span>%s</div>' % (
            kind, label, '<small title="%s">%s</small>' % (_e(where), _e(where)) if where else '')
    if kind == 'document':
        size = media.get('file_size')
        return ('<div class="doc"><span class="icon">📄</span><span class="meta2"><b>%s</b><small>%s</small></span>'
                '</div>' % (_e(media.get('file_name') or 'file'), _e(_size(size) if size else media.get('mime_type')
                                                                     or '')))
    if kind == 'invoice':
        invoice = media['invoice']
        return ('<div class="invoice"><b>%s</b><div>%s</div><div class="price">%s</div></div>'
                % (_e(invoice['title']), _e(invoice['description']), _e(Telegram._price(invoice))))
    return ''


def _size(size: int) -> str:
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return ('%d %s' if unit == 'B' else '%.1f %s') % (size, unit)
        size /= 1024.0
    return ''


def _buttons(message: Message) -> str:
    if not message.buttons:
        return ''
    rows = []
    for row in message.buttons:
        cells = []
        for button in row:
            label = _e(button.get('text', ''))
            if button.get('url'):
                cells.append('<a class="btn" href="%s" title="%s" target="_blank" rel="noopener noreferrer">'
                             '<span>%s</span><i class="arrow">↗</i></a>' % (_e(button['url']), _e(button['url']),
                                                                            label))
            else:
                hint = button.get('callback_data') or next((k for k in button if k != 'text'), '')
                cells.append('<span class="btn" title="%s"><span>%s</span></span>' % (_e(hint), label))
        rows.append('<div class="row">%s</div>' % ''.join(cells))
    return '<div class="kb">%s</div>' % ''.join(rows)


def _reply_keyboard(message: Message) -> str:
    keyboard = message.keyboard or {}
    if not keyboard.get('keyboard'):
        return ''
    rows = ''.join('<div class="row">%s</div>' % ''.join(
        '<span class="btn key"><span>%s</span></span>' % _e(b.get('text') if isinstance(b, dict) else b)
        for b in row) for row in keyboard['keyboard'])
    return '<div class="kb reply"><small>keyboard</small>%s</div>' % rows


def bubble(message: Message, tg: Optional[Telegram] = None, lang: Optional[str] = None,
           issues: Optional[List[lint.Issue]] = None, label: str = '') -> str:
    side = 'bot' if message.from_bot else 'you'
    if message.kind == 'payment':
        payment = message.media['payment']
        return '<div class="service">%s paid %s %s</div>' % (
            _e(message.sender.get('first_name', 'User')), _e(payment['total_amount']), _e(payment['currency']))
    parts = []
    if message.reply_to and tg is not None:
        target = tg.messages.get((message.chat_id, message.reply_to))
        if target is not None:
            snippet = (_plain(target) or target.kind)[:80]
            name = 'Bot' if target.from_bot else target.sender.get('first_name', 'User')
            parts.append('<div class="reply%s"><b>%s</b><span>%s</span></div>' % (
                ' gone' if target.deleted else '', _e(name), _e(snippet)))
        else:
            parts.append('<div class="reply gone"><span>#%d</span></div>' % message.reply_to)
    parts.append(_media(message))
    body = _body(message)
    if body and message.kind != 'invoice':
        parts.append('<div class="text">%s</div>' % body)
    meta = []
    if label:
        meta.append(_e(label))
    if message.history:
        meta.append('edited')
    meta.append('#%d' % message.message_id)
    parts.append('<div class="meta">%s</div>' % ' · '.join(meta))
    history = ''
    if message.history:
        versions = ''.join('<div class="old">%s</div>' % (_nodes(_tree(v)) or _e(v['kind']))
                           for v in message.history)
        history = '<details class="hist"><summary>%d earlier version%s</summary>%s</details>' % (
            len(message.history), '' if len(message.history) == 1 else 's', versions)
    if issues is None:
        issues = lint.check_message(message, lang) if message.from_bot else []
    found = ''.join('<li class="%s"><b>%s</b> %s</li>' % (i.level, _e(i.code), _e(i.text)) for i in issues)
    return ('<div class="line %s"><div class="msg %s%s" dir="auto">%s</div>%s%s%s%s</div>'
            % (side, side, ' deleted' if message.deleted else '', ''.join(parts), _buttons(message),
               _reply_keyboard(message), history, '<ul class="lint">%s</ul>' % found if found else ''))


def _tree(version: dict) -> html.Node:
    if str(version.get('parse_mode') or '').lower() == 'html':
        try:
            return html.parse(version['text'])
        except html.ParseError:
            pass
    return html.text_node(version['text'])


def _user_bubble(step) -> str:
    if step.sent is None:
        return '<div class="line you"><div class="msg you action">%s</div></div>' % _e(step.action)
    return bubble(step.sent, issues=[])


def step_html(step, tg: Optional[Telegram] = None, number: Optional[int] = None) -> str:
    issues = lint.check_step(step)
    by_message = {}
    for issue in issues:
        by_message.setdefault((issue.chat_id, issue.message_id), []).append(issue)
    out = ['<section class="step%s">' % (' failed' if step.failed else ''),
           '<h3><span>%s%s</span><small>%.1f s</small></h3>' % ('%d. ' % number if number else '',
                                                               _e(step.action), step.seconds),
           _user_bubble(step)]
    for message in step.new:
        out.append(bubble(message, tg, step.lang, by_message.get((message.chat_id, message.message_id), [])))
    for message in step.edited:
        out.append(bubble(message, tg, step.lang, by_message.get((message.chat_id, message.message_id), []),
                          label='edited now'))
    for message_id in step.deleted:
        out.append('<div class="service">message #%d deleted</div>' % message_id)
    for toast in step.toasts:
        if toast.text:
            out.append('<div class="%s">%s</div>' % ('alert' if toast.alert else 'toast', _e(toast.text)))
    for message in step.other_chats:
        out.append('<div class="elsewhere">to chat %s</div>' % _e(message.chat_id))
        out.append(bubble(message, tg, None, by_message.get((message.chat_id, message.message_id), [])))
    if step.inline_results is not None:
        items = ''.join('<li><b>%s</b> <small>%s</small></li>' % (_e(r.get('title') or r.get('id')),
                                                                  _e(r.get('description') or r.get('type')))
                        for r in step.inline_results)
        out.append('<ul class="inline">%s</ul>' % items)
    general = [i for i in issues if i.message_id is None]
    if general:
        out.append('<ul class="lint">%s</ul>' % ''.join('<li class="%s"><b>%s</b> %s</li>' % (
            i.level, _e(i.code), _e(i.text)) for i in general))
    for note in step.notes:
        out.append('<div class="note">%s</div>' % _e(note))
    out.append('</section>')
    return ''.join(out)


CSS = """
:root{--bg:#dfe7ee;--page:#f4f6f8;--fg:#14191f;--muted:#6b7785;--bot:#ffffff;--you:#e1fec6;--link:#2a7bd1;
--btn:rgba(30,50,70,.16);--btn-fg:#fff;--line:#d0d8e0;--warn:#a35f00;--warn-bg:#fff4dc;--err:#b3261e;
--err-bg:#fde8e6;--note:#4b5a6a;--code:#e9edf1;--toast:rgba(20,25,31,.86);--toast-fg:#fff}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#0e1621;--page:#17212b;--fg:#e8edf2;
--muted:#8899a8;--bot:#182533;--you:#2b5278;--link:#6ab3f3;--btn:rgba(255,255,255,.10);--btn-fg:#e8edf2;
--line:#24323f;--warn:#ffcf70;--warn-bg:#3a2e14;--err:#ff8a80;--err-bg:#3d1c1a;--note:#a9b6c2;--code:#22303d;
--toast:rgba(240,244,248,.92);--toast-fg:#0e1621}}
:root[data-theme="dark"]{--bg:#0e1621;--page:#17212b;--fg:#e8edf2;--muted:#8899a8;--bot:#182533;--you:#2b5278;
--link:#6ab3f3;--btn:rgba(255,255,255,.10);--btn-fg:#e8edf2;--line:#24323f;--warn:#ffcf70;--warn-bg:#3a2e14;
--err:#ff8a80;--err-bg:#3d1c1a;--note:#a9b6c2;--code:#22303d;--toast:rgba(240,244,248,.92);--toast-fg:#0e1621}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--fg);font:15px/1.38 -apple-system,BlinkMacSystemFont,"Segoe UI",
Roboto,"Helvetica Neue",Arial,sans-serif}
header{position:sticky;top:0;z-index:2;display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:10px 16px;
background:var(--page);border-bottom:1px solid var(--line)}
header h1{font-size:16px;margin:0;flex:1}
header label{font-size:13px;color:var(--muted)}
header select{font:inherit;font-size:13px;color:var(--fg);background:var(--bot);border:1px solid var(--line);
border-radius:6px;padding:2px 4px}
main{max-width:var(--width,390px);margin:0 auto;padding:12px 0 40px}
@media (max-width:430px){main{padding:12px 16px 40px}}
.summary{margin:0 16px 12px;font-size:13px;color:var(--muted)}
.step{background:var(--bg);margin:0 0 14px;padding:8px 8px 10px;border-radius:12px}
.step.failed{outline:2px solid var(--err)}
.step h3{display:flex;justify-content:space-between;gap:8px;margin:2px 4px 8px;font-size:12px;font-weight:600;
color:var(--muted);word-break:break-word}
.chat h3{font-size:13px}
.line{display:flex;flex-direction:column;align-items:flex-start;margin:0 0 6px}
.line.you{align-items:flex-end}
.msg{max-width:88%;min-width:64px;padding:6px 10px 4px;border-radius:14px;background:var(--bot);
white-space:pre-wrap;word-wrap:break-word;overflow-wrap:anywhere;box-shadow:0 1px 1px rgba(0,0,0,.08)}
.msg.you{background:var(--you)}
.msg.action{font-family:ui-monospace,Menlo,monospace;font-size:12px;white-space:normal}
.msg.deleted{opacity:.45;text-decoration:line-through}
.line.bot .msg:has(+ .kb){border-bottom-left-radius:6px;border-bottom-right-radius:6px}
.text a{color:var(--link);text-decoration:none}
code,pre{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;background:var(--code);border-radius:4px}
code{padding:0 3px}pre{padding:6px 8px;margin:4px 0;white-space:pre-wrap}
blockquote{margin:4px 0;padding:2px 8px;border-left:3px solid var(--link);background:var(--code);border-radius:4px}
blockquote.exp{max-height:4.2em;overflow:hidden;cursor:pointer;position:relative}
blockquote.exp:not(.open)::after{content:"show more";position:absolute;right:6px;bottom:2px;font-size:11px;
color:var(--link);background:var(--code);padding-left:6px}
blockquote.exp.open{max-height:none}
.spoiler{background:var(--muted);color:transparent;border-radius:3px;cursor:pointer}
.spoiler.shown{background:none;color:inherit}
.meta{text-align:right;font-size:11px;color:var(--muted);margin-top:2px;white-space:normal}
.reply{border-left:3px solid var(--link);padding:1px 6px;margin:0 0 4px;font-size:13px;border-radius:3px;
background:var(--code);display:flex;flex-direction:column;white-space:nowrap;overflow:hidden}
.reply span{overflow:hidden;text-overflow:ellipsis;color:var(--muted)}
.reply.gone{opacity:.6}
.audio,.doc{display:flex;gap:10px;align-items:center;margin:2px 0 4px;white-space:normal}
.play,.icon{flex:none;width:40px;height:40px;border-radius:50%;background:var(--link);color:#fff;display:flex;
align-items:center;justify-content:center;font-size:16px}
.icon{border-radius:10px}
.meta2{display:flex;flex-direction:column;min-width:0}
.meta2 b,.meta2 small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
small{color:var(--muted);font-size:12px}
.src{max-width:220px}
.wave{flex:1;height:18px;background:repeating-linear-gradient(90deg,var(--link) 0 2px,transparent 2px 5px);
opacity:.6;border-radius:2px;min-width:80px}
.pic{width:240px;max-width:100%;aspect-ratio:4/3;background:var(--code);border-radius:10px;display:flex;
flex-direction:column;align-items:center;justify-content:center;gap:4px;margin:2px 0 4px;white-space:normal;
overflow:hidden;text-align:center;padding:6px}
.pic small{max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pic.video_note{width:160px;aspect-ratio:1;border-radius:50%}
.invoice{white-space:normal}.invoice .price{font-weight:600;margin-top:4px}
.kb{width:88%;max-width:88%;display:flex;flex-direction:column;gap:4px;margin-top:4px}
.line.bot .kb{align-self:flex-start}
.kb .row{display:flex;gap:4px}
.btn{flex:1 1 0;min-width:0;position:relative;display:flex;align-items:center;justify-content:center;
padding:8px 8px;border-radius:8px;background:var(--btn);color:var(--btn-fg);font-size:14px;font-weight:500;
text-decoration:none;white-space:nowrap}
.btn span{overflow:hidden;text-overflow:ellipsis}
.btn .arrow{position:absolute;top:2px;right:4px;font-size:9px;font-style:normal}
:root:not([data-theme="dark"]) .btn{text-shadow:0 1px 1px rgba(0,0,0,.25)}
.kb.reply{width:100%;max-width:100%}.kb.reply small{text-align:center}
.btn.key{background:var(--bot);color:var(--fg);text-shadow:none;box-shadow:0 1px 1px rgba(0,0,0,.1)}
.hist{font-size:12px;color:var(--muted);margin:2px 0 0 4px;max-width:88%}
.hist .old{white-space:pre-wrap;border-left:2px solid var(--line);padding:2px 6px;margin:4px 0}
.service,.elsewhere{align-self:center;text-align:center;font-size:12px;color:var(--muted);margin:4px auto 6px;
padding:2px 10px;border-radius:10px;background:var(--code);width:max-content;max-width:90%}
.toast,.alert{margin:6px auto;padding:8px 12px;border-radius:10px;background:var(--toast);color:var(--toast-fg);
font-size:13px;width:max-content;max-width:90%;white-space:pre-wrap}
.alert{border:2px solid var(--link)}
.note{font-size:12px;color:var(--note);margin:4px 6px}
.lint{list-style:none;margin:4px 0 2px;padding:0;font-size:12px;max-width:100%}
.lint li{padding:2px 6px;border-radius:4px;margin:2px 0;background:var(--code);color:var(--note)}
.lint li.warn{background:var(--warn-bg);color:var(--warn)}
.lint li.error{background:var(--err-bg);color:var(--err)}
.inline{list-style:none;margin:6px 0;padding:0;background:var(--bot);border-radius:10px}
.inline li{padding:6px 10px;border-bottom:1px solid var(--line)}
"""

SCRIPT = """
(function(){var r=document.documentElement;function set(k,v){try{localStorage.setItem('tgsim-'+k,v)}catch(e){}}
function get(k){try{return localStorage.getItem('tgsim-'+k)}catch(e){return null}}
var t=document.getElementById('theme'),w=document.getElementById('width');
function apply(){if(t.value==='auto'){r.removeAttribute('data-theme')}else{r.setAttribute('data-theme',t.value)}
r.style.setProperty('--width',w.value+'px')}
t.value=get('theme')||'auto';w.value=get('width')||'390';apply();
t.onchange=function(){set('theme',t.value);apply()};w.onchange=function(){set('width',w.value);apply()}})();
"""


def _document(title: str, body: str, refresh: Optional[int] = None) -> str:
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">%s<title>%s</title>'
            '<style>%s</style></head><body><header><h1>%s</h1>'
            '<label>width <select id="width"><option value="360">360</option><option value="390">390 phone'
            '</option><option value="430">430</option><option value="720">desktop</option></select></label>'
            '<label>theme <select id="theme"><option value="auto">auto</option><option value="light">light'
            '</option><option value="dark">dark</option></select></label></header><main>%s</main>'
            '<script>%s</script></body></html>'
            % ('<meta http-equiv="refresh" content="%d">' % refresh if refresh else '', _e(title), CSS, _e(title),
               body, SCRIPT))


def page(steps: Iterable, title: str = 'tgsim', tg: Optional[Telegram] = None) -> str:
    """Every step: what the user did, then what the bot showed, with lint."""
    steps = list(steps)
    issues = [issue for step in steps for issue in lint.check_step(step)]
    counts = {level: sum(1 for i in issues if i.level == level) for level in ('error', 'warn', 'note')}
    summary = '<p class="summary">%d steps · %d errors · %d warnings · %d notes</p>' % (
        len(steps), counts['error'], counts['warn'], counts['note'])
    body = summary + ''.join(step_html(step, tg, number) for number, step in enumerate(steps, 1))
    return _document(title, body)


def chat_page(tg: Telegram, chat_ids: Optional[List[int]] = None, title: str = 'tgsim',
              refresh: Optional[int] = None) -> str:
    """The chats as they are now, deleted messages greyed out."""
    with tg.lock:
        ids = chat_ids if chat_ids is not None else sorted({chat for chat, _ in tg.messages})
        sections = []
        for chat_id in ids:
            chat = tg.chats.get(chat_id, {'title': chat_id})
            name = chat.get('title') or chat.get('first_name') or chat_id
            lang = tg.langs.get(chat_id)
            bubbles = ''.join(bubble(m, tg, lang) for m in tg.history(chat_id, deleted=True))
            sections.append('<section class="step chat"><h3><span>%s</span><small>%s</small></h3>%s</section>'
                            % (_e(name), _e(chat_id), bubbles or '<div class="note">no messages</div>'))
    return _document(title, ''.join(sections) or '<p class="summary">no chats yet</p>', refresh)
