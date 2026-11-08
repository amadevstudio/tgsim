# tgsim

A Telegram for bots to talk to in tests. The bot runs unchanged; tgsim plays Telegram on both sides: it answers
the Bot API the way Telegram does, refusals included, and turns what users do into updates. Every action
returns a step: what the bot showed, edited and deleted, its toasts, and the calls Telegram refused. Steps render
as a page that looks like the chat, and lint flags layout mistakes Telegram lets through.

No dependencies; Python 3.9+.

```sh
pip install git+https://github.com/amadevstudio/tgsim@v0.1.0
```

## Any bot, over HTTP

Point the bot's Bot API base at tgsim instead of `https://api.telegram.org`:

```sh
tgsim serve --bot "python examples/menu_bot.py" \
  -c "send /start" -c "tap Settings" -c "tap Buy" -c "pay" -c "expect Thanks" --html steps.html
```

`--bot` starts the bot with `TGSIM_API_URL` and `TGSIM_TOKEN` set and stops it at the end; without it, start the
bot yourself against `http://127.0.0.1:8081`. Long polling (`getUpdates`) and webhooks both work, uploads too
(multipart), and files are served at `/file/bot<token>/<path>`. Without actions `tgsim serve` reads them from
the terminal. While it runs, `http://127.0.0.1:8081/` shows the chats (`?refresh=2` reloads), and
`POST /tgsim/action {"line": "tap Menu"}` runs an action for tests in another language.

Library settings: python-telegram-bot `Application.builder().base_url(url + '/bot')`, aiogram
`AiohttpSession(api=TelegramAPIServer.from_base(url))`, pyTelegramBotAPI `apihelper.API_URL = url +
'/bot{0}/{1}'`, grammY `new Bot(token, {client: {apiRoot: url}})`.

## A Python bot, in process

Faster and deterministic: no HTTP, the bot's updates are handled in the acting thread.

```python
from tgsim import Telegram

tg = Telegram()
tg.listen(lambda update: dispatcher.feed(update))           # your library's way to feed an update
# and send the bot's requests to tg.call(method, params, files) instead of HTTP

alice = tg.user(1001, lang='ru')
step = alice.send('/start')
assert step.new[0].plain.startswith('Привет')
step = alice.tap('Настройки')            # a button by text (or part of it), or (row, column)
print(step)                               # what changed, as text
```

`tg.idle_checks.append(fn)` adds a condition for "the bot is done" (its own threads), when the bot works in the
background after an update.

## What the model does

- Messages, captions, media (photo, audio, document, video, voice, animation, sticker, albums), invoices, a
  rich message (`sendRichMessage`), copies and forwards, replies, reply keyboards, edits with their history,
  deletes, toasts and alerts, inline queries, groups and channels with members, restrictions and bans, the bot
  added or blocked (`my_chat_member`), files (`getFile`), Stars payments (pre-checkout, the payment, refunds,
  `getStarTransactions`, subscriptions).
- Telegram's refusals with its wording: HTML it cannot parse (`can't parse entities: Unsupported start tag "br"
  at byte offset 12`), a text over 4096 or a caption over 1024 characters (UTF-16, after tags), an empty text,
  callback data over 64 bytes, a text button in an inline keyboard, a reply to a message that is gone, an edit
  that changes nothing, a missing message, a tap answered twice, a blocked user (403), a kicked bot, a wrong
  token (401).
- `tg.fail(method, flood(7))` makes a call fail (429 and anything else); `tg.answer(method, value)` overrides an
  answer. `Telegram(strict=True)` refuses chats, file ids and methods it does not know; by default they pass.
- `Telegram(clock=lambda: 1700000000)` fixes the dates; `privacy=True` hides group chatter from a bot that is
  not an admin, as Telegram does by default.

## Scenarios

One action per line (`python -m tgsim serve --help` lists them): `send`, `start`, `tap`, `data`, `press`,
`file`, `photo`, `voice`, `pay`, `inline`, `block`, `group`, `in`, `reply`, `join`, `addbot`, `expect`,
`screen`, `user`, `wait`. A harness adds its own (`runner.action('deliver')(fn)`): a bot's jobs, a database
query, a clock jump.

## Rendering and lint

`page(steps)` writes one self-contained HTML file: bubbles with Telegram's formatting (spoilers, expandable
quotes, links), media placeholders, replies, edits with their history, inline keyboards cut where a phone cuts
them, toasts at their step, refusals and lint under each message, light and dark, phone and desktop width.
`chat_page(tg)` shows the chats as they are now.

Lint (`check_step`, `check_message`) flags a `%s` or `{name}` left in, an entity shown as text (`&nbsp;`), tags
shown because parse_mode was not set, two spaces, a word glued to a number (`additional5`), a space before
punctuation, a button label a 390 px phone cuts, more than three buttons in a row, a label not in the user's
language, an unanswered tap.

## Tests

```sh
python -m unittest discover -s tests -t .
```

MIT license.
