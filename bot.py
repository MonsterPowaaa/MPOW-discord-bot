"""
Territorial.io Clan Wins Bot  —  [MPOW]
---------------------------------------
Watches the official Territorial.io team-games log. Every time the clan wins,
it posts the win (map, contest or not, player count) with a Claim button.
Claiming gives 1 point per opponent and +1 win. Players who /link their
Territorial account get their wins claimed automatically.

Environment variables:
  DISCORD_TOKEN  - your bot token (required)
  DATABASE_URL   - Postgres link (e.g. from Neon). If not set, a local
                   clan.db file is used instead (fine on your own PC).
  PORT           - set automatically by Render; the bot answers web
                   requests on it so a pinger can keep it awake.
"""

import os
import re
import sqlite3
import discord
from aiohttp import web, ClientSession
from discord import app_commands
from discord.ext import commands, tasks

# ====================== CONFIG ======================
TOKEN = os.getenv("DISCORD_TOKEN", "PASTE_YOUR_TOKEN_HERE")
CLAN_TAG = "MPOW"                       # clan tag, without the [ ]
WIN_CHANNEL_ID = 1564970567667294259    # channel where wins get posted
STAFF_ROLE_ID = 1564970097226023013     # role allowed to use add/remove commands
LOG_URL = "https://territorial.io/log/team-games"
CHECK_EVERY_SECONDS = 60                # how often to check the log
CONTEST_MULTIPLIER = 1                  # set to 2 for double points in contests
DATABASE_URL = os.getenv("DATABASE_URL")
SQLITE_FILE = "clan.db"
# ====================================================


def points_for_game(player_count: int, contest: bool) -> int:
    """1 point for each player you played against (everyone except you)."""
    pts = max(player_count - 1, 0)
    return pts * (CONTEST_MULTIPLIER if contest else 1)


# ---------------- Database ----------------
# Works with Postgres (when DATABASE_URL is set) or a local SQLite file.
if DATABASE_URL:
    import psycopg

    def run(sql: str, params=()):
        """Runs one query. Returns (rows, rowcount)."""
        with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
            cur = conn.execute(sql.replace("?", "%s"), params)
            rows = cur.fetchall() if cur.description else []
            return rows, cur.rowcount

    GREATEST = "GREATEST"
else:
    _sqlite = sqlite3.connect(SQLITE_FILE)

    def run(sql: str, params=()):
        cur = _sqlite.execute(sql, params)
        rows = cur.fetchall()
        _sqlite.commit()
        return rows, cur.rowcount

    GREATEST = "MAX"

for statement in [
    """CREATE TABLE IF NOT EXISTS players (
        user_id BIGINT PRIMARY KEY,
        points  INTEGER NOT NULL DEFAULT 0,
        wins    INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS games (
        game_id      TEXT PRIMARY KEY,
        map          TEXT,
        contest      INTEGER,
        player_count INTEGER,
        points       INTEGER)""",
    """CREATE TABLE IF NOT EXISTS claims (
        game_id TEXT,
        user_id BIGINT,
        PRIMARY KEY (game_id, user_id))""",
    "CREATE TABLE IF NOT EXISTS seen (game_id TEXT PRIMARY KEY)",
    """CREATE TABLE IF NOT EXISTS links (
        user_id BIGINT PRIMARY KEY,
        code    TEXT UNIQUE NOT NULL)""",
]:
    run(statement)


def change_stats(user_id: int, points: int = 0, wins: int = 0):
    """Adds (or with negative numbers removes) points/wins. Never goes below 0."""
    run("INSERT INTO players (user_id, points, wins) VALUES (?, 0, 0) ON CONFLICT (user_id) DO NOTHING", (user_id,))
    run(
        f"UPDATE players SET points = {GREATEST}(points + ?, 0), wins = {GREATEST}(wins + ?, 0) WHERE user_id = ?",
        (points, wins, user_id),
    )


def get_stats(user_id: int):
    rows, _ = run("SELECT points, wins FROM players WHERE user_id = ?", (user_id,))
    return rows[0] if rows else (0, 0)


def get_rank(user_id: int):
    pts, _ = get_stats(user_id)
    rows, _ = run("SELECT COUNT(*) FROM players WHERE points > ?", (pts,))
    return rows[0][0] + 1


def get_link(user_id: int):
    rows, _ = run("SELECT code FROM links WHERE user_id = ?", (user_id,))
    return rows[0][0] if rows else None


def try_claim(game_id: str, user_id: int) -> bool:
    """Marks a game as claimed by this user. False if they already claimed it."""
    _, count = run(
        "INSERT INTO claims (game_id, user_id) VALUES (?, ?) ON CONFLICT DO NOTHING", (game_id, user_id)
    )
    return count == 1


def is_staff(member: discord.Member) -> bool:
    return member.guild_permissions.administrator or any(r.id == STAFF_ROLE_ID for r in member.roles)


