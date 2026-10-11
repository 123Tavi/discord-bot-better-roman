#!/usr/bin/env python3
"""Daily Discord digest of new online co-op games and demos on Steam.

Meant to run every hour (see .github/workflows/coop-digest.yml). Each day gets a random
post time inside POST_WINDOW; the first run after that time posts the digest, and the
other runs that day exit straight away.

    python coop_digest.py             post if today's digest is due
    python coop_digest.py --now       post right away
    python coop_digest.py --dry-run   print what would be posted; post and save nothing

Uses only the Python standard library and Steam's public store endpoints (no API key).
"""

import argparse
import html
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from datetime import time as clock
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# --- Settings ---------------------------------------------------------------

MIN_PLAYERS = 3              # online co-op must support at least this many players
MIN_REVIEWS = 250            # full games only; demos have no reviews
MIN_POSITIVE = 80            # percent of reviews that are positive
EXCLUDED_TAGS = ["Sports", "Turn-Based Tactics", "Tactical RPG", "Real Time Tactics"]
TAG_DEPTH = 10               # only a game's top N Steam tags count toward EXCLUDED_TAGS
GAME_LOOKBACK_DAYS = 30      # how long a new game has to reach the review bar
DEMO_LOOKBACK_DAYS = 7
INCLUDE_EARLY_ACCESS = True  # games launching in Early Access count as new releases
INCLUDE_EA_GRADUATIONS = True  # so do games leaving Early Access (their 1.0 launch)
INCLUDE_UNKNOWN_PLAYER_COUNT = False  # post games whose store page states no player count
REPOST_AFTER_DEMO = True     # a game posted for its demo is posted again when it launches
MAX_ENTRIES = 10             # per digest; 10 fits one Discord message. Extras wait a day.

TIMEZONE = "UTC"             # e.g. "Europe/London"; decides when a day starts
POST_WINDOW = (10, 22)       # the random post time falls between these hours (TIMEZONE)
COUNTRY = "US"               # Steam store region, sets the price currency

STATE_FILE = Path(__file__).with_name("posted_games.json")

# --- Steam ------------------------------------------------------------------

STORE_SEARCH = "https://store.steampowered.com/search/results/"
STORE_API = "https://api.steampowered.com"
ASSET_BASE = "https://shared.fastly.steamstatic.com/store_item_assets/"
SEARCH_GAMES, SEARCH_DEMOS = 998, 10   # search "category1" values
ONLINE_COOP, MMO = 38, 20              # Steam feature category ids
TYPE_GAME, TYPE_DEMO = 0, 1            # store item types
USER_AGENT = "coop-digest/1.0 (Discord webhook bot)"


def request(url, params=None, body=None, attempts=4):
    """GET `url` (or POST `body` as JSON) and return the parsed JSON reply.

    Rate limits, server errors and network errors are retried with backoff.
    """
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    data = None if body is None else json.dumps(body).encode()
    headers = {"User-Agent": USER_AGENT, "Content-Type": "application/json"}
    host = urllib.parse.urlsplit(url).netloc  # never log full URLs: the webhook URL is a secret
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data, headers), timeout=30) as resp:
                raw = resp.read()
            return json.loads(raw) if raw.strip() else None
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", "replace")
            if err.code != 429 and err.code < 500:
                raise RuntimeError(f"{host} answered {err.code}: {detail[:300]}") from None
            wait, problem = retry_after(err.headers.get("Retry-After"), detail), f"{host} answered {err.code}"
        except (urllib.error.URLError, TimeoutError, ConnectionError) as err:
            wait, problem = None, f"{host} unreachable: {err}"
        if attempt == attempts:
            raise RuntimeError(f"{problem} (gave up after {attempts} tries)")
        wait = wait or 5 * 2 ** attempt
        print(f"  {problem}; retrying in {wait:.0f}s")
        time.sleep(min(wait, 120) + 0.5)


def retry_after(header, body):
    """Seconds to wait before retrying, from Discord's JSON body or a Retry-After header."""
    for read in (lambda: json.loads(body)["retry_after"], lambda: header):
        try:
            return float(read())
        except (ValueError, KeyError, TypeError):
            pass
    return None


