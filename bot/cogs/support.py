"""Full-featured AI assistant cog inspired by VRT-Cogs/assistant.

Features: /chat with per-user per-channel memory, conversation management,
RAG embeddings, function calling, auto-response via triggers & listen channels,
image generation (/draw), channel summary (/tldr), dynamic system prompt
placeholders, per-channel prompts, token usage tracking, and full admin config.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse as _urlparse

import discord
from discord import app_commands
from discord.ext import commands

if TYPE_CHECKING:
    from bot.database import Database
    from bot.llm_service import LLMService

from bot.config import DEFAULTS
from bot.crawler import WebCrawler

logger = logging.getLogger(__name__)

DISCORD_MAX_LEN = 2000

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _split(text: str, limit: int = DISCORD_MAX_LEN) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        idx = text.rfind("\n", 0, limit)
        if idx == -1:
            idx = limit
        parts.append(text[:idx])
        text = text[idx:].lstrip("\n")
    return parts


def _fill_placeholders(text: str, ctx: dict[str, Any]) -> str:
    """Replace {placeholder} tokens in a prompt string."""
    mapping = {
        "botname": ctx["bot"].user.display_name if ctx["bot"].user else "Bot",
        "server": ctx["guild"].name if ctx.get("guild") else "DM",
        "members": str(ctx["guild"].member_count) if ctx.get("guild") else "0",
        "username": ctx["user"].name,
        "displayname": ctx["user"].display_name,
        "roles": ", ".join(r.name for r in getattr(ctx["user"], "roles", [])[1:]) or "None",
        "avatar": str(ctx["user"].display_avatar.url),
        "owner": str(ctx["guild"].owner) if ctx.get("guild") else "N/A",
        "servercreated": ctx["guild"].created_at.strftime("%Y-%m-%d") if ctx.get("guild") else "N/A",
        "channelname": getattr(ctx.get("channel"), "name", "DM"),
        "channelmention": ctx["channel"].mention if hasattr(ctx.get("channel"), "mention") else "DM",
        "timestamp": f"<t:{int(datetime.now(timezone.utc).timestamp())}:F>",
        "date": datetime.now(timezone.utc).strftime("%m-%d-%Y"),
        "time": datetime.now(timezone.utc).strftime("%I:%M %p UTC"),
        "user": ctx["user"].mention,
    }
    for key, val in mapping.items():
        text = text.replace(f"{{{key}}}", val)
    return text


def _embed_from_dict(data: dict) -> discord.Embed:
    """Build a Discord Embed from a dict returned by the create_embed tool."""
    color_str = data.get("color", "#5865F2")
    try:
        color = discord.Color(int(color_str.lstrip("#"), 16))
    except Exception:
        color = discord.Color.blurple()
    em = discord.Embed(
        title=data.get("title", ""),
        description=data.get("description", ""),
        color=color,
    )
    for f in data.get("fields", []):
        em.add_field(name=f["name"], value=f["value"], inline=f.get("inline", False))
    return em


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class SupportCog(commands.Cog, name="Support"):
    """AI-powered assistant with conversation memory, RAG, function calling, and more."""

    def __init__(
        self, bot: commands.Bot, db: "Database", llm: "LLMService"
    ) -> None:
        self.bot = bot
        self.db = db
        self.llm = llm
        self._processing_messages: set[int] = set()
        # Context menu: right-click message → "Ask AI about this"
        self._ask_ctx = app_commands.ContextMenu(name="Ask AI", callback=self._ask_context_menu)
        self.bot.tree.add_command(self._ask_ctx)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self._ask_ctx.name, type=self._ask_ctx.type)

    # ------------------------------------------------------------------
    # Internal: build system prompt for a guild/channel
    # ------------------------------------------------------------------

    async def _build_system_prompt(
        self,
        guild: discord.Guild | None,
        channel: discord.abc.Messageable | None,
        user: discord.User | discord.Member,
    ) -> str:
        base = DEFAULTS["assistant_prompt"]
        if guild:
            custom = await self.db.get_guild_config(guild.id, "assistant_prompt")
            if custom:
                base = custom
            if channel:
                chan_prompt = await self.db.get_guild_config(
                    guild.id, f"channel_prompt_{getattr(channel, 'id', 0)}"
                )
                if chan_prompt:
                    base += "\n\n" + chan_prompt
        ctx = {"bot": self.bot, "guild": guild, "channel": channel, "user": user}
        return _fill_placeholders(base, ctx)

    # ------------------------------------------------------------------
    # Internal: get guild-level assistant settings
    # ------------------------------------------------------------------

    async def _get_setting(self, guild_id: int, key: str, default: str = "") -> str:
        val = await self.db.get_guild_config(guild_id, f"assistant_{key}")
        return val or default

    async def _set_setting(self, guild_id: int, key: str, value: str) -> None:
        await self.db.set_guild_config(guild_id, f"assistant_{key}", value)

    async def _get_model(self, guild_id: int) -> str:
        return await self.db.get_setting(guild_id, "assistant_model")

    async def _get_temperature(self, guild_id: int) -> float:
        return await self.db.get_setting_float(guild_id, "assistant_temperature")

    async def _get_max_tokens(self, guild_id: int) -> int:
        return await self.db.get_setting_int(guild_id, "assistant_max_tokens")

    async def _get_max_retention(self, guild_id: int) -> int:
        return await self.db.get_setting_int(guild_id, "assistant_max_retention")

    async def _get_embedding_model(self, guild_id: int) -> str:
        return await self.db.get_setting(guild_id, "assistant_embedding_model")

    async def _get_image_model(self, guild_id: int) -> str:
        return await self.db.get_setting(guild_id, "assistant_image_model")

    async def _is_enabled(self, guild_id: int) -> bool:
        val = await self.db.get_guild_config(guild_id, "assistant_enabled")
        return val != "0"

    async def _function_calling_enabled(self, guild_id: int) -> bool:
        val = await self.db.get_guild_config(guild_id, "assistant_function_calls")
        return val != "0"

    # ------------------------------------------------------------------
    # Internal: RAG embedding retrieval
    # ------------------------------------------------------------------

    async def _get_rag_context(self, guild_id: int, query: str, top_n: int = 5) -> str:
        """Retrieve the top-N most relevant embeddings for a query."""
        try:
            emb_model = await self._get_embedding_model(guild_id)
            query_vec, _ = await self.llm.create_embedding(query, model=emb_model)
        except Exception:
            logger.warning("Embedding creation failed for RAG query")
            return ""

        all_embeds = await self.db.get_all_embeddings(guild_id)
        scored: list[tuple[float, str]] = []
        min_rel_str = await self.db.get_guild_config(guild_id, "assistant_relatedness")
        min_rel = float(min_rel_str) if min_rel_str else 0.3

        for row in all_embeds:
            if not row["embedding"]:
                continue
            stored_vec = self.llm.unpack_embedding(row["embedding"])
            sim = self.llm.similarity(query_vec, stored_vec)
            if sim >= min_rel:
                scored.append((sim, row["text"]))

        scored.sort(key=lambda x: x[0], reverse=True)
        if not scored:
            return ""
        chunks = [text for _, text in scored[:top_n]]
        return "Relevant knowledge:\n" + "\n---\n".join(chunks)

    # ------------------------------------------------------------------
    # Internal: full chat pipeline
    # ------------------------------------------------------------------

    async def _do_chat(
        self,
        guild: discord.Guild | None,
        channel: discord.abc.Messageable,
        user: discord.User | discord.Member,
        question: str,
    ) -> dict[str, Any]:
        """Run the full chat pipeline and return LLM result dict."""
        guild_id = guild.id if guild else 0
        channel_id = getattr(channel, "id", 0)
        user_id = user.id

        # Store user message
        await self.db.add_conversation_message(guild_id, channel_id, user_id, "user", question)

        # Fetch conversation history
        max_ret = await self._get_max_retention(guild_id) if guild else int(DEFAULTS["assistant_max_retention"])
        conversation = await self.db.get_conversation_history(guild_id, channel_id, user_id, limit=max_ret)

        # Build system prompt
        system_prompt = await self._build_system_prompt(guild, channel, user)

        # RAG context injection
        rag_ctx = await self._get_rag_context(guild_id, question)
        if rag_ctx:
            system_prompt += "\n\n" + rag_ctx

        # Function calling tools
        tools = None
        if guild and await self._function_calling_enabled(guild_id):
            from bot.llm_service import BUILTIN_TOOLS
            custom_fns = await self.db.get_enabled_functions(guild_id)
            tools_list: list[dict] = []
            for fn in custom_fns:
                tools_list.append({
                    "type": "function",
                    "function": {
                        "name": fn["name"],
                        "description": fn["description"],
                        "parameters": json.loads(fn["parameters"]),
                    },
                })
            if tools_list:
                tools = tools_list

        # Call LLM
        model = await self._get_model(guild_id) if guild else DEFAULTS["assistant_model"]
        temperature = await self._get_temperature(guild_id) if guild else float(DEFAULTS["assistant_temperature"])
        max_tokens = await self._get_max_tokens(guild_id) if guild else int(DEFAULTS["assistant_max_tokens"])

        result = await self.llm.get_response(
            conversation,
            system_prompt=system_prompt,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
        )

        # Store assistant reply
        content = result.get("content", "")
        if content:
            await self.db.add_conversation_message(guild_id, channel_id, user_id, "assistant", content)

        # Log token usage
        usage = result.get("usage", {})
        if guild and (usage.get("prompt_tokens") or usage.get("completion_tokens")):
            await self.db.log_token_usage(
                guild_id, user_id,
                usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0),
            )

        return result

    async def _send_result(
        self,
        send_func,
        result: dict[str, Any],
    ) -> None:
        """Send the LLM result (text + embeds) via a send-like callable."""
        content = result.get("content", "")
        embeds_data = result.get("embeds", [])

        discord_embeds = [_embed_from_dict(e) for e in embeds_data]

        if content:
            chunks = _split(content)
            first_embeds = discord_embeds[:10] if discord_embeds else []
            await send_func(chunks[0], embeds=first_embeds or discord.utils.MISSING)
            for chunk in chunks[1:]:
                await send_func(chunk)
        elif discord_embeds:
            await send_func(embeds=discord_embeds[:10])
        else:
            await send_func("I couldn't generate a response.")

    # ==================================================================
    # SLASH COMMANDS: Chat & Conversation Management
    # ==================================================================

    @app_commands.command(name="chat", description="Chat with the AI assistant")
    @app_commands.describe(
        question="Your message to the assistant",
        outputfile="Upload the response as a file with this name",
    )
    async def chat(
        self,
        interaction: discord.Interaction,
        question: str,
        outputfile: str | None = None,
    ) -> None:
        if interaction.guild and not await self._is_enabled(interaction.guild.id):
            await interaction.response.send_message("The assistant is disabled in this server.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        result = await self._do_chat(
            interaction.guild, interaction.channel, interaction.user, question  # type: ignore[arg-type]
        )

        if outputfile:
            content = result.get("content", "")
            file = discord.File(fp=__import__("io").BytesIO(content.encode()), filename=outputfile)
            await interaction.followup.send(file=file)
        else:
            await self._send_result(interaction.followup.send, result)

    # Keep /ask as an alias
    @app_commands.command(name="ask", description="Ask the AI a question (alias for /chat)")
    @app_commands.describe(question="Your question or message")
    async def ask(self, interaction: discord.Interaction, question: str) -> None:
        if interaction.guild and not await self._is_enabled(interaction.guild.id):
            await interaction.response.send_message("The assistant is disabled in this server.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        result = await self._do_chat(
            interaction.guild, interaction.channel, interaction.user, question  # type: ignore[arg-type]
        )
        await self._send_result(interaction.followup.send, result)

    # Context menu: Ask AI about a message
    async def _ask_context_menu(self, interaction: discord.Interaction, message: discord.Message) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        question = f"Regarding this message from {message.author.display_name}:\n\n{message.content}"
        result = await self._do_chat(
            interaction.guild, interaction.channel, interaction.user, question  # type: ignore[arg-type]
        )
        await self._send_result(interaction.followup.send, result)

    @app_commands.command(name="convostats", description="View token/message stats for your conversation")
    @app_commands.describe(user="User to check (default: yourself)")
    @app_commands.guild_only()
    async def convostats(
        self, interaction: discord.Interaction, user: discord.Member | None = None
    ) -> None:
        target = user or interaction.user
        stats = await self.db.get_conversation_stats(
            interaction.guild.id, interaction.channel.id, target.id  # type: ignore[union-attr]
        )
        usage = await self.db.get_user_usage(interaction.guild.id, target.id)  # type: ignore[union-attr]
        em = discord.Embed(title=f"Conversation Stats — {target.display_name}", color=discord.Color.blue())
        em.add_field(name="Messages", value=str(stats["messages"]))
        em.add_field(name="Tokens (this convo)", value=str(stats["tokens"]))
        em.add_field(name="Total prompt tokens", value=str(usage["prompt_tokens"]))
        em.add_field(name="Total completion tokens", value=str(usage["completion_tokens"]))
        await interaction.response.send_message(embed=em, ephemeral=True)

    @app_commands.command(name="convoclear", description="Reset your conversation in this channel")
    @app_commands.guild_only()
    async def convoclear(self, interaction: discord.Interaction) -> None:
        deleted = await self.db.clear_conversation_history(
            interaction.guild.id, interaction.channel.id, interaction.user.id  # type: ignore[union-attr]
        )
        await interaction.response.send_message(
            f"🗑️ Cleared {deleted} message(s) from your conversation.", ephemeral=True
        )

    # Keep /clear as alias
    @app_commands.command(name="clear", description="Clear your conversation history (alias for /convoclear)")
    async def clear(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild.id if interaction.guild else 0
        channel_id = interaction.channel.id if interaction.channel else 0
        deleted = await self.db.clear_conversation_history(guild_id, channel_id, interaction.user.id)
        await interaction.response.send_message(
            f"🗑️ Cleared {deleted} message(s).", ephemeral=True
        )

    @app_commands.command(name="convopop", description="Remove the last message from your conversation")
    @app_commands.guild_only()
    async def convopop(self, interaction: discord.Interaction) -> None:
        removed = await self.db.pop_last_conversation_message(
            interaction.guild.id, interaction.channel.id, interaction.user.id  # type: ignore[union-attr]
        )
        msg = "Removed last message." if removed else "No messages to remove."
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="compact", description="Compact your conversation using LLM summarisation")
    @app_commands.describe(focus="Optional focus phrase to guide the summary")
    @app_commands.guild_only()
    async def compact(self, interaction: discord.Interaction, focus: str | None = None) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        channel_id = interaction.channel.id  # type: ignore[union-attr]
        user_id = interaction.user.id

        history = await self.db.get_conversation_history(guild_id, channel_id, user_id, limit=200)
        if len(history) < 4:
            await interaction.followup.send("Not enough messages to compact.")
            return

        model = await self._get_model(guild_id)
        summary = await self.llm.compact_conversation(history, model=model, focus=focus)
        if not summary:
            await interaction.followup.send("Compaction failed.")
            return

        compacted = [{"role": "system", "content": f"[Compacted conversation summary]\n{summary}"}]
        await self.db.replace_conversation(guild_id, channel_id, user_id, compacted)
        await interaction.followup.send(
            f"✅ Compacted {len(history)} messages into a summary.\n"
            f"Summary preview: {summary[:300]}{'…' if len(summary) > 300 else ''}"
        )

    @app_commands.command(name="convoprompt", description="Set a custom system prompt for your conversation")
    @app_commands.describe(prompt="Custom prompt (leave empty to clear)")
    @app_commands.guild_only()
    async def convoprompt(self, interaction: discord.Interaction, prompt: str | None = None) -> None:
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        key = f"channel_prompt_{interaction.channel.id}"  # type: ignore[union-attr]
        if prompt:
            await self.db.set_guild_config(guild_id, key, prompt)
            await interaction.response.send_message(f"✅ Channel prompt set.", ephemeral=True)
        else:
            await self.db.set_guild_config(guild_id, key, "")
            await interaction.response.send_message("Channel prompt cleared.", ephemeral=True)

    # ==================================================================
    # /draw — Image generation
    # ==================================================================

    @app_commands.command(name="draw", description="Generate an image with AI")
    @app_commands.describe(
        prompt="What to draw",
        size="Image size",
        quality="Image quality",
        style="Image style",
    )
    @app_commands.choices(
        size=[
            app_commands.Choice(name="1024×1024", value="1024x1024"),
            app_commands.Choice(name="1792×1024", value="1792x1024"),
            app_commands.Choice(name="1024×1792", value="1024x1792"),
        ],
        quality=[
            app_commands.Choice(name="Standard", value="standard"),
            app_commands.Choice(name="HD", value="hd"),
        ],
        style=[
            app_commands.Choice(name="Vivid", value="vivid"),
            app_commands.Choice(name="Natural", value="natural"),
        ],
    )
    @app_commands.guild_only()
    async def draw(
        self,
        interaction: discord.Interaction,
        prompt: str,
        size: str = "1024x1024",
        quality: str = "standard",
        style: str = "vivid",
    ) -> None:
        enabled = await self.db.get_guild_config(interaction.guild.id, "assistant_draw_enabled")  # type: ignore[union-attr]
        if enabled == "0":
            await interaction.response.send_message("Image generation is disabled.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        img_model = await self._get_image_model(interaction.guild.id)  # type: ignore[union-attr]
        url = await self.llm.generate_image(prompt, model=img_model, size=size, quality=quality, style=style)
        if url:
            em = discord.Embed(title="🎨 Generated Image", description=prompt[:256], color=discord.Color.purple())
            em.set_image(url=url)
            await interaction.followup.send(embed=em)
        else:
            await interaction.followup.send("⚠️ Image generation failed. Check API key and model support.")

    # ==================================================================
    # /tldr — Channel summarisation
    # ==================================================================

    @app_commands.command(name="tldr", description="Summarise recent channel messages")
    @app_commands.describe(
        count="Number of messages to scan (default 50)",
        question="Ask something specific about the conversation",
        channel="Channel to summarise (default: current)",
        private="Only you can see the result",
    )
    @app_commands.guild_only()
    async def tldr(
        self,
        interaction: discord.Interaction,
        count: int = 50,
        question: str | None = None,
        channel: discord.TextChannel | None = None,
        private: bool = True,
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=private)
        target = channel or interaction.channel
        messages: list[discord.Message] = []
        async for msg in target.history(limit=min(count, 200)):  # type: ignore[union-attr]
            if not msg.author.bot:
                messages.append(msg)
        messages.reverse()

        if not messages:
            await interaction.followup.send("No messages found.")
            return

        text = "\n".join(f"[{m.author.display_name}]: {m.content}" for m in messages if m.content)
        model = await self._get_model(interaction.guild.id)  # type: ignore[union-attr]
        summary = await self.llm.summarise_messages(text, model=model, question=question)
        em = discord.Embed(
            title=f"📋 TLDR — #{getattr(target, 'name', 'channel')}",
            description=summary[:4000],
            color=discord.Color.gold(),
        )
        em.set_footer(text=f"Scanned {len(messages)} messages")
        await interaction.followup.send(embed=em)

    # ==================================================================
    # /embeddings, /query — RAG Knowledge Base
    # ==================================================================

    embed_group = app_commands.Group(
        name="embeddings", description="Manage RAG knowledge base entries",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @embed_group.command(name="add", description="Add a knowledge entry")
    @app_commands.describe(name="Unique name for this entry", text="The knowledge text content")
    async def embed_add(self, interaction: discord.Interaction, name: str, text: str) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        emb_model = await self._get_embedding_model(guild_id)
        try:
            _, packed = await self.llm.create_embedding(text, model=emb_model)
            model = emb_model
        except Exception:
            packed, model = None, None
            logger.warning("Embedding creation failed — storing without vector")

        ok = await self.db.add_embedding(guild_id, name, text, packed, model)
        if ok:
            await interaction.followup.send(f"✅ Embedding **{name}** added.")
        else:
            await interaction.followup.send(f"An embedding named **{name}** already exists. Use `/embeddings update`.")

    @embed_group.command(name="update", description="Update a knowledge entry")
    @app_commands.describe(name="Name of the entry to update", text="New text content")
    async def embed_update(self, interaction: discord.Interaction, name: str, text: str) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        emb_model = await self._get_embedding_model(guild_id)
        try:
            _, packed = await self.llm.create_embedding(text, model=emb_model)
            model = emb_model
        except Exception:
            packed, model = None, None

        ok = await self.db.update_embedding(guild_id, name, text, packed, model)
        msg = f"✅ Updated **{name}**." if ok else f"No embedding named **{name}** found."
        await interaction.followup.send(msg)

    @embed_group.command(name="remove", description="Remove a knowledge entry")
    @app_commands.describe(name="Name of the entry to delete")
    async def embed_remove(self, interaction: discord.Interaction, name: str) -> None:
        ok = await self.db.delete_embedding(interaction.guild.id, name)  # type: ignore[union-attr]
        msg = f"🗑️ Removed **{name}**." if ok else f"No embedding named **{name}**."
        await interaction.response.send_message(msg, ephemeral=True)

    @embed_group.command(name="list", description="List all knowledge entries")
    async def embed_list(self, interaction: discord.Interaction) -> None:
        rows = await self.db.get_all_embeddings(interaction.guild.id)  # type: ignore[union-attr]
        if not rows:
            await interaction.response.send_message("No embeddings stored.", ephemeral=True)
            return
        lines = [f"**{r['name']}** — {len(r['text'])} chars" for r in rows]
        em = discord.Embed(
            title=f"📚 Knowledge Base ({len(rows)} entries)",
            description="\n".join(lines)[:4000],
            color=discord.Color.teal(),
        )
        await interaction.response.send_message(embed=em, ephemeral=True)

    @embed_group.command(name="reset", description="Delete ALL knowledge entries for this server")
    async def embed_reset(self, interaction: discord.Interaction) -> None:
        count = await self.db.reset_embeddings(interaction.guild.id)  # type: ignore[union-attr]
        await self.db.reset_crawl_sources(interaction.guild.id)  # type: ignore[union-attr]
        await interaction.response.send_message(f"🗑️ Deleted {count} embedding(s) and all crawl sources.", ephemeral=True)

    @embed_group.command(name="crawl", description="Fetch a URL, chunk it, and store in the RAG knowledge base")
    @app_commands.describe(
        url="The web page URL to crawl",
        name_prefix="Optional prefix for entry names (default: page title)",
        chunk_size="Characters per chunk (default 800)",
        replace="Replace existing chunks for this URL if already crawled",
    )
    async def embed_crawl(
        self,
        interaction: discord.Interaction,
        url: str,
        name_prefix: str | None = None,
        chunk_size: int = 800,
        replace: bool = True,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild.id  # type: ignore[union-attr]

        crawler = WebCrawler(chunk_size=max(200, min(chunk_size, 4000)))
        result = await crawler.crawl_one(url)

        if result is None:
            await interaction.followup.send("⚠️ Failed to fetch or parse that URL. Check it's publicly accessible HTML.")
            return

        if replace:
            removed = await self.db.delete_embeddings_by_source(guild_id, result.url)
            if removed:
                await self.db.delete_crawl_source(guild_id, result.url)

        _slug = re.sub(r"[^a-z0-9]+", "-", _urlparse(result.url).netloc + _urlparse(result.url).path, flags=re.IGNORECASE).strip("-")[:50]
        prefix = name_prefix or f"{result.title[:30]}|{_slug}" or _slug or "page"
        emb_model = await self._get_embedding_model(guild_id)
        stored = 0
        for i, chunk in enumerate(result.chunks):
            entry_name = f"{prefix} [{i+1}]"
            try:
                _, packed = await self.llm.create_embedding(chunk, model=emb_model)
                model_used = emb_model
            except Exception:
                packed, model_used = None, None
                logger.warning("Embedding creation failed for chunk %d of %s", i, url)

            ok = await self.db.add_embedding(guild_id, entry_name, chunk, packed, model_used, source_url=result.url)
            if not ok:
                await self.db.update_embedding(guild_id, entry_name, chunk, packed, model_used, source_url=result.url)
            stored += 1

        if stored:
            await self.db.upsert_crawl_source(guild_id, result.url, result.title, stored)

        em = discord.Embed(
            title="🌐 URL Crawled",
            color=discord.Color.teal(),
        )
        em.add_field(name="URL", value=result.url, inline=False)
        em.add_field(name="Title", value=result.title[:200])
        em.add_field(name="Chunks stored", value=str(stored))
        em.add_field(name="Embedding model", value=emb_model)
        await interaction.followup.send(embed=em)

    @embed_group.command(name="crawl_site", description="Recursively crawl a site and store all pages in the RAG knowledge base")
    @app_commands.describe(
        url="Starting URL for the crawl",
        max_pages="Maximum pages to crawl (1–20, default 10)",
        chunk_size="Characters per chunk (default 800)",
        same_origin="Only follow links within the same domain (recommended)",
        replace="Replace existing chunks for pages already crawled",
    )
    async def embed_crawl_site(
        self,
        interaction: discord.Interaction,
        url: str,
        max_pages: int = 10,
        chunk_size: int = 800,
        same_origin: bool = True,
        replace: bool = True,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild.id  # type: ignore[union-attr]

        max_pages = max(1, min(max_pages, 20))
        crawler = WebCrawler(
            chunk_size=max(200, min(chunk_size, 4000)),
            max_pages=max_pages,
        )
        emb_model = await self._get_embedding_model(guild_id)

        pages_done = 0
        total_chunks = 0
        errors = 0
        page_summaries: list[str] = []

        async for result in crawler.crawl_site(url, max_pages=max_pages, same_origin_only=same_origin):
            if replace:
                await self.db.delete_embeddings_by_source(guild_id, result.url)
                await self.db.delete_crawl_source(guild_id, result.url)

            _slug = re.sub(r"[^a-z0-9]+", "-", _urlparse(result.url).netloc + _urlparse(result.url).path, flags=re.IGNORECASE).strip("-")[:50]
            prefix = f"{result.title[:30]}|{_slug}" or _slug or "page"
            stored = 0
            for i, chunk in enumerate(result.chunks):
                entry_name = f"{prefix} [{i+1}]"
                try:
                    _, packed = await self.llm.create_embedding(chunk, model=emb_model)
                    model_used = emb_model
                except Exception:
                    packed, model_used = None, None
                    errors += 1

                ok = await self.db.add_embedding(guild_id, entry_name, chunk, packed, model_used, source_url=result.url)
                if not ok:
                    await self.db.update_embedding(guild_id, entry_name, chunk, packed, model_used, source_url=result.url)
                stored += 1

            if stored:
                await self.db.upsert_crawl_source(guild_id, result.url, result.title, stored)
                total_chunks += stored
                pages_done += 1
                page_summaries.append(f"`{result.url[:80]}` — {stored} chunks")

        em = discord.Embed(
            title="🌐 Site Crawl Complete",
            color=discord.Color.teal(),
        )
        em.add_field(name="Starting URL", value=url, inline=False)
        em.add_field(name="Pages indexed", value=str(pages_done))
        em.add_field(name="Total chunks stored", value=str(total_chunks))
        if errors:
            em.add_field(name="Embedding errors", value=str(errors))
        if page_summaries:
            em.add_field(
                name="Pages",
                value="\n".join(page_summaries[:15])[:1024],
                inline=False,
            )
        await interaction.followup.send(embed=em)

    @embed_group.command(name="sources", description="List all crawled URL sources stored in the knowledge base")
    async def embed_sources(self, interaction: discord.Interaction) -> None:
        rows = await self.db.get_crawl_sources(interaction.guild.id)  # type: ignore[union-attr]
        if not rows:
            await interaction.response.send_message("No crawled sources found.", ephemeral=True)
            return
        lines = [
            f"**{r['title'] or 'Untitled'}** — {r['chunk_count']} chunks\n{r['url']}"
            for r in rows
        ]
        em = discord.Embed(
            title=f"🌐 Crawled Sources ({len(rows)})",
            description="\n\n".join(lines)[:4000],
            color=discord.Color.teal(),
        )
        await interaction.response.send_message(embed=em, ephemeral=True)

    @embed_group.command(name="forget", description="Remove all knowledge chunks from a specific crawled URL")
    @app_commands.describe(url="The URL whose chunks should be removed")
    async def embed_forget(
        self, interaction: discord.Interaction, url: str
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        removed = await self.db.delete_embeddings_by_source(guild_id, url)
        await self.db.delete_crawl_source(guild_id, url)
        if removed:
            await interaction.followup.send(f"🗑️ Removed {removed} chunk(s) from `{url}`.")
        else:
            await interaction.followup.send(f"No chunks found for `{url}`.")

    @app_commands.command(name="query", description="Test embedding search — find relevant knowledge")
    @app_commands.describe(query="Search query to test against the knowledge base")
    @app_commands.guild_only()
    async def query(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        emb_model = await self._get_embedding_model(guild_id)
        try:
            query_vec, _ = await self.llm.create_embedding(query, model=emb_model)
        except Exception:
            await interaction.followup.send("⚠️ Failed to create embedding for query.")
            return

        rows = await self.db.get_all_embeddings(interaction.guild.id)  # type: ignore[union-attr]
        scored = []
        for r in rows:
            if not r["embedding"]:
                continue
            vec = self.llm.unpack_embedding(r["embedding"])
            sim = self.llm.similarity(query_vec, vec)
            scored.append((sim, r["name"], r["text"][:100]))
        scored.sort(key=lambda x: x[0], reverse=True)

        if not scored:
            await interaction.followup.send("No embeddings with vectors found.")
            return

        lines = [f"**{name}** — `{sim:.4f}` — {preview}…" for sim, name, preview in scored[:10]]
        em = discord.Embed(title="🔍 Query Results", description="\n".join(lines)[:4000], color=discord.Color.green())
        await interaction.followup.send(embed=em)

    # ==================================================================
    # Custom functions management
    # ==================================================================

    @app_commands.command(name="customfunctions", description="Add a custom function for the AI to call")
    @app_commands.describe(
        name="Function name (no spaces)",
        description="What this function does",
        parameters="JSON schema for parameters",
        code="Python code (must define a function and return a string)",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def customfunctions(
        self,
        interaction: discord.Interaction,
        name: str,
        description: str,
        parameters: str,
        code: str,
    ) -> None:
        try:
            json.loads(parameters)
        except json.JSONDecodeError:
            await interaction.response.send_message("Invalid JSON for parameters.", ephemeral=True)
            return

        ok = await self.db.add_custom_function(interaction.guild.id, name, description, parameters, code)  # type: ignore[union-attr]
        if ok:
            await interaction.response.send_message(f"✅ Function **{name}** added.", ephemeral=True)
        else:
            await interaction.response.send_message(f"Function **{name}** already exists.", ephemeral=True)

    @app_commands.command(name="listfunctions", description="List all custom functions")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def listfunctions(self, interaction: discord.Interaction) -> None:
        fns = await self.db.get_all_functions(interaction.guild.id)  # type: ignore[union-attr]
        if not fns:
            await interaction.response.send_message("No custom functions defined.", ephemeral=True)
            return
        lines = []
        for f in fns:
            status = "✅" if f["enabled"] else "❌"
            lines.append(f"{status} **{f['name']}** — {f['description'][:60]}")
        em = discord.Embed(
            title=f"⚙️ Custom Functions ({len(fns)})",
            description="\n".join(lines)[:4000],
            color=discord.Color.orange(),
        )
        await interaction.response.send_message(embed=em, ephemeral=True)

    @app_commands.command(name="togglefunctions", description="Enable or disable custom functions")
    @app_commands.describe(name="Function name to toggle")
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    async def togglefunctions(self, interaction: discord.Interaction, name: str) -> None:
        result = await self.db.toggle_custom_function(interaction.guild.id, name)  # type: ignore[union-attr]
        if result is None:
            await interaction.response.send_message(f"No function named **{name}**.", ephemeral=True)
        else:
            status = "enabled" if result else "disabled"
            await interaction.response.send_message(f"Function **{name}** is now **{status}**.", ephemeral=True)

    # ==================================================================
    # /assistant — Admin configuration group
    # ==================================================================

    assist_group = app_commands.Group(
        name="assistant", description="Configure the AI assistant",
        default_permissions=discord.Permissions(manage_guild=True),
    )

    @assist_group.command(name="toggle", description="Enable or disable the assistant")
    async def assist_toggle(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        current = await self._is_enabled(guild_id)
        await self._set_setting(guild_id, "enabled", "0" if current else "1")
        status = "disabled" if current else "enabled"
        await interaction.response.send_message(f"Assistant **{status}**.", ephemeral=True)

    @assist_group.command(name="model", description="Set the LLM model")
    @app_commands.describe(model="Model name (e.g. gpt-4o, gpt-3.5-turbo)")
    async def assist_model(self, interaction: discord.Interaction, model: str) -> None:
        await self._set_setting(interaction.guild.id, "model", model)  # type: ignore[union-attr]
        await interaction.response.send_message(f"Model set to **{model}**.", ephemeral=True)

    @assist_group.command(name="temperature", description="Set temperature (0.0–2.0)")
    @app_commands.describe(value="Temperature value")
    async def assist_temperature(self, interaction: discord.Interaction, value: float) -> None:
        value = max(0.0, min(2.0, value))
        await self._set_setting(interaction.guild.id, "temperature", str(value))  # type: ignore[union-attr]
        await interaction.response.send_message(f"Temperature set to **{value}**.", ephemeral=True)

    @assist_group.command(name="maxtokens", description="Set maximum response tokens")
    @app_commands.describe(tokens="Max tokens for responses")
    async def assist_maxtokens(self, interaction: discord.Interaction, tokens: int) -> None:
        tokens = max(64, min(16384, tokens))
        await self._set_setting(interaction.guild.id, "max_tokens", str(tokens))  # type: ignore[union-attr]
        await interaction.response.send_message(f"Max tokens set to **{tokens}**.", ephemeral=True)

    @assist_group.command(name="maxretention", description="Set max conversation messages to retain")
    @app_commands.describe(messages="Max messages per conversation (0 = no retention)")
    async def assist_maxretention(self, interaction: discord.Interaction, messages: int) -> None:
        await self._set_setting(interaction.guild.id, "max_retention", str(max(0, messages)))  # type: ignore[union-attr]
        await interaction.response.send_message(f"Max retention set to **{messages}** messages.", ephemeral=True)

    @assist_group.command(name="prompt", description="Set the system prompt for the assistant")
    @app_commands.describe(prompt="System prompt text (supports {placeholders})")
    async def assist_prompt(self, interaction: discord.Interaction, prompt: str) -> None:
        await self.db.set_guild_config(interaction.guild.id, "assistant_prompt", prompt)  # type: ignore[union-attr]
        await interaction.response.send_message("✅ System prompt updated.", ephemeral=True)

    @assist_group.command(name="channelprompt", description="Set a channel-specific prompt addition")
    @app_commands.describe(
        channel="Target channel",
        prompt="Prompt text to append for this channel (empty to clear)",
    )
    async def assist_channelprompt(
        self, interaction: discord.Interaction, channel: discord.TextChannel, prompt: str = ""
    ) -> None:
        key = f"channel_prompt_{channel.id}"
        await self.db.set_guild_config(interaction.guild.id, key, prompt)  # type: ignore[union-attr]
        msg = f"Channel prompt set for {channel.mention}." if prompt else f"Channel prompt cleared for {channel.mention}."
        await interaction.response.send_message(msg, ephemeral=True)

    @assist_group.command(name="functioncalls", description="Toggle function calling on/off")
    async def assist_functioncalls(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        current = await self._function_calling_enabled(guild_id)
        await self._set_setting(guild_id, "function_calls", "0" if current else "1")
        status = "disabled" if current else "enabled"
        await interaction.response.send_message(f"Function calling **{status}**.", ephemeral=True)

    @assist_group.command(name="toggledraw", description="Toggle image generation on/off")
    async def assist_toggledraw(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        current = await self.db.get_guild_config(guild_id, "assistant_draw_enabled")
        new_val = "0" if current != "0" else "1"
        await self.db.set_guild_config(guild_id, "assistant_draw_enabled", new_val)
        status = "enabled" if new_val == "1" else "disabled"
        await interaction.response.send_message(f"Image generation **{status}**.", ephemeral=True)

    @assist_group.command(name="relatedness", description="Set minimum embedding relatedness (0.0–1.0)")
    @app_commands.describe(value="Minimum similarity score for RAG results")
    async def assist_relatedness(self, interaction: discord.Interaction, value: float) -> None:
        value = max(0.0, min(1.0, value))
        await self._set_setting(interaction.guild.id, "relatedness", str(value))  # type: ignore[union-attr]
        await interaction.response.send_message(f"Relatedness threshold set to **{value}**.", ephemeral=True)

    @assist_group.command(name="listen", description="Toggle this channel as an auto-response channel")
    async def assist_listen(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        channel_id = interaction.channel.id  # type: ignore[union-attr]
        key = f"listen_channel_{channel_id}"
        current = await self.db.get_guild_config(guild_id, key)
        new_val = "" if current == "1" else "1"
        await self.db.set_guild_config(guild_id, key, new_val)
        status = "now listening" if new_val == "1" else "no longer listening"
        await interaction.response.send_message(
            f"Assistant is **{status}** in this channel.", ephemeral=True
        )

    @assist_group.command(name="mention", description="Toggle whether the bot pings users on replies")
    async def assist_mention(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        current = await self.db.get_guild_config(guild_id, "assistant_mention")
        new_val = "0" if current != "0" else "1"
        await self.db.set_guild_config(guild_id, "assistant_mention", new_val)
        status = "enabled" if new_val == "1" else "disabled"
        await interaction.response.send_message(f"Mention on reply **{status}**.", ephemeral=True)

    @assist_group.command(name="trigger", description="Add or remove a trigger phrase (regex)")
    @app_commands.describe(phrase="Regex pattern to match messages against")
    async def assist_trigger(self, interaction: discord.Interaction, phrase: str) -> None:
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        try:
            re.compile(phrase)
        except re.error:
            await interaction.response.send_message("Invalid regex pattern.", ephemeral=True)
            return

        existing = await self.db.get_triggers(guild_id)
        if phrase in existing:
            await self.db.remove_trigger(guild_id, phrase)
            await interaction.response.send_message(f"Removed trigger: `{phrase}`", ephemeral=True)
        else:
            await self.db.add_trigger(guild_id, phrase)
            await interaction.response.send_message(f"Added trigger: `{phrase}`", ephemeral=True)

    @assist_group.command(name="triggerlist", description="View configured trigger phrases")
    async def assist_triggerlist(self, interaction: discord.Interaction) -> None:
        triggers = await self.db.get_triggers(interaction.guild.id)  # type: ignore[union-attr]
        if not triggers:
            await interaction.response.send_message("No triggers configured.", ephemeral=True)
            return
        lines = [f"`{t}`" for t in triggers]
        await interaction.response.send_message("**Trigger phrases:**\n" + "\n".join(lines), ephemeral=True)

    @assist_group.command(name="usage", description="View token usage stats for this server")
    async def assist_usage(self, interaction: discord.Interaction) -> None:
        usage = await self.db.get_guild_usage(interaction.guild.id)  # type: ignore[union-attr]
        em = discord.Embed(title="📊 Token Usage", color=discord.Color.blue())
        em.add_field(name="Prompt tokens", value=f"{usage['prompt_tokens']:,}")
        em.add_field(name="Completion tokens", value=f"{usage['completion_tokens']:,}")
        total = usage["prompt_tokens"] + usage["completion_tokens"]
        em.add_field(name="Total", value=f"{total:,}")
        await interaction.response.send_message(embed=em, ephemeral=True)

    @assist_group.command(name="resetusage", description="Reset token usage stats")
    async def assist_resetusage(self, interaction: discord.Interaction) -> None:
        await self.db.reset_usage(interaction.guild.id)  # type: ignore[union-attr]
        await interaction.response.send_message("Usage stats reset.", ephemeral=True)

    @assist_group.command(name="resetconversations", description="Wipe ALL conversations in this server")
    async def assist_resetconversations(self, interaction: discord.Interaction) -> None:
        # Wipe all conversation_history for this guild
        await self.db.conn.execute(
            "DELETE FROM conversation_history WHERE guild_id = ?",
            (interaction.guild.id,),  # type: ignore[union-attr]
        )
        await self.db.conn.commit()
        await interaction.response.send_message("All conversations wiped.", ephemeral=True)

    @assist_group.command(name="embeddingmodel", description="Set the embedding model for RAG")
    @app_commands.describe(model="Embedding model name (e.g. text-embedding-3-small)")
    async def assist_embeddingmodel(self, interaction: discord.Interaction, model: str) -> None:
        await self._set_setting(interaction.guild.id, "embedding_model", model)  # type: ignore[union-attr]
        await interaction.response.send_message(f"Embedding model → `{model}`", ephemeral=True)

    @assist_group.command(name="imagemodel", description="Set the image generation model")
    @app_commands.describe(model="Image model name (e.g. dall-e-3, dall-e-2)")
    async def assist_imagemodel(self, interaction: discord.Interaction, model: str) -> None:
        await self._set_setting(interaction.guild.id, "image_model", model)  # type: ignore[union-attr]
        await interaction.response.send_message(f"Image model → `{model}`", ephemeral=True)

    @assist_group.command(name="view", description="View current assistant settings")
    async def assist_view(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild.id  # type: ignore[union-attr]
        em = discord.Embed(title="🤖 Assistant Settings", color=discord.Color.blurple())
        em.add_field(name="Enabled", value="Yes" if await self._is_enabled(guild_id) else "No")
        em.add_field(name="Chat model", value=await self._get_model(guild_id))
        em.add_field(name="Embedding model", value=await self._get_embedding_model(guild_id))
        em.add_field(name="Image model", value=await self._get_image_model(guild_id))
        em.add_field(name="Temperature", value=str(await self._get_temperature(guild_id)))
        em.add_field(name="Max tokens", value=str(await self._get_max_tokens(guild_id)))
        em.add_field(name="Max retention", value=str(await self._get_max_retention(guild_id)))
        em.add_field(name="Function calls", value="Yes" if await self._function_calling_enabled(guild_id) else "No")

        draw = await self.db.get_guild_config(guild_id, "assistant_draw_enabled")
        em.add_field(name="Image gen", value="No" if draw == "0" else "Yes")

        rel = await self.db.get_guild_config(guild_id, "assistant_relatedness")
        em.add_field(name="Relatedness", value=rel or "0.3")

        prompt = await self.db.get_guild_config(guild_id, "assistant_prompt")
        em.add_field(name="Custom prompt", value=(prompt[:100] + "…") if prompt else "Default (env)", inline=False)

        triggers = await self.db.get_triggers(guild_id)
        em.add_field(name="Triggers", value=", ".join(f"`{t}`" for t in triggers) if triggers else "None", inline=False)

        embed_count = len(await self.db.get_all_embeddings(guild_id))
        em.add_field(name="Embeddings", value=str(embed_count))

        fn_count = len(await self.db.get_all_functions(guild_id))
        em.add_field(name="Custom functions", value=str(fn_count))

        await interaction.response.send_message(embed=em, ephemeral=True)

    # ==================================================================
    # Auto-response listener (listen channels + triggers + @mentions)
    # ==================================================================

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild or not message.content:
            return
        if not await self._is_enabled(message.guild.id):
            return

        should_respond = False
        guild_id = message.guild.id

        # Check if channel is a listen channel
        listen_key = f"listen_channel_{message.channel.id}"
        if await self.db.get_guild_config(guild_id, listen_key) == "1":
            should_respond = True

        # Check @mention
        if not should_respond and self.bot.user and self.bot.user.mentioned_in(message):
            should_respond = True

        # Check trigger phrases
        if not should_respond:
            triggers = await self.db.get_triggers(guild_id)
            for pattern in triggers:
                try:
                    if re.search(pattern, message.content, re.IGNORECASE):
                        should_respond = True
                        break
                except re.error:
                    continue

        if not should_respond:
            return

        # Deduplicate: ignore if this message is already being processed
        if message.id in self._processing_messages:
            return
        self._processing_messages.add(message.id)

        try:
            # Strip bot mention from content
            content = message.content
            if self.bot.user:
                content = content.replace(f"<@{self.bot.user.id}>", "").replace(f"<@!{self.bot.user.id}>", "").strip()
            if not content:
                return

            async with message.channel.typing():
                result = await self._do_chat(message.guild, message.channel, message.author, content)
        finally:
            self._processing_messages.discard(message.id)

        text = result.get("content", "")
        embeds_data = result.get("embeds", [])
        discord_embeds = [_embed_from_dict(e) for e in embeds_data]

        mention_enabled = await self.db.get_guild_config(guild_id, "assistant_mention")
        prefix = f"{message.author.mention} " if mention_enabled == "1" else ""

        if text:
            for i, chunk in enumerate(_split(text)):
                kwargs: dict[str, Any] = {"content": (prefix + chunk) if i == 0 else chunk}
                if i == 0 and discord_embeds:
                    kwargs["embeds"] = discord_embeds[:10]
                await message.channel.send(**kwargs)
        elif discord_embeds:
            await message.channel.send(content=prefix or None, embeds=discord_embeds[:10])

    # ==================================================================
    # /help_support — Full command reference
    # ==================================================================

    @app_commands.command(name="help_support", description="Show all bot features and commands")
    async def help_support(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(title="🤖 Bot Command Reference", color=discord.Color.blurple())

        embed.add_field(
            name="💬 AI Assistant",
            value=(
                "**/chat** `<question>` — Talk to the AI (per-channel memory)\n"
                "**/ask** — Alias for /chat\n"
                "**/draw** `<prompt>` — Generate an image\n"
                "**/tldr** — Summarise recent channel messages\n"
                "**/convostats** / **/convoclear** / **/convopop** / **/compact**\n"
                "**/convoprompt** — Set per-conversation prompt\n"
                "**/query** — Test RAG knowledge search"
            ),
            inline=False,
        )
        embed.add_field(
            name="📚 Knowledge & Functions",
            value=(
                "**/embeddings add/update/remove/list/reset** — Manage RAG knowledge\n"
                "**/embeddings crawl** `<url>` — Fetch a URL and store it in the RAG\n"
                "**/embeddings crawl_site** `<url>` — Recursively crawl a site (up to 20 pages)\n"
                "**/embeddings sources** — List all crawled URL sources\n"
                "**/embeddings forget** `<url>` — Remove all chunks for a URL\n"
                "**/customfunctions** / **/listfunctions** / **/togglefunctions**"
            ),
            inline=False,
        )
        embed.add_field(
            name="⚙️ Assistant Config (/assistant …)",
            value=(
                "**toggle** · **model** · **temperature** · **maxtokens** · **maxretention**\n"
                "**prompt** · **channelprompt** · **functioncalls** · **toggledraw**\n"
                "**relatedness** · **listen** · **mention** · **trigger** · **triggerlist**\n"
                "**usage** · **resetusage** · **resetconversations** · **view**"
            ),
            inline=False,
        )
        embed.add_field(
            name="🎫 Tickets",
            value=(
                "Click **Open Ticket** on a ticket panel.\n"
                "**/ticket_panel** · **/ticket_category** · **/ticket_close**"
            ),
            inline=False,
        )
        embed.add_field(
            name="🛡️ Moderation",
            value=(
                "**/warn** · **/mute** · **/unmute** · **/kick** · **/ban** · **/unban**\n"
                "**/warnings** · **/clearwarnings** · **/modlog**"
            ),
            inline=False,
        )
        embed.add_field(
            name="🔧 More",
            value=(
                "Admin · Cleanup · Custom Commands · Economy · Reports\n"
                "Utility · Permissions · Auto-Mod · Welcome\n"
                "Use each cog's commands for details."
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed)
