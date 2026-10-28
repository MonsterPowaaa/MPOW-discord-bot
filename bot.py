"""
Territorial.io Clan Wins Bot  —  [MPOW]
---------------------------------------
Watches the official Territorial.io team-games log. Every time the clan wins,
it posts the win (map, contest or not, player count) with claim buttons.
Claiming gives 1 point per opponent and +1 win. Players whose Territorial
account is linked get their wins claimed automatically.

Environment variables:
  DISCORD_TOKEN  - your bot token (required)
  DATABASE_URL   - Postgres link (e.g. from Neon). If not set, a local
                   clan.db file is used instead (fine on your own PC).
  PORT           - set automatically by Render; the bot answers web
                   requests on it so a pinger can keep it awake.
"""

import os
import re
import time
import sqlite3
import discord
from aiohttp import web, ClientSession
from discord import app_commands
from discord.ext import commands, tasks

# ====================== DEFAULTS ======================
# These are used until you change them with /winlog.
TOKEN = os.getenv("DISCORD_TOKEN", "PASTE_YOUR_TOKEN_HERE")
DEFAULT_CLAN_TAG = "MPOW"                    # clan tag, without the [ ]
DEFAULT_WIN_CHANNEL_ID = 1564970567667294259 # channel where wins get posted
STAFF_ROLE_ID = 1564970097226023013          # always counts as a manager role
LOG_URL = "https://territorial.io/log/team-games"
CHECK_EVERY_SECONDS = 60
MAX_MANAGER_ROLES = 3
DATABASE_URL = os.getenv("DATABASE_URL")
SQLITE_FILE = "clan.db"
# ======================================================


def points_for_game(player_count: int) -> int:
    """1 point for each player you played against (everyone except you)."""
    return max(player_count - 1, 0)


# =========================== Database ===========================
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
    """CREATE TABLE IF NOT EXISTS reward_roles (
        role_id    BIGINT NOT NULL,
        guild_id   BIGINT NOT NULL,
        kind       TEXT NOT NULL,
        amount     INTEGER NOT NULL,
        channel_id BIGINT,
        PRIMARY KEY (role_id, kind))""",
    "CREATE TABLE IF NOT EXISTS manager_roles (role_id BIGINT PRIMARY KEY, guild_id BIGINT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS buttons (
        name       TEXT PRIMARY KEY,
        emoji      TEXT NOT NULL,
        multiplier REAL NOT NULL)""",
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)",
]:
    run(statement)

# Move roles made with the older /role_create into the new reward_roles table.
try:
    run("""INSERT INTO reward_roles (role_id, guild_id, kind, amount, channel_id)
           SELECT role_id, guild_id, 'points', points, NULL FROM point_roles WHERE 1 = 1
           ON CONFLICT DO NOTHING""")
    run("DROP TABLE point_roles")
except Exception:
    pass  # no old table, nothing to move


# ---------- settings ----------
def get_setting(key: str, default=None):
    rows, _ = run("SELECT value FROM settings WHERE key = ?", (key,))
    return rows[0][0] if rows else default


def set_setting(key: str, value):
    run("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (key, None if value is None else str(value)))


def clan_tag() -> str:
    return get_setting("clan_tag", DEFAULT_CLAN_TAG)


def win_channel_id() -> int:
    return int(get_setting("win_channel", DEFAULT_WIN_CHANNEL_ID))


def winlog_enabled() -> bool:
    return get_setting("winlog_enabled", "1") == "1"


def event_multiplier():
    """Returns (multiplier, ends_at or None). Expired events count as 1."""
    value = float(get_setting("mult_value", "1"))
    ends = get_setting("mult_ends")
    ends = float(ends) if ends else None
    if ends and time.time() >= ends:
        set_setting("mult_value", 1)
        set_setting("mult_ends", None)
        return 1.0, None
    return value, ends


def fmt_mult(x: float) -> str:
    return f"×{x:g}"


# ---------- players ----------
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
    _, count = run("INSERT INTO claims (game_id, user_id) VALUES (?, ?) ON CONFLICT DO NOTHING", (game_id, user_id))
    return count == 1


# ---------- permissions ----------
def is_owner(member: discord.Member) -> bool:
    return member.id == member.guild.owner_id or member.guild_permissions.administrator


def is_manager(member: discord.Member) -> bool:
    if is_owner(member):
        return True
    rows, _ = run("SELECT role_id FROM manager_roles WHERE guild_id = ?", (member.guild.id,))
    allowed = {r for (r,) in rows} | {STAFF_ROLE_ID}
    return any(role.id in allowed for role in member.roles)


async def deny(interaction: discord.Interaction, who: str):
    await interaction.response.send_message(f"Only {who} can use this command.", ephemeral=True)


async def managers_only(interaction: discord.Interaction) -> bool:
    if is_manager(interaction.user):
        return True
    await deny(interaction, "managers")
    return False


async def owners_only(interaction: discord.Interaction) -> bool:
    if is_owner(interaction.user):
        return True
    await deny(interaction, "the server owner")
    return False


# ---------- reward roles ----------
async def update_roles(guild: discord.Guild | None, user_id: int):
    """Gives a player every reward role they've reached and removes ones they dropped below."""
    if guild is None:
        return
    rows, _ = run("SELECT role_id, kind, amount, channel_id FROM reward_roles WHERE guild_id = ?", (guild.id,))
    if not rows:
        return
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.HTTPException:
            return  # left the server
    pts, wins = get_stats(user_id)

    # A role can be a reward for both points and wins; it's kept if either is reached.
    per_role = {}
    for role_id, kind, amount, channel_id in rows:
        reached = (pts if kind == "points" else wins) >= amount
        entry = per_role.setdefault(role_id, {"reached": False, "announce": []})
        if reached:
            entry["reached"] = True
            entry["announce"].append((kind, amount, channel_id))

    for role_id, entry in per_role.items():
        role = guild.get_role(role_id)
        if role is None:
            continue
        has = role in member.roles
        try:
            if entry["reached"] and not has:
                await member.add_roles(role, reason="Reached reward milestone")
                for kind, amount, channel_id in entry["announce"]:
                    channel = guild.get_channel(channel_id) if channel_id else None
                    if channel:
                        await channel.send(
                            f"🎉 {member.mention} reached **{amount} {kind}** and earned {role.mention}!",
                            allowed_mentions=discord.AllowedMentions(users=True, roles=False),
                        )
                    break
            elif not entry["reached"] and has:
                await member.remove_roles(role, reason="Dropped below reward milestone")
        except discord.Forbidden:
            print("Missing permission to manage roles: give the bot 'Manage Roles' and put its role above the reward roles.")


