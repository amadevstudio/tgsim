"""A bot with no library: long polling over plain HTTP, a menu, an edit, a toast and a Stars invoice.

    tgsim serve --bot "python examples/menu_bot.py" -c "send /start" -c "tap Settings" -c "tap Buy" -c "pay"

It reads TGSIM_API_URL and TGSIM_TOKEN (tgsim sets them); against Telegram it would be https://api.telegram.org.
"""
import json
import os
import threading
import urllib.error
import urllib.request

MENU = {'inline_keyboard': [[{'text': '⚙️ Settings', 'callback_data': 'settings'},
                             {'text': '⭐ Buy', 'callback_data': 'buy'}],
                            [{'text': 'Website', 'url': 'https://example.com'}]]}


class Bot:
    def __init__(self, api: str, token: str):
        self.base = '%s/bot%s/' % (api.rstrip('/'), token)

    def call(self, method: str, **params):
        request = urllib.request.Request(self.base + method, json.dumps(params).encode('utf-8'),
                                         {'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read())['result']
        except urllib.error.HTTPError as e:
            return json.loads(e.read())

    def handle(self, update: dict):
        message = update.get('message')
        if message and message.get('text', '').startswith('/start'):
            self.call('sendMessage', chat_id=message['chat']['id'], parse_mode='HTML', reply_markup=MENU,
                      text='<b>Hello, %s!</b>\nPick one:' % message['from']['first_name'])
        elif message and message.get('successful_payment'):
            self.call('sendMessage', chat_id=message['chat']['id'], text='Thanks! Your plan is on.')
        query = update.get('callback_query')
        if query and query['data'] == 'settings':
            self.call('answerCallbackQuery', callback_query_id=query['id'], text='Settings opened')
            self.call('editMessageText', chat_id=query['message']['chat']['id'],
                      message_id=query['message']['message_id'], parse_mode='HTML',
                      text='<b>Settings</b>\n<blockquote expandable>Language: English\nTheme: auto</blockquote>',
                      reply_markup=MENU)
        elif query and query['data'] == 'buy':
            self.call('answerCallbackQuery', callback_query_id=query['id'])
            self.call('sendInvoice', chat_id=query['message']['chat']['id'], title='One month',
                      description='Everything for 30 days', payload='month', currency='XTR',
                      prices=[{'label': 'Month', 'amount': 250}])
        checkout = update.get('pre_checkout_query')
        if checkout:
            self.call('answerPreCheckoutQuery', pre_checkout_query_id=checkout['id'], ok=True)

    def poll(self, stop: threading.Event):
        offset = 0
        while not stop.is_set():
            updates = self.call('getUpdates', offset=offset, timeout=1)
            if not isinstance(updates, list):
                continue
            for update in updates:
                offset = update['update_id'] + 1
                self.handle(update)


if __name__ == '__main__':
    Bot(os.environ.get('TGSIM_API_URL', 'https://api.telegram.org'), os.environ['TGSIM_TOKEN']).poll(
        threading.Event())