# ---------------- Log parsing ----------------
def parse_log(text: str):
    """Turns the plain-text log into a list of dicts, one per game."""
    games = []
    for block in re.split(r"\n\s*\n", text.strip()):
        game = {}
        for line in block.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                game[key.strip()] = value.strip()
        if "Time" in game and "Map" in game:
            games.append(game)
    return games


def game_id_of(game: dict) -> str:
    return f"{game.get('Time')}|{game.get('Map')}"


def is_our_win(game: dict) -> bool:
    return game.get("Winning Clan", "").strip("[] ").upper() == CLAN_TAG.upper()


def winner_codes(game: dict) -> set[str]:
    """Account names of everyone in our clan who won this game."""
    # "Clan Winners" = winners wearing our tag. Fall back to "Payout" if it's missing.
    field = "Clan Winners" if game.get("Clan Winners") else "Payout"
    codes = set()
    for part in game.get(field, "").split(","):
        part = re.sub(r"\(.*?\)", "", part).strip()  # drop "(8 more...)"
        if part:
            codes.add(part.split()[0].upper())  # "ZZDTX 42.00" -> "ZZDTX"
    return codes


# ---------------- Claim button ----------------
class ClaimButton(discord.ui.DynamicItem[discord.ui.Button], template=r"claim:(?P<gid>.+)"):
    """A button that keeps working even after the bot restarts."""

    def __init__(self, gid: str):
        super().__init__(discord.ui.Button(
            label="Claim", style=discord.ButtonStyle.green, emoji="🏆", custom_id=f"claim:{gid}"
        ))
        self.gid = gid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["gid"])

    async def callback(self, interaction: discord.Interaction):
        rows, _ = run("SELECT points FROM games WHERE game_id = ?", (self.gid,))
        if not rows:
            return await interaction.response.send_message("This game wasn't found.", ephemeral=True)
        if not try_claim(self.gid, interaction.user.id):
            return await interaction.response.send_message("You already claimed this win!", ephemeral=True)
        reward = rows[0][0]
        change_stats(interaction.user.id, points=reward, wins=1)
        pts, wins = get_stats(interaction.user.id)
        await interaction.response.send_message(
            f"{interaction.user.mention} claimed **+{reward} points** and **+1 win** "
            f"(now {pts} points, {wins} wins)"
        )


# ---------------- Bot ----------------
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


@tasks.loop(seconds=CHECK_EVERY_SECONDS)
async def check_log():
    try:
        async with bot.http_session.get(LOG_URL) as resp:
            text = await resp.text()
    except Exception as e:
        print("Couldn't read the log:", e)
        return

    games = parse_log(text)
    first_run = run("SELECT COUNT(*) FROM seen")[0][0][0] == 0
    channel = bot.get_channel(WIN_CHANNEL_ID)

    for game in reversed(games):  # oldest first
        gid = game_id_of(game)
        _, new = run("INSERT INTO seen (game_id) VALUES (?) ON CONFLICT DO NOTHING", (gid,))
        if new != 1:
            continue  # already handled

        # On the very first run, just remember old games instead of posting them all
        if first_run or not is_our_win(game) or channel is None:
            continue

        contest = game.get("Contest", "No").lower() == "yes"
        players = int(game.get("Player Count", "0") or 0)
        pts = points_for_game(players, contest)
        run(
            "INSERT INTO games VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (gid, game["Map"], int(contest), players, pts),
        )

        embed = discord.Embed(
            title=f"🎉 [{CLAN_TAG}] won a game!",
            color=discord.Color.gold() if contest else discord.Color.green(),
        )
        embed.add_field(name="Map", value=game["Map"])
        embed.add_field(name="Contest", value="✅ Yes" if contest else "❌ No")
        embed.add_field(name="Players", value=str(players))
        embed.add_field(name="Reward", value=f"{pts} points + 1 win", inline=False)

        # Auto-claim for everyone who linked an account that played in this win
        auto = []
        for code in winner_codes(game):
            rows, _ = run("SELECT user_id FROM links WHERE code = ?", (code,))
            for (uid,) in rows:
                if try_claim(gid, uid):
                    change_stats(uid, points=pts, wins=1)
                    auto.append(f"<@{uid}>")
        if auto:
            embed.add_field(name="✅ Auto-claimed", value=" ".join(auto), inline=False)
        embed.set_footer(text=f"Time: {game['Time']}")

        view = discord.ui.View(timeout=None)
        view.add_item(ClaimButton(gid))
        await channel.send(embed=embed, view=view)


@check_log.before_loop
async def before_check():
    await bot.wait_until_ready()


async def start_web_server():
    """Tiny web page so Render sees a web service and a pinger can keep it awake."""
    app = web.Application()
    app.router.add_get("/", lambda request: web.Response(text="Bot is running!"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "8080"))).start()


@bot.event
async def setup_hook():
    bot.http_session = ClientSession()
    bot.add_dynamic_items(ClaimButton)
    await start_web_server()
    await bot.tree.sync()
    check_log.start()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} — watching for [{CLAN_TAG}] wins")