# =========================== Log parsing ===========================
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


def winning_tag(game: dict) -> str:
    return game.get("Winning Clan", "").strip("[] ").upper()


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


# =========================== Claim buttons ===========================
def get_buttons():
    rows, _ = run("SELECT name, emoji, multiplier FROM buttons ORDER BY multiplier, name")
    return rows


class ClaimButton(discord.ui.DynamicItem[discord.ui.Button], template=r"claim:(?P<rest>.+)"):
    """A claim button that keeps working after restarts.
    custom_id is 'claim:<gameid>' for the normal button, or 'claim:<name>~<gameid>' for custom ones."""

    def __init__(self, gid: str, name: str = "", emoji: str = "🏆", multiplier: float = 1.0):
        custom_id = f"claim:{name}~{gid}" if name else f"claim:{gid}"
        label = "Claim" if not name else f"{name} ({fmt_mult(multiplier)})"
        style = discord.ButtonStyle.green if not name else discord.ButtonStyle.blurple
        super().__init__(discord.ui.Button(label=label, style=style, emoji=emoji, custom_id=custom_id[:100]))
        self.gid, self.name = gid, name

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        rest = match["rest"]
        name, gid = rest.split("~", 1) if "~" in rest else ("", rest)
        return cls(gid, name)

    async def callback(self, interaction: discord.Interaction):
        if self.gid == "TEST":
            return await interaction.response.send_message("This is a test message, so nothing was claimed.", ephemeral=True)
        rows, _ = run("SELECT points FROM games WHERE game_id = ?", (self.gid,))
        if not rows:
            return await interaction.response.send_message("This game wasn't found.", ephemeral=True)
        mult = 1.0
        if self.name:
            b, _ = run("SELECT multiplier FROM buttons WHERE name = ?", (self.name,))
            if not b:
                return await interaction.response.send_message("That button was removed. Use the normal Claim button.", ephemeral=True)
            mult = b[0][0]
        if not try_claim(self.gid, interaction.user.id):
            return await interaction.response.send_message("You already claimed this win!", ephemeral=True)
        reward = round(rows[0][0] * mult)
        change_stats(interaction.user.id, points=reward, wins=1)
        await update_roles(interaction.guild, interaction.user.id)
        pts, wins = get_stats(interaction.user.id)
        extra = f" with **{self.name}** ({fmt_mult(mult)})" if self.name else ""
        await interaction.response.send_message(
            f"{interaction.user.mention} claimed **+{reward} points** and **+1 win**{extra} "
            f"(now {pts} points, {wins} wins)"
        )