def search(start, **filters):
    """App ids from one page (100 results) of Steam's store search, limited to online co-op."""
    params = {"category3": ONLINE_COOP, "start": start, "count": 100, "infinite": 1,
              "l": "english", "cc": COUNTRY, **filters}
    page = request(STORE_SEARCH, params) or {}
    return [int(appid) for appid in re.findall(r'data-ds-appid="(\d+)"', page.get("results_html", ""))]


def store_items(appids):
    """Store details (reviews, tags, categories, price, release, text, images) keyed by app id."""
    items = {}
    for i in range(0, len(appids), 100):
        query = {
            "ids": [{"appid": appid} for appid in appids[i:i + 100]],
            "context": {"language": "english", "country_code": COUNTRY, "steam_realm": 1},
            "data_request": {"include_basic_info": True, "include_release": True,
                             "include_reviews": True, "include_assets": True,
                             "include_full_description": True, "include_tag_count": TAG_DEPTH},
        }
        reply = request(f"{STORE_API}/IStoreBrowseService/GetItems/v1/", {"input_json": json.dumps(query)}) or {}
        for item in reply.get("response", {}).get("store_items", []):
            if item.get("success") == 1 and item.get("visible") and item.get("appid"):
                items[item["appid"]] = item
    return items


def recently_released(category, since):
    """Online co-op games or demos released on Steam after `since`, with their store details."""
    items = {}
    for start in range(0, 3000, 100):
        appids = search(start, category1=category, sort_by="Released_DESC")
        page = store_items(appids)
        items.update(page)
        dates = [released(page[appid]) for appid in appids if appid in page]
        dates = [d for d in dates if d]
        if len(appids) < 100 or (dates and dates[-1] < since):
            break
        time.sleep(1)
    return items


def wishlist_ranks(pages=5):
    """Upcoming online co-op games in Steam's "popular upcoming" (wishlist) order: {appid: rank}."""
    ranked = []
    for start in range(0, pages * 100, 100):
        appids = search(start, category1=SEARCH_GAMES, filter="popularwishlist")
        ranked += appids
        if len(appids) < 100:
            break
        time.sleep(1)
    return {appid: rank for rank, appid in enumerate(ranked)}


def excluded_tag_ids():
    tags = request(f"{STORE_API}/IStoreService/GetTagList/v1/", {"language": "english"})["response"]["tags"]
    by_name = {tag["name"].casefold(): tag["tagid"] for tag in tags}
    unknown = [name for name in EXCLUDED_TAGS if name.casefold() not in by_name]
    if unknown:
        print(f"Warning: not Steam tags, ignored: {', '.join(unknown)}")
    return {by_name[name.casefold()] for name in EXCLUDED_TAGS if name.casefold() in by_name}


def released(item):
    stamp = item.get("release", {}).get("steam_release_date")
    return datetime.fromtimestamp(stamp, timezone.utc) if stamp else None


def left_early_access(game):
    """True when a game's latest launch came well after its first one: Steam keeps the
    original date when a game leaves Early Access."""
    release = game.get("release", {})
    first, latest = release.get("original_steam_release_date"), release.get("steam_release_date")
    return bool(first and latest and latest - first > 7 * 86400 and not game.get("is_early_access"))


def description(item):
    return item.get("basic_info", {}).get("short_description", "") + "\n" + item.get("full_description_bbcode", "")


# --- Player counts ----------------------------------------------------------
# Steam has no "max players" field, so the count is read from the store description.

NUMBER_WORDS = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "sixteen": 16, "twenty": 20}
N = r"(?<![\w,.])(\d{1,3}|" + "|".join(NUMBER_WORDS) + r")\b"
RANGE = r"\b\d{1,2}\s*(?:-|–|—|~|to)\s*(\d{1,3})"
FRIENDS = (r"(?:(?:more|other|additional)\s*\)?\s+(?:players|people)"
           r"|(?:(?:more|other|additional)\s*\)?\s+)?"
           r"(?:friends?|others|teammates?|allies|buddies|pals|mates|companions|partners|comrades))\b")

