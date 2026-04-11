from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import bot.db.base as database_module
from bot.cogs.support import SupportCog, _message_text_for_learning
from bot.db import Database
from bot.llm_service import LLMService


class MessageLearningDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._original_db_path = database_module.DB_PATH
        self._tmpdir = tempfile.TemporaryDirectory()
        database_module.DB_PATH = f"{self._tmpdir.name}/test.db"
        self.db = Database()
        await self.db.setup()

    async def asyncTearDown(self) -> None:
        if self.db._db is not None:
            await self.db._db.close()
        database_module.DB_PATH = self._original_db_path
        self._tmpdir.cleanup()

    async def test_learned_message_mark_is_idempotent_per_guild_message(self) -> None:
        created = await self.db.add_learned_message_mark(1, 10, 100, 200, 300)
        duplicate = await self.db.add_learned_message_mark(1, 10, 100, 200, 301)

        self.assertTrue(created)
        self.assertFalse(duplicate)
        self.assertTrue(await self.db.has_learned_message_mark(1, 100))
        self.assertFalse(await self.db.has_learned_message_mark(1, 101))


class MessageLearningSupportTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_text_for_learning_includes_embed_content(self) -> None:
        message = SimpleNamespace(
            content="",
            embeds=[
                SimpleNamespace(
                    title="Title",
                    description="Description",
                    fields=[SimpleNamespace(name="Field", value="Value")],
                )
            ],
        )

        text = _message_text_for_learning(message)

        self.assertEqual(text, "Title\n\nDescription\n\nField\nValue")

    async def test_marked_message_is_learned_once_and_persisted(self) -> None:
        db = MagicMock()
        db.has_learned_message_mark = AsyncMock(return_value=False)
        db.add_learned_fact = AsyncMock(return_value=True)
        db.add_learned_message_mark = AsyncMock(return_value=True)

        llm = MagicMock()
        llm.is_storable_fact = MagicMock(return_value=True)
        llm.create_embedding = AsyncMock(return_value=([0.1, 0.2], b"packed"))

        qdrant = MagicMock()
        qdrant.upsert_fact = AsyncMock()

        bot = MagicMock()
        bot.tree = MagicMock()
        bot.tree.add_command = MagicMock()

        cog = SupportCog(bot=bot, db=db, llm=llm, qdrant=qdrant)
        cog._get_embedding_model = AsyncMock(return_value="embed-model")

        message = SimpleNamespace(
            id=555,
            content="The support channel is monitored by moderators.",
            embeds=[],
            guild=SimpleNamespace(id=42),
            channel=SimpleNamespace(id=99),
            author=SimpleNamespace(id=77),
        )

        status = await cog._learn_from_marked_message(message, marked_by=88)

        self.assertEqual(status, "learned")
        db.add_learned_fact.assert_awaited_once_with(
            42,
            "The support channel is monitored by moderators.",
            b"packed",
            "embed-model",
            qdrant_id="555",
            source="brain_reaction",
        )
        db.add_learned_message_mark.assert_awaited_once_with(42, 99, 555, 77, 88)
        qdrant.upsert_fact.assert_awaited_once_with(
            42,
            "555",
            [0.1, 0.2],
            "The support channel is monitored by moderators.",
            source="brain_reaction",
        )

    async def test_brain_marked_message_that_is_not_a_fact_is_rejected(self) -> None:
        db = MagicMock()
        db.has_learned_message_mark = AsyncMock(return_value=False)

        llm = MagicMock()
        llm.is_storable_fact = MagicMock(return_value=False)
        llm.create_embedding = AsyncMock()

        qdrant = MagicMock()

        bot = MagicMock()
        bot.tree = MagicMock()
        bot.tree.add_command = MagicMock()

        cog = SupportCog(bot=bot, db=db, llm=llm, qdrant=qdrant)

        message = SimpleNamespace(
            id=555,
            content="Can someone help me with this?",
            embeds=[],
            guild=SimpleNamespace(id=42),
            channel=SimpleNamespace(id=99),
            author=SimpleNamespace(id=77),
        )

        status = await cog._learn_from_marked_message(message, marked_by=88)

        self.assertEqual(status, "not_a_fact")
        llm.create_embedding.assert_not_called()
        self.assertFalse(db.add_learned_fact.called)
        self.assertFalse(qdrant.upsert_fact.called)

    async def test_already_marked_message_is_skipped(self) -> None:
        db = MagicMock()
        db.has_learned_message_mark = AsyncMock(return_value=True)

        llm = MagicMock()
        qdrant = MagicMock()

        bot = MagicMock()
        bot.tree = MagicMock()
        bot.tree.add_command = MagicMock()

        cog = SupportCog(bot=bot, db=db, llm=llm, qdrant=qdrant)

        message = SimpleNamespace(
            id=555,
            content="Stored already",
            embeds=[],
            guild=SimpleNamespace(id=42),
            channel=SimpleNamespace(id=99),
            author=SimpleNamespace(id=77),
        )

        status = await cog._learn_from_marked_message(message, marked_by=88)

        self.assertEqual(status, "already_marked")
        self.assertFalse(db.add_learned_fact.called)

    async def test_exchange_learning_stores_new_facts_pending_review(self) -> None:
        db = MagicMock()
        db.add_learned_fact = AsyncMock(return_value=True)

        llm = MagicMock()
        llm.extract_facts = AsyncMock(return_value=["The support queue is triaged by moderators."])
        llm.create_embedding = AsyncMock(return_value=([0.1, 0.2], b"packed"))

        qdrant = MagicMock()
        qdrant.upsert_fact = AsyncMock()

        bot = MagicMock()
        bot.tree = MagicMock()
        bot.tree.add_command = MagicMock()

        cog = SupportCog(bot=bot, db=db, llm=llm, qdrant=qdrant)

        await cog._learn_from_exchange(
            42,
            "Who handles the support queue?",
            "The support queue is triaged by moderators.",
            "chat-model",
            "embed-model",
        )

        db.add_learned_fact.assert_awaited_once()
        args = db.add_learned_fact.await_args
        self.assertEqual(args.args[:4], (42, "The support queue is triaged by moderators.", None, "embed-model"))
        self.assertEqual(args.kwargs["source"], "conversation")
        self.assertFalse(args.kwargs["approved"])
        self.assertIsNotNone(args.kwargs["qdrant_id"])

        qdrant.upsert_fact.assert_awaited_once()
        qdrant_args = qdrant.upsert_fact.await_args
        self.assertEqual(qdrant_args.args[0], 42)
        self.assertEqual(qdrant_args.args[2], [0.1, 0.2])
        self.assertEqual(qdrant_args.args[3], "The support queue is triaged by moderators.")
        self.assertEqual(qdrant_args.kwargs["source"], "conversation")
        self.assertEqual(qdrant_args.kwargs["approved"], 0)