def claim_view(gid: str, custom: bool = True) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(ClaimButton(gid))
    if custom:
        for name, emoji, mult in get_buttons()[:24]:
            view.add_item(ClaimButton(gid, name, emoji, mult))
    return view


async def send_win(channel, embed, gid: str):
    """Sends a win message; if a custom button breaks it, sends it with just the normal Claim button."""
    try:
        await channel.send(embed=embed, view=claim_view(gid))
    except (discord.HTTPException, ValueError, TypeError) as e:
        print("Sending with custom buttons failed, retrying without them:", e)
        await channel.send(embed=embed, view=claim_view(gid, custom=False))


def win_embed(game: dict, tag: str, pts: int, mult: float) -> discord.Embed:
    contest = game.get("Contest", "No").lower() == "yes"
    embed = discord.Embed(
        title=f"🎉 [{tag}] won a game!",
        color=discord.Color.gold() if contest else discord.Color.green(),
    )
    embed.add_field(name="Map", value=game.get("Map", "?"))
    embed.add_field(name="Contest", value="✅ Yes" if contest else "❌ No")
    embed.add_field(name="Players", value=game.get("Player Count", "?"))
    reward = f"{pts} points + 1 win"
    if mult != 1:
        reward += f"  (event {fmt_mult(mult)} included)"
    embed.add_field(name="Reward", value=reward, inline=False)
    embed.set_footer(text=f"Time: {game.get('Time', '?')}")
    return embed


# =========================== Bot ===========================
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
    tag = clan_tag().upper()
    channel = bot.get_channel(win_channel_id())
    enabled = winlog_enabled()

    for game in reversed(games):  # oldest first
        gid = game_id_of(game)
        _, new = run("INSERT INTO seen (game_id) VALUES (?) ON CONFLICT DO NOTHING", (gid,))
        if new != 1:
            continue  # already handled

        # On the very first run, just remember old games instead of posting them all
        if first_run or not enabled or winning_tag(game) != tag or channel is None:
            continue

        contest = game.get("Contest", "No").lower() == "yes"
        players = int(game.get("Player Count", "0") or 0)
        mult, _ = event_multiplier()
        pts = round(points_for_game(players) * mult)
        run("INSERT INTO games VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            (gid, game["Map"], int(contest), players, pts))

        embed = win_embed(game, tag, pts, mult)

        # Auto-claim for everyone whose linked account played in this win
        auto = []
        for code in winner_codes(game):
            rows, _ = run("SELECT user_id FROM links WHERE code = ?", (code,))
            for (uid,) in rows:
                if try_claim(gid, uid):
                    change_stats(uid, points=pts, wins=1)
                    await update_roles(channel.guild, uid)
                    auto.append(f"<@{uid}>")
        if auto:
            embed.add_field(name="✅ Auto-claimed", value=" ".join(auto), inline=False)

        await send_win(channel, embed, gid)


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
    print(f"Logged in as {bot.user} — watching for [{clan_tag()}] wins")


# =========================== General ===========================
HELP_PAGES = [
    ("General", [
        ("/help", "Display all available commands"),
        ("/ping", "Check bot latency"),
        ("/profile [user]", "View a profile with stats"),
        ("/leaderboard [sort_by]", "View the server leaderboard"),
        ("/account_info [user]", "View a linked Territorial account"),
        ("/rewardrole points · wins", "View reward roles"),
        ("/roles", "List all reward roles"),
        ("/button_info", "View custom claim buttons"),
        ("/multiplier info", "View the current event multiplier"),
    ]),
    ("Server owner", [
        ("/manager add <role>", f"Add a manager role (max {MAX_MANAGER_ROLES})"),
        ("/manager remove <role>", "Remove a manager role"),
        ("/manager info", "View manager roles"),
    ]),
    ("Managers: points & wins", [
        ("/add <points> · /remove <points>", "Change your own points"),
        ("/addpoints · /removepoints <user> <amount>", "Change a player's points"),
        ("/addwins · /removewins <user> <amount>", "Change a player's wins"),
        ("/multiplier set · edit · end", "Run an event multiplier"),
        ("/account_linking <user> <account>", "Link a Territorial account"),
        ("/account_unlink <user>", "Unlink an account"),
    ]),
    ("Managers: reward roles", [
        ("/rewardrole_setup <type> <amount> <role> [channel]", "Give a role at a milestone"),
        ("/rewardrole_edit <reward> <value>", "Change a milestone"),
        ("/rewardrole_delete <reward>", "Delete one reward"),
        ("/rewardrole_remove <role>", "Remove every reward for a role"),
        ("/role_create <name> <points> [color]", "Create a new role as a points reward"),
        ("/role_delete <role>", "Delete a reward role from the server"),
    ]),
    ("Managers: win log", [
        ("/winlog <clan> <channel>", "Set up win tracking"),
        ("/winlog_disable", "Stop posting wins"),
        ("/winlog_test", "Send a test win message"),
        ("/button_add <name> <emoji> <multiplier>", "Add a claim button"),
        ("/button_remove <name>", "Remove a claim button"),
    ]),
]


@bot.tree.command(name="help", description="Display all available commands")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="Bot commands", color=discord.Color.blurple())
    for section, cmds in HELP_PAGES:
        embed.add_field(name=section, value="\n".join(f"`{c}` — {d}" for c, d in cmds), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="ping", description="Check bot latency")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message(f"🏓 Pong! {round(bot.latency * 1000)} ms")


