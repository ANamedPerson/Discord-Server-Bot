import discord
from discord.ext import commands
import json
import os
import asyncio
import google.generativeai as genai
from googletrans import Translator
from collections import defaultdict
from discord.ui import View, Select

translator = Translator()

thread_timers = defaultdict(lambda: None)
translation_threads = {}

# Configure Gemini
genai.configure(api_key="Google-Gemini-Key")
gemini_model = genai.GenerativeModel(model_name="gemini-2.0-flash")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

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

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

bot.run('Discord-Bot-Key')
