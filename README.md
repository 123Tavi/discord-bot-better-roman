# Steam co-op digest for Discord

Once a day, at a random time, this posts one message to a Discord channel listing new Steam
games and demos with online co-op for 3 or more players. It runs for free on GitHub Actions.
It needs no server, no Steam API key and no Python packages.

Each entry shows the game's name (linked to its Steam page), price, short description and max
players. Full games also show their review score:

> **New release · Early Access**
> **Invokyr**
> A co-op survival horror game about a cursed board game. Play in a group of 6 players. Roll the dice…
> Price: ~~$12.99~~ $9.74 (-25%) · Max players: 6 · Reviews: 94% of 264 positive

## Setup

### 1. Create the Discord webhook

1. In Discord, hover over the channel the digest should go to and click its gear icon (**Edit Channel**).
2. Go to **Integrations → Webhooks → New Webhook**.
3. Click the new webhook to open it. Give it a name (for example *Co-op Scout*) and, if you like,
   an avatar. Check that the right channel is selected.
4. Click **Copy Webhook URL**. It looks like `https://discord.com/api/webhooks/1234567890/AbCdEf…`

You need the *Manage Webhooks* permission for the channel (server owners and admins have it). Use a
normal text channel; forum channels need extra setup.

Treat the URL like a password: anyone who has it can post in the channel. If it leaks, delete the
webhook in the same menu and make a new one.

### 2. Put the files on GitHub