@bot.tree.command(name="profile", description="View a profile with stats")
async def profile(interaction: discord.Interaction, user: discord.Member | None = None):
    member = user or interaction.user
    pts, wins = get_stats(member.id)
    embed = discord.Embed(title=f"{member.display_name}'s profile", color=discord.Color.blurple())
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.add_field(name="Points", value=str(pts))
    embed.add_field(name="Wins", value=str(wins))
    embed.add_field(name="Rank", value=f"#{get_rank(member.id)}")
    embed.add_field(name="Territorial account", value=get_link(member.id) or "Not linked", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="leaderboard", description="View the server leaderboard")
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
    embed = discord.Embed(title=f"🏆 [{clan_tag()}] Leaderboard — by {column}",
                          description="\n".join(lines), color=discord.Color.gold())
    await interaction.response.send_message(embed=embed)


# =========================== Server owner ===========================
manager = app_commands.Group(name="manager", description="Manage manager roles (server owner)")


@manager.command(name="add", description="Add a manager role")
async def manager_add(interaction: discord.Interaction, role: discord.Role):
    if not await owners_only(interaction):
        return
    rows, _ = run("SELECT role_id FROM manager_roles WHERE guild_id = ?", (interaction.guild.id,))
    if role.id in {r for (r,) in rows}:
        return await interaction.response.send_message(f"{role.mention} is already a manager role.", ephemeral=True)
    if len(rows) >= MAX_MANAGER_ROLES:
        return await interaction.response.send_message(
            f"You already have {MAX_MANAGER_ROLES} manager roles. Remove one first.", ephemeral=True)
    run("INSERT INTO manager_roles (role_id, guild_id) VALUES (?, ?)", (role.id, interaction.guild.id))
    await interaction.response.send_message(f"{role.mention} is now a manager role.")


@manager.command(name="remove", description="Remove a manager role")
async def manager_remove(interaction: discord.Interaction, role: discord.Role):
    if not await owners_only(interaction):
        return
    _, count = run("DELETE FROM manager_roles WHERE role_id = ?", (role.id,))
    msg = f"{role.mention} is no longer a manager role." if count else f"{role.mention} wasn't a manager role."
    await interaction.response.send_message(msg)


@manager.command(name="info", description="View manager roles")
async def manager_info(interaction: discord.Interaction):
    rows, _ = run("SELECT role_id FROM manager_roles WHERE guild_id = ?", (interaction.guild.id,))
    lines = [f"<@&{r}>" for (r,) in rows] or ["None yet"]
    embed = discord.Embed(title="Manager roles", color=discord.Color.blurple(),
                          description="\n".join(lines) + f"\n\nAlso always allowed: <@&{STAFF_ROLE_ID}>, the owner and admins.")
    embed.set_footer(text=f"{len(rows)}/{MAX_MANAGER_ROLES} used")
    await interaction.response.send_message(embed=embed)


bot.tree.add_command(manager)


# =========================== Points & wins ===========================
async def change_and_reply(interaction, member, points=0, wins=0, verb=""):
    change_stats(member.id, points=points, wins=wins)
    await update_roles(interaction.guild, member.id)
    pts, w = get_stats(member.id)
    await interaction.response.send_message(f"{verb} — {member.mention} now has **{pts}** points and **{w}** wins.")


