# LurkerPurge

A Discord bot that finds members who joined your server a while ago but have
**never posted anything**, and removes them, after you've reviewed the list and
confirmed.

It's useful when silent accounts are watching a community without taking part,
for example stalkers, scrapers, or alt accounts.

- **Dry run first:** you get a spreadsheet of exactly who matches before anything happens.
- **Confirm button:** nothing is removed until a moderator clicks it.
- **Private replies:** only the moderator who ran a command sees the results.
- **Logged:** every kick or ban is logged with a reason in Discord's audit log and in `purge_log.csv`.

## Commands

Only members with the **Kick Members** permission see these (configurable in
Server Settings → Integrations).

| Command | What it does |
|---|---|
| `/lurkers scan` | Reads every channel and thread the bot can see and records who has posted. Run it once; after that the bot tracks new posts live. Ends with a summary of how many members never posted. |
| `/lurkers list days:90` | Dry run: everyone who joined 90+ days ago and never posted, as a CSV. |
| `/lurkers purge days:90 action:ban` | The same list, then a red confirm button. `kick` lets people rejoin with an invite; `ban` doesn't. |
| `/lurkers status` | When the last scan ran and which channels couldn't be read. |

Pick the command from the menu that pops up when you type `/lurkers`. If you
type the whole thing and press Enter, it's sent as a normal message.

### Who is never removed

Bots, the server owner, admins, server boosters, anyone with a role listed in
`EXEMPT_ROLE_IDS`, and anyone whose role is at or above the bot's own role.

### What counts as "posted"

Any normal message or reply in any channel or thread, or using a slash command.
Automatic messages like "X joined the server" don't count.

Not visible to the bot:

- deleted messages (someone whose posts were all deleted looks like a lurker)
- reactions
- voice-only activity
- "days" counts from the member's most recent join, so leaving and rejoining resets it

### Channels the bot can't read

If the bot can't read a channel, members who can't see it either are judged
normally, since they can't have posted there. Members who *can* see it are listed
as **uncertain** and left alone, unless you add `include_uncertain:True` to
`/lurkers purge`.

## Run it yourself

You need a computer or server that stays on while you use the bot, with either
**Docker** or **Python 3.10+**.

### 1. Create the Discord bot

1. Go to <https://discord.com/developers/applications> and click **New Application**. Name it, e.g. *LurkerPurge*.
2. Open the **Bot** tab:
   - Click **Reset Token** and copy the token. You'll need it in step 3. Never share it or paste it anywhere public.
   - Turn **Public Bot** off, so only you can add it to servers.
   - Under *Privileged Gateway Intents*, turn on **Server Members Intent**. The other two can stay off.
   - Click **Save Changes**.
3. Note the **Application ID** on the *General Information* tab.

### 2. Invite it to your server

Open this link with your Application ID in place of `YOUR_APP_ID`:

```
https://discord.com/oauth2/authorize?client_id=YOUR_APP_ID&scope=bot+applications.commands&permissions=17179935750
```

The permissions in that link are View Channels, Read Message History, Manage Threads, Kick Members, and Ban Members.

Then go to **Server Settings → Roles** and drag the bot's role **above** the roles
of the people it should be able to remove. Discord won't let a bot remove anyone
whose role is equal to or higher than its own.

> **If the link only shows "opening Discord app" and nothing happens:** paste the
> link into any Discord chat and click it there instead.

### 3. Configure

```sh
git clone https://github.com/sirus20x6/lurkerpurge.git
cd lurkerpurge
cp .env.example .env
```

Edit `.env`:

- `DISCORD_TOKEN`: the token from step 1.
- `GUILD_ID` (recommended): your server's ID. To get it, turn on Developer Mode
  in Discord (User Settings → Advanced), then right-click the server icon →
  **Copy Server ID**.
- `EXEMPT_ROLE_IDS` (optional): roles to protect, comma-separated. Get a role's ID by right-clicking it with Developer Mode on.

### 4. Start it

**With Docker (recommended: restarts automatically):**

```sh
docker compose up -d --build
docker logs -f lurkerpurge      # watch for "logged in as ..."; Ctrl+C to stop watching
```

To update later, run `git pull && docker compose up -d --build`. To stop it, run `docker compose down`.

**With Python:**

```sh
pip install -r requirements.txt
python bot.py
```

Keep the terminal open; the bot stops when you close it.

When you see `logged in as LurkerPurge#1234`, go to your server and run
`/lurkers scan`. If the commands don't appear, press Ctrl+R to reload Discord.

### Troubleshooting

| Problem | Fix |
|---|---|
| `PrivilegedIntentsRequired` on startup | Turn on **Server Members Intent** (step 1) and save. |
| `Improper token has been passed` | The token in `.env` is wrong or was reset. Copy a fresh one. |
| `/lurkers` doesn't show up | Set `GUILD_ID`, restart the bot, and reload Discord. Make sure the invite link included `applications.commands`. |
| Some members "can't be removed" | They're admins or at/above the bot's role. Move the bot's role higher. |
| The scan reports channels it couldn't read | Give the bot's role View Channels + Read Message History there, then scan again. |
| Every command gets answered twice or errors | Two copies of the bot are running with the same token. Stop one. |

## Data and privacy

The bot stores only user IDs of people who have posted (`purge.db`) and a log of
removals (`purge_log.csv`). It doesn't need Discord's Message Content intent and
never reads or stores what anyone wrote. With Docker, both files live in the
`lurkerpurge-data` volume; with Python, they're next to `bot.py`.

## License

MIT
