"""The Bot API as the model answers it, driven in process by a listener that plays the bot."""
import unittest

from tgsim import ApiError, Telegram, flood


def started(tg, menu=None):
    """A bot that answers /start with a menu and taps with a toast and an edit."""
    def bot(update):
        message = update.get('message')
        if message and message.get('text') == '/start':
            tg.call('sendMessage', {'chat_id': message['chat']['id'], 'text': '<b>Menu</b>', 'parse_mode': 'HTML',
                                    'reply_markup': menu or {'inline_keyboard': [[{'text': 'Open',
                                                                                   'callback_data': 'open'}]]}})
        query = update.get('callback_query')
        if query:
            tg.call('answerCallbackQuery', {'callback_query_id': query['id'], 'text': 'Opened'})
            tg.call('editMessageText', {'chat_id': query['message']['chat']['id'],
                                        'message_id': query['message']['message_id'], 'text': 'Opened page'})
    tg.listen(bot)
    return bot


class SendTest(unittest.TestCase):
    def setUp(self):
        self.tg = Telegram()
        self.alice = self.tg.user(1001, 'en', 'Alice')

    def test_a_step_shows_what_the_bot_did(self):
        started(self.tg)
        step = self.alice.send('/start')
        self.assertEqual([m.plain for m in step.new], ['Menu'])
        self.assertEqual(step.new[0].button_texts(), ['Open'])
        step = self.alice.tap('Open')
        self.assertEqual([t.text for t in step.toasts], ['Opened'])
        self.assertEqual([m.plain for m in step.edited], ['Opened page'])
        self.assertEqual(step.edited[0].history[0]['text'], '<b>Menu</b>')
        self.assertFalse(step.failed)

    def test_html_telegram_cannot_parse_is_refused(self):
        with self.assertRaises(ApiError) as caught:
            self.tg.call('sendMessage', {'chat_id': 1001, 'text': '5 < 6', 'parse_mode': 'HTML'})
        self.assertEqual(caught.exception.description,
                         'Bad Request: can\'t parse entities: Unsupported start tag "" at byte offset 2')
        self.assertIs(self.tg.calls[-1].error, caught.exception)

    def test_limits(self):
        cases = [
            ('sendMessage', {'text': 'x' * 4097}, 'Bad Request: message is too long'),
            ('sendMessage', {'text': '   '}, 'Bad Request: message text is empty'),
            ('sendPhoto', {'photo': 'https://x.y/a.jpg', 'caption': 'x' * 1025},
             'Bad Request: message caption is too long'),
            ('sendMessage', {'text': 'x', 'reply_markup': {'inline_keyboard': [[{'text': 'a',
                                                                                 'callback_data': 'd' * 65}]]}},
             'Bad Request: BUTTON_DATA_INVALID'),
            ('sendMessage', {'text': 'x', 'reply_markup': {'inline_keyboard': [[{'text': 'a'}]]}},
             "Bad Request: can't parse inline keyboard button: Text buttons are unallowed in the inline keyboard"),
            ('sendMessage', {'text': 'x', 'reply_to_message_id': 99}, 'Bad Request: message to be replied not found'),
        ]
        for method, params, error in cases:
            with self.subTest(error=error):
                with self.assertRaises(ApiError) as caught:
                    self.tg.call(method, dict(params, chat_id=1001))
                self.assertEqual(caught.exception.description, error)
        # 4096 counts what the user reads, not the tags
        self.tg.call('sendMessage', {'chat_id': 1001, 'text': '<b>%s</b>' % ('x' * 4096), 'parse_mode': 'HTML'})
        self.tg.call('sendMessage', {'chat_id': 1001, 'text': 'x', 'reply_to_message_id': 99,
                                     'allow_sending_without_reply': True})

    def test_edits_as_telegram_refuses_them(self):
        sent = self.tg.call('sendMessage', {'chat_id': 1001, 'text': 'same'})
        key = {'chat_id': 1001, 'message_id': sent['message_id']}
        with self.assertRaises(ApiError) as caught:
            self.tg.call('editMessageText', dict(key, text='same'))
        self.assertIn('message is not modified', caught.exception.description)
        self.assertEqual(self.tg.history(1001)[0].history, [])
        photo = self.tg.call('sendPhoto', {'chat_id': 1001, 'photo': 'https://x.y/a.jpg'})
        with self.assertRaises(ApiError) as caught:
            self.tg.call('editMessageText', {'chat_id': 1001, 'message_id': photo['message_id'], 'text': 'x'})
        self.assertEqual(caught.exception.description, 'Bad Request: there is no text in the message to edit')
        self.tg.call('deleteMessage', key)
        with self.assertRaises(ApiError) as caught:
            self.tg.call('editMessageText', dict(key, text='new'))
        self.assertEqual(caught.exception.description, 'Bad Request: message to edit not found')
        with self.assertRaises(ApiError) as caught:
            self.tg.call('deleteMessage', key)
        self.assertEqual(caught.exception.description, 'Bad Request: message to delete not found')

    def test_a_tap_is_answered_once(self):
        answered = []

        def bot(update):
            query = update.get('callback_query')
            if query:
                self.tg.call('answerCallbackQuery', {'callback_query_id': query['id']})
                try:
                    self.tg.call('answerCallbackQuery', {'callback_query_id': query['id']})
                except ApiError as e:
                    answered.append(e.description)
        self.tg.listen(bot)
        self.tg.call('sendMessage', {'chat_id': 1001, 'text': 'x',
                                     'reply_markup': {'inline_keyboard': [[{'text': 'a', 'callback_data': 'a'}]]}})
        step = self.alice.tap('a')
        self.assertFalse(step.unanswered_tap)
        self.assertIn('query is too old', answered[0])

    def test_an_unanswered_tap_is_said(self):
        self.tg.listen(lambda update: None)
        self.tg.call('sendMessage', {'chat_id': 1001, 'text': 'x',
                                     'reply_markup': {'inline_keyboard': [[{'text': 'a', 'callback_data': 'a'}]]}})
        self.assertTrue(self.alice.tap('a').unanswered_tap)

    def test_a_blocked_user(self):
        self.tg.listen(lambda update: None)
        self.alice.block()
        with self.assertRaises(ApiError) as caught:
            self.tg.call('sendMessage', {'chat_id': 1001, 'text': 'x'})
        self.assertEqual((caught.exception.code, caught.exception.description),
                         (403, 'Forbidden: bot was blocked by the user'))
        self.alice.send('/start')  # writing to the bot unblocks it
        self.tg.call('sendMessage', {'chat_id': 1001, 'text': 'x'})

    def test_faults_and_overrides(self):
        self.tg.fail('sendMessage', flood(7))
        with self.assertRaises(ApiError) as caught:
            self.tg.call('sendMessage', {'chat_id': 1001, 'text': 'x'})
        self.assertEqual(caught.exception.parameters, {'retry_after': 7})
        self.tg.call('sendMessage', {'chat_id': 1001, 'text': 'x'})  # once
        self.tg.answer('getChat', lambda params, files: ApiError(400, 'Bad Request: chat not found'))
        with self.assertRaises(ApiError):
            self.tg.call('getChat', {'chat_id': -100})

    def test_method_names_are_case_insensitive_and_unknown_ones_lenient(self):
        self.assertEqual(self.tg.call('getme')['id'], self.tg.bot['id'])
        self.assertIs(self.tg.call('setMyCommands', {'commands': '[]'}), True)
        strict = Telegram(strict=True)
        with self.assertRaises(ApiError) as caught:
            strict.call('setMyCommandz')
        self.assertEqual(caught.exception.code, 404)
        with self.assertRaises(ApiError):
            strict.call('sendMessage', {'chat_id': 5, 'text': 'x'})  # a chat nobody made

    def test_files(self):
        sent = self.tg.call('sendDocument', {'chat_id': 1001}, {'document': ('a.txt', b'hello', 'text/plain')})
        file_id = sent['document']['file_id']
        self.assertEqual(sent['document']['file_name'], 'a.txt')
        again = self.tg.call('sendDocument', {'chat_id': 1001, 'document': file_id})
        self.assertEqual(again['document']['file_id'], file_id)
        info = self.tg.call('getFile', {'file_id': file_id})
        self.assertEqual(self.tg.file_bytes(info['file_path']), b'hello')
        with self.assertRaises(ApiError) as caught:
            self.tg.call('sendPhoto', {'chat_id': 1001})
        self.assertEqual(caught.exception.description, 'Bad Request: there is no photo in the request')

    def test_a_rich_message(self):
        rich = {'html': '<audio src="tg://audio?id=a"></audio><b>Show notes</b><br>more',
                'media': [{'id': 'a', 'media': {'type': 'audio', 'media': 'https://x.y/e.mp3', 'title': 'E1'}}]}
        sent = self.tg.call('sendRichMessage', {'chat_id': 1001, 'rich_message': rich})
        self.assertTrue(sent['rich_message']['blocks'][0]['audio']['file_id'])
        message = self.tg.history(1001)[-1]
        self.assertEqual((message.plain, message.media['title']), ('Show notes\nmore', 'E1'))
        with self.assertRaises(ApiError):
            self.tg.call('sendRichMessage', {'chat_id': 1001, 'rich_message': {'html': '<b>open'}})

    def test_the_user_sends_a_file(self):
        got = []
        self.tg.listen(got.append)
        self.alice.send_document('subs.opml', b'<opml/>', 'text/xml')
        document = got[0]['message']['document']
        path = self.tg.call('getFile', {'file_id': document['file_id']})['file_path']
        self.assertEqual(self.tg.file_bytes(path), b'<opml/>')
        self.assertEqual(document['file_name'], 'subs.opml')