@bot.tree.command(name="add", description="(Manager) Add points to your own account")
async def add(interaction: discord.Interaction, points: app_commands.Range[int, 1]):
    if await managers_only(interaction):
        await change_and_reply(interaction, interaction.user, points=points, verb=f"Added {points} points")


@bot.tree.command(name="remove", description="(Manager) Remove points from your own account")
async def remove(interaction: discord.Interaction, points: app_commands.Range[int, 1]):
    if await managers_only(interaction):
        await change_and_reply(interaction, interaction.user, points=-points, verb=f"Removed {points} points")


@bot.tree.command(name="addpoints", description="(Manager) Give points to a player")
async def addpoints(interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1]):
    if await managers_only(interaction):
        await change_and_reply(interaction, user, points=amount, verb=f"Added {amount} points")


@bot.tree.command(name="removepoints", description="(Manager) Take points from a player")
async def removepoints(interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1]):
    if await managers_only(interaction):
        await change_and_reply(interaction, user, points=-amount, verb=f"Removed {amount} points")


@bot.tree.command(name="addwins", description="(Manager) Give wins to a player")
async def addwins(interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1]):
    if await managers_only(interaction):
        await change_and_reply(interaction, user, wins=amount, verb=f"Added {amount} wins")


@bot.tree.command(name="removewins", description="(Manager) Take wins from a player")
async def removewins(interaction: discord.Interaction, user: discord.Member, amount: app_commands.Range[int, 1]):
    if await managers_only(interaction):
        await change_and_reply(interaction, user, wins=-amount, verb=f"Removed {amount} wins")


# ---------- event multiplier ----------
multiplier = app_commands.Group(name="multiplier", description="Event multipliers for win rewards")


@multiplier.command(name="set", description="(Manager) Start an event multiplier")
@app_commands.describe(value="e.g. 2 for double points", hours="How long it lasts (leave empty = until /multiplier end)")
async def multiplier_set(interaction: discord.Interaction, value: app_commands.Range[float, 0.1, 100.0],
                         hours: app_commands.Range[float, 0.1] | None = None):
    if not await managers_only(interaction):
        return
    ends = time.time() + hours * 3600 if hours else None
    set_setting("mult_value", value)
    set_setting("mult_ends", ends)
    until = f" until <t:{int(ends)}:f> (<t:{int(ends)}:R>)" if ends else " until someone runs /multiplier end"
    await interaction.response.send_message(f"⚡ Event multiplier **{fmt_mult(value)}** is active{until}.")


@multiplier.command(name="edit", description="(Manager) Change the current multiplier")
async def multiplier_edit(interaction: discord.Interaction, value: app_commands.Range[float, 0.1, 100.0]):
    if not await managers_only(interaction):
        return
    current, _ = event_multiplier()
    if current == 1:
        return await interaction.response.send_message("No event is running. Use /multiplier set.", ephemeral=True)
    set_setting("mult_value", value)
    await interaction.response.send_message(f"Event multiplier changed to **{fmt_mult(value)}**.")


@multiplier.command(name="end", description="(Manager) End the event multiplier")
async def multiplier_end(interaction: discord.Interaction):
    if not await managers_only(interaction):
        return
    set_setting("mult_value", 1)
    set_setting("mult_ends", None)
    await interaction.response.send_message("Event multiplier ended. Rewards are back to normal.")


@multiplier.command(name="info", description="View the current event multiplier")
async def multiplier_info(interaction: discord.Interaction):
    value, ends = event_multiplier()
    if value == 1:
        msg = "No event multiplier is running."
    else:
        msg = f"⚡ **{fmt_mult(value)}** is active" + (f", ends <t:{int(ends)}:R>." if ends else " with no end time.")
    await interaction.response.send_message(msg)


bot.tree.add_command(multiplier)


# ---------- account linking ----------
@bot.tree.command(name="account_linking", description="(Manager) Link a player's Territorial account for auto-claiming")
@app_commands.describe(account="Territorial account name as it appears in the team-games log (e.g. ZZDTX)")
async def account_linking(interaction: discord.Interaction, user: discord.Member, account: str):
    if not await managers_only(interaction):
        return
    code = account.strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{3,10}", code):
        return await interaction.response.send_message("That doesn't look like an account name.", ephemeral=True)
    rows, _ = run("SELECT user_id FROM links WHERE code = ?", (code,))
    if rows and rows[0][0] != user.id:
        return await interaction.response.send_message(
            f"**{code}** is already linked to <@{rows[0][0]}>. Unlink them first.", ephemeral=True)
    run("INSERT INTO links (user_id, code) VALUES (?, ?) ON CONFLICT (user_id) DO UPDATE SET code = excluded.code",
        (user.id, code))
    await interaction.response.send_message(f"Linked {user.mention} to **{code}**. Their wins will be claimed automatically.")