# (pattern, kind). "total" counts everyone, "plus_one" counts everyone but you,
# "pair" is two-player wording without a number.
PLAYER_PATTERNS = [(re.compile(p, re.I), kind) for p, kind in [
    (RANGE + r"\s*-?\s*(?:players?|people|persons?|p\b|co-?op)", "total"),            # 1-4 players, 1–4P, a 1-4 co-op game
    (r"\bplayers?\s*:\s*" + RANGE, "total"),                                          # Players: 1-4
    (N + r"\s*-?\s*players?\b(?!\s*(?:factions?|classes|characters?|roles?|types?|skins?|colou?rs?)\b)", "total"),
    (N + r"-person\b", "total"),                                                      # 4-person squad
    (r"\bup\s+to\s+" + N + r"\s+(?:people|persons)\b", "total"),
    (r"\b(?:squad|team|group|party|crew|fireteam)\s+of\s+(?:up\s+to\s+)?" + N
     + r"(?!\s+(?:developers?|devs?|indie|students?|brothers|sisters|siblings)\b)", "total"),
    (r"\bco-?op(?:erative)?\s+for\s+(?:up\s+to\s+)?" + N, "total"),                    # co-op for three
    (r"\b(?:with|and|alongside|plus|invite|bring|grab|join)\s+(?:up\s+to\s+)?(?:your\s+)?" + N + r"\s+" + FRIENDS, "plus_one"),
    (r"\bup\s+to\s+" + N + r"\s+(?:of\s+your\s+)?" + FRIENDS, "plus_one"),            # (up to 3 more) friends
    (r"\b(?:as\s+a\s+duo|duo\s+co-?op|co-?op\s+duo|for\s+two\b|with\s+(?:a|your|one)\s+(?:friend|partner|buddy|pal)\b)", "pair"),
    (r"\d{1,2}\s*[~～\-－–至到]\s*(\d{1,2})\s*[人名]", "total"),                            # 1-4人
    (r"最[多大]\s*(\d{1,2})\s*[人名位]", "total"),                                          # 最多4人 / 最大4人
    (r"(\d{1,2})\s*人\s*(?:合作|联机|聯機|協力|协力|同时|同時|组队|組隊|一起|连线|連線|在线|線上|マルチ|プレイ)", "total"),
    (r"최대\s*(\d{1,2})\s*(?:인|명)", "total"),
    (r"(\d{1,2})\s*인\s*(?:협동|코옵|멀티)", "total"),
    (r"\bдо\s+(\d{1,2})\s+(?:игрок|человек)", "total"),
    (r"\b(?:bis zu|jusqu'(?:à|a)|hasta|até)\s+(\d{1,2})\s+(?:spieler|joueurs|jugadores|jogadores)", "total"),
]]
PVP = re.compile(r"\bpvp\b|\bversus\b|\bvs\b|\b\d+\s*v\s*\d+\b|battle royale|deathmatch|head-to-head|\bcompetitive\b"
                 r"|\branked\b|\bagainst\s+(?:each other|other players|real players|(?:your )?friends)|\bopponents?\b|\brivals?\b", re.I)
COOP = re.compile(r"co-?op|cooperat|co-operat|together|team\s*up|teamwork|\bsquad|\bparty\b|\bcrew\b|\bfriends?\b|\ballies\b", re.I)
LOCAL = re.compile(r"\blocal\b|split[- ]?screen|\bcouch\b|same (?:screen|pc|keyboard)|shared screen|hot[- ]?seat", re.I)
ONLINE = re.compile(r"\bonline\b|\binternet\b|\bnetwork", re.I)
SOLO_DEMO = re.compile(r"\b(?:single[- ]?player|solo)(?:\s+mode)?\s+only\b"
                       r"|\b(?:co-?op|multiplayer|online play)\s+(?:is\s+|will\s+be\s+)?(?:not\s+(?:available|included|supported)|disabled|unavailable)"
                       r"|\bno\s+(?:co-?op|multiplayer)\b", re.I)
INLINE_MARKUP = re.compile(r"\[/?(?:b|i|u|s|strike|spoiler|url|noparse)\b[^\]]*\]|</?(?:b|i|u|em|strong|a|span)\b[^>]*>", re.I)
BLOCK_MARKUP = re.compile(r"\[[^\]]*\]|<[^>]+>")


def plain(text):
    """Store BBCode/HTML as plain text, with paragraph and list breaks kept as newlines."""
    return html.unescape(BLOCK_MARKUP.sub("\n", INLINE_MARKUP.sub("", text or "")))


