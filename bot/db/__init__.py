"""bot.db — modular database package.

``Database`` is the single public interface consumed by all cogs and the
dashboard.  It inherits from ``BaseDatabase`` for connection management and
composes every domain repository so the calling code changes are zero.
"""

from __future__ import annotations

from .base import BaseDatabase
from .schema import SCHEMA as _SCHEMA  # backward-compat alias
from .automod import AutomodRepo
from .community import CommunityRepo
from .custom_commands import CustomCommandsRepo
from .economy import EconomyRepo
from .guild import GuildRepo
from .integrations import IntegrationsRepo
from .mcp import MCPRepo
from .moderation import ModerationRepo
from .permissions import PermissionsRepo
from .reports import ReportsRepo
from .support import SupportRepo
from .tickets import TicketsRepo


class Database(BaseDatabase):
    """Unified database facade.  All methods delegate to domain repositories."""

    # ── Domain repos (initialised in setup()) ─────────────────────────
    _guild: GuildRepo
    _mod: ModerationRepo
    _tickets: TicketsRepo
    _automod: AutomodRepo
    _support: SupportRepo
    _economy: EconomyRepo
    _community: CommunityRepo
    _permissions: PermissionsRepo
    _custom_commands: CustomCommandsRepo
    _reports: ReportsRepo
    _integrations: IntegrationsRepo
    _mcp: MCPRepo

    async def setup(self) -> None:
        await super().setup()
        c = self.conn
        self._guild = GuildRepo(c)
        self._mod = ModerationRepo(c)
        self._tickets = TicketsRepo(c)
        self._automod = AutomodRepo(c)
        self._support = SupportRepo(c)
        self._economy = EconomyRepo(c)
        self._community = CommunityRepo(c)
        self._permissions = PermissionsRepo(c)
        self._custom_commands = CustomCommandsRepo(c)
        self._reports = ReportsRepo(c)
        self._integrations = IntegrationsRepo(c)
        self._mcp = MCPRepo(c)

    # ── Guild config ──────────────────────────────────────────────────

    async def get_guild_config(self, guild_id: int, key: str) -> str | None:
        return await self._guild.get_guild_config(guild_id, key)

    async def get_setting(self, guild_id: int, key: str) -> str:
        return await self._guild.get_setting(guild_id, key)

    async def get_setting_int(self, guild_id: int, key: str) -> int:
        return await self._guild.get_setting_int(guild_id, key)

    async def get_setting_float(self, guild_id: int, key: str) -> float:
        return await self._guild.get_setting_float(guild_id, key)

    async def set_guild_config(self, guild_id: int, key: str, value: str) -> None:
        return await self._guild.set_guild_config(guild_id, key, value)

    # ── Mod cases ─────────────────────────────────────────────────────

    async def add_case(self, guild_id, user_id, moderator_id, action, reason=None, duration=None):
        return await self._mod.add_case(guild_id, user_id, moderator_id, action, reason, duration)

    async def get_cases(self, guild_id, user_id=None, limit=25):
        return await self._mod.get_cases(guild_id, user_id, limit)

    async def get_case_by_id(self, guild_id, case_id):
        return await self._mod.get_case_by_id(guild_id, case_id)

    async def update_case_reason(self, guild_id, case_id, reason):
        return await self._mod.update_case_reason(guild_id, case_id, reason)

    async def count_cases(self, guild_id, user_id=None):
        return await self._mod.count_cases(guild_id, user_id)

    # ── Warnings ──────────────────────────────────────────────────────

    async def add_warning(self, guild_id, user_id, moderator_id, reason):
        return await self._mod.add_warning(guild_id, user_id, moderator_id, reason)

    async def get_active_warnings(self, guild_id, user_id):
        return await self._mod.get_active_warnings(guild_id, user_id)

    async def clear_warnings(self, guild_id, user_id):
        return await self._mod.clear_warnings(guild_id, user_id)

    async def delete_warning(self, guild_id, warning_id):
        return await self._mod.delete_warning(guild_id, warning_id)

    # ── Case notes ────────────────────────────────────────────────────

    async def add_note(self, guild_id, user_id, moderator_id, note):
        return await self._mod.add_note(guild_id, user_id, moderator_id, note)

    async def get_notes(self, guild_id, user_id):
        return await self._mod.get_notes(guild_id, user_id)

    async def delete_note(self, guild_id, note_id):
        return await self._mod.delete_note(guild_id, note_id)

    # ── Tickets ───────────────────────────────────────────────────────

    async def create_ticket(self, guild_id, user_id, channel_id, subject):
        return await self._tickets.create_ticket(guild_id, user_id, channel_id, subject)

    async def get_open_tickets(self, guild_id, user_id=None):
        return await self._tickets.get_open_tickets(guild_id, user_id)

    async def close_ticket(self, ticket_id):
        return await self._tickets.close_ticket(ticket_id)

    async def claim_ticket(self, ticket_id, moderator_id):
        return await self._tickets.claim_ticket(ticket_id, moderator_id)

    async def add_ticket_message(self, ticket_id, user_id, content):
        return await self._tickets.add_ticket_message(ticket_id, user_id, content)

    async def get_ticket_transcript(self, ticket_id):
        return await self._tickets.get_ticket_transcript(ticket_id)

    async def get_ticket_by_channel(self, channel_id):
        return await self._tickets.get_ticket_by_channel(channel_id)

    # ── Auto-mod filters ──────────────────────────────────────────────

    async def add_filter(self, guild_id, filter_type, pattern):
        return await self._automod.add_filter(guild_id, filter_type, pattern)

    async def remove_filter(self, guild_id, filter_type, pattern):
        return await self._automod.remove_filter(guild_id, filter_type, pattern)

    async def get_filters(self, guild_id, filter_type=None):
        return await self._automod.get_filters(guild_id, filter_type)

    # ── Conversation history ───────────────────────────────────────────

    async def add_conversation_message(self, guild_id, channel_id, user_id, role, content, token_count=0):
        return await self._support.add_conversation_message(guild_id, channel_id, user_id, role, content, token_count)

    async def get_conversation_history(self, guild_id, channel_id, user_id, limit=40):
        return await self._support.get_conversation_history(guild_id, channel_id, user_id, limit)

    async def get_conversation_stats(self, guild_id, channel_id, user_id):
        return await self._support.get_conversation_stats(guild_id, channel_id, user_id)

    async def clear_conversation_history(self, guild_id, channel_id, user_id):
        return await self._support.clear_conversation_history(guild_id, channel_id, user_id)

    async def pop_last_conversation_message(self, guild_id, channel_id, user_id):
        return await self._support.pop_last_conversation_message(guild_id, channel_id, user_id)

    async def replace_conversation(self, guild_id, channel_id, user_id, messages):
        return await self._support.replace_conversation(guild_id, channel_id, user_id, messages)

    # ── Embeddings ────────────────────────────────────────────────────

    async def add_embedding(self, guild_id, name, text, embedding, model, source_url=None, qdrant_id=None):
        return await self._support.add_embedding(guild_id, name, text, embedding, model, source_url, qdrant_id)

    async def update_embedding(self, guild_id, name, text, embedding, model, source_url=None, qdrant_id=None):
        return await self._support.update_embedding(guild_id, name, text, embedding, model, source_url, qdrant_id)

    async def delete_embedding(self, guild_id, name):
        return await self._support.delete_embedding(guild_id, name)

    async def get_embedding_by_name(self, guild_id, name):
        return await self._support.get_embedding_by_name(guild_id, name)

    async def get_embedding(self, guild_id, name):
        return await self._support.get_embedding(guild_id, name)

    async def get_all_embeddings(self, guild_id):
        return await self._support.get_all_embeddings(guild_id)

    async def delete_embeddings_by_source(self, guild_id, source_url):
        return await self._support.delete_embeddings_by_source(guild_id, source_url)

    async def reset_embeddings(self, guild_id):
        return await self._support.reset_embeddings(guild_id)

    # ── Crawl sources ─────────────────────────────────────────────────

    async def upsert_crawl_source(self, guild_id, url, title, chunk_count):
        return await self._support.upsert_crawl_source(guild_id, url, title, chunk_count)

    async def get_crawl_sources(self, guild_id):
        return await self._support.get_crawl_sources(guild_id)

    async def delete_crawl_source(self, guild_id, url):
        return await self._support.delete_crawl_source(guild_id, url)

    async def reset_crawl_sources(self, guild_id):
        return await self._support.reset_crawl_sources(guild_id)

    # ── Custom functions ──────────────────────────────────────────────

    async def add_custom_function(self, guild_id, name, description, parameters, code):
        return await self._support.add_custom_function(guild_id, name, description, parameters, code)

    async def delete_custom_function(self, guild_id, name):
        return await self._support.delete_custom_function(guild_id, name)

    async def toggle_custom_function(self, guild_id, name):
        return await self._support.toggle_custom_function(guild_id, name)

    async def get_enabled_functions(self, guild_id):
        return await self._support.get_enabled_functions(guild_id)

    async def get_all_functions(self, guild_id):
        return await self._support.get_all_functions(guild_id)

    # ── Token usage ───────────────────────────────────────────────────

    async def log_token_usage(self, guild_id, user_id, prompt_tokens, completion_tokens):
        return await self._support.log_token_usage(guild_id, user_id, prompt_tokens, completion_tokens)

    async def get_guild_usage(self, guild_id):
        return await self._support.get_guild_usage(guild_id)

    async def get_user_usage(self, guild_id, user_id):
        return await self._support.get_user_usage(guild_id, user_id)

    async def reset_usage(self, guild_id):
        return await self._support.reset_usage(guild_id)

    # ── Assistant triggers ────────────────────────────────────────────

    async def add_trigger(self, guild_id, pattern):
        return await self._support.add_trigger(guild_id, pattern)

    async def remove_trigger(self, guild_id, pattern):
        return await self._support.remove_trigger(guild_id, pattern)

    async def get_triggers(self, guild_id):
        return await self._support.get_triggers(guild_id)

    # ── Economy ───────────────────────────────────────────────────────

    async def ensure_account(self, guild_id, user_id):
        return await self._economy.ensure_account(guild_id, user_id)

    async def get_balance(self, guild_id, user_id):
        return await self._economy.get_balance(guild_id, user_id)

    async def set_balance(self, guild_id, user_id, amount):
        return await self._economy.set_balance(guild_id, user_id, amount)

    async def add_balance(self, guild_id, user_id, amount):
        return await self._economy.add_balance(guild_id, user_id, amount)

    async def transfer_balance(self, guild_id, from_id, to_id, amount):
        return await self._economy.transfer_balance(guild_id, from_id, to_id, amount)

    async def get_last_payday(self, guild_id, user_id):
        return await self._economy.get_last_payday(guild_id, user_id)

    async def set_last_payday(self, guild_id, user_id, ts):
        return await self._economy.set_last_payday(guild_id, user_id, ts)

    async def get_leaderboard(self, guild_id, limit=10):
        return await self._economy.get_leaderboard(guild_id, limit)

    # ── Custom commands ───────────────────────────────────────────────

    async def add_custom_command(self, guild_id, name, response, creator_id):
        return await self._custom_commands.add_custom_command(guild_id, name, response, creator_id)

    async def edit_custom_command(self, guild_id, name, response):
        return await self._custom_commands.edit_custom_command(guild_id, name, response)

    async def delete_custom_command(self, guild_id, name):
        return await self._custom_commands.delete_custom_command(guild_id, name)

    async def get_custom_command(self, guild_id, name):
        return await self._custom_commands.get_custom_command(guild_id, name)

    async def list_custom_commands(self, guild_id):
        return await self._custom_commands.list_custom_commands(guild_id)

    # ── Reports ───────────────────────────────────────────────────────

    async def create_report(self, guild_id, reporter_id, reported_user_id, reason):
        return await self._reports.create_report(guild_id, reporter_id, reported_user_id, reason)

    async def get_open_reports(self, guild_id, limit=25):
        return await self._reports.get_open_reports(guild_id, limit)

    async def resolve_report(self, report_id, resolved_by, note, status="resolved"):
        return await self._reports.resolve_report(report_id, resolved_by, note, status)

    async def get_report(self, report_id):
        return await self._reports.get_report(report_id)

    # ── Self-roles ────────────────────────────────────────────────────

    async def add_selfrole(self, guild_id, role_id):
        return await self._community.add_selfrole(guild_id, role_id)

    async def remove_selfrole(self, guild_id, role_id):
        return await self._community.remove_selfrole(guild_id, role_id)

    async def get_selfroles(self, guild_id):
        return await self._community.get_selfroles(guild_id)

    # ── Permissions ───────────────────────────────────────────────────

    async def set_command_permission(self, guild_id, command, target_type, target_id, allowed):
        return await self._permissions.set_command_permission(guild_id, command, target_type, target_id, allowed)

    async def remove_command_permission(self, guild_id, command, target_type, target_id):
        return await self._permissions.remove_command_permission(guild_id, command, target_type, target_id)

    async def get_command_permissions(self, guild_id, command):
        return await self._permissions.get_command_permissions(guild_id, command)

    async def check_command_allowed(self, guild_id, command, user_id, channel_id, role_ids):
        return await self._permissions.check_command_allowed(guild_id, command, user_id, channel_id, role_ids)

    # ── Leveling / XP ─────────────────────────────────────────────────

    async def get_level_row(self, guild_id, user_id):
        return await self._community.get_level_row(guild_id, user_id)

    async def ensure_level_row(self, guild_id, user_id):
        return await self._community.ensure_level_row(guild_id, user_id)

    async def add_xp(self, guild_id, user_id, amount, last_xp_at):
        return await self._community.add_xp(guild_id, user_id, amount, last_xp_at)

    async def set_level(self, guild_id, user_id, level):
        return await self._community.set_level(guild_id, user_id, level)

    async def set_xp(self, guild_id, user_id, xp, level):
        return await self._community.set_xp(guild_id, user_id, xp, level)

    async def get_level_leaderboard(self, guild_id, limit=10):
        return await self._community.get_level_leaderboard(guild_id, limit)

    async def get_level_rank(self, guild_id, user_id):
        return await self._community.get_level_rank(guild_id, user_id)

    async def reset_levels(self, guild_id):
        return await self._community.reset_levels(guild_id)

    # ── Giveaways ─────────────────────────────────────────────────────

    async def create_giveaway(self, guild_id, channel_id, prize, end_time, winner_count, host_id):
        return await self._community.create_giveaway(guild_id, channel_id, prize, end_time, winner_count, host_id)

    async def set_giveaway_message(self, giveaway_id, message_id):
        return await self._community.set_giveaway_message(giveaway_id, message_id)

    async def get_giveaway(self, giveaway_id):
        return await self._community.get_giveaway(giveaway_id)

    async def get_active_giveaways(self, guild_id=None):
        return await self._community.get_active_giveaways(guild_id)

    async def end_giveaway(self, giveaway_id):
        return await self._community.end_giveaway(giveaway_id)

    async def enter_giveaway(self, giveaway_id, user_id):
        return await self._community.enter_giveaway(giveaway_id, user_id)

    async def leave_giveaway(self, giveaway_id, user_id):
        return await self._community.leave_giveaway(giveaway_id, user_id)

    async def get_giveaway_entries(self, giveaway_id):
        return await self._community.get_giveaway_entries(giveaway_id)

    async def get_giveaway_entry_count(self, giveaway_id):
        return await self._community.get_giveaway_entry_count(giveaway_id)

    # ── Reminders ─────────────────────────────────────────────────────

    async def create_reminder(self, user_id, message, end_time, guild_id=None, channel_id=None):
        return await self._community.create_reminder(user_id, message, end_time, guild_id, channel_id)

    async def get_due_reminders(self, now):
        return await self._community.get_due_reminders(now)

    async def delete_reminder(self, reminder_id):
        return await self._community.delete_reminder(reminder_id)

    async def get_user_reminders(self, user_id):
        return await self._community.get_user_reminders(user_id)

    # ── Starboard ─────────────────────────────────────────────────────

    async def get_starboard_message(self, message_id):
        return await self._community.get_starboard_message(message_id)

    async def upsert_starboard_message(self, message_id, guild_id, channel_id, author_id, star_count, starboard_msg_id=None):
        return await self._community.upsert_starboard_message(message_id, guild_id, channel_id, author_id, star_count, starboard_msg_id)

    async def set_starboard_msg_id(self, message_id, starboard_msg_id):
        return await self._community.set_starboard_msg_id(message_id, starboard_msg_id)

    async def delete_starboard_message(self, message_id):
        return await self._community.delete_starboard_message(message_id)

    # ── Highlights ────────────────────────────────────────────────────

    async def add_highlight(self, user_id, guild_id, keyword):
        return await self._community.add_highlight(user_id, guild_id, keyword)

    async def remove_highlight(self, user_id, guild_id, keyword):
        return await self._community.remove_highlight(user_id, guild_id, keyword)

    async def get_user_highlights(self, user_id, guild_id):
        return await self._community.get_user_highlights(user_id, guild_id)

    async def get_guild_highlights(self, guild_id):
        return await self._community.get_guild_highlights(guild_id)

    async def clear_user_highlights(self, user_id, guild_id):
        return await self._community.clear_user_highlights(user_id, guild_id)

    # ── GitHub integrations ───────────────────────────────────────────

    async def add_github_subscription(self, guild_id, channel_id, repo, events, added_by):
        return await self._integrations.add_github_subscription(guild_id, channel_id, repo, events, added_by)

    async def update_github_subscription_events(self, guild_id, channel_id, repo, events):
        return await self._integrations.update_github_subscription_events(guild_id, channel_id, repo, events)

    async def remove_github_subscription(self, guild_id, channel_id, repo):
        return await self._integrations.remove_github_subscription(guild_id, channel_id, repo)

    async def get_github_subscriptions(self, guild_id):
        return await self._integrations.get_github_subscriptions(guild_id)

    async def get_all_github_subscriptions(self):
        return await self._integrations.get_all_github_subscriptions()

    async def get_github_poll_state(self, repo, event_type):
        return await self._integrations.get_github_poll_state(repo, event_type)

    async def set_github_poll_state(self, repo, event_type, last_id, etag):
        return await self._integrations.set_github_poll_state(repo, event_type, last_id, etag)

    # ── GitLab integrations ───────────────────────────────────────────

    async def add_gitlab_subscription(self, guild_id, channel_id, project, events, added_by):
        return await self._integrations.add_gitlab_subscription(guild_id, channel_id, project, events, added_by)

    async def update_gitlab_subscription_events(self, guild_id, channel_id, project, events):
        return await self._integrations.update_gitlab_subscription_events(guild_id, channel_id, project, events)

    async def remove_gitlab_subscription(self, guild_id, channel_id, project):
        return await self._integrations.remove_gitlab_subscription(guild_id, channel_id, project)

    async def get_gitlab_subscriptions(self, guild_id):
        return await self._integrations.get_gitlab_subscriptions(guild_id)

    async def get_all_gitlab_subscriptions(self):
        return await self._integrations.get_all_gitlab_subscriptions()

    async def get_gitlab_poll_state(self, project, event_type):
        return await self._integrations.get_gitlab_poll_state(project, event_type)

    async def set_gitlab_poll_state(self, project, event_type, last_id):
        return await self._integrations.set_gitlab_poll_state(project, event_type, last_id)

    # ── Learned facts ─────────────────────────────────────────────────

    async def add_learned_fact(self, guild_id, fact, embedding, model, qdrant_id=None, source="conversation", confidence=1.0, approved=True):
        return await self._support.add_learned_fact(guild_id, fact, embedding, model, qdrant_id, source, confidence, approved)

    async def get_learned_fact(self, guild_id, fact_id):
        return await self._support.get_learned_fact(guild_id, fact_id)

    async def get_learned_facts(self, guild_id, approved_only=True):
        return await self._support.get_learned_facts(guild_id, approved_only)

    async def delete_learned_fact(self, guild_id, fact_id):
        return await self._support.delete_learned_fact(guild_id, fact_id)

    async def set_fact_approval(self, guild_id, fact_id, approved):
        return await self._support.set_fact_approval(guild_id, fact_id, approved)

    async def reset_learned_facts(self, guild_id):
        return await self._support.reset_learned_facts(guild_id)

    async def count_learned_facts(self, guild_id):
        return await self._support.count_learned_facts(guild_id)

    async def has_learned_message_mark(self, guild_id, message_id):
        return await self._support.has_learned_message_mark(guild_id, message_id)

    async def add_learned_message_mark(self, guild_id, channel_id, message_id, author_id, marked_by):
        return await self._support.add_learned_message_mark(guild_id, channel_id, message_id, author_id, marked_by)

    # ── Response feedback ─────────────────────────────────────────────

    async def add_feedback(self, guild_id, channel_id, user_id, message_id, rating, user_input=None, bot_response=None):
        return await self._support.add_feedback(guild_id, channel_id, user_id, message_id, rating, user_input, bot_response)

    async def get_feedback_stats(self, guild_id):
        return await self._support.get_feedback_stats(guild_id)

    async def get_negative_feedback(self, guild_id, limit=20):
        return await self._support.get_negative_feedback(guild_id, limit)

    async def reset_feedback(self, guild_id):
        return await self._support.reset_feedback(guild_id)

    # ── Prompt templates ──────────────────────────────────────────────

    async def save_prompt_template(self, guild_id, name, content, created_by):
        return await self._support.save_prompt_template(guild_id, name, content, created_by)

    async def get_prompt_template(self, guild_id, name):
        return await self._support.get_prompt_template(guild_id, name)

    async def list_prompt_templates(self, guild_id):
        return await self._support.list_prompt_templates(guild_id)

    async def delete_prompt_template(self, guild_id, name):
        return await self._support.delete_prompt_template(guild_id, name)

    # ── MCP servers ───────────────────────────────────────────────────

    async def add_mcp_server(self, guild_id, name, transport, command, args, env, url):
        return await self._mcp.add_mcp_server(guild_id, name, transport, command, args, env, url)

    async def remove_mcp_server(self, guild_id, name):
        return await self._mcp.remove_mcp_server(guild_id, name)

    async def get_mcp_servers(self, guild_id, enabled_only=False):
        return await self._mcp.get_mcp_servers(guild_id, enabled_only)

    async def get_mcp_server(self, guild_id, name):
        return await self._mcp.get_mcp_server(guild_id, name)

    async def toggle_mcp_server(self, guild_id, name):
        return await self._mcp.toggle_mcp_server(guild_id, name)

    async def update_mcp_server(self, guild_id, name, *, transport=None, command=None, args=None, env=None, url=None):
        return await self._mcp.update_mcp_server(guild_id, name, transport=transport, command=command, args=args, env=env, url=url)