@bot.tree.command(name="account_unlink", description="(Manager) Unlink a player's Territorial account")
async def account_unlink(interaction: discord.Interaction, user: discord.Member):
    if not await managers_only(interaction):
        return
    _, count = run("DELETE FROM links WHERE user_id = ?", (user.id,))
    msg = f"Unlinked {user.mention}." if count else f"{user.display_name} has no linked account."
    await interaction.response.send_message(msg)


@bot.tree.command(name="account_info", description="View a linked Territorial account")
async def account_info(interaction: discord.Interaction, user: discord.Member | None = None):
    member = user or interaction.user
    code = get_link(member.id)
    msg = f"{member.mention} is linked to **{code}**." if code else f"{member.mention} has no linked account."
    await interaction.response.send_message(msg, allowed_mentions=discord.AllowedMentions.none())


# =========================== Reward roles ===========================
KIND_CHOICES = [app_commands.Choice(name="Points", value="points"), app_commands.Choice(name="Wins", value="wins")]


async def reward_autocomplete(interaction: discord.Interaction, current: str):
    rows, _ = run("SELECT role_id, kind, amount FROM reward_roles WHERE guild_id = ? ORDER BY kind, amount",
                  (interaction.guild.id,))
    choices = []
    for role_id, kind, amount in rows:
        role = interaction.guild.get_role(role_id)
        label = f"{amount} {kind} → {role.name if role else 'deleted role'}"
        if current.lower() in label.lower():
            choices.append(app_commands.Choice(name=label[:100], value=f"{kind}:{role_id}"))
    return choices[:25]


def parse_reward(value: str):
    try:
        kind, role_id = value.split(":")
        return kind, int(role_id)
    except ValueError:
        return None, None


async def sync_everyone(guild: discord.Guild):
    rows, _ = run("SELECT user_id FROM players")
    for (uid,) in rows:
        await update_roles(guild, uid)


@bot.tree.command(name="rewardrole_setup", description="(Manager) Give a role at a points or wins milestone")
@app_commands.describe(type="Points or wins", amount="How many are needed", role="Role to give",
                       channel="Where to announce it (optional)")
@app_commands.choices(type=KIND_CHOICES)
async def rewardrole_setup(interaction: discord.Interaction, type: app_commands.Choice[str],
                           amount: app_commands.Range[int, 0], role: discord.Role,
                           channel: discord.TextChannel | None = None):
    if not await managers_only(interaction):
        return
    if role >= interaction.guild.me.top_role:
        return await interaction.response.send_message(
            f"I can't give {role.mention} because it's above my role. Move my role higher first.", ephemeral=True)
    await interaction.response.defer()
    run("""INSERT INTO reward_roles (role_id, guild_id, kind, amount, channel_id) VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (role_id, kind) DO UPDATE SET amount = excluded.amount, channel_id = excluded.channel_id""",
        (role.id, interaction.guild.id, type.value, amount, channel.id if channel else None))
    await sync_everyone(interaction.guild)
    where = f", announced in {channel.mention}" if channel else ""
    await interaction.followup.send(f"Players now get {role.mention} at **{amount} {type.value}**{where}.")


rewardrole = app_commands.Group(name="rewardrole", description="View reward roles")


async def show_rewards(interaction: discord.Interaction, kind: str):
    rows, _ = run("SELECT role_id, amount, channel_id FROM reward_roles WHERE guild_id = ? AND kind = ? ORDER BY amount",
                  (interaction.guild.id, kind))
    if not rows:
        return await interaction.response.send_message(f"No {kind} reward roles yet.")
    lines = [f"<@&{r}> — **{a}** {kind}" + (f" · announced in <#{c}>" if c else "") for r, a, c in rows]
    await interaction.response.send_message(embed=discord.Embed(
        title=f"🎖️ {kind.capitalize()} reward roles", description="\n".join(lines), color=discord.Color.purple()))


@rewardrole.command(name="points", description="View points reward roles")
async def rewardrole_points(interaction: discord.Interaction):
    await show_rewards(interaction, "points")


@rewardrole.command(name="wins", description="View wins reward roles")
async def rewardrole_wins(interaction: discord.Interaction):
    await show_rewards(interaction, "wins")


bot.tree.add_command(rewardrole)


