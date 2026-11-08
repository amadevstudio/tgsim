import unittest

from tgsim import html


class ParseTest(unittest.TestCase):
    def test_tags_become_entities(self):
        text, entities = html.flatten(html.parse('<b>Hi</b> <a href="https://x.y">there</a>'))
        self.assertEqual(text, 'Hi there')
        self.assertEqual(entities, [{'type': 'bold', 'offset': 0, 'length': 2},
                                    {'type': 'text_link', 'offset': 3, 'length': 5, 'url': 'https://x.y'}])

    def test_offsets_count_utf16(self):
        text, entities = html.flatten(html.parse('🎧 <i>new</i>'))
        self.assertEqual(entities[0]['offset'], 3)  # the emoji is two UTF-16 units, then a space

    def test_only_four_named_entities(self):
        self.assertEqual(html.plain('a &lt;b&gt; &amp; &quot;c&quot; &#33; &#x41;', 'HTML'), 'a <b> & "c" ! A')
        self.assertEqual(html.plain('educational:&nbsp; explaining', 'HTML'), 'educational:&nbsp; explaining')

    def test_refusals_are_worded_as_telegram(self):
        cases = {
            'a < b': 'Unsupported start tag "" at byte offset 2',
            'a <br> b': 'Unsupported start tag "br" at byte offset 2',
            '<b>bold': 'Can\'t find end tag corresponding to start tag "b"',
            '<b><i>x</b></i>': 'Unmatched end tag at byte offset 7, expected "</i>", found "</b>"',
            'x</b>': 'Unexpected end tag at byte offset 1',
            '<span>x</span>': 'Tag "span" must have class "tg-spoiler" at byte offset 0',
        }
        for source, error in cases.items():
            with self.subTest(source=source):
                with self.assertRaises(html.ParseError) as caught:
                    html.parse(source)
                self.assertEqual(str(caught.exception), error)

    def test_byte_offsets(self):
        with self.assertRaises(html.ParseError) as caught:
            html.parse('Привет <x>')
        self.assertIn('byte offset 13', str(caught.exception))

    def test_attributes_and_expandable_quote(self):
        root = html.parse("<blockquote expandable>long</blockquote><a href='tg://user?id=1'>me</a>"
                          '<pre><code class="language-py">x</code></pre><tg-spoiler>s</tg-spoiler>')
        kinds = [e['type'] for e in html.flatten(root)[1]]
        self.assertEqual(kinds, ['expandable_blockquote', 'text_link', 'pre', 'code', 'spoiler'])
        self.assertEqual(html.flatten(root)[1][2]['language'], 'py')

    def test_without_parse_mode_the_text_is_as_sent(self):
        self.assertEqual(html.plain('<b>x</b>', None), '<b>x</b>')


if __name__ == '__main__':
    unittest.main()