def max_players(*texts):
    """Largest co-op player count the text states, or None if it states none.

    Explicit totals ("1-4 players") beat counts relative to you ("with up to 3 friends"), which
    beat counts for local play only, which beat two-player wording ("with a friend"). Sentences
    that are only about PvP are ignored, since lobby sizes say nothing about co-op.
    """
    found = {"total": [], "plus_one": [], "local": [], "pair": []}
    for sentence in re.split(r"[.!?;\n\r•·|]+", plain("\n".join(texts))):
        if PVP.search(sentence) and not COOP.search(sentence):
            continue
        local_only = LOCAL.search(sentence) and not ONLINE.search(sentence)
        for pattern, kind in PLAYER_PATTERNS:
            for match in pattern.finditer(sentence):
                if kind == "pair":
                    count = 2
                else:
                    word = match.group(1)
                    count = NUMBER_WORDS.get(word.lower()) or int(word)
                    count += kind == "plus_one"
                if 2 <= count <= 999:
                    found["local" if local_only and kind != "pair" else kind].append(count)
    for kind in ("total", "plus_one", "local", "pair"):
        if found[kind]:
            return max(found[kind])
    return None


# --- Picking the games ------------------------------------------------------

@dataclass
class Entry:
    kind: str     # "release" or "demo"
    game: dict    # store item of the full game (the parent, for a demo)
    players: str  # what the "Max players" field shows
    sort_key: tuple


def check_coop(game, excluded_tags, extra_text=""):
    """(players text, None) if `game` qualifies on co-op and genre, else (None, reason)."""
    categories = game.get("categories", {}).get("supported_player_categoryids", [])
    if ONLINE_COOP not in categories:  # split-screen-only and LAN-only games stop here
        return None, "no online co-op"
    top_tags = sorted(game.get("tags", []), key=lambda tag: -tag.get("weight", 0))[:TAG_DEPTH]
    if any(tag["tagid"] in excluded_tags for tag in top_tags):
        return None, "excluded genre"
    count = max_players(description(game), extra_text)
    if count is None and MMO in categories:
        return "Massively multiplayer", None
    if count is None:
        return ("Not stated", None) if INCLUDE_UNKNOWN_PLAYER_COUNT else (None, "player count not stated")
    if count < MIN_PLAYERS:
        return None, f"fewer than {MIN_PLAYERS} players"
    return str(count), None


def check_release(game, posted, since, excluded_tags):
    record = posted.get(str(game["appid"]), {})
    if "release" in record or ("demo" in record and not REPOST_AFTER_DEMO):
        return None, "already posted"
    if game.get("type") != TYPE_GAME or game.get("is_coming_soon") or not released(game):
        return None, "not released"
    if released(game) < since:
        return None, "older release"
    if game.get("is_early_access") and not INCLUDE_EARLY_ACCESS:
        return None, "early access"
    if left_early_access(game) and not INCLUDE_EA_GRADUATIONS:
        return None, "left early access"
    reviews = game.get("reviews", {}).get("summary_filtered", {})
    if reviews.get("review_count", 0) < MIN_REVIEWS:
        return None, f"under {MIN_REVIEWS} reviews"
    if reviews.get("percent_positive", 0) < MIN_POSITIVE:
        return None, f"under {MIN_POSITIVE}% positive"
    return check_coop(game, excluded_tags)


def check_demo(demo, game, posted, demo_since, game_since, excluded_tags):
    if demo.get("type") != TYPE_DEMO or game is None:
        return None, "demo without a game"
    if str(game["appid"]) in posted:
        return None, "already posted"
    if not released(demo) or released(demo) < demo_since:
        return None, "older demo"
    if not game.get("is_coming_soon") and released(game) and released(game) < game_since:
        return None, "demo of an older game"
    demo_text = description(demo)
    if demo_text.strip() != description(game).strip() and SOLO_DEMO.search(plain(demo_text)):
        return None, "demo is single-player"
    return check_coop(game, excluded_tags, demo_text)


