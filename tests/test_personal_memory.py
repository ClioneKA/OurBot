import json
import tempfile
import unittest
from pathlib import Path

from core.memory import MemoryStore


class PersonalMemoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = MemoryStore(str(Path(self.directory.name) / 'memory.db'))
        self.store.add_impression_observation(1, 2, '不喝咖啡，昨天心情不好')
        self.observations = self.store.list_impression_observations(1)

    def candidate(self, **kwargs):
        item = dict(id=0, action='upsert', category='preference', content='不喝咖啡',
                    basis='explicit', importance=3, evidence_ids=[self.observations[0].id])
        item.update(kwargs)
        return item

    def save(self, *items, user_id=2):
        self.store.complete_impression_summary(
            1, [], {user_id: '吾輩記得。'}, 10,
            personal_candidates={user_id: list(items)}, observations=self.observations,
        )

    def test_provenance_update_and_owner_isolation(self):
        self.save(self.candidate())
        first = self.store.list_personal_memories(1, 2)[0]
        evidence = json.loads(first['evidence'])[0]
        self.assertEqual(evidence['content'], self.observations[0].content)
        self.assertTrue(evidence['created_at'])
        self.save(self.candidate(id=first['id'], content='最近不喝咖啡', basis='inferred'))
        self.assertEqual(self.store.list_personal_memories(1, 2)[0]['content'], '最近不喝咖啡')
        self.assertEqual(self.store.list_personal_memories(2, 2), [])
        self.assertEqual(self.store.list_personal_memories(1, 3), [])
        self.assertFalse(self.store.forget_personal_memory(1, 3, first['id']))
        self.assertTrue(self.store.forget_personal_memory(1, 2, first['id']))

    def test_missing_or_other_person_evidence_cannot_create_memory(self):
        self.save(self.candidate(evidence_ids=[999]))
        self.save(self.candidate(), user_id=3)
        self.assertEqual(self.store.list_personal_memories(1, 2), [])
        self.assertEqual(self.store.list_personal_memories(1, 3), [])

    def test_recent_state_expires_and_relevance_wins(self):
        self.save(self.candidate(), self.candidate(category='recent', content='昨天心情不好', importance=5))
        self.assertEqual(self.store.relevant_personal_memories(1, 2, '要喝咖啡嗎', 1)[0]['content'], '不喝咖啡')
        recent = next(i for i in self.store.list_personal_memories(1, 2) if i['category'] == 'recent')
        self.assertIsNotNone(recent['expires_at'])
        with self.store._connection() as connection:
            connection.execute("UPDATE personal_memories SET expires_at='2000-01-01' WHERE id=?", (recent['id'],))
        self.assertEqual(len(self.store.list_personal_memories(1, 2)), 1)

    def test_deduplication_delete_and_atomic_observation_consumption(self):
        self.save(self.candidate(), self.candidate())
        items = self.store.list_personal_memories(1, 2)
        self.assertEqual(len(items), 1)
        self.store.complete_impression_summary(
            1, [self.observations[0].id], {2: '改變印象'}, 10,
            personal_candidates={2: [self.candidate(id=items[0]['id'], action='delete')]},
            observations=self.observations,
        )
        self.assertEqual(self.store.list_personal_memories(1, 2), [])
        self.assertEqual(self.store.list_impression_observations(1), [])
        self.assertEqual(self.store.get_impression(1, 2), '改變印象')


if __name__ == '__main__':
    unittest.main()
