import unittest

from tgsim import Telegram, check_message
from tgsim.lint import button_room, check_text, text_width


def codes(found):
    return [code for _, code, _ in found]


class TextTest(unittest.TestCase):
    def test_mistakes_the_user_would_see(self):
        self.assertEqual(codes(check_text('Hello, %s!')), ['placeholder'])
        self.assertEqual(codes(check_text('Days left: {days}')), ['placeholder'])
        self.assertEqual(codes(check_text('educational:&nbsp;explaining')), ['entity'])
        self.assertEqual(codes(check_text('Plan:  Relay')), ['double_space'])
        self.assertEqual(codes(check_text('additional5 days')), ['glued'])
        self.assertEqual(codes(check_text('осталось 5дней')), ['glued'])
        self.assertEqual(codes(check_text('Done !')), ['space_punct'])

    def test_what_is_fine(self):
        for text in ('MP3, 4G, 1-й, H2O, 2026-11-08', 'https://example.com/a%20b?x=1', '/start_ref42',
                     'Price: $5 · 250 ⭐', 'Bonjour !'):
            with self.subTest(text=text):
                self.assertEqual(check_text(text, 'fr' if text.startswith('Bonjour') else 'en'), [])


class MessageTest(unittest.TestCase):
    def setUp(self):
        self.tg = Telegram()
        self.tg.user(1, 'ru')

    def sent(self, **params):
        self.tg.call('sendMessage', dict({'chat_id': 1, 'text': 'x'}, **params))
        return self.tg.history(1)[-1]

    def test_tags_without_parse_mode(self):
        found = [i.code for i in check_message(self.sent(text='<b>Plan</b>'), 'ru')]
        self.assertIn('raw_tags', found)

    def test_a_label_a_phone_cuts(self):
        row = [{'text': 'Подписаться на подкаст целиком', 'callback_data': 'a'},
               {'text': 'Назад', 'callback_data': 'b'}]
        found = check_message(self.sent(reply_markup={'inline_keyboard': [row]}), 'ru')
        self.assertEqual([(i.code, i.text.split(' ')[1]) for i in found if i.code == 'cut_label'], [('cut_label',
                                                                                                    '0,0')])

    def test_a_label_in_another_language(self):
        found = check_message(self.sent(reply_markup={'inline_keyboard': [[{'text': 'Settings',
                                                                            'callback_data': 'a'}]]}), 'ru')
        self.assertEqual([i.code for i in found], ['language'])
        self.assertEqual(check_message(self.tg.history(1)[-1], 'en'), [])

    def test_widths(self):
        self.assertLess(text_width('Back'), button_room(3))
        self.assertGreater(text_width('🎧' * 20), button_room(1))


if __name__ == '__main__':
    unittest.main()