def pick_entries(posted, now):
    """Everything worth posting today, best first, plus a count of why the rest was skipped."""
    game_since = now - timedelta(days=GAME_LOOKBACK_DAYS)
    demo_since = now - timedelta(days=DEMO_LOOKBACK_DAYS)
    excluded_tags = excluded_tag_ids()
    games = recently_released(SEARCH_GAMES, game_since)
    demos = recently_released(SEARCH_DEMOS, demo_since)
    parent_ids = {demo.get("related_items", {}).get("parent_appid") for demo in demos.values()}
    parents = {**store_items(sorted(appid for appid in parent_ids - games.keys() if appid)), **games}
    print(f"Checking {len(games)} games and {len(demos)} demos with online co-op.")

    skipped, entries = Counter(), {}
    for game in games.values():
        players, reason = check_release(game, posted, game_since, excluded_tags)
        if reason:
            skipped[reason] += 1
            continue
        reviews = game["reviews"]["summary_filtered"]["review_count"]
        entries[game["appid"]] = Entry("release", game, players, (0, -reviews))
    for demo in demos.values():
        game = parents.get(demo.get("related_items", {}).get("parent_appid"))
        players, reason = check_demo(demo, game, posted, demo_since, game_since, excluded_tags)
        if reason:
            skipped[reason] += 1
            continue
        if game["appid"] not in entries:  # a game's release beats its demo
            entries[game["appid"]] = Entry("demo", game, players, (1, 0, -released(demo).timestamp()))

    picked = sorted(entries.values(), key=lambda entry: entry.sort_key)
    demos_found = [entry for entry in picked if entry.kind == "demo"]
    if len(picked) > MAX_ENTRIES and demos_found:
        # More than fits: put demos of the most wishlisted games first.
        ranks = wishlist_ranks()
        for entry in demos_found:
            entry.sort_key = (1, ranks.get(entry.game["appid"], len(ranks)), *entry.sort_key[2:])
        picked.sort(key=lambda entry: entry.sort_key)
    return picked, skipped


# --- Discord message --------------------------------------------------------

def store_link(game):
    return f"https://store.steampowered.com/app/{game['appid']}/"


def price_text(game):
    if game.get("is_free"):
        return "Free to play"
    option = game.get("best_purchase_option")
    if not option or not option.get("formatted_final_price"):
        return "See store page"
    price = option["formatted_final_price"]
    if option.get("discount_pct") and option.get("formatted_original_price"):
        return f"~~{option['formatted_original_price']}~~ {price} (-{option['discount_pct']}%)"
    return price


def release_text(game):
    """Steam's release date wording for an upcoming game: 'Nov 4, 2026', 'Q1 2027', 'Coming soon'..."""
    release = game.get("release", {})
    style, stamp = release.get("coming_soon_display"), release.get("steam_release_date")
    if not stamp or style in ("text_comingsoon", "text_tba"):
        return release.get("custom_release_date_message") or "Coming soon"
    day = datetime.fromtimestamp(stamp, timezone.utc)
    if style == "date_year":
        return str(day.year)
    if style == "date_quarter":
        return f"Q{(day.month - 1) // 3 + 1} {day.year}"
    if style == "date_month":
        return f"{day:%B %Y}"
    return f"{day:%b} {day.day}, {day.year}"


def escape_markdown(text):
    return re.sub(r"([\\*_~`|\[\]])", r"\\\1", text)


def blurb(game, limit=300):
    text = game.get("basic_info", {}).get("short_description") or game.get("full_description_bbcode", "")
    text = re.sub(r"\s+", " ", plain(text)).strip()
    if len(text) > limit:
        text = text[:limit - 1].rsplit(" ", 1)[0] + "…"
    return escape_markdown(text)


def embed(entry):
    game = entry.game
    if entry.kind == "demo":
        label, color = "New demo", 0x3498DB
        full_game = release_text(game) if game.get("is_coming_soon") else price_text(game)
        fields = [("Price", f"Free demo\nFull game: {full_game}"), ("Max players", entry.players)]
    else:
        label, color = "New release", 0x2ECC71
        if game.get("is_early_access"):
            label += " · Early Access"
        elif left_early_access(game):
            label = "Full release · left Early Access"
        reviews = game["reviews"]["summary_filtered"]
        fields = [("Price", price_text(game)), ("Max players", entry.players),
                  ("Reviews", f"{reviews['percent_positive']}% of {reviews['review_count']:,} positive")]
    result = {
        "author": {"name": label},
        "title": html.unescape(game.get("name", "?"))[:256],
        "url": store_link(game),
        "description": blurb(game),
        "color": color,
        "fields": [{"name": name, "value": value, "inline": True} for name, value in fields],
    }
    assets = game.get("assets", {})
    if assets.get("header") and assets.get("asset_url_format"):
        result["thumbnail"] = {"url": ASSET_BASE + assets["asset_url_format"].replace("${FILENAME}", assets["header"])}
    return result


