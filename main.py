import discord
from discord.ext import commands
from discord import app_commands
from discord.ui import View, Select
import json
import os
import asyncio
import aiohttp
import base64
import zipfile
from io import BytesIO
import requests
from datetime import datetime, timezone
import google.generativeai as genai
from googletrans import Translator
from collections import defaultdict
from dotenv import load_dotenv


load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GOOGLE_GENAI_KEY = os.getenv("GOOGLE_GENAI_KEY")
JIRA_BASE_URL = os.getenv("JIRA_BASE_URL")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")

translator = Translator()

thread_timers = defaultdict(lambda: None)
translation_threads = {}

# Configure Gemini
genai.configure(api_key=GOOGLE_GENAI_KEY)
gemini_model = genai.GenerativeModel(model_name="gemini-2.0-flash")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# --- Persistent storage files ---
LEVEL_FILE = "role_levels.json"
ANNOUNCE_FILE = "announce_channels.json"
TRANSLATE_FILE = "translate_channels.json"

if os.path.exists(TRANSLATE_FILE):
    with open(TRANSLATE_FILE, "r") as f:
        translate_channels = {int(k): [int(cid) for cid in v] for k, v in json.load(f).items()}
else:
    translate_channels = {}

def save_translate_channels():
    with open(TRANSLATE_FILE, "w") as f:
        json.dump({str(k): v for k, v in translate_channels.items()}, f)

# --- Load Role Levels ---
if os.path.exists(LEVEL_FILE):
    with open(LEVEL_FILE, "r") as f:
        role_levels = {int(g): {int(r): lvl for r, lvl in d.items()} for g, d in json.load(f).items()}
else:
    role_levels = {}

# --- Load Announcement Channels ---
if os.path.exists(ANNOUNCE_FILE):
    with open(ANNOUNCE_FILE, "r") as f:
        announce_channels = {int(k): int(v) for k, v in json.load(f).items()}
else:
    announce_channels = {}

# --- Save Functions ---
def save_levels():
    with open(LEVEL_FILE, "w") as f:
        json.dump(role_levels, f)

def save_announce_channels():
    with open(ANNOUNCE_FILE, "w") as f:
        json.dump(announce_channels, f)

# --- Permission System ---
def get_user_level(member):
    roles = role_levels.get(member.guild.id, {})
    return max([roles.get(role.id, 0) for role in member.roles], default=0)

def requires_level(min_level):
    def predicate(ctx):
        return get_user_level(ctx.author) >= min_level
    return commands.check(predicate)

def is_flag_emoji(emoji):
    return (
        len(emoji) == 2 and
        all(0x1F1E6 <= ord(c) <= 0x1F1FF for c in emoji)
    )

def emoji_to_country_code(emoji):
    return ''.join([chr(ord(c) - 127397) for c in emoji])

@bot.command()
@commands.has_permissions(administrator=True)
async def translateconfig(ctx, action: str, channel: discord.TextChannel):
    guild_id = ctx.guild.id
    if guild_id not in translate_channels:
        translate_channels[guild_id] = []

    if action.lower() == "add":
        if channel.id in translate_channels[guild_id]:
            await ctx.send(f"✅ {channel.mention} is already in the translation list.")
        else:
            translate_channels[guild_id].append(channel.id)
            save_translate_channels()
            await ctx.send(f"✅ Added {channel.mention} to the translation-enabled channels.")
    elif action.lower() == "remove":
        if channel.id in translate_channels[guild_id]:
            translate_channels[guild_id].remove(channel.id)
            save_translate_channels()
            await ctx.send(f"✅ Removed {channel.mention} from the translation-enabled channels.")
        else:
            await ctx.send(f"⚠️ {channel.mention} is not in the translation list.")
    else:
        await ctx.send("⚠️ Usage: `!translateconfig <add/remove> #channel`")