Create a new repository on github.com (public or private; see [Notes](#notes)) and add these files,
keeping the folders:

```
coop_digest.py
posted_games.json
README.md
.github/workflows/coop-digest.yml
```

With git, from this folder:

```
git init -b main
git add .
git commit -m "Co-op digest bot"
git remote add origin https://github.com/YOUR-NAME/YOUR-REPO.git
git push -u origin main
```

Or in the browser, use **Add file → Upload files** and drag the files in. If the `.github` folder
doesn't come along, use **Add file → Create new file**, type `.github/workflows/coop-digest.yml` as
the name, and paste in the file's contents.

The workflow file has to be on the default branch (usually `main`), because GitHub only runs
schedules from there.

### 3. Store the webhook URL as a secret

In the repository, go to **Settings → Secrets and variables → Actions → New repository secret**:

- **Name:** `DISCORD_WEBHOOK_URL`
- **Secret:** the webhook URL from step 1

Don't put the URL anywhere else in the repository.

### 4. Test it

Open the **Actions** tab, pick **Co-op digest** on the left, and click **Run workflow**:

- First run it with **Dry run** ticked. The log shows what would be posted, and nothing is sent.
- Then run it with only **Post now** ticked. That posts a digest straight away.

After a run that posts, the repository gets a commit called "Update posted games" from
github-actions. That commit is the bot's record of what it has posted.

After that it runs on its own. The first scheduled run can take up to an hour to appear.

## When it posts

The workflow starts every hour, at 17 minutes past (UTC). Each day the script picks a random time
between 10:00 and 22:00, and the first hourly run after that time posts the digest. Every other run
exits within seconds. GitHub often starts scheduled runs a few minutes late, which only adds to the
randomness. On days with nothing new, nothing is posted.

Those hours are UTC unless you set `TIMEZONE` (see [Settings](#settings)).

## What gets posted

- **Online co-op:** the game's developer must have ticked Steam's "Online Co-op" feature. That rules
  out split-screen-only and LAN-only games. Remote Play Together doesn't count.
- **3+ players:** Steam has no field for player count, so the script reads it from the store
  description ("1-4 players", "up to 3 friends", "4-player co-op", plus common Chinese, Japanese,
  Korean and Russian wording). Games that don't state a count are skipped. Massively multiplayer
  games are kept.
- **Full games:** released in the last 30 days, with at least 250 reviews and at least 80% positive.
  A game has 30 days to reach that bar, so a good one may appear a week or two after launch. Early
  Access launches count. So do games leaving Early Access, which are labelled that way.
- **Demos:** released in the last 7 days, for a game that is still upcoming or came out less than 30
  days ago. Demos have no review requirement. A demo whose description says it is single-player
  only is skipped.
- **Skipped genres:** games with Sports, Turn-Based Tactics, Tactical RPG or Real Time Tactics among
  their top 10 Steam tags.
- **No repeats:** every posted game is recorded in `posted_games.json`, which the workflow commits
  back to the repository. One exception: a game first posted for its demo is posted once more when
  the full game comes out. Set `REPOST_AFTER_DEMO = False` to turn that off.
- **At most 10 per digest,** which fits in one Discord message. Full games come first, most-reviewed
  first, then demos. Sometimes more demos qualify than fit, which is common around Steam Next Fest,
  when dozens come out each day. Then demos of the most-wishlisted games go first, and the rest get
  another chance the next day while they are still under 7 days old.

## Settings

All settings are at the top of `coop_digest.py`. Commit a change and it applies from the next run.

| Setting | Default | What it does |
|---|---|---|
| `MIN_PLAYERS` | `3` | Minimum number of online co-op players |
| `MIN_REVIEWS`, `MIN_POSITIVE` | `250`, `80` | Review bar for full games |
| `EXCLUDED_TAGS` | Sports, Turn-Based Tactics, Tactical RPG, Real Time Tactics | Steam tag names, spelled as on Steam. Add `"Tactical"` to also drop tactical shooters |
| `TAG_DEPTH` | `10` | How many of a game's top tags are checked against `EXCLUDED_TAGS` |
| `GAME_LOOKBACK_DAYS`, `DEMO_LOOKBACK_DAYS` | `30`, `7` | How recent a release has to be to count as new |
| `INCLUDE_EARLY_ACCESS` | `True` | Post games launching in Early Access |
| `INCLUDE_EA_GRADUATIONS` | `True` | Post games leaving Early Access |
| `INCLUDE_UNKNOWN_PLAYER_COUNT` | `False` | Also post games that don't state a player count (shown as "Not stated") |
| `REPOST_AFTER_DEMO` | `True` | Post a game again when it launches, if its demo was posted |
| `MAX_ENTRIES` | `10` | Games per digest. Over 10, the digest is split across several messages |
| `TIMEZONE` | `"UTC"` | Time zone for the posting window and the date, e.g. `"Europe/London"` |
| `POST_WINDOW` | `(10, 22)` | The hours between which the daily post time is picked |
| `COUNTRY` | `"US"` | Steam store region. It sets the currency prices are shown in |

## Running it on your computer

You need Python 3.9 or newer and nothing else:

```
python coop_digest.py --dry-run
```

This prints what would be posted, without posting or saving anything. On Windows, use `py`
instead of `python`. If you set `TIMEZONE` to anything other than UTC on Windows, run
`pip install tzdata` once first.

To post from your computer, set `DISCORD_WEBHOOK_URL` and run `python coop_digest.py --now`. That
only updates your local copy of `posted_games.json`, though, so the GitHub copy won't know about
those posts.

## Notes

- **Private repository:** GitHub counts each run as at least one minute, so the hourly schedule uses
  about 720 of the 2,000 free minutes a month. To use fewer, limit the hours in the `cron` line of
  the workflow (they're UTC), for example `17 9-21 * * *`. Make sure those hours cover your
  `POST_WINDOW`. Public repositories don't use minutes.
- **Public repository:** GitHub turns off schedules in public repositories after 60 days without
  activity. If the digests stop, check the Actions tab and re-enable the workflow.
- **To start over, or to post a game again,** delete its entry from `posted_games.json` (or reset
  the file to `{"last_digest": null, "posted": {}}`) and commit.
- **Failures:** if a run fails (for example because Steam is down), GitHub sends you a notification
  and the next hourly run tries again. The script uses Steam's public store endpoints, which aren't
  an official API. If Steam changes them, the script will need updating.
- **Player counts come from text,** so now and then one will be off. That usually happens when a
  description mixes co-op and PvP numbers.
