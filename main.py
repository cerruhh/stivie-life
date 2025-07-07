import asyncio
import discord
import tomllib
import telnetlib3
from discord import app_commands
from typing import Optional, Tuple, List
import time
import re

CONFIG_FILE = "config.toml"

# --- Load config ---
with open(CONFIG_FILE, "rb") as f:
    config = tomllib.load(f)

TELNET_HOST: str = config["credentials"]["host"]
TELNET_PORT: int = config["credentials"]["port"]
TELNET_USER: str = config["credentials"]["username"]
TELNET_PASS: str = config["credentials"]["password"]

DISCORD_TOKEN: str = config["discord"]["token"]
WATCHTOWER_CHANNEL_ID: int = config["discord"]["watchtower_channel_id"]
GUILD_ID: int = config["discord"]["guild_id"]
IGNORED_USERS:List[str] = config.get("discord").get("ignored_users")

def remove_ignored_user_lines(text: str) -> str:
    """
    Removes all lines that start with '<USERNAME> says:' for any username in IGNORED_USERS.
    """
    # Build a regex pattern that matches any of the ignored users at the start of a line
    pattern = r'^(?:' + '|'.join(re.escape(user) for user in IGNORED_USERS) + r') says:.*$'
    # Remove matching lines
    return re.sub(pattern, '', text, flags=re.MULTILINE).strip()

class TelnetDiscordBridge:
    """
    Handles the telnet connection and relays messages between Discord and telnet.
    """

    def __init__(self) -> None:
        self.reader: Optional[telnetlib3.TelnetReader] = None
        self.writer: Optional[telnetlib3.TelnetWriter] = None
        self.connected: bool = False
        self.listen_task: Optional[asyncio.Task] = None
        self.discord_channel: Optional[discord.TextChannel] = None
        self.connect_time: Optional[float] = None

    async def connect(self, channel: discord.TextChannel) -> Tuple[bool, str]:
        """
        Connect to the telnet server and log in.
        """
        if self.connected:
            return False, "Already connected."
        try:
            self.reader, self.writer = await telnetlib3.open_connection(
                TELNET_HOST, TELNET_PORT, encoding='utf8', force_binary=True
            )
            self.discord_channel = channel
            await asyncio.sleep(0.5)
            await self._send_line(TELNET_USER)
            await asyncio.sleep(0.5)
            await self._send_line(TELNET_PASS)
            await asyncio.sleep(2)
            self.connected = True
            self.listen_task = asyncio.create_task(self._listen_telnet())
            return True, "Connected to telnet server."
        except Exception as e:
            self.connected = False
            return False, f"Failed to connect: {e}"

    async def disconnect(self) -> Tuple[bool, str]:
        """
        Disconnect from the telnet server.
        """
        if not self.connected:
            return False, "Not connected."
        try:
            if self.listen_task:
                self.listen_task.cancel()
                try:
                    await self.listen_task
                except asyncio.CancelledError:
                    pass
            if self.writer:
                self.writer.close()
            self.connected = False
            return True, "Disconnected."
        except Exception as e:
            return False, f"Error disconnecting: {e}"

    async def send(self, message: str) -> None:
        """
        Send a message to the telnet server.
        """
        if self.connected and self.writer:
            await self._send_line(message)

    async def _send_line(self, line: str) -> None:
        self.writer.write(line + "\r\n")  # write() expects a string, will encode as UTF-8
        await self.writer.drain()

    async def _listen_telnet(self) -> None:
        """
        Listen for messages from telnet and send them to Discord.
        """
        try:
            while self.connected:
                data: str = await self.reader.read(1024)
                if data and self.discord_channel:
                    msg = f"```ansi\n{data}\n```"
                    await self.discord_channel.send(remove_ignored_user_lines(text=msg))
        except asyncio.CancelledError:
            pass
        except Exception as e:
            if self.discord_channel:
                await self.discord_channel.send(f"Telnet listener error: {e}")


# --- Discord Bot ---
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True  # Needed to read channel messages

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

bridge = TelnetDiscordBridge()


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user}")
    try:
        synced = await tree.sync(guild=discord.Object(id=GUILD_ID))
        print(f"Synced {len(synced)} commands.")
    except Exception as e:
        print(f"Sync error: {e}")


@tree.command(
    name="connect",
    description="Connect to the telnet server",
    guild=discord.Object(id=GUILD_ID)
)
async def connect_command(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    channel = bot.get_channel(WATCHTOWER_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        await interaction.followup.send("Configured channel is not a text channel.", ephemeral=True)
        return
    success, msg = await bridge.connect(channel)
    if success:
        bridge.connect_time = time.monotonic()  # Set the timestamp for ignoring first 5 seconds
    await interaction.followup.send(msg, ephemeral=True)

@tree.command(
    name="disconnect",
    description="Disconnect from the telnet server",
    guild=discord.Object(id=GUILD_ID)
)
async def disconnect_command(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    success, msg = await bridge.disconnect()
    await interaction.followup.send(msg, ephemeral=True)


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return
    if message.channel.id != WATCHTOWER_CHANNEL_ID:
        return
    # If not a command, relay to telnet
    if bridge.connected:
        await bridge.send(message.content)

bot.run(DISCORD_TOKEN)
