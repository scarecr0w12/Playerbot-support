---
description: Sync Discord slash commands after adding or renaming commands in Playerbot-support
---

# Sync slash commands

Discord slash commands do not update automatically — a **restart of `main.py`** is required to push changes to the Discord API.

## When this is needed

- After adding a new slash command to any cog.
- After renaming, removing, or changing parameter types on a command.
- After adding a new cog to the load list in `main.py`.

## Steps

1. **Confirm `.env` is populated** — `DISCORD_TOKEN`, `DISCORD_GUILD_ID` (for guild-scoped sync), and all required service vars must be set.

2. Stop any running bot process.

3. Start the bot (this triggers `on_ready` → command tree sync):
```
python main.py
```

4. In Discord, verify the updated commands appear in the slash command picker for the target guild.

## Notes

- `main.py` syncs commands globally **or** to a specific guild depending on the `tree.sync` call — do not change this without understanding the rate-limit implications of global sync (~1 hour propagation delay vs instant guild sync).
- If a command does not appear after restart, check that the cog was loaded without errors in the bot log output.
