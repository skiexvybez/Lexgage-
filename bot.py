import os
import re
import sqlite3
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import commands, tasks

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DB_PATH = os.getenv("DB_PATH", "engagement_board.db")
POST_LIFETIME_HOURS = 48

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                url TEXT NOT NULL,
                created_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS completions (
                submission_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                completed_at TEXT NOT NULL,
                PRIMARY KEY (submission_id, user_id)
            )
        """)

    logger.info("Database initialized.")


def now_utc():
    return datetime.now(timezone.utc)


def valid_url(url):
    try:
        parsed = urlparse(url)
        return (
            parsed.scheme in ("http", "https")
            and bool(parsed.netloc)
            and "." in parsed.netloc
        )
    except Exception:
        return False


def expire_old_posts():
    cutoff = (now_utc() - timedelta(hours=POST_LIFETIME_HOURS)).isoformat()

    with db() as conn:
        conn.execute(
            """
            UPDATE submissions
            SET active = 0
            WHERE active = 1 AND created_at <= ?
            """,
            (cutoff,),
        )


@tasks.loop(minutes=5)
async def expiry_task():
    expire_old_posts()


@bot.event
async def on_ready():
    logger.info("Logged in as %s", bot.user)

    if not expiry_task.is_running():
        expiry_task.start()

    try:
        synced = await bot.tree.sync()
        logger.info("Synced %s slash commands.", len(synced))
    except Exception:
        logger.exception("Failed to sync slash commands.")


@bot.tree.command(
    name="submit",
    description="Submit a post link to this server's engagement board.",
)
@app_commands.describe(url="The link you want members to engage with.")
async def submit(interaction: discord.Interaction, url: str):
    if interaction.guild_id is None:
        await interaction.response.send_message(
            "Use this command inside a server, not in a DM.",
            ephemeral=True,
        )
        return

    if not valid_url(url):
        await interaction.response.send_message(
            "Please provide a valid URL beginning with http:// or https://.",
            ephemeral=True,
        )
        return

    created = now_utc().isoformat()

    with db() as conn:
        existing = conn.execute(
            """
            SELECT id FROM submissions
            WHERE guild_id = ? AND user_id = ?
              AND url = ? AND active = 1
            """,
            (interaction.guild_id, interaction.user.id, url),
        ).fetchone()

        if existing:
            await interaction.response.send_message(
                "You've already submitted that link to this server's board.",
                ephemeral=True,
            )
            return

        conn.execute(
            """
            INSERT INTO submissions
                (guild_id, user_id, username, url, created_at, active)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (
                interaction.guild_id,
                interaction.user.id,
                interaction.user.display_name,
                url,
                created,
            ),
        )

    await interaction.response.send_message(
        "✅ Your link has been added to this server's engagement board. "
        "It will expire after 48 hours.",
        ephemeral=True,
    )