class FactExtractionTests(unittest.IsolatedAsyncioTestCase):
    def _make_llm(self, payload: str) -> LLMService:
        llm = LLMService.__new__(LLMService)
        llm._client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=AsyncMock(
                        return_value=SimpleNamespace(
                            choices=[SimpleNamespace(message=SimpleNamespace(content=payload))]
                        )
                    )
                )
            )
        )
        return llm

    async def test_extract_facts_keeps_only_grounded_durable_candidates(self) -> None:
        llm = self._make_llm(
            """[
                {"fact": "The assistant replied with a bulleted list.", "category": "topic_fact", "grounded_in": "assistant_reply", "confidence": 0.98, "should_store": true, "reason": "Describes the reply."},
                {"fact": "The user prefers concise answers.", "category": "user_preference", "grounded_in": "both", "confidence": 0.91, "should_store": true, "reason": "Stable preference."},
                {"fact": "Maybe the deployment uses Docker.", "category": "topic_fact", "grounded_in": "assistant_reply", "confidence": 0.88, "should_store": true, "reason": "Speculative."}
            ]"""
        )

        facts = await llm.extract_facts(
            "Please keep the replies concise.",
            "I will keep future replies concise.",
            model="test-model",
        )

        self.assertEqual(facts, ["The user prefers concise answers."])

    async def test_extract_facts_filters_string_output_with_same_rules(self) -> None:
        llm = self._make_llm(
            "[\"The assistant answered in a friendly tone.\", \"The support channel is monitored by moderators.\"]"
        )

        facts = await llm.extract_facts(
            "Who watches the support channel?",
            "The support channel is monitored by moderators.",
            model="test-model",
        )

        self.assertEqual(facts, ["The support channel is monitored by moderators."])


if __name__ == "__main__":
    unittest.main()