@bot.command()
@commands.has_permissions(administrator=True)
async def rolelevel(ctx, role: discord.Role, level: int):
    gid = ctx.guild.id
    if gid not in role_levels:
        role_levels[gid] = {}
    role_levels[gid][role.id] = level
    save_levels()
    await ctx.send(f"Set level `{level}` for role **{role.name}**.")

@bot.event
async def on_raw_reaction_add(payload):
    try:
        channel = await bot.fetch_channel(payload.channel_id)
        message = await channel.fetch_message(payload.message_id)
        user = await bot.fetch_user(payload.user_id)
        emoji = payload.emoji
        emoji_name = emoji.name
        guild_id = payload.guild_id

        print(f"📡 Reaction detected from {user.name} in #{channel.name} with {emoji_name}")

        if not guild_id or guild_id not in translate_channels or channel.id not in translate_channels[guild_id]:
            return

        if not is_flag_emoji(emoji_name) or not message.content:
            return

        lang_code = emoji_to_country_code(emoji_name)
        thread_key = (message.id, lang_code)

        if thread_key in translation_threads:
            thread = translation_threads[thread_key]
            try:
                ping = await thread.send(f"{user.mention}")
                await asyncio.sleep(1)
                await ping.delete()
                print(f"👋 Pinged {user.name} in existing thread.")
            except Exception as e:
                print(f"⚠️ Failed to ping in thread: {e}")
            return

        prompt = f"Only output the single best answer for this prompt. Do not output anything else. " \
                 f"Understand that the content of the prompt may be in slang and need to be " \
                 f"completed for an accurate translation." \
                 f"Translate this to the native language of the country with the ISO code {lang_code} " \
                 f"(formal and clear):\"{message.content}\""

        print(f"🌐 Prompt: {prompt}")
        response = gemini_model.generate_content(prompt)
        translated = response.text.strip()

        thread = await channel.create_thread(
            name=f"[{lang_code}] Translation of Msg {message.id}",
            type=discord.ChannelType.private_thread,
            auto_archive_duration=60,
            invitable=False
        )

        await thread.send(f"📄 Original message:\n{message.content}")
        await thread.send(f"🌍 Translation ({emoji_name} / {lang_code}):\n{translated}")
        ping = await thread.send(f"{user.mention}")
        await asyncio.sleep(1)
        await ping.delete()

        translation_threads[thread_key] = thread

    except Exception as e:
        print(f"❌ Error in on_raw_reaction_add: {e}")

@bot.event
async def on_message(message):
    if message.channel.type == discord.ChannelType.private_thread:
        thread = message.channel

        if thread_timers[thread.id]:
            thread_timers[thread.id].cancel()

        async def delete_after_inactive():
            await asyncio.sleep(3600)
            try:
                await thread.delete()
                print(f"🧹 Deleted inactive thread: {thread.name}")
            except Exception as e:
                print(f"⚠️ Could not delete thread: {e}")

        task = bot.loop.create_task(delete_after_inactive())
        thread_timers[thread.id] = task

    await bot.process_commands(message)

@bot.command()
async def genInviteLink(ctx, channel: discord.TextChannel):
    try:
        invite = await channel.create_invite(max_age=0, max_uses=0, unique=True)
        await ctx.send(f"Here's the invite link for {channel.mention}: {invite.url}")
    except Exception as e:
        print(e)
        await ctx.send("ANamedPlayer messed up ping him quick")

@bot.command()
@commands.has_permissions(administrator=True)
async def announceconfig(ctx, channel: discord.TextChannel):
    announce_channels[ctx.guild.id] = channel.id
    save_announce_channels()
    await ctx.send(f"Announcement channel set to {channel.mention}")