@bot.tree.command(
    name="board",
    description="View active links on this server's engagement board.",
)
async def board(interaction: discord.Interaction):
    if interaction.guild_id is None:
        await interaction.response.send_message(
            "Use this command inside a server.",
            ephemeral=True,
        )
        return

    expire_old_posts()

    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, user_id, username, url, created_at
            FROM submissions
            WHERE guild_id = ? AND active = 1
            ORDER BY created_at DESC
            LIMIT 15
            """,
            (interaction.guild_id,),
        ).fetchall()

    if not rows:
        await interaction.response.send_message(
            "The engagement board is empty. Use `/submit` to add a link.",
            ephemeral=True,
        )
        return

    embed = discord.Embed(
        title="📋 Engagement Board",
        description="Active links for this server. Posts expire after 48 hours.",
        color=discord.Color.blurple(),
    )

    for row in rows:
        embed.add_field(
            name=f"Post #{row['id']} — {row['username']}",
            value=f"{row['url']}\nSubmitted by <@{row['user_id']}>",
            inline=False,
        )

    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="done",
    description="Mark a submitted post as engaged with.",
)
@app_commands.describe(post_id="The ID shown on the engagement board.")
async def done(interaction: discord.Interaction, post_id: int):
    if interaction.guild_id is None:
        await interaction.response.send_message(
            "Use this command inside a server.",
            ephemeral=True,
        )
        return

    with db() as conn:
        row = conn.execute(
            """
            SELECT id FROM submissions
            WHERE id = ? AND guild_id = ? AND active = 1
            """,
            (post_id, interaction.guild_id),
        ).fetchone()

        if not row:
            await interaction.response.send_message(
                "That active post wasn't found on this server's board.",
                ephemeral=True,
            )
            return

        if conn.execute(
            """
            SELECT 1 FROM completions
            WHERE submission_id = ? AND user_id = ?
            """,
            (post_id, interaction.user.id),
        ).fetchone():
            await interaction.response.send_message(
                "You've already marked this post as completed.",
                ephemeral=True,
            )
            return

        conn.execute(
            """
            INSERT INTO completions (submission_id, user_id, completed_at)
            VALUES (?, ?, ?)
            """,
            (post_id, interaction.user.id, now_utc().isoformat()),
        )

    await interaction.response.send_message(
        f"✅ You've marked post #{post_id} as completed.",
        ephemeral=True,
    )


@bot.tree.command(
    name="mylinks",
    description="View your active submissions in this server.",
)
async def mylinks(interaction: discord.Interaction):
    if interaction.guild_id is None:
        await interaction.response.send_message(
            "Use this command inside a server.",
            ephemeral=True,
        )
        return

    expire_old_posts()

    with db() as conn:
        rows = conn.execute(
            """
            SELECT id, url FROM submissions
            WHERE guild_id = ? AND user_id = ? AND active = 1
            ORDER BY created_at DESC
            """,
            (interaction.guild_id, interaction.user.id),
        ).fetchall()

    if not rows:
        await interaction.response.send_message(
            "You have no active submissions in this server.",
            ephemeral=True,
        )
        return

    text = "\n".join(
        f"**Post #{row['id']}** — {row['url']}"
        for row in rows
    )

    await interaction.response.send_message(text, ephemeral=True)


@bot.tree.command(
    name="delete",
    description="Remove one of your own active submissions.",
)
@app_commands.describe(post_id="The ID of your submission.")
async def delete(interaction: discord.Interaction, post_id: int):
    if interaction.guild_id is None:
        await interaction.response.send_message(
            "Use this command inside a server.",
            ephemeral=True,
        )
        return

    with db() as conn:
        row = conn.execute(
            """
            SELECT id FROM submissions
            WHERE id = ? AND guild_id = ?
              AND user_id = ? AND active = 1
            """,
            (post_id, interaction.guild_id, interaction.user.id),
        ).fetchone()

        if not row:
            await interaction.response.send_message(
                "That active post wasn't found, or it doesn't belong to you.",
                ephemeral=True,
            )
            return

        conn.execute(
            """
            UPDATE submissions
            SET active = 0
            WHERE id = ? AND guild_id = ?
            """,
            (post_id, interaction.guild_id),
        )

    await interaction.response.send_message(
        f"🗑️ Post #{post_id} has been removed from the board.",
        ephemeral=True,
    )


@bot.tree.command(
    name="help",
    description="Show the engagement bot commands.",
)
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🤖 Skiex Engagement Bot",
        description="Help your community organize engagement links.",
        color=discord.Color.green(),
    )

    embed.add_field(
        name="Commands",
        value=(
            "`/submit url` — submit a link\n"
            "`/board` — view active links\n"
            "`/done post_id` — mark a post as completed\n"
            "`/mylinks` — view your active submissions\n"
            "`/delete post_id` — delete your own submission\n"
            "`/help` — show this guide"
        ),
        inline=False,
    )

    embed.add_field(
        name="Expiry",
        value="Submitted posts automatically expire after 48 hours.",
        inline=False,
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(
            "Missing DISCORD_TOKEN environment variable. "
            "Set it in your hosting dashboard."
        )

    init_db()
    bot.run(TOKEN)
