import asyncio
import unittest
from collections import defaultdict, deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from cmds.ai import AI, VISION_MAX_IMAGE_BYTES


def attachment(name="photo.png", size=1000, content_type="image/png"):
    return SimpleNamespace(
        filename=name, size=size, content_type=content_type,
        url=f"https://cdn.discordapp.com/attachments/1/2/{name}",
    )


class VisionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ai = AI.__new__(AI)
        self.ai.bot = SimpleNamespace(user=SimpleNamespace(id=99))
        self.ai.allowed_channels = {10}
        self.ai.allowed_guilds = set()
        self.ai.memory = Mock()
        self.ai.memory.get_affinity.return_value = 0
        self.ai.direct_reply_chance = 1.0
        self.ai.reply_chance = 1.0
        self.ai.client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(
            return_value=SimpleNamespace(output_text='{"text":"一隻貓","output":"image"}')
        )))
        self.ai.histories = defaultdict(lambda: deque(maxlen=20))
        self.ai.model = "configured-model"
        self.ai.persona = "安安"
        self.ai.scene_prompts = {"direct": "直接回覆", "ambient": "群聊"}
        self.ai._media_guidance = Mock(return_value="")
        self.ai._current_time_context = Mock(return_value="")
        self.ai._wants_web_search = Mock(return_value=False)
        self.ai._enforce_media_policy = Mock(side_effect=lambda reply, *args: reply)
        self.ai.max_output_tokens = 250
        self.ai.concurrency = asyncio.Semaphore(1)

    def message(self, content="分享一下", mentioned=False, attachments=None, reference=None):
        return SimpleNamespace(
            id=100, content=content,
            mentions=[self.ai.bot.user] if mentioned else [],
            attachments=attachments or [], reference=reference,
            channel=SimpleNamespace(id=10, fetch_message=AsyncMock()),
            guild=SimpleNamespace(id=1), author=SimpleNamespace(id=2, bot=False),
        )

    async def test_ordinary_post_never_sends_images(self):
        message = self.message(attachments=[attachment()])
        for scene in ("ambient", "direct"):
            self.assertEqual(await self.ai._vision_input(message, message.content, scene), ([], ""))
        message.channel.fetch_message.assert_not_awaited()

    async def test_mention_attaches_images_with_low_detail(self):
        message = self.message("<@99>", mentioned=True, attachments=[attachment()])
        inputs, _ = await self.ai._vision_input(message, "有人叫你。", "direct")
        self.assertEqual(inputs[0]["content"][1], {
            "type": "input_image", "image_url": attachment().url, "detail": "low",
        })

    async def test_question_without_mention_does_not_read_images(self):
        for content in ("這張圖是什麼？", "圖片裡寫什麼", "幫我看看"):
            message = self.message(content, attachments=[attachment()])
            self.assertEqual(await self.ai._reply_scene(message), "ambient")
            for scene in ("ambient", "direct"):
                inputs, _ = await self.ai._vision_input(message, content, scene)
                self.assertFalse(inputs)

    async def test_question_does_not_expand_allowed_channels(self):
        message = self.message("這張圖是什麼？", attachments=[attachment()])
        message.channel.id = 11
        self.assertIsNone(await self.ai._reply_scene(message))

    async def test_reply_reads_referenced_attachment(self):
        reference = SimpleNamespace(message_id=50, channel_id=10, resolved=None)
        message = self.message("這是什麼？", mentioned=True, reference=reference)
        message.channel.fetch_message.return_value = self.message(attachments=[attachment()])
        self.assertEqual(await self.ai._reply_scene(message), "direct")
        inputs, _ = await self.ai._vision_input(message, message.content, "direct")
        self.assertEqual(inputs[0]["content"][1]["image_url"], attachment().url)

    async def test_cross_channel_reference_not_fetched(self):
        message = self.message("這是什麼？", mentioned=True, reference=SimpleNamespace(message_id=50, channel_id=11))
        inputs, guidance = await self.ai._vision_input(message, message.content, "direct")
        self.assertFalse(inputs)
        self.assertIn("沒有可讀取的圖片", guidance)
        message.channel.fetch_message.assert_not_awaited()

    async def test_deleted_reference_has_helpful_fallback(self):
        message = self.message("這是什麼？", mentioned=True, reference=SimpleNamespace(message_id=50, channel_id=10))
        message.channel.fetch_message.side_effect = discord.NotFound(
            SimpleNamespace(status=404, reason="Not Found"), "deleted"
        )
        inputs, guidance = await self.ai._vision_input(message, message.content, "direct")
        self.assertFalse(inputs)
        self.assertIn("不要猜測", guidance)

    async def test_filters_unsupported_large_files_and_caps_images(self):
        message = self.message(mentioned=True, attachments=[
            attachment("notes.txt", content_type="text/plain"),
            attachment(size=VISION_MAX_IMAGE_BYTES + 1),
            attachment("movie.gif", content_type="image/gif"),
            attachment("one.JPG", content_type=None), attachment("two.png"), attachment("three.png"),
        ])
        inputs, _ = await self.ai._vision_input(message, message.content, "direct")
        images = inputs[0]["content"][1:]
        self.assertEqual(len(images), 2)
        self.assertTrue(images[0]["image_url"].endswith("one.JPG"))
        self.assertTrue(images[1]["image_url"].endswith("two.png"))

    async def test_images_are_request_only_and_reply_is_text(self):
        message = self.message("<@99> 這是什麼？", mentioned=True, attachments=[attachment()])
        message.guild = None
        history = self.ai.histories[10]
        history.append({"role": "user", "content": "這是什麼？"})
        reply = await self.ai._generate_reply(message, "這是什麼？", "direct")
        self.assertEqual(reply.output, "text")
        first_input = self.ai.client.responses.create.call_args.kwargs["input"]
        self.assertIsInstance(first_input[-1]["content"], list)
        self.assertTrue(all(isinstance(item["content"], str) for item in history))
        followup = self.message("謝謝")
        followup.guild = None
        await self.ai._generate_reply(followup, "謝謝", "direct")
        next_input = self.ai.client.responses.create.call_args.kwargs["input"]
        self.assertTrue(all(isinstance(item["content"], str) for item in next_input))

    async def test_attachment_only_post_never_calls_model(self):
        await self.ai.on_message(self.message("", attachments=[attachment()]))
        self.ai.client.responses.create.assert_not_awaited()

    async def test_reply_to_bot_reads_attachment_without_mention_or_question(self):
        bot_message = Mock(spec=discord.Message)
        bot_message.author = self.ai.bot.user
        reference = SimpleNamespace(message_id=50, channel_id=10, resolved=bot_message)
        message = self.message("給你看", attachments=[attachment()], reference=reference)
        self.assertEqual(await self.ai._reply_scene(message), "direct")
        inputs, _ = await self.ai._vision_input(message, message.content, "direct")
        self.assertTrue(inputs)

    async def test_reply_to_other_user_without_mention_does_not_read(self):
        other_message = Mock(spec=discord.Message)
        other_message.author = SimpleNamespace(id=3)
        reference = SimpleNamespace(message_id=50, channel_id=10, resolved=other_message)
        message = self.message("這是什麼？", attachments=[attachment()], reference=reference)
        self.assertIsNone(await self.ai._reply_scene(message))
        inputs, _ = await self.ai._vision_input(message, message.content, "direct")
        self.assertFalse(inputs)

    async def test_image_only_reply_to_bot_reaches_rate_limit_check(self):
        bot_message = Mock(spec=discord.Message)
        bot_message.author = self.ai.bot.user
        message = self.message("", attachments=[attachment()], reference=SimpleNamespace(
            message_id=50, channel_id=10, resolved=bot_message,
        ))
        self.ai._clean_content = Mock(return_value="")
        self.ai._remember_channel_message = Mock()
        self.ai.memory_summary_enabled = False
        self.ai._is_rate_limit_exempt = Mock(return_value=False)
        self.ai._reserve_request = AsyncMock(return_value=False)
        await self.ai.on_message(message)
        self.ai._reserve_request.assert_awaited_once()

    async def test_rate_limit_blocks_image_lookup_and_model_call(self):
        message = self.message("<@99> 這張圖是什麼？", mentioned=True, attachments=[attachment()])
        self.ai._clean_content = Mock(return_value="這張圖是什麼？")
        self.ai._remember_channel_message = Mock()
        self.ai.memory_summary_enabled = False
        self.ai._is_rate_limit_exempt = Mock(return_value=False)
        self.ai._reserve_request = AsyncMock(return_value=False)
        await self.ai.on_message(message)
        self.ai.client.responses.create.assert_not_awaited()
        message.channel.fetch_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