@bot.command()
@requires_level(9)
async def announce(ctx, target_channel: discord.TextChannel, flag_emoji: str = '🇺🇸', message_link: str = None):
    try:
        # Identify the source message
        if message_link:
            parts = message_link.strip().split('/')
            channel_id = int(parts[-2])
            message_id = int(parts[-1])
            source_channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
            message = await source_channel.fetch_message(message_id)
        elif ctx.message.reference:
            ref = ctx.message.reference.resolved
            if isinstance(ref, discord.Message):
                message = ref
            else:
                raise ValueError("Couldn't read replied message.")
        else:
            await ctx.send("⚠️ Please provide a message link or reply to a message.")
            return
        # Translate if valid flag emoji
        content = message.content if message.content else None
        if content and len(flag_emoji) == 2 and all(0x1F1E6 <= ord(c) <= 0x1F1FF for c in flag_emoji) and \
                flag_emoji != '🇺🇸':
            lang_code = ''.join([chr(ord(c) - 127397) for c in flag_emoji])
            prompt = f"Only output the single best answer for this prompt. Do not output anything else. " \
                     f"Understand that the content of the prompt may be in slang and need to be " \
                     f"completed for an accurate translation." \
                     f"Translate this to the native language of the country with the ISO code {lang_code} " \
                     f"(formal and clear):\"{content}\""
            response = gemini_model.generate_content(prompt)
            content = response.text.strip()

        files = [await a.to_file() for a in message.attachments]
        embeds = message.embeds if message.embeds else None

        await target_channel.send(content=content, files=files, embeds=embeds)
        await ctx.send("✅ Announcement sent.")

    except Exception as e:
        await ctx.send(f"❌ Error: {e}")

@bot.command()
@requires_level(8)
@commands.has_permissions(manage_messages=True)
async def clear(ctx, amount: str):
    try:
        if amount.lower() == "all":
            deleted = await ctx.channel.purge()
            confirmation = await ctx.send(f"🧹 Deleted all messages I could.")
        else:
            count = int(amount)
            deleted = await ctx.channel.purge(limit=count)
            confirmation = await ctx.send(f"🧹 Deleted {len(deleted)} messages.")
        await asyncio.sleep(3)
        await confirmation.delete()
    except Exception as e:
        await ctx.send(f"❌ Failed to clear messages: {e}")
    except Exception as e:
        await ctx.send(f"❌ Failed to clear messages: {e}")

       
        
        
'''
    JIRA INTEGRATION PORTION
'''
def get_guild_config(guild_id):
    try:
        with open("guild_config.json", "r") as f:
            config = json.load(f)
        return config.get(str(guild_id), {})
    except FileNotFoundError:
        return {}
@bot.event
async def on_reaction_add(reaction, user):
    if user.bot or not reaction.message.guild:
        return

    channel = reaction.message.channel
    if not isinstance(channel, discord.Thread) or not isinstance(channel.parent, discord.ForumChannel):
        return

    member = await reaction.message.guild.fetch_member(user.id)
    if not member.guild_permissions.manage_messages:
        return

    config = get_guild_config(reaction.message.guild.id)
    configured_emoji = config.get("jiraEmoji")
    required_tag_id = int(config.get("forumTagId")) if config.get("forumTagId") else None
    emoji = reaction.emoji
    matched = (
        configured_emoji and (
        str(emoji) == configured_emoji or
        (hasattr(emoji, "id") and f"<:{emoji.name}:{emoji.id}>" == configured_emoji)
        )
        and (
            not required_tag_id or
            any(tag.id == required_tag_id for tag in channel.applied_tags)
        )
    )

    if matched:
        try:
            messages = await fetch_thread_messages(channel)
            jira_issue = await create_jira_issue_from_thread(channel, messages)

            all_attachments = [att for msg in messages for att in msg.attachments]
            await upload_attachments_to_jira(jira_issue["id"], all_attachments)

            config = get_guild_config(reaction.message.guild.id)
            log_channel_id = config.get("logChannelId")
            log_channel = bot.get_channel(int(log_channel_id)) if log_channel_id else None

            if log_channel and isinstance(log_channel, discord.TextChannel):
                await log_channel.send(
                    f"Bug synced to Jira: [{jira_issue['key']}]({JIRA_BASE_URL}/browse/{jira_issue['key']})\n"
                    f"Thread: https://discord.com/channels/{channel.guild.id}/{channel.id}"
                )
        except Exception as e:
            print(f"Jira error: {e}")
            await channel.send("Failed to sync to Jira.")