# ---------------- Slash commands ----------------
async def staff_only(interaction: discord.Interaction) -> bool:
    if not is_staff(interaction.user):
        await interaction.response.send_message("Only staff can do that.", ephemeral=True)
        return False
    return True


@bot.tree.command(name="addpoints", description="(Staff) Give points to a player")
async def addpoints(interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1]):
    if await staff_only(interaction):
        change_stats(member.id, points=amount)
        await interaction.response.send_message(f"Added **{amount}** points to {member.mention} (now {get_stats(member.id)[0]}).")


@bot.tree.command(name="removepoints", description="(Staff) Take points from a player")
async def removepoints(interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1]):
    if await staff_only(interaction):
        change_stats(member.id, points=-amount)
        await interaction.response.send_message(f"Removed **{amount}** points from {member.mention} (now {get_stats(member.id)[0]}).")


@bot.tree.command(name="addwins", description="(Staff) Give wins to a player")
async def addwins(interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1]):
    if await staff_only(interaction):
        change_stats(member.id, wins=amount)
        await interaction.response.send_message(f"Added **{amount}** wins to {member.mention} (now {get_stats(member.id)[1]}).")


@bot.tree.command(name="removewins", description="(Staff) Take wins from a player")
async def removewins(interaction: discord.Interaction, member: discord.Member, amount: app_commands.Range[int, 1]):
    if await staff_only(interaction):
        change_stats(member.id, wins=-amount)
        await interaction.response.send_message(f"Removed **{amount}** wins from {member.mention} (now {get_stats(member.id)[1]}).")


@bot.tree.command(name="link", description="Link your Territorial.io account so your wins are claimed automatically")
@app_commands.describe(name="Your Territorial.io account name as it appears in the team-games log (e.g. ZZDTX)")
async def link(interaction: discord.Interaction, name: str):
    code = name.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{3,10}", code):
        return await interaction.response.send_message("That doesn't look like an account name.", ephemeral=True)
    rows, _ = run("SELECT user_id FROM links WHERE code = ?", (code,))
    if rows and rows[0][0] != interaction.user.id:
        return await interaction.response.send_message(
            "That account is already linked to someone else. Ask staff if it's yours.", ephemeral=True
        )
    run(
        "INSERT INTO links (user_id, code) VALUES (?, ?) ON CONFLICT (user_id) DO UPDATE SET code = excluded.code",
        (interaction.user.id, code),
    )
    await interaction.response.send_message(
        f"Linked to **{code}**. Your [{CLAN_TAG}] wins will now be claimed automatically.", ephemeral=True
    )


@bot.tree.command(name="unlink", description="Unlink a Territorial.io account (staff can unlink others)")
async def unlink(interaction: discord.Interaction, member: discord.Member | None = None):
    target = member or interaction.user
    if target.id != interaction.user.id and not is_staff(interaction.user):
        return await interaction.response.send_message("Only staff can unlink other people.", ephemeral=True)
    if not get_link(target.id):
        return await interaction.response.send_message(f"{target.display_name} has no linked account.", ephemeral=True)
    run("DELETE FROM links WHERE user_id = ?", (target.id,))
    await interaction.response.send_message(f"Unlinked {target.mention}.", ephemeral=True)


@bot.tree.command(name="profile", description="See a player's points, wins and rank")
async def profile(interaction: discord.Interaction, member: discord.Member | None = None):
    member = member or interaction.user
    pts, wins = get_stats(member.id)
    embed = discord.Embed(title=f"{member.display_name}'s profile", color=discord.Color.blurple())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Points", value=str(pts))
    embed.add_field(name="Wins", value=str(wins))
    embed.add_field(name="Rank", value=f"#{get_rank(member.id)}")
    embed.add_field(name="Territorial account", value=get_link(member.id) or "Not linked (use /link)", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="leaderboard", description="Top 10 players")
@app_commands.choices(sort_by=[
    app_commands.Choice(name="Points", value="points"),
    app_commands.Choice(name="Wins", value="wins"),
])
async def leaderboard(interaction: discord.Interaction, sort_by: app_commands.Choice[str] | None = None):
    column = "wins" if sort_by and sort_by.value == "wins" else "points"
    rows, _ = run(f"SELECT user_id, points, wins FROM players ORDER BY {column} DESC LIMIT 10")
    if not rows:
        return await interaction.response.send_message("No one has any points yet!")
    medals = ["🥇", "🥈", "🥉"]
    lines = [
        f"{medals[i] if i < 3 else f'**{i + 1}.**'} <@{uid}> — **{pts}** pts · **{wins}** wins"
        for i, (uid, pts, wins) in enumerate(rows)
    ]
    embed = discord.Embed(
        title=f"🏆 [{CLAN_TAG}] Leaderboard — by {column}",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )
    await interaction.response.send_message(embed=embed)


bot.run(TOKEN)
