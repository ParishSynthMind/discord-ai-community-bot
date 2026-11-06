"""The Discord side: a discord.py client with six slash commands, registered to one
server. Intents are kept to the minimum: GUILDS only, plus GUILD_MEMBERS when new-member
events are switched on. Slash commands don't need the Message Content intent, so the
bot never reads members' chat."""
import logging
from typing import Optional

import discord
import yarl
from discord import app_commands

from .community import Community

log = logging.getLogger("community_bot.bot")
NO_PINGS = discord.AllowedMentions.none()


def point_at(api_base: str, gateway_url: str):
    """Send discord.py's REST and gateway traffic somewhere else. Only the tests and the
    local demo use this, to talk to fake_discord instead of discord.com."""
    if api_base:
        discord.http.Route.BASE = api_base.rstrip("/")
    if gateway_url:
        discord.gateway.DiscordWebSocket.DEFAULT_GATEWAY = yarl.URL(gateway_url)


def intents_for(settings) -> discord.Intents:
    intents = discord.Intents.none()
    intents.guilds = True
    intents.members = settings.members_intent
    return intents


class CommunityBot(discord.Client):
    def __init__(self, community: Community, **kw):
        super().__init__(intents=intents_for(community.s), allowed_mentions=NO_PINGS, **kw)
        self.c = community
        self.tree = app_commands.CommandTree(self)
        self.guild_obj = discord.Object(id=int(community.s.guild_id))
        self.ready_seen = False
        register_commands(self)

    async def setup_hook(self):
        # Guild commands appear at once; global ones can take a while to spread.
        synced = await self.tree.sync(guild=self.guild_obj)
        log.info("synced %d slash commands to guild %s", len(synced), self.c.s.guild_id)

    async def on_ready(self):
        self.ready_seen = True
        log.info("connected as %s", self.user)

    async def on_member_join(self, member: discord.Member):
        if str(member.guild.id) == self.c.s.guild_id:
            await self.c.member_joined(str(member.id), member.name)
            await self.c.hooks.flush()

    async def on_raw_member_remove(self, payload: discord.RawMemberRemoveEvent):
        if str(payload.guild_id) == self.c.s.guild_id:
            await self.c.member_left(str(payload.user.id))
            await self.c.hooks.flush()


def _who(interaction: discord.Interaction):
    return str(interaction.user.id), interaction.user.name


def register_commands(bot: CommunityBot):
    c = bot.c
    guild = bot.guild_obj

    @bot.tree.command(name="ask", description="Ask a question about the server", guild=guild)
    @app_commands.describe(question="What do you want to know?")
    async def ask(interaction: discord.Interaction, question: app_commands.Range[str, 3, 1000]):
        uid, name = _who(interaction)
        c.store.remember_member(uid, name)
        # Discord drops an interaction not answered within 3 seconds. A model call can take
        # longer, so acknowledge first ("thinking...") and send the answer as a follow-up.
        await interaction.response.defer(thinking=True, ephemeral=not c.s.ai.public_answers)
        result = await c.ask_service.ask(uid, question)
        reply = result.reply
        if result.handover:
            tid = await c.open_ticket(uid, name, question[:80], question, f"/ask ({result.status})")
            reply += f" (ticket #{tid})"
        await interaction.followup.send(reply[:2000], allowed_mentions=NO_PINGS)
        await c.hooks.flush()

    @bot.tree.command(name="ticket", description="Open a ticket for the moderators", guild=guild)
    @app_commands.describe(subject="A few words", details="What's going on?")
    async def ticket(interaction: discord.Interaction, subject: app_commands.Range[str, 3, 120],
                     details: app_commands.Range[str, 1, 1500]):
        uid, name = _who(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        tid = await c.open_ticket(uid, name, subject, details, "/ticket")
        await interaction.followup.send(f"Ticket #{tid} is open. A moderator will reply here or by DM.",
                                        ephemeral=True)
        await c.hooks.flush()

    @bot.tree.command(name="premium", description="Your Premium membership, or how to get it", guild=guild)
    async def premium(interaction: discord.Interaction):
        uid, name = _who(interaction)
        c.store.remember_member(uid, name)
        await interaction.response.defer(ephemeral=True, thinking=True)
        link = c.store.link_for_user(uid)
        if link and c.membership.wants_role(link):
            outcome = await c.membership.sync_member(uid, actor="/premium")
            extra = " Your Premium role was missing and has been put back." if outcome == "added" else ""
            note = {"canceling": " It is set to end at the close of this billing period.",
                    "past_due": " Your last payment failed: please update your card to keep it."}.get(link["status"], "")
            await interaction.followup.send(f"You have Premium.{note}{extra}", ephemeral=True)
            return
        url = c.s.checkout_url.format(discord_user_id=uid) if c.s.checkout_url else ""
        msg = "You don't have Premium yet."
        if url:
            msg += f" Join here: {url}\nThe link is tied to your Discord account, so the role arrives by itself."
        await interaction.followup.send(msg, ephemeral=True)

    @bot.tree.command(name="contact", description="Leave your email for the team (sales or partnerships)",
                      guild=guild)
    @app_commands.describe(email="Where the team can reach you", message="What would you like to talk about?")
    async def contact(interaction: discord.Interaction, email: app_commands.Range[str, 5, 254],
                      message: app_commands.Range[str, 1, 1500]):
        uid, name = _who(interaction)
        await interaction.response.defer(ephemeral=True, thinking=True)
        problem = await c.capture_lead(uid, name, email, message)
        await interaction.followup.send(problem or "Thanks, the team has your details and will be in touch.",
                                        ephemeral=True)
        await c.hooks.flush()

    @bot.tree.command(name="book", description="Book a call with the team", guild=guild)
    async def book(interaction: discord.Interaction):
        uid, _ = _who(interaction)
        if not c.s.booking_url:
            await interaction.response.send_message("Bookings aren't open right now.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Pick a time here: {c.s.booking_url.format(discord_user_id=uid)}\n"
            "The link is tied to your Discord account, so you'll get the Call booked role when it's confirmed.",
            ephemeral=True)

    @bot.tree.command(name="ticket-close", description="Close a ticket (moderators)", guild=guild)
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(ticket_id="The ticket number")
    async def ticket_close(interaction: discord.Interaction, ticket_id: int):
        perms = getattr(interaction, "permissions", None)
        if not (perms and (perms.manage_messages or perms.administrator)):
            await interaction.response.send_message("Only moderators can close tickets.", ephemeral=True)
            return
        uid, name = _who(interaction)
        t = await c.close_ticket(ticket_id, name)
        await interaction.response.send_message(
            f"Ticket #{ticket_id} closed." if t else f"Ticket #{ticket_id} isn't open.", ephemeral=True)
        await c.hooks.flush()


async def run_bot(community: Community, token: str, bot: Optional[CommunityBot] = None):
    point_at(community.s.api_base if "discord.com" not in community.s.api_base else "", community.s.gateway_url)
    bot = bot or CommunityBot(community)
    async with bot:
        await bot.start(token)
