"""Post a one-off message to the Discord #commands channel.

Standalone on purpose: run.command's auto-update supervisor needs to announce
"an update landed, restarting" at exactly the moment the tracker itself is not
running, so it cannot go through the app's outbox.

    python3 notify_discord.py "message"
    python3 notify_discord.py --title "Update" --body "..." [--colour 0x2ECC71]

Never fails loudly — a broken notification must not stop the supervisor from
restarting the tracker, which is the part that actually matters.
"""

from __future__ import annotations

import argparse
import sys

import requests

import config


def post(title: str, body: str, colour: int = 0x5865F2) -> bool:
    if not (config.DISCORD_BOT_TOKEN and config.DISCORD_COMMANDS_CHANNEL_ID):
        return False
    embed = {
        "title": title[:256],
        "description": body[:4096],
        "color": colour,
        "footer": {"text": "auto-update"},
    }
    try:
        resp = requests.post(
            f"https://discord.com/api/v10/channels/"
            f"{config.DISCORD_COMMANDS_CHANNEL_ID}/messages",
            headers={"Authorization": f"Bot {config.DISCORD_BOT_TOKEN}"},
            json={"embeds": [embed], "allowed_mentions": {"parse": []}},
            timeout=10,
        )
        return resp.status_code < 300
    except Exception:
        return False


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("message", nargs="?", default="")
    p.add_argument("--title", default="ATC Tracker")
    p.add_argument("--body", default="")
    p.add_argument("--colour", "--color", default="0x5865F2")
    args = p.parse_args(argv)
    try:
        colour = int(str(args.colour), 0)
    except ValueError:
        colour = 0x5865F2
    ok = post(args.title, args.body or args.message, colour)
    # Exit 0 either way: the caller is a supervisor loop and a failed Discord
    # post is not a reason for it to take any different action.
    if not ok:
        print("notify_discord: not sent (Discord not configured or unreachable)",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
