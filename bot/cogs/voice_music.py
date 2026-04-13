"""Voice utilities and music playback.

Playback resolves audio via yt-dlp (YouTube, SoundCloud, direct URLs, etc.) and
streams through FFmpeg → Opus. Requires FFmpeg on PATH.

Bot needs Connect + Speak in the destination channel (invite URL in main.py).
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

import yt_dlp

if TYPE_CHECKING:
    from bot.db import Database

logger = logging.getLogger(__name__)
_YTDL_OPTS: dict = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "auto",
    "extract_flat": False,
    "socket_timeout": 20,
}


@dataclass
class Track:
    title: str
    webpage_url: str
    stream_url: str
    duration: int | None
    requester_id: int


@dataclass
class GuildMusicState:
    guild_id: int
    voice: discord.VoiceClient | None = None
    queue: list[Track] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    volume: float = 1.0
    current: Track | None = None
    inactivity_task: asyncio.Task | None = None

    def cancel_inactivity(self) -> None:
        if self.inactivity_task and not self.inactivity_task.done():
            self.inactivity_task.cancel()
        self.inactivity_task = None


def _sanitize_volume(value: int) -> float:
    return max(0.0, min(200, value)) / 100.0


def _blocking_extract(query: str) -> Track | None:
    """Run inside asyncio.to_thread — returns None if nothing playable."""
    q = query.strip()
    if not q:
        return None
    if not re.match(r"https?://", q, re.I):
        q = f"ytsearch1:{q}"

    with yt_dlp.YoutubeDL(_YTDL_OPTS) as ydl:
        info = ydl.extract_info(q, download=False)

    if info is None:
        return None

    if info.get("_type") == "playlist" and info.get("entries"):
        info = next((e for e in info["entries"] if e), None)
        if info is None:
            return None

    if "entries" in info and info["entries"]:
        info = info["entries"][0]
    if not isinstance(info, dict):
        return None

    title = info.get("title") or info.get("id") or "Unknown track"
    webpage = info.get("webpage_url") or info.get("url") or ""
    duration = info.get("duration")
    if isinstance(duration, float):
        duration = int(duration)

    stream_url = info.get("url")
    if not stream_url and info.get("formats"):
        for f in reversed(info["formats"]):
            if f.get("url") and f.get("acodec") and f["acodec"] != "none":
                stream_url = f["url"]
                break
    if not stream_url:
        return None

    return Track(
        title=str(title)[:256],
        webpage_url=str(webpage)[:512],
        stream_url=stream_url,
        duration=duration,
        requester_id=0,
    )


class VoiceMusicCog(commands.Cog, name="Voice & Music"):
    """Music queue + basic voice-channel moderation."""

    def __init__(self, bot: commands.Bot, db: "Database") -> None:
        self.bot = bot
        self.db = db
        self._music: dict[int, GuildMusicState] = {}

    async def _read_bounded_int(self, guild_id: int, key: str, default: int, lo: int, hi: int) -> int:
        raw = await self.db.get_guild_config(guild_id, key)
        if raw is None:
            return default
        try:
            return max(lo, min(hi, int(raw)))
        except ValueError:
            return default

    async def _music_module_enabled(self, guild_id: int) -> bool:
        v = await self.db.get_guild_config(guild_id, "music_enabled")
        return v != "0"

    async def _max_queue(self, guild_id: int) -> int:
        return await self._read_bounded_int(guild_id, "music_max_queue", 50, 5, 100)

    async def _inactivity_seconds(self, guild_id: int) -> int:
        minutes = await self._read_bounded_int(guild_id, "music_inactivity_minutes", 3, 1, 60)
        return minutes * 60

    async def _ensure_state(self, guild_id: int) -> GuildMusicState:
        st = self._music.get(guild_id)
        if st is None:
            vol_pct = await self._read_bounded_int(guild_id, "music_default_volume", 100, 0, 200)
            st = GuildMusicState(guild_id=guild_id, volume=_sanitize_volume(vol_pct))
            self._music[guild_id] = st
        return st

    async def _ensure_music_allowed(self, interaction: discord.Interaction) -> bool:
        gid = interaction.guild_id
        if gid is None:
            return True
        if await self._music_module_enabled(gid):
            return True
        msg = "Music is disabled for this server. Ask a server admin to enable it in the dashboard under **Voice & Music**."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return False

    async def cog_unload(self) -> None:
        for st in list(self._music.values()):
            st.cancel_inactivity()
            if st.voice and st.voice.is_connected():
                await st.voice.disconnect(force=True)
        self._music.clear()

    # ------------------------------------------------------------------
    # Inactivity: leave voice after idle when alone or queue empty
    # ------------------------------------------------------------------

    async def _inactivity_leave(self, guild_id: int) -> None:
        try:
            delay = await self._inactivity_seconds(guild_id)
            await asyncio.sleep(delay)
            st = self._music.get(guild_id)
            if not st or not st.voice or not st.voice.is_connected():
                return
            if st.voice.is_playing() or st.voice.is_paused():
                return
            if st.queue or st.current:
                return
            ch = st.voice.channel
            humans = [m for m in ch.members if not m.bot] if ch else []
            if len(humans) == 0:
                await st.voice.disconnect(force=True)
                st.voice = None
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("inactivity_leave guild=%s", guild_id)

    def _schedule_inactivity(self, guild_id: int) -> None:
        st = self._music.get(guild_id)
        if not st:
            return
        st.cancel_inactivity()
        st.inactivity_task = asyncio.create_task(self._inactivity_leave(guild_id))

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.guild is None:
            return
        st = self._music.get(member.guild.id)
        if not st or not st.voice or not st.voice.is_connected():
            return
        if st.voice.channel != before.channel and st.voice.channel != after.channel:
            return
        ch = st.voice.channel
        if ch is None:
            return
        humans = [m for m in ch.members if not m.bot]
        if not humans and not st.voice.is_playing() and not st.voice.is_paused() and not st.queue:
            self._schedule_inactivity(member.guild.id)
        else:
            st.cancel_inactivity()

    # ------------------------------------------------------------------
    # Playback internals
    # ------------------------------------------------------------------

    def _after_play(self, guild_id: int, error: BaseException | None) -> None:
        if error:
            logger.error("Music playback error guild=%s: %s", guild_id, error)
        asyncio.run_coroutine_threadsafe(self._advance(guild_id), self.bot.loop)

    async def _advance(self, guild_id: int) -> None:
        st = self._music.get(guild_id)
        if not st:
            return
        async with st.lock:
            st.current = None
        await self._start_playback(guild_id)

    async def _start_playback(self, guild_id: int) -> None:
        st = self._music.get(guild_id)
        if not st:
            return
        if not st.voice or not st.voice.is_connected():
            return
        async with st.lock:
            if st.voice.is_playing() or st.voice.is_paused():
                return
            if not st.queue:
                self._schedule_inactivity(guild_id)
                return
            track = st.queue.pop(0)
            st.current = track

        before = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
        try:
            audio = discord.FFmpegOpusAudio(
                track.stream_url,
                before_options=before,
                options="-vn",
            )
            wrapped = discord.PCMVolumeTransformer(audio, volume=st.volume)

            def _after(err: BaseException | None) -> None:
                self._after_play(guild_id, err)

            st.voice.play(wrapped, after=_after)
        except Exception as e:
            logger.exception("Failed to start playback: %s", e)
            st.current = None
            await self._start_playback(guild_id)

    async def _connect_user_channel(
        self, interaction: discord.Interaction
    ) -> tuple[GuildMusicState, discord.VoiceChannel] | tuple[None, None]:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return None, None
        member = interaction.user
        vs = member.voice
        if not vs or not vs.channel:
            return None, None
        ch = vs.channel
        if not isinstance(ch, discord.VoiceChannel):
            return None, None

        st = await self._ensure_state(interaction.guild.id)
        perms = ch.permissions_for(interaction.guild.me)
        if not perms.connect or not perms.speak:
            return None, None

        if st.voice and st.voice.is_connected():
            if st.voice.channel != ch:
                await st.voice.move_to(ch)
        else:
            st.voice = await ch.connect(self_deaf=True)

        st.cancel_inactivity()
        return st, ch

    # ------------------------------------------------------------------
    # /music …
    # ------------------------------------------------------------------

    music = app_commands.Group(
        name="music",
        description="Play music in a voice channel (requires FFmpeg on the host)",
        guild_only=True,
    )

    @music.command(name="join", description="Join your current voice channel")
    async def music_join(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_music_allowed(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        u = interaction.user
        uv = u.voice if isinstance(u, discord.Member) else None  # type: ignore[union-attr]
        if uv and uv.channel and not isinstance(uv.channel, discord.VoiceChannel):
            await interaction.followup.send(
                "Music playback uses normal **voice channels** only (not stage channels).",
                ephemeral=True,
            )
            return
        st, ch = await self._connect_user_channel(interaction)
        if st is None or ch is None:
            if interaction.guild:
                me = interaction.guild.me
                ch2 = u.voice.channel if isinstance(u, discord.Member) and u.voice else None  # type: ignore[union-attr]
                if isinstance(ch2, discord.VoiceChannel):
                    p = ch2.permissions_for(me)
                    if not p.connect or not p.speak:
                        await interaction.followup.send(
                            "I need **Connect** and **Speak** in that voice channel.", ephemeral=True
                        )
                        return
            await interaction.followup.send(
                "Join a **voice channel** first (stage channels are not supported for playback).",
                ephemeral=True,
            )
            return
        await interaction.followup.send(f"Joined {ch.mention}.", ephemeral=True)

    @music.command(name="play", description="Add a track to the queue (URL or search words)")
    @app_commands.describe(query="Link or search query")
    async def music_play(self, interaction: discord.Interaction, query: str) -> None:
        if not await self._ensure_music_allowed(interaction):
            return
        await interaction.response.defer()
        u = interaction.user
        uv = u.voice if isinstance(u, discord.Member) else None  # type: ignore[union-attr]
        if uv and uv.channel and not isinstance(uv.channel, discord.VoiceChannel):
            await interaction.followup.send(
                "Use a normal **voice channel** for music (stage channels are not supported).",
                ephemeral=True,
            )
            return
        st, _ = await self._connect_user_channel(interaction)
        if st is None:
            await interaction.followup.send(
                "Join a voice channel I can **Connect** and **Speak** in, then try again.",
                ephemeral=True,
            )
            return

        try:
            track = await asyncio.to_thread(_blocking_extract, query)
        except Exception as e:
            logger.info("yt-dlp extract failed: %s", e)
            await interaction.followup.send(
                f"Could not resolve that source: `{e}`",
                ephemeral=True,
            )
            return

        if track is None:
            await interaction.followup.send("No playable audio found for that query.", ephemeral=True)
            return

        track = Track(
            title=track.title,
            webpage_url=track.webpage_url,
            stream_url=track.stream_url,
            duration=track.duration,
            requester_id=interaction.user.id,
        )

        max_q = await self._max_queue(interaction.guild.id)  # type: ignore[union-attr]
        async with st.lock:
            if len(st.queue) >= max_q:
                await interaction.followup.send(f"Queue is full (max {max_q}).", ephemeral=True)
                return
            st.queue.append(track)

        pos = len(st.queue)
        dur = f"{track.duration // 60}:{track.duration % 60:02d}" if track.duration else "?"
        await interaction.followup.send(
            f"Added **{track.title}** (`{dur}`) — position **{pos}** in queue.",
            ephemeral=False,
        )

        if st.voice and not st.voice.is_playing() and not st.voice.is_paused():
            await self._start_playback(interaction.guild.id)  # type: ignore[arg-type]

    @music.command(name="pause", description="Pause playback")
    async def music_pause(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_music_allowed(interaction):
            return
        st = await self._ensure_state(interaction.guild_id)  # type: ignore[arg-type]
        if not st.voice or not st.voice.is_playing():
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        st.voice.pause()
        await interaction.response.send_message("Paused.", ephemeral=True)

    @music.command(name="resume", description="Resume playback")
    async def music_resume(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_music_allowed(interaction):
            return
        st = await self._ensure_state(interaction.guild_id)  # type: ignore[arg-type]
        if not st.voice or not st.voice.is_paused():
            await interaction.response.send_message("Nothing is paused.", ephemeral=True)
            return
        st.voice.resume()
        await interaction.response.send_message("Resumed.", ephemeral=True)

    @music.command(name="skip", description="Skip the current track")
    async def music_skip(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_music_allowed(interaction):
            return
        st = await self._ensure_state(interaction.guild_id)  # type: ignore[arg-type]
        if not st.voice or not st.voice.is_playing():
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        st.voice.stop()
        await interaction.response.send_message("Skipped.", ephemeral=True)

    @music.command(name="stop", description="Stop and clear the queue")
    async def music_stop(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_music_allowed(interaction):
            return
        st = await self._ensure_state(interaction.guild_id)  # type: ignore[arg-type]
        async with st.lock:
            st.queue.clear()
            st.current = None
        if st.voice and (st.voice.is_playing() or st.voice.is_paused()):
            st.voice.stop()
        await interaction.response.send_message("Stopped and cleared the queue.", ephemeral=True)

    @music.command(name="leave", description="Disconnect from voice")
    async def music_leave(self, interaction: discord.Interaction) -> None:
        # Allowed even when the music module is disabled so the bot can be cleared from voice.
        gid = interaction.guild_id  # type: ignore[assignment]
        st = await self._ensure_state(gid)
        st.cancel_inactivity()
        async with st.lock:
            st.queue.clear()
            st.current = None
        if st.voice and st.voice.is_connected():
            await st.voice.disconnect(force=True)
        st.voice = None
        await interaction.response.send_message("Disconnected.", ephemeral=True)

    @music.command(name="queue", description="Show the current queue")
    async def music_queue(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_music_allowed(interaction):
            return
        st = await self._ensure_state(interaction.guild_id)  # type: ignore[arg-type]
        lines: list[str] = []
        if st.current:
            lines.append(f"**Now:** {st.current.title}")
        for i, t in enumerate(st.queue[:15], start=1):
            lines.append(f"`{i}.` {t.title}")
        if len(st.queue) > 15:
            lines.append(f"*…and {len(st.queue) - 15} more*")
        if not lines:
            await interaction.response.send_message("Queue is empty.", ephemeral=True)
            return
        em = discord.Embed(title="Music queue", description="\n".join(lines), color=discord.Color.blurple())
        await interaction.response.send_message(embed=em, ephemeral=True)

    @music.command(name="nowplaying", description="Show the track that is playing")
    async def music_np(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_music_allowed(interaction):
            return
        st = await self._ensure_state(interaction.guild_id)  # type: ignore[arg-type]
        if not st.current:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)
            return
        t = st.current
        extra = ""
        if t.webpage_url:
            extra = f"\n{t.webpage_url}"
        dur = f"{t.duration // 60}:{t.duration % 60:02d}" if t.duration else "unknown length"
        await interaction.response.send_message(
            f"**{t.title}** (`{dur}`) — requested by <@{t.requester_id}>{extra}",
            ephemeral=True,
        )

    @music.command(name="volume", description="Set playback volume (0–200%)")
    @app_commands.describe(percent="Volume percentage (default 100)")
    async def music_volume(self, interaction: discord.Interaction, percent: int = 100) -> None:
        if not await self._ensure_music_allowed(interaction):
            return
        st = await self._ensure_state(interaction.guild_id)  # type: ignore[arg-type]
        st.volume = _sanitize_volume(percent)
        src = st.voice.source if st.voice else None
        if isinstance(src, discord.PCMVolumeTransformer):
            src.volume = st.volume
        await interaction.response.send_message(f"Volume set to **{int(st.volume * 100)}%**.", ephemeral=True)

    # ------------------------------------------------------------------
    # /voice …
    # ------------------------------------------------------------------

    voice = app_commands.Group(name="voice", description="Voice channel utilities", guild_only=True)

    @voice.command(name="move", description="Move a member to another voice channel")
    @app_commands.describe(member="Member to move", channel="Destination voice channel")
    @app_commands.default_permissions(move_members=True)
    @app_commands.checks.bot_has_permissions(move_members=True)
    async def voice_move(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        channel: discord.VoiceChannel,
    ) -> None:
        if not interaction.guild:
            return
        if not member.voice or not member.voice.channel:
            await interaction.response.send_message("That member is not in a voice channel.", ephemeral=True)
            return
        await member.move_to(channel, reason=f"Moved by {interaction.user}")
        await interaction.response.send_message(
            f"Moved {member.mention} → {channel.mention}.",
            ephemeral=True,
        )

    @voice.command(name="disconnect", description="Disconnect a member from voice")
    @app_commands.describe(member="Member to disconnect from voice")
    @app_commands.default_permissions(move_members=True)
    @app_commands.checks.bot_has_permissions(move_members=True)
    async def voice_disconnect(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if not member.voice or not member.voice.channel:
            await interaction.response.send_message("That member is not in voice channel.", ephemeral=True)
            return
        await member.move_to(None, reason=f"Disconnected by {interaction.user}")
        await interaction.response.send_message(f"Disconnected {member.mention} from voice.", ephemeral=True)

    @voice.command(name="members", description="List members in a voice channel")
    @app_commands.describe(channel="Voice channel to inspect")
    async def voice_members(self, interaction: discord.Interaction, channel: discord.VoiceChannel) -> None:
        names = [m.mention for m in channel.members] if channel.members else ["*(empty)*"]
        em = discord.Embed(
            title=f"Members in #{channel.name}",
            description="\n".join(names[:50]),
            color=discord.Color.green(),
        )
        if len(names) > 50:
            em.set_footer(text=f"Showing 50 of {len(names)}")
        await interaction.response.send_message(embed=em, ephemeral=True)
