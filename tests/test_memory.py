import tempfile
import unittest
from pathlib import Path

from core.memory import MemoryStore


class GuildMemoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = MemoryStore(str(Path(self.directory.name) / "memory.db"))

    @staticmethod
    def candidate(content, *, memory_id=0, evidence=2, importance=3, category="culture"):
        return {
            "id": memory_id,
            "category": category,
            "content": content,
            "importance": importance,
            "evidence_count": evidence,
        }

    def test_new_memory_requires_repeated_evidence_and_is_guild_scoped(self):
        self.store.apply_guild_memory_candidates(1, [
            self.candidate("大家習慣週五晚上一起玩遊戲", evidence=1),
            self.candidate("大家稱這個頻道為客廳", evidence=2),
        ])

        memories = self.store.list_guild_memories(1)
        self.assertEqual([item.content for item in memories], ["大家稱這個頻道為客廳"])
        self.assertEqual(self.store.list_guild_memories(2), [])

    def test_existing_memory_can_be_reinforced_but_not_cross_guild_updated(self):
        self.store.apply_guild_memory_candidates(1, [self.candidate("固定在週末舉辦活動")])
        memory = self.store.list_guild_memories(1)[0]
        self.store.apply_guild_memory_candidates(1, [
            self.candidate(
                "固定在週六舉辦活動", memory_id=memory.id,
                evidence=2, importance=5, category="activity",
            )
        ])
        updated = self.store.list_guild_memories(1)[0]
        self.assertEqual(updated.content, "固定在週六舉辦活動")
        self.assertEqual(updated.evidence_count, 4)

        self.store.apply_guild_memory_candidates(2, [
            self.candidate("不應覆寫", memory_id=memory.id, evidence=2)
        ])
        self.assertEqual(self.store.list_guild_memories(2), [])
        self.assertEqual(self.store.list_guild_memories(1)[0].content, "固定在週六舉辦活動")

    def test_limit_keeps_strongest_memories_and_admin_can_forget(self):
        self.store.apply_guild_memory_candidates(1, [
            self.candidate("低重要度", importance=1),
            self.candidate("高重要度", importance=5),
            self.candidate("中重要度", importance=3),
        ], max_per_guild=2)
        memories = self.store.list_guild_memories(1)
        self.assertEqual([item.content for item in memories], ["高重要度", "中重要度"])
        self.assertTrue(self.store.forget_guild_memory(1, memories[0].id))
        self.assertFalse(self.store.forget_guild_memory(2, memories[1].id))
        self.assertEqual([item.content for item in self.store.list_guild_memories(1)], ["中重要度"])

    def test_invalid_candidate_is_ignored(self):
        self.store.apply_guild_memory_candidates(1, [
            self.candidate("未知分類", category="secret"),
            self.candidate(""),
            {"id": "bad", "category": "culture", "content": "內容",
             "importance": 3, "evidence_count": 2},
        ])
        self.assertEqual(self.store.list_guild_memories(1), [])


if __name__ == "__main__":
    unittest.main()