async def format_thread_to_markdown(thread: discord.Thread, messages: list[discord.Message]) -> str:
    lines = [
        f"# {thread.name}",
        f"https://discord.com/channels/{thread.guild.id}/{thread.parent_id}/threads/{thread.id}",
        ""
    ]
    for msg in sorted(messages, key=lambda m: m.created_at):
        timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"**{msg.author.display_name}** ({timestamp}):")
        if msg.content:
            lines.append(msg.content)
        for att in msg.attachments:
            lines.append(f"[Attachment: {att.filename}]({att.url})")
        lines.append("")
    return "\n".join(lines)


import zipfile
from io import BytesIO

def create_zip_from_logs(files: list[dict]) -> BytesIO:
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for file in files:
            zip_file.writestr(file["name"], file["content"])
    zip_buffer.seek(0)
    return zip_buffer


async def fetch_thread_messages(thread: discord.Thread) -> list[discord.Message]:
    return [msg async for msg in thread.history(limit=100, oldest_first=True)]

async def create_jira_issue_from_thread(thread: discord.Thread, messages: list[discord.Message]) -> dict:
    jira_base_url = JIRA_BASE_URL
    jira_email = JIRA_EMAIL
    jira_api_token = JIRA_API_TOKEN
    jira_project_key = JIRA_PROJECT_KEY
    
    auth = base64.b64encode(f"{jira_email}:{jira_api_token}".encode()).decode()

    thread_url = f"https://discord.com/channels/{thread.guild.id}/{thread.parent_id}/threads/{thread.id}"

    content_blocks = [{
        "type": "paragraph",
        "content": [
            {"type": "text", "text": f"Bug reported in thread: {thread.name} \n"},
            {"type": "text", "text": "(View on Discord)", "marks": [{"type": "link", "attrs": {"href": thread_url}}]},
            {"type": "text", "text": f" Discord Server: {thread.guild.name}"}
        ],
    }]

    for msg in messages:
        attachments = ", ".join([a.filename for a in msg.attachments])
        value = msg.content or ""
        if attachments:
            value += f"\n\nAttachments: {attachments}" if value else f"Attachments: {attachments}"
        content_blocks.append({
            "type": "paragraph",
            "content": [
                {"type": "text", "text": f"Message by {msg.author.display_name}: ", "marks": [{"type": "strong"}]},
                {"type": "text", "text": value or "[no text]"}
            ]
        })

    payload = {
        "fields": {
            "project": {"key": jira_project_key},
            "summary": thread.name,
            "description": {"type": "doc", "version": 1, "content": content_blocks},
            "issuetype": {"name": "Bug"}
        }
    }

    headers = {
        "Authorization": f"Basic {auth}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(f"{jira_base_url}/rest/api/3/issue", json=payload, headers=headers) as resp:
            if resp.status != 201:
                raise Exception(f"Failed to create Jira issue: {await resp.text()}")
            return await resp.json()

async def upload_attachments_to_jira(issue_id: str, attachments: list[discord.Attachment]):
    jira_base_url = JIRA_BASE_URL
    auth = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth}",
        "X-Atlassian-Token": "no-check"
    }

    async with aiohttp.ClientSession() as session:
        for attachment in attachments:
            async with session.get(attachment.url) as file_resp:
                file_data = await file_resp.read()
                form = aiohttp.FormData()
                form.add_field("file", file_data, filename=attachment.filename)

                async with session.post(
                    f"{jira_base_url}/rest/api/3/issue/{issue_id}/attachments",
                    data=form,
                    headers=headers
                ) as upload_resp:
                    if upload_resp.status != 200:
                        print(f"Failed to upload {attachment.filename}: {await upload_resp.text()}")