class PaymentTest(unittest.TestCase):
    def setUp(self):
        self.tg = Telegram()
        self.alice = self.tg.user(1001)
        self.answer = True

        def bot(update):
            if 'pre_checkout_query' in update:
                query = update['pre_checkout_query']
                self.tg.call('answerPreCheckoutQuery', {'pre_checkout_query_id': query['id'], 'ok': self.answer,
                                                        'error_message': None if self.answer else 'Sold out'})
            message = update.get('message') or {}
            if 'successful_payment' in message:
                self.tg.call('sendMessage', {'chat_id': message['chat']['id'], 'text': 'Thanks: %s' % (
                    message['successful_payment']['invoice_payload'])})
        self.tg.listen(bot)
        self.tg.call('sendInvoice', {'chat_id': 1001, 'title': 'Month', 'description': '30 days',
                                     'payload': 'm1', 'currency': 'XTR', 'prices': '[{"label":"x","amount":250}]'})

    def test_paid(self):
        step = self.alice.tap('Pay')
        self.assertEqual([m.plain for m in step.new], ['Thanks: m1'])
        self.assertEqual(self.tg.payments[0]['total_amount'], 250)
        charge = self.tg.payments[0]['telegram_payment_charge_id']
        self.assertIs(self.tg.call('refundStarPayment', {'user_id': 1001, 'telegram_payment_charge_id': charge}),
                      True)
        with self.assertRaises(ApiError):
            self.tg.call('refundStarPayment', {'user_id': 1001, 'telegram_payment_charge_id': charge})

    def test_refused(self):
        self.answer = False
        step = self.alice.pay()
        self.assertEqual(step.new, [])
        self.assertIn('Sold out', step.notes[0])

    def test_invoice_rules(self):
        with self.assertRaises(ApiError) as caught:
            self.tg.call('sendInvoice', {'chat_id': 1001, 'title': 'x' * 33, 'description': 'd', 'payload': 'p',
                                         'currency': 'XTR', 'prices': [{'label': 'x', 'amount': 1}]})
        self.assertEqual(caught.exception.description, 'Bad Request: INVOICE_TITLE_INVALID')


