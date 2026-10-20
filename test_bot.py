import asyncio
from datetime import datetime, timezone
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from telegram import Chat, Message, MessageEntity, PhotoSize
import bot


class FormattingTests(unittest.TestCase):
    def test_append_keeps_bold_and_emoji(self):
        e = MessageEntity('bold', 3, 4)
        text, entities = bot.compose('😀 test', [e], dict(bot.DEFAULTS, footer='حقوق'), 4096)
        self.assertEqual(text, '😀 test\n\nحقوق')
        self.assertEqual(entities[0].offset, 3)

    def test_remove_links_adjusts_utf16_offsets(self):
        text = '😀 https://x.com hi'
        url = MessageEntity('url', 3, 13)
        bold = MessageEntity('bold', 17, 2)
        result, entities = bot.clean_entities(text, [url, bold])
        self.assertEqual(result, '😀  hi')
        self.assertEqual(entities[0].offset, 4)
        self.assertEqual(entities[0].length, 2)

    def test_nested_entity_survives_removal(self):
        text, entities = bot.clean_entities('a @abc z', [MessageEntity('bold', 0, 8), MessageEntity('mention', 2, 4)])
        self.assertEqual(text, 'a  z')
        self.assertEqual(entities[0].length, 4)

    def test_linked_labels_removed(self):
        text, entities = bot.clean_entities('click', [MessageEntity('text_link', 0, 5, url='https://example.com')])
        self.assertEqual((text, entities), ('', []))

    def test_footer_is_idempotent(self):
        c = dict(bot.DEFAULTS, footer='حقوق')
        result = bot.compose('hello\n\nحقوق', [], c, 4096)
        self.assertEqual(result[0], 'hello\n\nحقوق')

    def test_too_long_never_truncates(self):
        self.assertIsNone(bot.compose('x' * 1024, [], bot.DEFAULTS, 1024))

    def test_caption_without_text(self):
        self.assertEqual(bot.compose('', [], bot.DEFAULTS, 1024)[0], bot.DEFAULTS['footer'])


class StorageAndApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        bot.init_db(Path(self.tmp.name) / 'test.db')
        bot.save_channel({'id': -100123, 'title': 'قناتي', **bot.DEFAULTS})
        self.api = SimpleNamespace(edit_message_text=AsyncMock(), edit_message_caption=AsyncMock(),
            edit_message_reply_markup=AsyncMock(), send_message=AsyncMock())
        self.context = SimpleNamespace(bot=self.api)

    def tearDown(self):
        bot.DB.close()
        self.tmp.cleanup()

    def message(self, **kwargs):
        return Message(message_id=5, date=datetime.now(timezone.utc), chat=Chat(-100123, 'channel', title='قناتي'), **kwargs)

    async def test_text_processed_only_once(self):
        m = self.message(text='اختبار')
        await bot.apply_post(m, self.context)
        await bot.apply_post(m, self.context)
        self.api.edit_message_text.assert_awaited_once()
        self.assertEqual(self.api.edit_message_text.call_args.kwargs['text'], 'اختبار\n\nجميع الحقوق محفوظة ©')

    async def test_caption_api(self):
        m = self.message(photo=[PhotoSize('id', 'unique', 100, 100)])
        await bot.apply_post(m, self.context)
        self.api.edit_message_caption.assert_awaited_once()
        self.api.edit_message_text.assert_not_awaited()

    async def test_long_caption_preserved(self):
        m = self.message(photo=[PhotoSize('id', 'unique', 100, 100)], caption='x' * 1024)
        await bot.apply_post(m, self.context)
        self.api.edit_message_caption.assert_not_awaited()
        self.api.send_message.assert_awaited_once()

    async def test_album_once(self):
        one = self.message(photo=[PhotoSize('a', 'u1', 10, 10)], media_group_id='album1')
        two = Message(message_id=6, date=one.date, chat=one.chat, photo=[PhotoSize('b', 'u2', 10, 10)], media_group_id='album1')
        await bot.apply_post(one, self.context)
        await bot.apply_post(two, self.context)
        self.api.edit_message_caption.assert_awaited_once()

    async def test_votes_saved_and_toggled(self):
        c = bot.channel(-100123)
        c['likes'] = True
        bot.save_channel(c)
        m = self.message(text='vote')
        await bot.apply_post(m, self.context)
        q = SimpleNamespace(message=m, from_user=SimpleNamespace(id=123), data='vote:1',
                            answer=AsyncMock(), edit_message_reply_markup=AsyncMock())
        await bot.vote_callback(SimpleNamespace(callback_query=q), self.context)
        self.assertEqual(bot.vote_counts(-100123, 5), (1, 0))
        q.data = 'vote:-1'
        await bot.vote_callback(SimpleNamespace(callback_query=q), self.context)
        self.assertEqual(bot.vote_counts(-100123, 5), (0, 1))
        await bot.vote_callback(SimpleNamespace(callback_query=q), self.context)
        self.assertEqual(bot.vote_counts(-100123, 5), (0, 0))

    async def test_disabled_channel_not_modified(self):
        c = bot.channel(-100123)
        c['enabled'] = False
        bot.save_channel(c)
        await bot.apply_post(self.message(text='untouched'), self.context)
        self.api.edit_message_text.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
