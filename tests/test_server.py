"""A bot on its own (examples/menu_bot.py, plain HTTP and long polling) against the server."""
import json
import os
import sys
import threading
import unittest
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'examples'))

from menu_bot import Bot  # noqa: E402

from tgsim import Runner, Server, Telegram, page  # noqa: E402
from tgsim.server import parse_body  # noqa: E402


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tg = Telegram(token='123:abc', quiet=0.3)
        self.runner = Runner(self.tg, 1001, 'en')
        self.server = Server(self.tg, port=0, runner=self.runner).start()
        self.stop = threading.Event()
        self.bot = Bot(self.server.url, '123:abc')
        self.thread = threading.Thread(target=self.bot.poll, args=(self.stop,), daemon=True)
        self.thread.start()

    def tearDown(self):
        self.stop.set()
        self.server.stop()
        self.thread.join(5)

    def test_a_scenario_over_http(self):
        start = self.runner.run('send /start')
        self.assertEqual(start.new[0].plain, 'Hello, User 1001!\nPick one:')
        settings = self.runner.run('tap Settings')
        self.assertEqual([t.text for t in settings.toasts], ['Settings opened'])
        self.assertTrue(settings.edited[0].plain.startswith('Settings'))
        self.runner.run('tap Buy')
        paid = self.runner.run('pay')
        self.assertEqual([m.plain for m in paid.new], ['Thanks! Your plan is on.'])
        self.assertTrue(all(not step.failed for step in self.runner.steps))
        rendered = page(self.runner.steps, tg=self.tg)
        self.assertIn('Hello, User 1001!', rendered)
        self.assertIn('blockquote class="exp"', rendered)

    def test_a_wrong_token_is_unauthorized(self):
        request = urllib.request.Request(self.server.url + '/botWRONG/getMe')
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        self.assertEqual(caught.exception.code, 401)

    def test_a_refusal_is_the_bot_api_json(self):
        answer = self.bot.call('sendMessage', chat_id=1001, text='<b>x', parse_mode='HTML')
        self.assertEqual(answer['error_code'], 400)
        self.assertIn("can't parse entities", answer['description'])

    def test_the_action_endpoint_drives_a_user(self):
        request = urllib.request.Request(self.server.url + '/tgsim/action',
                                         json.dumps({'line': 'send /start'}).encode('utf-8'),
                                         {'Content-Type': 'application/json'})
        with urllib.request.urlopen(request) as response:
            answer = json.loads(response.read())
        self.assertTrue(answer['ok'])
        self.assertIn('Hello, User 1001!', answer['step'])

    def test_the_chats_page(self):
        self.runner.run('send /start')
        with urllib.request.urlopen(self.server.url + '/') as response:
            self.assertIn('Pick one:', response.read().decode('utf-8'))


class BodyTest(unittest.TestCase):
    def test_multipart_upload(self):
        boundary = 'XyZ'
        body = ('--XyZ\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n1001\r\n'
                '--XyZ\r\nContent-Disposition: form-data; name="document"; filename="a.txt"\r\n'
                'Content-Type: text/plain\r\n\r\nhello\r\n--XyZ--\r\n').encode('utf-8')
        params, files = parse_body('multipart/form-data; boundary=%s' % boundary, body)
        self.assertEqual(params, {'chat_id': '1001'})
        self.assertEqual(files['document'], ('a.txt', b'hello', 'text/plain'))

    def test_form_and_json(self):
        self.assertEqual(parse_body('application/x-www-form-urlencoded', b'chat_id=5&text=a+b')[0],
                         {'chat_id': '5', 'text': 'a b'})
        self.assertEqual(parse_body('application/json', b'{"chat_id": 5}')[0], {'chat_id': 5})


if __name__ == '__main__':
    unittest.main()
