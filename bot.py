"""Lurker purge bot.

Finds members who joined more than N days ago and have never posted a message
anywhere in the server, then (after explicit confirmation) kicks or bans them.

Discord has no "has this user ever posted" API, so the bot builds its own index:
it walks the history of every channel and thread it can read, records each
author, and keeps the index current with a live on_message listener. Scans are
incremental -- after the first full pass only new messages are fetched.
"""

import asyncio
import csv
import io
import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord import app_commands

log = logging.getLogger("purge")


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv(Path(__file__).with_name(".env"))

TOKEN = os.environ.get("DISCORD_TOKEN", "")
GUILD_ID = int(os.environ["GUILD_ID"]) if os.environ.get("GUILD_ID") else None
DB_PATH = os.environ.get("PURGE_DB", str(Path(__file__).with_name("purge.db")))
LOG_PATH = os.environ.get("PURGE_LOG", str(Path(__file__).with_name("purge_log.csv")))
EXEMPT_ROLE_IDS = {int(x) for x in os.environ.get("EXEMPT_ROLE_IDS", "").split(",") if x.strip()}


# --------------------------------------------------------------------------- store


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS posters (
                guild_id INTEGER NOT NULL,
                user_id  INTEGER NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS channel_progress (
                channel_id      INTEGER PRIMARY KEY,
                guild_id        INTEGER NOT NULL,
                last_message_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS scan_state (
                guild_id     INTEGER PRIMARY KEY,
                completed_at TEXT,
                unreadable   TEXT
            );
            """
        )

    def add_posters(self, guild_id: int, user_ids) -> None:
        self.db.executemany(
            "INSERT OR IGNORE INTO posters VALUES (?, ?)", [(guild_id, u) for u in user_ids]
        )
        self.db.commit()

    def posters(self, guild_id: int) -> set[int]:
        rows = self.db.execute("SELECT user_id FROM posters WHERE guild_id = ?", (guild_id,))
        return {r[0] for r in rows}

    def has_posted(self, guild_id: int, user_id: int) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM posters WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ).fetchone()
        return row is not None

    def progress(self, channel_id: int) -> int | None:
        row = self.db.execute(
            "SELECT last_message_id FROM channel_progress WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        return row[0] if row else None

    def set_progress(self, guild_id: int, channel_id: int, last_message_id: int) -> None:
        self.db.execute(
            "INSERT INTO channel_progress VALUES (?, ?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET last_message_id = excluded.last_message_id",
            (channel_id, guild_id, last_message_id),
        )
        self.db.commit()

    def finish_scan(self, guild_id: int, unreadable: list[tuple[int, str]]) -> None:
        self.db.execute(
            "INSERT INTO scan_state VALUES (?, ?, ?) ON CONFLICT(guild_id) DO UPDATE SET "
            "completed_at = excluded.completed_at, unreadable = excluded.unreadable",
            (guild_id, datetime.now(timezone.utc).isoformat(), json.dumps(unreadable)),
        )
        self.db.commit()

    def scan_state(self, guild_id: int) -> tuple[datetime | None, list[tuple[int, str]]]:
        row = self.db.execute(
            "SELECT completed_at, unreadable FROM scan_state WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        if not row:
            return None, []
        return datetime.fromisoformat(row[0]), [tuple(u) for u in json.loads(row[1])]


# --------------------------------------------------------------------------- scanning


def activity_user_ids(msg: discord.Message) -> list[int]:
    """Users who should count as 'having posted' because of this message.

    System messages are skipped: the "X joined the server" message is authored
    by the joining user and would otherwise mark every lurker as a poster.
    Slash-command invocations are credited to the human who ran the command.
    """
    ids = []
    if msg.webhook_id is None and not msg.is_system():
        ids.append(msg.author.id)
    meta = msg.interaction_metadata
    if meta is not None:
        ids.append(meta.user.id)
    return ids


def can_read(channel, me: discord.Member) -> bool:
    perms = channel.permissions_for(me)
    return perms.view_channel and perms.read_message_history


async def collect_targets(guild: discord.Guild) -> tuple[list, list[tuple[int, str]]]:
    """Every channel/thread with message history, plus the ones we can't read.

    Unreadable entries are (gating_channel_id, label): whoever can't view the
    gating channel can't have posted in the unreadable location either.
    """
    me = guild.me
    targets: dict[int, discord.abc.Messageable] = {}
    unreadable: list[tuple[int, str]] = []

    for ch in guild.channels:
        if isinstance(ch, (discord.TextChannel, discord.VoiceChannel, discord.StageChannel)):
            if can_read(ch, me):
                targets[ch.id] = ch
            else:
                unreadable.append((ch.id, f"#{ch.name}"))

        if isinstance(ch, (discord.TextChannel, discord.ForumChannel)):
            if not can_read(ch, me):
                if isinstance(ch, discord.ForumChannel):
                    unreadable.append((ch.id, f"forum #{ch.name}"))
                continue
            try:
                async for t in ch.archived_threads(limit=None):
                    targets[t.id] = t
            except discord.Forbidden:
                unreadable.append((ch.id, f"archived threads in #{ch.name}"))
            if isinstance(ch, discord.TextChannel):
                if ch.permissions_for(me).manage_threads:
                    try:
                        async for t in ch.archived_threads(private=True, limit=None):
                            targets[t.id] = t
                    except discord.Forbidden:
                        unreadable.append((ch.id, f"private archived threads in #{ch.name}"))
                else:
                    unreadable.append((ch.id, f"private archived threads in #{ch.name} (needs Manage Threads)"))

    for t in await guild.active_threads():
        parent = t.parent
        if parent is None or can_read(parent, me):
            targets[t.id] = t

    return list(targets.values()), unreadable


@dataclass
class ScanResult:
    channels: int = 0
    messages: int = 0
    unreadable: list[tuple[int, str]] = field(default_factory=list)


async def scan_guild(store: Store, guild: discord.Guild, progress=None) -> ScanResult:
    """Incrementally index message authors. Resumable: progress is saved per channel."""
    targets, unreadable = await collect_targets(guild)
    result = ScanResult(unreadable=unreadable)

    for i, ch in enumerate(targets, 1):
        last = store.progress(ch.id)
        after = discord.Object(id=last) if last else None
        seen: set[int] = set()
        newest = last
        try:
            async for msg in ch.history(limit=None, after=after, oldest_first=True):
                seen.update(activity_user_ids(msg))
                newest = msg.id
                result.messages += 1
                if result.messages % 1000 == 0:
                    store.add_posters(guild.id, seen)
                    store.set_progress(guild.id, ch.id, newest)
                    seen.clear()
                    if progress:
                        await progress(i, len(targets), result.messages)
        except discord.Forbidden:
            gate = getattr(ch, "parent_id", None) or ch.id
            result.unreadable.append((gate, f"#{getattr(ch, 'name', ch.id)}"))
            continue
        store.add_posters(guild.id, seen)
        if newest:
            store.set_progress(guild.id, ch.id, newest)
        result.channels += 1

    store.finish_scan(guild.id, result.unreadable)
    return result


# --------------------------------------------------------------------------- selection


@dataclass
class Lurkers:
    purgeable: list[discord.Member] = field(default_factory=list)
    # Could view a channel the bot couldn't scan, so may have posted there.
    uncertain: list[discord.Member] = field(default_factory=list)
    # Admins or at/above the bot's top role: the bot can't remove them.
    protected: list[discord.Member] = field(default_factory=list)


def find_lurkers(
    guild: discord.Guild, posters: set[int], days: int, unreadable: list[tuple[int, str]]
) -> Lurkers:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    me = guild.me
    gates = [c for c in (guild.get_channel(cid) for cid, _ in unreadable) if c is not None]
    out = Lurkers()
    for m in guild.members:
        if m.bot or m.id == guild.owner_id or m.id in posters:
            continue
        if m.joined_at is None or m.joined_at > cutoff:
            continue
        if m.premium_since is not None:  # server boosters
            continue
        if EXEMPT_ROLE_IDS & {r.id for r in m.roles}:
            continue
        if m.guild_permissions.administrator or m.top_role >= me.top_role:
            out.protected.append(m)
        elif any(c.permissions_for(m).view_channel for c in gates):
            out.uncertain.append(m)
        else:
            out.purgeable.append(m)
    out.purgeable.sort(key=lambda m: m.joined_at)
    return out


def members_csv(members: list[discord.Member], filename: str) -> discord.File:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["user_id", "username", "display_name", "joined_at", "account_created"])
    for m in members:
        w.writerow([m.id, m.name, m.display_name, m.joined_at.isoformat(), m.created_at.isoformat()])
    return discord.File(io.BytesIO(buf.getvalue().encode()), filename=filename)


# --------------------------------------------------------------------------- bot


class PurgeBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True  # privileged: enable in the developer portal
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.store = Store(DB_PATH)
        self.scan_lock = asyncio.Lock()

    async def setup_hook(self):
        self.tree.add_command(lurkers)
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self):
        log.info("logged in as %s in %d guild(s)", self.user, len(self.guilds))

    async def on_message(self, msg: discord.Message):
        if msg.guild:
            ids = activity_user_ids(msg)
            if ids:
                self.store.add_posters(msg.guild.id, ids)


bot = PurgeBot()
lurkers = app_commands.Group(
    name="lurkers",
    description="Find and remove members who have never posted",
    guild_only=True,
    default_permissions=discord.Permissions(kick_members=True),
)


def describe_unreadable(unreadable: list[tuple[int, str]]) -> str:
    if not unreadable:
        return ""
    labels = [label for _, label in unreadable]
    shown = ", ".join(labels[:15]) + (f" (+{len(labels) - 15} more)" if len(labels) > 15 else "")
    return f"\nCouldn't scan {len(labels)} location(s): {shown}"


def describe_result(found: Lurkers, days: int) -> str:
    msg = f"**{len(found.purgeable)}** member(s) joined >{days} days ago and have never posted."
    if found.uncertain:
        msg += (
            f"\n**{len(found.uncertain)}** more look inactive but can see a channel I couldn't scan, "
            "so they're left alone (pass `include_uncertain:True` to purge to include them)."
        )
    if found.protected:
        msg += f"\n{len(found.protected)} more match but are admins or above my top role, so I can't remove them."
    return msg


async def ensure_indexed(interaction: discord.Interaction) -> list[str] | None:
    """Bring the index up to date. Returns unreadable list, or None if no full scan exists yet."""
    guild = interaction.guild
    completed, _ = bot.store.scan_state(guild.id)
    if completed is None:
        await interaction.followup.send(
            "No full scan yet. Run `/lurkers scan` first (it can take a while on large servers).",
            ephemeral=True,
        )
        return None
    if not guild.chunked:
        await guild.chunk()
    async with bot.scan_lock:
        result = await scan_guild(bot.store, guild)
    return result.unreadable


@lurkers.command(name="scan", description="Index who has posted, across every channel and thread")
async def scan_cmd(interaction: discord.Interaction):
    if bot.scan_lock.locked():
        await interaction.response.send_message("A scan is already running.", ephemeral=True)
        return
    await interaction.response.send_message(
        "Scanning message history. I'll post progress in this channel.", ephemeral=True
    )
    status = await interaction.channel.send("🔎 Lurker scan starting…")

    async def progress(i, total, messages):
        await status.edit(content=f"🔎 Scanning channel {i}/{total} — {messages:,} messages read")

    guild = interaction.guild
    async with bot.scan_lock:
        result = await scan_guild(bot.store, guild, progress)
    if not guild.chunked:
        await guild.chunk()
    summary = scan_summary(guild, bot.store.posters(guild.id), result)
    # The summary goes only to the moderator; lurkers can read the channel too.
    try:
        await interaction.followup.send(summary, ephemeral=True)
        await status.delete()
    except discord.HTTPException:  # interaction token expires after 15 minutes
        await status.edit(content=summary)


def scan_summary(guild: discord.Guild, posters: set[int], result: ScanResult) -> str:
    now = datetime.now(timezone.utc)
    humans = [m for m in guild.members if not m.bot]
    silent = [m for m in humans if m.id not in posters]
    lines = [
        f"✅ **Scan finished.** Read {result.messages:,} new message(s) across "
        f"{result.channels} channel(s)/thread(s).",
        f"👥 **{len(humans):,}** members: **{len(humans) - len(silent):,}** have posted, "
        f"**{len(silent):,}** never have.",
    ]
    if silent:
        buckets = []
        for days in (7, 30, 90, 365):
            n = sum(1 for m in silent if m.joined_at and m.joined_at <= now - timedelta(days=days))
            buckets.append(f"{n:,} joined {days}+ days ago")
        lines.append("Of the ones who never posted: " + ", ".join(buckets) + ".")
        lines.append(
            "**Next:** `/lurkers list days:<number>` shows exactly who (only you see it). "
            "Nobody has been removed."
        )
    return "\n".join(lines) + describe_unreadable(result.unreadable)


@lurkers.command(name="list", description="Dry run: show who would be purged")
@app_commands.describe(days="Minimum days since joining")
async def list_cmd(interaction: discord.Interaction, days: app_commands.Range[int, 1, 3650]):
    await interaction.response.defer(ephemeral=True, thinking=True)
    unreadable = await ensure_indexed(interaction)
    if unreadable is None:
        return
    guild = interaction.guild
    found = find_lurkers(guild, bot.store.posters(guild.id), days, unreadable)
    msg = describe_result(found, days) + describe_unreadable(unreadable)
    files = []
    if found.purgeable:
        files.append(members_csv(found.purgeable, f"lurkers_{days}d.csv"))
    if found.uncertain:
        files.append(members_csv(found.uncertain, f"uncertain_{days}d.csv"))
    await interaction.followup.send(msg, files=files, ephemeral=True)


class ConfirmPurge(discord.ui.View):
    def __init__(self, invoker_id: int):
        super().__init__(timeout=120)
        self.invoker_id = invoker_id
        self.confirmed: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.invoker_id

    @discord.ui.button(label="Yes, purge them", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _):
        self.confirmed = True
        await interaction.response.edit_message(content="Purging…", view=None)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _):
        self.confirmed = False
        await interaction.response.edit_message(content="Cancelled. Nobody was removed.", view=None)
        self.stop()


@lurkers.command(name="purge", description="Kick or ban members who joined long ago and never posted")
@app_commands.describe(
    days="Minimum days since joining",
    action="kick (can rejoin with an invite) or ban (cannot rejoin)",
    reason="Shown in the audit log",
    include_uncertain="Also remove people who can see a channel I couldn't scan (risky)",
)
@app_commands.choices(
    action=[app_commands.Choice(name="kick", value="kick"), app_commands.Choice(name="ban", value="ban")]
)
async def purge_cmd(
    interaction: discord.Interaction,
    days: app_commands.Range[int, 1, 3650],
    action: app_commands.Choice[str],
    reason: str = "Inactive: never posted",
    include_uncertain: bool = False,
):
    perms = interaction.user.guild_permissions
    if action.value == "ban" and not perms.ban_members:
        await interaction.response.send_message("You need Ban Members to ban.", ephemeral=True)
        return
    if action.value == "kick" and not perms.kick_members:
        await interaction.response.send_message("You need Kick Members to kick.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    unreadable = await ensure_indexed(interaction)
    if unreadable is None:
        return
    guild = interaction.guild
    found = find_lurkers(guild, bot.store.posters(guild.id), days, unreadable)
    purgeable = found.purgeable + (found.uncertain if include_uncertain else [])
    if not purgeable:
        await interaction.followup.send("Nobody matches. Nothing to do.", ephemeral=True)
        return

    view = ConfirmPurge(interaction.user.id)
    await interaction.followup.send(
        f"About to **{action.value}** **{len(purgeable)}** member(s). The attached CSV is the full list. "
        f"Reason: `{reason}`\n\n" + describe_result(found, days),
        files=[members_csv(purgeable, f"to_{action.value}_{days}d.csv")],
        view=view,
        ephemeral=True,
    )
    await view.wait()
    if not view.confirmed:
        if view.confirmed is None:
            await interaction.followup.send("Timed out. Nobody was removed.", ephemeral=True)
        return

    audit_reason = f"{reason} (lurker purge by {interaction.user}, >{days}d)"
    done, failed = [], []
    for m in purgeable:
        # Re-check right before acting: they may have posted or left since the list was built.
        if bot.store.has_posted(guild.id, m.id) or guild.get_member(m.id) is None:
            continue
        try:
            if action.value == "ban":
                await guild.ban(m, reason=audit_reason, delete_message_seconds=0)
            else:
                await guild.kick(m, reason=audit_reason)
            done.append(m)
        except discord.HTTPException as e:
            failed.append((m, str(e)))

    write_log(guild, interaction.user, action.value, days, done)
    msg = f"Done. {action.value.capitalize()}ed **{len(done)}** member(s)."
    if failed:
        msg += f"\n{len(failed)} failed: " + ", ".join(f"{m} ({err})" for m, err in failed[:10])
    files = [members_csv(done, f"{action.value}ed_{days}d.csv")] if done else []
    await interaction.followup.send(msg, files=files, ephemeral=True)


def write_log(guild, moderator, action, days, members) -> None:
    new = not Path(LOG_PATH).exists()
    with open(LOG_PATH, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["timestamp", "guild_id", "moderator", "action", "days", "user_id", "username", "joined_at"])
        now = datetime.now(timezone.utc).isoformat()
        for m in members:
            w.writerow([now, guild.id, str(moderator), action, days, m.id, m.name, m.joined_at.isoformat()])


@lurkers.command(name="status", description="Show index status")
async def status_cmd(interaction: discord.Interaction):
    completed, unreadable = bot.store.scan_state(interaction.guild.id)
    if completed is None:
        msg = "No scan yet. Run `/lurkers scan`."
    else:
        msg = (
            f"Last scan finished {discord.utils.format_dt(completed, 'R')}; "
            f"{len(bot.store.posters(interaction.guild.id)):,} distinct posters indexed."
            + (" A scan is running now." if bot.scan_lock.locked() else "")
            + describe_unreadable(unreadable)
        )
    await interaction.response.send_message(msg, ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN (in the environment or a .env file next to bot.py).")
    discord.utils.setup_logging(level=logging.INFO)
    bot.run(TOKEN, log_handler=None)