@bot.tree.command(name="rewardrole_edit", description="(Manager) Change a reward's milestone")
@app_commands.autocomplete(reward=reward_autocomplete)
async def rewardrole_edit(interaction: discord.Interaction, reward: str, value: app_commands.Range[int, 0]):
    if not await managers_only(interaction):
        return
    kind, role_id = parse_reward(reward)
    _, count = run("UPDATE reward_roles SET amount = ? WHERE role_id = ? AND kind = ?", (value, role_id, kind))
    if not count:
        return await interaction.response.send_message("Pick a reward from the list.", ephemeral=True)
    await interaction.response.defer()
    await sync_everyone(interaction.guild)
    await interaction.followup.send(f"<@&{role_id}> now needs **{value} {kind}**.")


@bot.tree.command(name="rewardrole_delete", description="(Manager) Delete one reward")
@app_commands.autocomplete(reward=reward_autocomplete)
async def rewardrole_delete(interaction: discord.Interaction, reward: str):
    if not await managers_only(interaction):
        return
    kind, role_id = parse_reward(reward)
    _, count = run("DELETE FROM reward_roles WHERE role_id = ? AND kind = ?", (role_id, kind))
    msg = f"Deleted the **{kind}** reward for <@&{role_id}>. Players keep the role unless you remove it." \
        if count else "Pick a reward from the list."
    await interaction.response.send_message(msg, ephemeral=not count)


@bot.tree.command(name="rewardrole_remove", description="(Manager) Remove every reward for a role")
async def rewardrole_remove(interaction: discord.Interaction, role: discord.Role):
    if not await managers_only(interaction):
        return
    _, count = run("DELETE FROM reward_roles WHERE role_id = ?", (role.id,))
    msg = f"{role.mention} is no longer a reward role." if count else f"{role.mention} isn't a reward role."
    await interaction.response.send_message(msg)


@bot.tree.command(name="role_create", description="(Manager) Create a new role given at a points milestone")
@app_commands.describe(name="Name of the new role", points="Points needed to get it", color="Optional hex color, e.g. #ff9900")
async def role_create(interaction: discord.Interaction, name: str, points: app_commands.Range[int, 0], color: str | None = None):
    if not await managers_only(interaction):
        return
    try:
        role_color = discord.Color.from_str(color) if color else discord.Color.default()
    except ValueError:
        return await interaction.response.send_message("Color must look like #ff9900.", ephemeral=True)
    await interaction.response.defer()
    try:
        role = await interaction.guild.create_role(name=name, color=role_color, reason=f"Reward role ({points} pts)")
    except discord.Forbidden:
        return await interaction.followup.send("I need the **Manage Roles** permission to create roles.")
    run("INSERT INTO reward_roles (role_id, guild_id, kind, amount, channel_id) VALUES (?, ?, 'points', ?, NULL)",
        (role.id, interaction.guild.id, points))
    await sync_everyone(interaction.guild)
    await interaction.followup.send(f"Created {role.mention} for **{points}+ points**.")


@bot.tree.command(name="role_delete", description="(Manager) Delete a reward role from the server")
async def role_delete(interaction: discord.Interaction, role: discord.Role):
    if not await managers_only(interaction):
        return
    _, count = run("DELETE FROM reward_roles WHERE role_id = ?", (role.id,))
    if count == 0:
        return await interaction.response.send_message(f"{role.mention} isn't a reward role.", ephemeral=True)
    try:
        await role.delete(reason="Reward role removed")
        await interaction.response.send_message(f"Deleted the reward role **{role.name}**.")
    except discord.Forbidden:
        await interaction.response.send_message(
            f"**{role.name}** is no longer a reward role, but I couldn't delete it from the server. Delete it manually.")


@bot.tree.command(name="roles", description="List all reward roles")
async def roles(interaction: discord.Interaction):
    rows, _ = run("SELECT role_id, kind, amount FROM reward_roles WHERE guild_id = ? ORDER BY kind, amount",
                  (interaction.guild.id,))
    if not rows:
        return await interaction.response.send_message("No reward roles yet.")
    lines = [f"<@&{r}> — **{a}** {k}" for r, k, a in rows]
    await interaction.response.send_message(embed=discord.Embed(
        title="🎖️ Reward roles", description="\n".join(lines), color=discord.Color.purple()))