async def upload_attachments_to_jira(issue_id: str, attachments: list[discord.Attachment]):
    jira_base_url = JIRA_BASE_URL
    auth = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_API_TOKEN}".encode()).decode()

    headers = {
        "Authorization": f"Basic {auth}",
        "X-Atlassian-Token": "no-check"
    }

    async with aiohttp.ClientSession() as session:
        for attachment in attachments:
            async with session.get(attachment.url) as file_resp:
                file_data = await file_resp.read()
                form = aiohttp.FormData()
                form.add_field("file", file_data, filename=attachment.filename)

                async with session.post(
                    f"{jira_base_url}/rest/api/3/issue/{issue_id}/attachments",
                    data=form,
                    headers=headers
                ) as upload_resp:
                    if upload_resp.status != 200:
                        print(f"Failed to upload {attachment.filename}: {await upload_resp.text()}")


# Simulate your config functions
def set_guild_config(guild_id, updates):
    try:
        with open("guild_config.json", "r") as f:
            config = json.load(f)
    except FileNotFoundError:
        config = {}
    guild_config = config.get(str(guild_id), {})
    guild_config.update(updates)
    config[str(guild_id)] = guild_config
    with open("guild_config.json", "w") as f:
        json.dump(config, f, indent=2)

@tree.command(name="setup_bug_forum", description="Setup the bug reporting forum and log channel")
@app_commands.default_permissions(administrator=True)
async def setup_bug_forum(interaction: discord.Interaction):
    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("Guild not found.", ephemeral=True)
        return

    text_channels = [c for c in guild.text_channels if c.permissions_for(guild.me).view_channel]
    forum_channels = [c for c in guild.channels if isinstance(c, discord.ForumChannel)]

    if not forum_channels:
        await interaction.response.send_message("No accessible forum channels found.", ephemeral=True)
        return

    options_text = [discord.SelectOption(label=f"#{c.name}", value=str(c.id)) for c in text_channels[:25]]
    options_forum = [discord.SelectOption(label=f"#{c.name}", value=str(c.id)) for c in forum_channels[:25]]

    forum = forum_channels[0]  # default one to fetch tags from
    fetched_forum = await guild.fetch_channel(forum.id)
    if isinstance(fetched_forum, discord.ForumChannel):
        tag_options = [
            discord.SelectOption(label=tag.name, value=tag.id)
            for tag in fetched_forum.available_tags[:25]
        ]
    else:
        tag_options = []

    class SetupView(discord.ui.View):
        @discord.ui.select(placeholder="Select forum channel", options=options_forum, custom_id="forum")
        async def select_forum(self, interaction2: discord.Interaction, select: discord.ui.Select):
            set_guild_config(guild.id, {"forumChannelId": select.values[0]})
            await interaction2.response.send_message(f"Forum set to <#{select.values[0]}>", ephemeral=True)

        @discord.ui.select(placeholder="Select log channel", options=options_text, custom_id="log")
        async def select_log(self, interaction2: discord.Interaction, select: discord.ui.Select):
            set_guild_config(guild.id, {"logChannelId": select.values[0]})
            await interaction2.response.send_message(f"Log set to <#{select.values[0]}>", ephemeral=True)

        if tag_options:
            @discord.ui.select(placeholder="Select required forum tag", options=tag_options, custom_id="tag")
            async def select_tag(self, interaction2: discord.Interaction, select: discord.ui.Select):
                set_guild_config(guild.id, {"forumTagId": select.values[0]})
                await interaction2.response.send_message(f"Required tag set: `{select.values[0]}`", ephemeral=True)

    await interaction.response.send_message(
        "**Setup Bug Reporting**\nSelect forum, log channel, and required tag below:",
        view=SetupView(),
        ephemeral=True
    )

    