class GroupTest(unittest.TestCase):
    def test_members_and_restrictions(self):
        tg = Telegram()
        owner, stranger = tg.user(1), tg.user(2)
        tg.group(-1001, 'Club', creator=1)
        seen = []
        tg.listen(seen.append)
        stranger.say(-1001, 'hello')
        self.assertEqual(seen[-1]['message']['chat']['type'], 'supergroup')
        tg.call('restrictChatMember', {'chat_id': -1001, 'user_id': 2,
                                       'permissions': {'can_send_messages': False}})
        self.assertEqual(tg.call('getChatMember', {'chat_id': -1001, 'user_id': 2})['status'], 'restricted')
        step = stranger.say(-1001, 'again')
        self.assertIn('does not let', step.notes[0])
        admins = tg.call('getChatAdministrators', {'chat_id': -1001})
        self.assertEqual(sorted(m['status'] for m in admins), ['administrator', 'creator'])
        self.assertEqual(tg.call('getChatMember', {'chat_id': -1001, 'user_id': 3})['status'], 'left')

    def test_privacy_mode(self):
        tg = Telegram(privacy=True)
        user = tg.user(1)
        tg.group(-5, bot='member')
        seen = []
        tg.listen(seen.append)
        user.say(-5, 'chatter')
        user.say(-5, '/help')
        self.assertEqual([u['message']['text'] for u in seen], ['/help'])

    def test_a_kicked_bot_cannot_post(self):
        tg = Telegram()
        tg.group(-7, bot='kicked')
        with self.assertRaises(ApiError) as caught:
            tg.call('sendMessage', {'chat_id': -7, 'text': 'x'})
        self.assertEqual(caught.exception.description, 'Forbidden: bot was kicked from the supergroup chat')


class InlineTest(unittest.TestCase):
    def test_results(self):
        tg = Telegram()
        user = tg.user(1)
        tg.listen(lambda u: tg.call('answerInlineQuery', {
            'inline_query_id': u['inline_query']['id'],
            'results': [{'type': 'article', 'id': '1', 'title': u['inline_query']['query'],
                         'input_message_content': {'message_text': 'x'}}]}))
        self.assertEqual(user.inline('jazz').inline_results[0]['title'], 'jazz')


if __name__ == '__main__':
    unittest.main()