# =========================== Win log ===========================
@bot.tree.command(name="winlog", description="(Manager) Set which clan to track and where to post wins")
@app_commands.describe(clan="Clan tag, with or without [ ]", channel="Channel for win messages")
async def winlog(interaction: discord.Interaction, clan: str, channel: discord.TextChannel):
    if not await managers_only(interaction):
        return
    tag = clan.strip().strip("[]").strip().upper()
    if not tag:
        return await interaction.response.send_message("Give me a clan tag, e.g. MPOW.", ephemeral=True)
    set_setting("clan_tag", tag)
    set_setting("win_channel", channel.id)
    set_setting("winlog_enabled", 1)
    await interaction.response.send_message(f"Tracking **[{tag}]** wins in {channel.mention}.")


@bot.tree.command(name="winlog_disable", description="(Manager) Stop posting wins")
async def winlog_disable(interaction: discord.Interaction):
    if not await managers_only(interaction):
        return
    set_setting("winlog_enabled", 0)
    await interaction.response.send_message("Win tracking is off. Turn it back on with /winlog.")


@bot.tree.command(name="winlog_test", description="(Manager) Send a test win message")
async def winlog_test(interaction: discord.Interaction):
    if not await managers_only(interaction):
        return
    channel = bot.get_channel(win_channel_id())
    if channel is None:
        return await interaction.response.send_message("I can't find the win channel. Set it with /winlog.", ephemeral=True)
    mult, _ = event_multiplier()
    sample = {"Time": "TEST", "Map": "Europe", "Contest": "No", "Player Count": "25"}
    embed = win_embed(sample, clan_tag(), round(points_for_game(25) * mult), mult)
    embed.title = "🧪 TEST — " + embed.title
    await send_win(channel, embed, "TEST")
    status = "" if winlog_enabled() else " (Note: win tracking is currently disabled.)"
    await interaction.response.send_message(f"Sent a test message to {channel.mention}.{status}", ephemeral=True)


@bot.tree.command(name="button_add", description="(Manager) Add a custom claim button to win messages")
@app_commands.describe(name="Button name (letters, numbers, spaces)", emoji="Emoji for the button",
                       multiplier="Reward multiplier, e.g. 1.5")
async def button_add(interaction: discord.Interaction, name: str, emoji: str,
                     multiplier: app_commands.Range[float, 0.1, 100.0]):
    if not await managers_only(interaction):
        return
    name = name.strip()
    if not re.fullmatch(r"[A-Za-z0-9 ]{1,20}", name):
        return await interaction.response.send_message("Name must be 1–20 letters, numbers or spaces.", ephemeral=True)
    if len(get_buttons()) >= 24:
        return await interaction.response.send_message("You can have at most 24 custom buttons.", ephemeral=True)
    emoji = emoji.strip()
    # Show a preview of the button; if Discord rejects the emoji, nothing gets saved.
    preview = discord.ui.View()
    preview.add_item(discord.ui.Button(label=f"{name} ({fmt_mult(multiplier)})", emoji=emoji,
                                       style=discord.ButtonStyle.blurple, disabled=True))
    try:
        await interaction.response.send_message(
            f"Added button **{name}** ({fmt_mult(multiplier)}). It shows on new win messages:", view=preview)
    except (discord.HTTPException, ValueError, TypeError):
        if not interaction.response.is_done():
            await interaction.response.send_message(
                "Discord didn't accept that emoji. Use a normal emoji like ⭐ or one from this server.", ephemeral=True)
        return
    run("""INSERT INTO buttons (name, emoji, multiplier) VALUES (?, ?, ?)
           ON CONFLICT (name) DO UPDATE SET emoji = excluded.emoji, multiplier = excluded.multiplier""",
        (name, emoji, multiplier))


@bot.tree.command(name="button_remove", description="(Manager) Remove a custom claim button")
async def button_remove(interaction: discord.Interaction, name: str):
    if not await managers_only(interaction):
        return
    _, count = run("DELETE FROM buttons WHERE name = ?", (name.strip(),))
    msg = f"Removed **{name}**." if count else f"There's no button called **{name}**."
    await interaction.response.send_message(msg)


@button_remove.autocomplete("name")
async def button_name_autocomplete(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=n, value=n) for n, _, _ in get_buttons() if current.lower() in n.lower()][:25]


@bot.tree.command(name="button_info", description="View custom claim buttons")
async def button_info(interaction: discord.Interaction):
    rows = get_buttons()
    lines = ["🏆 **Claim** — ×1 (always shown)"] + [f"{e} **{n}** — {fmt_mult(m)}" for n, e, m in rows]
    embed = discord.Embed(title="Claim buttons", description="\n".join(lines), color=discord.Color.blurple())
    embed.set_footer(text="Each player can claim a win once, with one button.")
    await interaction.response.send_message(embed=embed)


bot.run(TOKEN)