@tree.command(name="setup_jira_emoji", description="Set the emoji that will trigger Jira sync")
@app_commands.describe(emoji="Custom or unicode emoji to use for Jira sync")
@app_commands.default_permissions(administrator=True)
async def setup_jira_emoji(interaction: discord.Interaction, emoji: str):
    guild_id = str(interaction.guild_id)
    try:
        with open("guild_config.json", "r") as f:
            config = json.load(f)
    except FileNotFoundError:
        config = {}
    config[guild_id] = {**config.get(guild_id, {}), "jiraEmoji": emoji}
    with open("guild_config.json", "w") as f:
        json.dump(config, f, indent=2)
    await interaction.response.send_message(f"Jira sync emoji set to: {emoji}", ephemeral=True)

async def create_jira_issue_from_thread(thread: discord.Thread, messages: list[discord.Message]) -> dict:
    jira_base_url = JIRA_BASE_URL
    jira_email = JIRA_EMAIL
    jira_api_token = JIRA_API_TOKEN
    jira_project_key = JIRA_PROJECT_KEY
    
    auth = base64.b64encode(f"{jira_email}:{jira_api_token}".encode()).decode()

    # Format messages into Atlassian Document Format (ADF)
    content_blocks = [
        {
            "type": "paragraph",
            "content": [
                {"type": "text", "text": f"Bug reported in thread: {thread.name} \n"},
                {
                    "type": "text",
                    "text": "(View on Discord)",
                    "marks": [{"type": "link", "attrs": {"href": f"https://discord.com/channels/{thread.guild.id}/{thread.id}"}}],
                },
                {"type": "text", "text": f" Discord Server: {thread.guild.name}"}
            ],
        }
    ]

    for msg in messages:
        attachments = ", ".join([a.filename for a in msg.attachments])
        value = msg.content or ""
        if attachments:
            value += f"\n\nAttachments: {attachments}" if value else f"Attachments: {attachments}"
        content_blocks.append({
            "type": "paragraph",
            "content": [
                {"type": "text", "text": f"Message by {msg.author.display_name}: ", "marks": [{"type": "strong"}]},
                {"type": "text", "text": value or "[no text]"}
            ]
        })

    payload = {
        "fields": {
            "project": {"key": jira_project_key},
            "summary": thread.name,
            "description": {
                "type": "doc",
                "version": 1,
                "content": content_blocks
            },
            "issuetype": {"name": "Bug"}
        }
    }

    headers = {
        "Authorization": f"Basic {auth}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    async with aiohttp.ClientSession() as session:
        async with session.post(f"{jira_base_url}/rest/api/3/issue", json=payload, headers=headers) as resp:
            if resp.status != 201:
                raise Exception(f"Failed to create Jira issue: {await resp.text()}")
            issue = await resp.json()

    return issue  # contains "key" and "id"
    
@tree.command(name="end_tournament", description="Export all forum bug threads since last tournament")
@app_commands.describe(name="Optional tournament name")
@app_commands.default_permissions(manage_messages=True)
async def end_tournament(interaction: discord.Interaction, name: str = "Unnamed Tournament"):
    config = get_guild_config(interaction.guild_id)
    forum_channel_id = config.get("forumChannelId")
    log_channel_id = config.get("logChannelId")
    required_tag_id = int(config.get("forumTagId")) if config.get("forumTagId") else None
    print(f"required_tag_id: {required_tag_id}")
    last_timestamp = config.get("lastTournamentEnd")

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    last_run = None
    if isinstance(last_timestamp, str):
        try:
            last_run = datetime.fromisoformat(last_timestamp).astimezone(timezone.utc)
        except ValueError:
            pass

    forum = interaction.guild.get_channel(int(forum_channel_id)) if forum_channel_id else None
    log_channel = interaction.guild.get_channel(int(log_channel_id)) if log_channel_id else None

    if not isinstance(forum, discord.ForumChannel):
        await interaction.response.send_message("Invalid or missing forum channel.", ephemeral=True)
        return
    if not isinstance(log_channel, discord.TextChannel):
        await interaction.response.send_message("Invalid or missing log channel.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    active_threads = forum.threads
    matched_threads = [
    thread for thread in active_threads
        if (not last_run or thread.created_at > last_run) and
        (not required_tag_id or any(tag.id == required_tag_id for tag in thread.applied_tags))
    ]


    if not matched_threads:
        await interaction.edit_original_response(content="No new threads found since last tournament.")
        return

    files = []
    for thread in matched_threads:
        messages = [m async for m in thread.history(limit=100)]
        content = await format_thread_to_markdown(thread, messages)
        files.append({"name": f"{thread.name}.md", "content": content})

    zip_buffer = create_zip_from_logs(files)

    # Update timestamp
    set_guild_config(interaction.guild_id, {**config, "lastTournamentEnd": now.isoformat()})

    await log_channel.send(
        content=f"📦 Exported **{len(files)}** thread(s) from **{name}**.",
        file=discord.File(zip_buffer, filename=f"{name.replace(' ', '_')}.zip")
    )

    await interaction.edit_original_response(content="Tournament export sent.")
    
@tree.command(name="mass_sync_jira", description="Mass sync bug threads to Jira")
@app_commands.describe(since="Optional ISO timestamp to override last sync time")
@app_commands.default_permissions(manage_messages=True)
async def mass_sync_jira(interaction: discord.Interaction, since: str = None):
    config = get_guild_config(interaction.guild_id)
    forum_channel_id = config.get("forumChannelId")
    log_channel_id = config.get("logChannelId")
    required_tag_id = int(config.get("forumTagId")) if config.get("forumTagId") else None
    last_timestamp = since or config.get("lastJiraMassSync")

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    last_run = None
    if isinstance(last_timestamp, str):
        try:
            last_run = datetime.fromisoformat(last_timestamp).astimezone(timezone.utc)
        except ValueError:
            pass

    forum = interaction.guild.get_channel(int(forum_channel_id)) if forum_channel_id else None
    log_channel = interaction.guild.get_channel(int(log_channel_id)) if log_channel_id else None

    if not isinstance(forum, discord.ForumChannel):
        await interaction.response.send_message("Invalid or missing forum channel.", ephemeral=True)
        return
    if not isinstance(log_channel, discord.TextChannel):
        await interaction.response.send_message("Invalid or missing log channel.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    matched_threads = [
        thread for thread in forum.threads
        if (not last_run or thread.created_at > last_run) and
           (not required_tag_id or any(tag.id == required_tag_id for tag in thread.applied_tags))
    ]

    if not matched_threads:
        await interaction.edit_original_response(content="No matching threads found for Jira sync.")
        return

    results = []
    for thread in matched_threads:
        try:
            messages = await fetch_thread_messages(thread)
            jira_issue = await create_jira_issue_from_thread(thread, messages)
            await upload_attachments_to_jira(jira_issue["id"], [att for msg in messages for att in msg.attachments])
            results.append(f"[{jira_issue['key']}]({JIRA_BASE_URL}/browse/{jira_issue['key']}) - {thread.name}")
        except Exception as e:
            print(f"Jira sync failed for {thread.name}: {e}")
            results.append(f"{thread.name}: Failed to sync")

    set_guild_config(interaction.guild_id, {**config, "lastJiraMassSync": now.isoformat()})

    # Break large messages into chunks of 2000 characters or less
    chunks = []
    current_chunk = "**Jira Sync Report**\n"
    for line in results:
        if len(current_chunk) + len(line) + 1 > 2000:
            chunks.append(current_chunk)
            current_chunk = ""
        current_chunk += line + "\n"
    if current_chunk:
        chunks.append(current_chunk)

    for chunk in chunks:
        await log_channel.send(chunk)

    await interaction.edit_original_response(content="Mass Jira sync completed.")

@bot.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {bot.user}")

bot.run(DISCORD_TOKEN)