def embed_size(item):
    fields = sum(len(f["name"]) + len(f["value"]) for f in item["fields"])
    return len(item["title"]) + len(item["description"]) + len(item["author"]["name"]) + fields


def build_messages(entries, day):
    """Split the digest into Discord messages (10 embeds and 6000 characters at most each).
    Returns (message, entries in it) pairs."""
    kinds = Counter(entry.kind for entry in entries)
    counts = [f"{kinds[k]} new {k}{'s' if kinds[k] != 1 else ''}" for k in ("release", "demo") if kinds[k]]
    header = f"**New co-op games on Steam · {day:%A}, {day:%B} {day.day}**\n{' · '.join(counts)}"
    chunks, size = [], 0
    for entry in entries:
        item = embed(entry)
        if not chunks or len(chunks[-1]) == 10 or size + embed_size(item) > 5900:
            chunks.append([])
            size = 0
        chunks[-1].append((entry, item))
        size += embed_size(item)
    messages = []
    for i, chunk in enumerate(chunks):
        message = {"embeds": [item for _, item in chunk], "allowed_mentions": {"parse": []}}
        if i == 0:
            message["content"] = header
        messages.append((message, [entry for entry, _ in chunk]))
    return messages


def webhook_url():
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if "/api/webhooks/" not in url:
        sys.exit("DISCORD_WEBHOOK_URL is missing or isn't a Discord webhook URL (see README.md).")
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.urlencode(urllib.parse.parse_qsl(parts.query) + [("wait", "true")])
    return urllib.parse.urlunsplit(parts._replace(query=query))  # wait=true: Discord confirms each message


# --- State and schedule -----------------------------------------------------

def load_state():
    state = json.loads(STATE_FILE.read_text("utf-8")) if STATE_FILE.exists() else {}
    return {"last_digest": state.get("last_digest"), "posted": state.get("posted", {})}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n", "utf-8")


def local_zone():
    if TIMEZONE.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(TIMEZONE)
    except ZoneInfoNotFoundError:
        sys.exit(f"Unknown TIMEZONE {TIMEZONE!r}. (On Windows, run `pip install tzdata` first.)")


def due_time(day, zone):
    """Today's random post time. Seeded by the date, so every run that day agrees on it."""
    start, end = POST_WINDOW
    minute = random.Random(f"coop-digest {day.isoformat()}").randrange(start * 60, end * 60)
    return datetime.combine(day, clock(minute // 60, minute % 60), zone)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--now", action="store_true", help="post now instead of at today's random time")
    parser.add_argument("--dry-run", action="store_true", help="print the digest; post and save nothing")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    zone = local_zone()
    now = datetime.now(zone)
    today = now.date()
    state = load_state()
    if not (args.now or args.dry_run):
        if state["last_digest"] == today.isoformat():
            print("Today's digest is already done.")
            return
        due = due_time(today, zone)
        if now < due:
            print(f"Today's digest is due after {due:%H:%M} ({TIMEZONE}). Nothing to do yet.")
            return
    url = None if args.dry_run else webhook_url()

    entries, skipped = pick_entries(state["posted"], now)
    print("Skipped: " + (", ".join(f"{n} {reason}" for reason, n in skipped.most_common()) or "nothing"))
    waiting = entries[MAX_ENTRIES:]
    entries = entries[:MAX_ENTRIES]
    print(f"{len(entries) + len(waiting)} new co-op games found"
          + (f"; posting {len(entries)}, {len(waiting)} wait for the next digest." if waiting else "."))

    if args.dry_run:
        for entry in entries:
            print(f"  [{entry.kind}] {entry.game['name']} | players: {entry.players} | "
                  f"{embed(entry)['fields'][0]['value']!r} | {store_link(entry.game)}")
        if entries:
            print(json.dumps([message for message, _ in build_messages(entries, today)], indent=2, ensure_ascii=False))
        return

    for message, batch in build_messages(entries, today):
        request(url, body=message)
        for entry in batch:  # recorded per message, so a failure later can't cause a repost
            record = state["posted"].setdefault(str(entry.game["appid"]), {"name": entry.game["name"]})
            record[entry.kind] = today.isoformat()
        save_state(state)
        time.sleep(1)
    state["last_digest"] = today.isoformat()
    save_state(state)
    print(f"Posted {len(entries)} games." if entries else "Nothing new today; no message posted.")


if __name__ == "__main__":
    main()
