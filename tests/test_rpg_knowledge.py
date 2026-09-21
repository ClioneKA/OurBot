import asyncio
import unittest
from collections import defaultdict, deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from cmds.ai import AI
from core.rpg_knowledge import RPGKnowledgeBase


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class RPGKnowledgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.knowledge = RPGKnowledgeBase.from_markdown(PROJECT_ROOT / "config" / "RPG.md")

    def test_fishing_question_retrieves_fishing_rules(self):
        context = self.knowledge.context("安安，釣魚要怎麼開始？")
        self.assertIn("生活技能：釣魚", context)
        self.assertIn("釣竿", context)

    def test_job_change_question_retrieves_job_rules(self):
        context = self.knowledge.context("安安大冒險幾級可以轉職？")
        self.assertIn("職業、晉升與裝備", context)
        self.assertIn("Lv.10", context)

    def test_unrelated_chat_has_no_knowledge_context(self):
        self.assertEqual(self.knowledge.context("安安，今天過得好嗎？"), "")

    def test_equipment_source_question_finds_monster_drop(self):
        context = self.knowledge.context("鐵核重鎚哪裡拿？")
        self.assertIn("鐵殼魔像", context)
        self.assertIn("鐵核重鎚", context)

    def test_material_source_question_finds_life_skill_rule(self):
        context = self.knowledge.context("浮光珍珠怎麼取得？")
        self.assertIn("生活技能：釣魚", context)
        self.assertIn("魔女島海灣", context)

    def test_item_purpose_question_finds_recipe_and_source(self):
        context = self.knowledge.context("浮光珍珠是什麼？")
        self.assertIn("浮光墜飾", context)
        self.assertIn("魔女島海灣", context)

    def test_unrelated_item_source_question_has_no_context(self):
        self.assertEqual(self.knowledge.context("咖啡豆哪裡買？"), "")
        self.assertEqual(self.knowledge.context("咖啡豆是什麼？"), "")


class RPGKnowledgePromptTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_rpg_question_injects_reference(self):
        ai = AI.__new__(AI)
        ai.bot = SimpleNamespace(get_cog=Mock(return_value=None))
        ai.client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(
            return_value=SimpleNamespace(output_text='{"text":"十級。"}')
        )))
        ai.histories = defaultdict(lambda: deque(maxlen=20))
        ai.persona = "安安"
        ai.scene_prompts = {"direct": "直接回覆", "ambient": "群聊"}
        ai._media_guidance = Mock(return_value="")
        ai._current_time_context = Mock(return_value="")
        ai._vision_input = AsyncMock(return_value=([], ""))
        ai._wants_web_search = Mock(return_value=True)
        ai._enforce_media_policy = Mock(side_effect=lambda reply, *args: reply)
        ai.rpg_knowledge = RPGKnowledgeBase.from_markdown(PROJECT_ROOT / "config" / "RPG.md")
        ai.memory = Mock()
        ai.memory.relevant_personal_memories.return_value = []
        ai.memory.list_guild_memories.return_value = []
        ai.memory.get_affinity.return_value = 0
        ai.memory.get_preferred_name.return_value = None
        ai.memory.get_impression.return_value = None
        ai.preferred_name_cache = {}
        ai.guild_memory_prompt_limit = 20
        ai.max_output_tokens = 250
        ai.affinity_daily_changes = 3
        ai.concurrency = asyncio.Semaphore(1)
        message = SimpleNamespace(
            author=SimpleNamespace(id=2), guild=SimpleNamespace(id=1),
            channel=SimpleNamespace(id=10),
        )

        await ai._generate_reply(message, "安安大冒險幾級可以轉職？", "direct")

        instructions = ai.client.responses.create.call_args.kwargs["instructions"]
        self.assertIn("<rpg_knowledge>", instructions)
        self.assertIn("職業、晉升與裝備", instructions)
        self.assertIn("不能自行補造", instructions)
        self.assertNotIn("tools", ai.client.responses.create.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
