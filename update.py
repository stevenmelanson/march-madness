#!/usr/bin/env python3
"""
March Madness Pool Auto-Updater
Fetches scores from ESPN, spreads from The Odds API,
applies spread acquisition rules, and patches index.html.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

import requests

# ─── Config ──────────────────────────────────────────────
HTML_FILE = os.path.join(os.path.dirname(__file__), "index.html")
MAPPING_FILE = os.path.join(os.path.dirname(__file__), "team_mapping.json")

ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard"
ODDS_API_URL = "https://api.the-odds-api.com/v4/sports/basketball_ncaab/odds"
ODDS_API_KEY = os.environ.get("ODDS_API_KEY", "")

# Tournament dates (2026)
TOURNAMENT_START = datetime(2026, 3, 19)
TOURNAMENT_END = datetime(2026, 4, 7)

# Round dates for schedule generation
ROUND_DATES = {
    1: ["2026-03-19", "2026-03-20"],  # R64
    2: ["2026-03-21", "2026-03-22"],  # R32
    3: ["2026-03-27", "2026-03-28"],  # S16
    4: ["2026-03-29", "2026-03-30"],  # E8
    5: ["2026-04-04"],                 # Final Four
    6: ["2026-04-06"],                 # Championship
}

ROUND_NAMES = {
    1: "R64", 2: "R32", 3: "S16", 4: "E8", 5: "F4", 6: "CHAMP"
}

# Bracket advancement mapping: game_id -> (next_game_id, position 'top'|'bot')
ADVANCEMENT = {
    # East
    "E1": ("E9", "top"), "E2": ("E9", "bot"),
    "E5": ("E10", "top"), "E6": ("E10", "bot"),
    "E3": ("E11", "top"), "E4": ("E11", "bot"),
    "E7": ("E12", "top"), "E8": ("E12", "bot"),
    "E9": ("E13", "top"), "E10": ("E13", "bot"),
    "E11": ("E14", "top"), "E12": ("E14", "bot"),
    "E13": ("E15", "top"), "E14": ("E15", "bot"),
    # West
    "W3": ("W9", "top"), "W4": ("W9", "bot"),
    "W5": ("W10", "top"), "W6": ("W10", "bot"),
    "W1": ("W11", "top"), "W2": ("W11", "bot"),
    "W7": ("W12", "top"), "W8": ("W12", "bot"),
    "W9": ("W13", "top"), "W10": ("W13", "bot"),
    "W11": ("W14", "top"), "W12": ("W14", "bot"),
    "W13": ("W15", "top"), "W14": ("W15", "bot"),
    # South (game order: S3,S4,S5,S6,S7,S8,S1,S2)
    # S3+S4 -> S10, S5+S6 -> S11, S7+S8 -> S12, S1+S2 -> S9
    "S1": ("S9", "top"), "S2": ("S9", "bot"),
    "S3": ("S10", "top"), "S4": ("S10", "bot"),
    "S5": ("S11", "top"), "S6": ("S11", "bot"),
    "S7": ("S12", "top"), "S8": ("S12", "bot"),
    "S9": ("S13", "top"), "S10": ("S13", "bot"),
    "S11": ("S14", "top"), "S12": ("S14", "bot"),
    "S13": ("S15", "top"), "S14": ("S15", "bot"),
    # Midwest
    "M1": ("M9", "top"), "M2": ("M9", "bot"),
    "M3": ("M10", "top"), "M4": ("M10", "bot"),
    "M5": ("M11", "top"), "M6": ("M11", "bot"),
    "M7": ("M12", "top"), "M8": ("M12", "bot"),
    "M9": ("M13", "top"), "M10": ("M13", "bot"),
    "M11": ("M14", "top"), "M12": ("M14", "bot"),
    "M13": ("M15", "top"), "M14": ("M15", "bot"),
}


# ─── Team Name Mapping ──────────────────────────────────
def load_team_mapping():
    with open(MAPPING_FILE) as f:
        mapping = json.load(f)
    # Remove comment key
    mapping.pop("_comment", None)
    return mapping


def resolve_team_name(espn_name, mapping):
    """Resolve an ESPN team name to the HTML name used in index.html."""
    if espn_name in mapping:
        return mapping[espn_name]
    # Try stripping common mascot suffixes
    common_mascots = [
        " Buckeyes", " Blue Devils", " Wildcats", " Bulldogs", " Hoosiers",
        " Jayhawks", " Tigers", " Bears", " Cougars", " Gators", " Cavaliers",
        " Terrapins", " Volunteers", " Cyclones", " Red Raiders", " Crimson Tide",
        " Razorbacks", " Spartans", " Boilermakers", " Huskies", " Hurricanes",
        " Wolverines", " Fighting Illini", " Commodores", " Horned Frogs",
        " Golden Hurricane", " Red Storm", " Bruins", " Knights", " Panthers",
        " Broncos", " Zips", " Pride", " Rams", " Trojans", " Aggies",
        " Paladins", " Flashes", " Highlanders", " Royals", " Bison",
        " Lancers", " Blackbirds", " Rainbow Warriors", " Cougars",
        " Cardinals", " Bulls", " Mountaineers", " Hawkeyes",
    ]
    for suffix in common_mascots:
        if espn_name.endswith(suffix):
            stripped = espn_name[:-len(suffix)]
            if stripped in mapping:
                return mapping[stripped]
    # Try first word(s) match — but only exact key matches, no substring
    # This avoids "Arkansas" matching "Kansas"
    return None


# ─── HTML Parsing ────────────────────────────────────────
def read_html():
    with open(HTML_FILE, "r") as f:
        return f.read()


def write_html(content):
    with open(HTML_FILE, "w") as f:
        f.write(content)


def extract_js_block(html, var_name):
    """Extract a JS const block like `const ALLOC={...};` from the HTML."""
    # Match const VARNAME = ... up to the matching closing bracket + semicolon
    # We need to handle nested braces/brackets
    pattern = rf'const\s+{var_name}\s*=\s*'
    match = re.search(pattern, html)
    if not match:
        raise ValueError(f"Could not find 'const {var_name}' in HTML")

    start = match.end()
    # Find the opening bracket/brace
    open_char = html[start]
    if open_char == '{':
        close_char = '}'
    elif open_char == '[':
        close_char = ']'
    else:
        raise ValueError(f"Expected {{ or [ after const {var_name} =, got {open_char}")

    depth = 0
    in_string = False
    string_char = None
    i = start
    while i < len(html):
        c = html[i]
        if in_string:
            if c == '\\':
                i += 2
                continue
            if c == string_char:
                in_string = False
        else:
            if c in ('"', "'", '`'):
                in_string = True
                string_char = c
            elif c == open_char:
                depth += 1
            elif c == close_char:
                depth -= 1
                if depth == 0:
                    # Found the end — grab up through the semicolon
                    end = i + 1
                    if end < len(html) and html[end] == ';':
                        end += 1
                    return html[match.start():end], match.start(), end
        i += 1

    raise ValueError(f"Could not find matching close for const {var_name}")


def js_to_python(js_str):
    """Convert JS object/array literal to Python-parseable form."""
    # Remove 'const VARNAME=' prefix
    js_str = re.sub(r'^const\s+\w+\s*=\s*', '', js_str).rstrip(';')

    # Replace JS-specific syntax
    # Single quotes to double quotes (careful with apostrophes in names)
    # Actually, let's use a more robust approach: evaluate as JS-like syntax

    # Handle single-quoted strings by converting to double quotes
    # But names like "Saint Mary's" and "St. John's" and "Hawai'i" have apostrophes
    # Strategy: replace property keys and simple values, leave apostrophes in names

    # Convert true/false/null
    result = js_str
    result = re.sub(r'\btrue\b', 'True', result)
    result = re.sub(r'\bfalse\b', 'False', result)
    result = re.sub(r'\bnull\b', 'None', result)

    return result


def parse_alloc(html):
    """Parse the ALLOC object from HTML into a Python dict."""
    block_str, start, end = extract_js_block(html, 'ALLOC')
    # Extract just the object part
    obj_str = re.sub(r'^const\s+ALLOC\s*=\s*', '', block_str).rstrip(';')

    # Parse player entries manually for reliability
    alloc = {}
    # Find each player key
    player_pattern = r"(\w+)\s*:\s*\["
    for m in re.finditer(player_pattern, obj_str):
        player = m.group(1)
        # Find the array for this player
        arr_start = m.end() - 1  # include the [
        depth = 0
        i = arr_start
        while i < len(obj_str):
            if obj_str[i] == '[':
                depth += 1
            elif obj_str[i] == ']':
                depth -= 1
                if depth == 0:
                    arr_str = obj_str[arr_start:i + 1]
                    break
            i += 1

        # Parse individual team objects from the array
        teams = []
        team_pattern = r"\{([^}]+)\}"
        for tm in re.finditer(team_pattern, arr_str):
            team_str = tm.group(1)
            team = {}

            # Extract n (name) — handle apostrophes in names like Hawai'i, St. John's
            # Look for n:'...' but handle embedded apostrophes by finding the pattern
            # n:'text' where ' is followed by , or } (end of property)
            nm = re.search(r'n\s*:\s*"([^"]+)"', team_str)
            if not nm:
                # Single-quoted: match n:'...' greedily, then trim to last ' before , or end
                nm = re.search(r"n\s*:\s*'(.+?)'(?=\s*[,}])", team_str)
            if nm:
                team['n'] = nm.group(1)

            # Extract s (seed)
            sm = re.search(r"s\s*:\s*(\d+)", team_str)
            if sm:
                team['s'] = int(sm.group(1))

            # Extract r (region)
            rm = re.search(r"r\s*:\s*['\"]([^'\"]+)['\"]", team_str)
            if rm:
                team['r'] = rm.group(1)

            # Extract elim
            if re.search(r"elim\s*:\s*true", team_str):
                team['elim'] = True

            # Extract acq
            if re.search(r"acq\s*:\s*true", team_str):
                team['acq'] = True

            # Extract from
            fm = re.search(r"from\s*:\s*['\"]([^'\"]+)['\"]", team_str)
            if fm:
                team['from'] = fm.group(1)

            # Extract transferred
            if re.search(r"transferred\s*:\s*true", team_str):
                team['transferred'] = True

            if team.get('n'):
                teams.append(team)

        alloc[player] = teams

    return alloc, start, end


def parse_regions(html):
    """Parse the REGIONS array from HTML."""
    block_str, start, end = extract_js_block(html, 'REGIONS')
    obj_str = re.sub(r'^const\s+REGIONS\s*=\s*', '', block_str).rstrip(';')

    regions = []
    # Parse each region
    reg_pattern = r"\{id:'(\w+)',name:'(\w+)',games:\["
    for rm in re.finditer(reg_pattern, obj_str):
        reg = {'id': rm.group(1), 'name': rm.group(2), 'games': []}

        # Find the games array end
        games_start = rm.end() - 1
        depth = 0
        i = games_start
        while i < len(obj_str):
            if obj_str[i] == '[':
                depth += 1
            elif obj_str[i] == ']':
                depth -= 1
                if depth == 0:
                    games_str = obj_str[games_start:i + 1]
                    break
            i += 1

        # Parse each game object by finding balanced braces at depth 1
        # Games are direct children of the games array
        depth = 0
        game_start = None
        i2 = 0
        while i2 < len(games_str):
            c = games_str[i2]
            if c == '{':
                depth += 1
                if depth == 1:
                    game_start = i2
            elif c == '}':
                if depth == 1 and game_start is not None:
                    game_str = games_str[game_start:i2 + 1]
                    game = parse_game_object(game_str)
                    if game:
                        reg['games'].append(game)
                    game_start = None
                depth -= 1
            i2 += 1

        for _skip in []:
            game = {
                'id': gm.group(1),
                'rd': int(gm.group(2)),
                'top': parse_team_obj(gm.group(3)),
                'bot': parse_team_obj(gm.group(4)),
                'sp': None if gm.group(5) == 'null' else gm.group(5).strip("'"),
                'st': gm.group(6),
                'score': gm.group(7) if gm.group(7) else None,
            }
            reg['games'].append(game)

        regions.append(reg)

    return regions, start, end


def parse_game_object(s):
    """Parse a single game object string like {id:'E1',rd:1,top:{s:1,n:'Duke'},bot:{s:16,n:'Siena'},sp:'Duke -29.5',st:'ct',score:'71-65'}"""
    id_m = re.search(r"id:'([^']+)'", s)
    rd_m = re.search(r"rd:(\d+)", s)
    st_m = re.search(r"st:'([^']+)'", s)
    if not id_m or not rd_m or not st_m:
        return None

    # Extract top:{...} and bot:{...} by finding balanced braces
    def extract_sub_obj(text, key):
        pat = re.search(rf'{key}:\{{', text)
        if not pat:
            return ''
        start = pat.end()
        depth = 1
        j = start
        while j < len(text) and depth > 0:
            if text[j] == '{':
                depth += 1
            elif text[j] == '}':
                depth -= 1
            j += 1
        return text[start:j - 1]

    top_inner = extract_sub_obj(s, 'top')
    bot_inner = extract_sub_obj(s, 'bot')

    # Extract spread - it's between bot:{...} and st:'...'
    # sp can be: null, 'TeamName -X.5', or "St. John's -9.5"
    sp_val = None
    sp_m = re.search(r"sp:(null|'.*?'|\".*?\")(?=,st:)", s)
    if sp_m:
        raw = sp_m.group(1)
        if raw == 'null':
            sp_val = None
        else:
            sp_val = raw.strip("'\"")

    # But the above regex won't work for spreads with apostrophes like "St. John's -9.5"
    # Try a more flexible approach if above didn't match
    if not sp_m:
        sp_m2 = re.search(r",sp:(.+?),st:", s)
        if sp_m2:
            raw = sp_m2.group(1).strip()
            if raw == 'null':
                sp_val = None
            else:
                sp_val = raw.strip("'\"")

    score_m = re.search(r"score:'([^']+)'", s)

    return {
        'id': id_m.group(1),
        'rd': int(rd_m.group(1)),
        'top': parse_team_obj(top_inner),
        'bot': parse_team_obj(bot_inner),
        'sp': sp_val,
        'st': st_m.group(1),
        'score': score_m.group(1) if score_m else None,
    }


def parse_team_obj(s):
    """Parse a team sub-object like `s:1,n:'Duke'` (inner content, no braces)."""
    team = {}
    sm = re.search(r"s:(\d+|null)", s)
    team['s'] = int(sm.group(1)) if sm and sm.group(1) != 'null' else None
    # Handle apostrophes in names like Hawai'i, Saint Mary's, St. John's
    nm = re.search(r'n:"([^"]+)"', s)
    if not nm:
        # Match n:'...' — the value goes to the end of the string (no } needed)
        nm = re.search(r"n:'(.+)'$", s.rstrip())
    if not nm:
        # Fallback: match n:'...' before a comma
        nm = re.search(r"n:'([^']+)'", s)
    team['n'] = nm.group(1) if nm else 'TBD'
    return team


def parse_log(html):
    """Parse the LOG array from HTML."""
    block_str, start, end = extract_js_block(html, 'LOG')
    obj_str = re.sub(r'^const\s+LOG\s*=\s*', '', block_str).rstrip(';')

    log = []
    entry_pattern = r"\{([^}]+)\}"
    for m in re.finditer(entry_pattern, obj_str):
        entry_str = m.group(1)
        entry = {}

        for key in ['date', 'round', 'top', 'bot', 'score', 'spread', 'result', 'acq']:
            vm = re.search(rf"{key}\s*:\s*'([^']*)'", entry_str)
            if not vm:
                vm = re.search(rf'{key}\s*:\s*"([^"]*)"', entry_str)
            if vm:
                entry[key] = vm.group(1)
            else:
                # Check for null
                nm = re.search(rf"{key}\s*:\s*null", entry_str)
                if nm:
                    entry[key] = None

        if entry.get('top') or entry.get('bot'):
            log.append(entry)

    return log, start, end


def parse_schedule(html):
    """Parse the SCHEDULE array from HTML."""
    block_str, start, end = extract_js_block(html, 'SCHEDULE')
    # Return raw string for now — we'll append to it rather than fully parse
    return block_str, start, end


# ─── ESPN API ────────────────────────────────────────────
def fetch_espn_scores(date_str=None):
    """
    Fetch tournament scores from ESPN API.
    date_str: YYYYMMDD format, or None for today.
    Returns list of game dicts with teams, scores, status.
    """
    params = {"groups": "50", "limit": "100"}
    if date_str:
        params["dates"] = date_str

    try:
        r = requests.get(ESPN_SCOREBOARD, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"ESPN API error: {e}")
        return []

    games = []
    for event in data.get("events", []):
        competition = event.get("competitions", [{}])[0]
        competitors = competition.get("competitors", [])
        if len(competitors) < 2:
            continue

        # Competitors: index 0 is home, index 1 is away (or vice versa)
        # We need to figure out which is which by seed/name
        teams = []
        for comp in competitors:
            team_data = comp.get("team", {})
            teams.append({
                "name": team_data.get("shortDisplayName", team_data.get("displayName", "")),
                "full_name": team_data.get("displayName", ""),
                "abbreviation": team_data.get("abbreviation", ""),
                "score": int(comp.get("score", 0)) if comp.get("score") else 0,
                "seed": int(comp.get("curatedRank", {}).get("current", 0)) if comp.get("curatedRank") else 0,
                "home_away": comp.get("homeAway", ""),
                "winner": comp.get("winner", False),
            })

        status = event.get("status", {})
        status_type = status.get("type", {})

        game_date = event.get("date", "")

        games.append({
            "espn_id": event.get("id", ""),
            "name": event.get("name", ""),
            "teams": teams,
            "completed": status_type.get("completed", False),
            "in_progress": status_type.get("name") == "STATUS_IN_PROGRESS",
            "scheduled": status_type.get("name") == "STATUS_SCHEDULED",
            "start_time": game_date,
            "status_detail": status_type.get("detail", ""),
        })

    return games


def fetch_espn_tournament_scores():
    """Fetch scores for all tournament dates that have games."""
    all_games = []
    now = datetime.now()

    for rd, dates in ROUND_DATES.items():
        for date_str in dates:
            d = datetime.strptime(date_str, "%Y-%m-%d")
            if d.date() > now.date():
                continue  # Don't fetch future dates
            date_fmt = d.strftime("%Y%m%d")
            games = fetch_espn_scores(date_fmt)
            for g in games:
                g['round'] = rd
            all_games.extend(games)

    return all_games


# ─── Odds API ────────────────────────────────────────────
def fetch_draftkings_spreads():
    """Fetch current DraftKings spreads from The Odds API."""
    if not ODDS_API_KEY:
        print("No ODDS_API_KEY set — skipping spread updates")
        return {}

    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us",
        "markets": "spreads",
        "bookmakers": "draftkings",
    }

    try:
        r = requests.get(ODDS_API_URL, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"Odds API error: {e}")
        return {}

    # Parse into {team_name: spread_value} format
    spreads = {}
    for event in data:
        bookmakers = event.get("bookmakers", [])
        for bk in bookmakers:
            if bk.get("key") != "draftkings":
                continue
            for market in bk.get("markets", []):
                if market.get("key") != "spreads":
                    continue
                for outcome in market.get("outcomes", []):
                    team_name = outcome.get("name", "")
                    point = outcome.get("point", 0)
                    if point < 0:  # This is the favorite
                        spreads[team_name] = point

    return spreads


# ─── Spread Logic ────────────────────────────────────────
def parse_spread_string(sp_str):
    """
    Parse spread string like 'Duke -29.5' into (favorite_name, spread_value).
    Returns (favorite_name, abs_spread) or (None, None).
    """
    if not sp_str:
        return None, None
    m = re.match(r"(.+?)\s*-\s*([\d.]+)$", sp_str)
    if m:
        return m.group(1).strip(), float(m.group(2))
    return None, None


def determine_game_result(game_data, spread_str):
    """
    Given a completed game and its spread, determine the result.

    Returns dict with:
      - result: 'WIN' | 'COVER' | 'UPSET'
      - winner: team name (HTML name)
      - loser: team name (HTML name)
      - winner_pos: 'top' | 'bot'
      - st_code: status code for REGIONS (wt/wb/ct/cb/xt/xb)
      - score_str: 'XX-YY'
    """
    top_name = game_data['top_html']
    bot_name = game_data['bot_html']
    top_score = game_data['top_score']
    bot_score = game_data['bot_score']

    fav_name, spread_val = parse_spread_string(spread_str)
    if not fav_name or not spread_val:
        return None

    # Determine which position (top/bot) is the favorite
    fav_is_top = (fav_name == top_name)

    # Margin from favorite's perspective
    if fav_is_top:
        margin = top_score - bot_score
    else:
        margin = bot_score - top_score

    # Who won?
    if top_score > bot_score:
        winner, loser = top_name, bot_name
        winner_pos = 'top'
    else:
        winner, loser = bot_name, top_name
        winner_pos = 'bot'

    top_won = (top_score > bot_score)

    if margin > 0:
        # Favorite won
        if margin > spread_val:
            # Favorite won AND covered — clean win
            result = 'WIN'
            st = 'wt' if fav_is_top else 'wb'
        else:
            # Favorite won but DIDN'T cover — spread acquisition
            result = 'COVER'
            st = 'ct' if fav_is_top else 'cb'
    else:
        # Underdog won (upset)
        result = 'UPSET'
        # The underdog won, which is the non-favorite
        if fav_is_top:
            st = 'xb'  # bottom (underdog) won
        else:
            st = 'xt'  # top (underdog) won

    score_str = f"{top_score}-{bot_score}"

    return {
        'result': result,
        'winner': winner,
        'loser': loser,
        'winner_pos': winner_pos,
        'winner_seed': game_data['top_seed'] if top_won else game_data['bot_seed'],
        'loser_seed': game_data['bot_seed'] if top_won else game_data['top_seed'],
        'st': st,
        'score': score_str,
        'fav_name': fav_name,
        'spread_val': spread_val,
    }


# ─── Ownership Helpers ───────────────────────────────────
def build_ownership(alloc):
    """Build team -> owner mapping from ALLOC (non-elim wins over elim)."""
    own = {}
    for player, teams in alloc.items():
        for t in teams:
            name = t['n']
            existing = own.get(name)
            if not existing or (not t.get('elim') and existing.get('elim')):
                own[name] = {
                    'owner': player,
                    'acq': t.get('acq', False),
                    'elim': t.get('elim', False),
                }
    return own


def find_team_owner(alloc, team_name, active_only=True):
    """Find who owns a team. If active_only, skip elim entries."""
    for player, teams in alloc.items():
        for t in teams:
            if t['n'] == team_name:
                if active_only and t.get('elim'):
                    continue
                if active_only and t.get('transferred'):
                    continue
                return player
    # Fallback: any entry
    for player, teams in alloc.items():
        for t in teams:
            if t['n'] == team_name and not t.get('transferred'):
                return player
    return None


# ─── Core Update Logic ───────────────────────────────────
def match_espn_to_bracket(espn_game, regions, mapping):
    """
    Match an ESPN game to a bracket game in REGIONS.
    Returns (region_idx, game_idx, bracket_game) or None.
    """
    teams = espn_game['teams']
    if len(teams) < 2:
        return None

    # Resolve ESPN names to HTML names
    names = []
    for t in teams:
        html_name = resolve_team_name(t['name'], mapping)
        if not html_name:
            html_name = resolve_team_name(t['full_name'], mapping)
        if not html_name:
            html_name = resolve_team_name(t['abbreviation'], mapping)
        names.append(html_name)

    if None in names:
        return None

    # Find matching bracket game
    for ri, reg in enumerate(regions):
        for gi, game in enumerate(reg['games']):
            if game['st'] != 'p':  # Already processed
                continue
            game_teams = {game['top']['n'], game['bot']['n']}
            if set(names) == game_teams:
                return ri, gi, game

    return None


def apply_game_result(alloc, regions, log, game_id, result, espn_date, bracket_game):
    """
    Apply a single game result to ALLOC, REGIONS, and LOG.
    """
    # 1. Update REGIONS game status and score
    bracket_game['st'] = result['st']
    bracket_game['score'] = result['score']

    # 2. Apply spread logic to ALLOC
    loser_name = result['loser']
    winner_name = result['winner']

    # Mark loser as eliminated
    for player, teams in alloc.items():
        for t in teams:
            if t['n'] == loser_name and not t.get('elim') and not t.get('transferred'):
                t['elim'] = True

    # Handle COVER transfer
    acq_note = None
    if result['result'] == 'COVER':
        # Find current owner of the winner (the favorite)
        winner_owner = find_team_owner(alloc, winner_name)
        # Find owner of the loser (the underdog) — this is who gets the team
        loser_owner = find_team_owner(alloc, loser_name, active_only=False)

        if winner_owner and loser_owner and winner_owner != loser_owner:
            # Mark the winner's entry under original owner as transferred/elim
            for t in alloc[winner_owner]:
                if t['n'] == winner_name and not t.get('elim') and not t.get('transferred'):
                    t['elim'] = True
                    break

            # Add the team to the new owner
            winner_team_data = None
            for reg in regions:
                for g in reg['games']:
                    if g['id'] == game_id:
                        if g['top']['n'] == winner_name:
                            winner_team_data = g['top']
                        else:
                            winner_team_data = g['bot']
                        break

            if winner_team_data:
                # Determine region
                region_code = game_id[0]  # E/W/S/M
                alloc[loser_owner].append({
                    'n': winner_name,
                    's': winner_team_data['s'],
                    'r': region_code,
                    'acq': True,
                    'from': winner_owner,
                })

            acq_note = f"{winner_name} → {loser_owner} ({loser_name} covered)"

    # 3. Advance winner to next round
    if game_id in ADVANCEMENT:
        next_id, pos = ADVANCEMENT[game_id]
        for reg in regions:
            for g in reg['games']:
                if g['id'] == next_id:
                    target = g['top'] if pos == 'top' else g['bot']
                    target['n'] = winner_name
                    target['s'] = result['winner_seed']
                    break

    # 4. Add to LOG
    # Format date
    date_obj = datetime.strptime(espn_date[:10], "%Y-%m-%d") if espn_date else datetime.now()
    date_str = date_obj.strftime("Mar %d").replace(" 0", " ")

    log_entry = {
        'date': date_str,
        'round': ROUND_NAMES.get(bracket_game['rd'], f"R{bracket_game['rd']}"),
        'top': bracket_game['top']['n'],
        'bot': bracket_game['bot']['n'],
        'score': result['score'],
        'spread': bracket_game['sp'] or '',
        'result': result['result'],
        'acq': acq_note,
    }
    log.append(log_entry)

    return acq_note


# ─── Serialization Back to JS ────────────────────────────
def js_quote(s):
    """Quote a string for JS, using double quotes if it contains an apostrophe."""
    if "'" in s:
        return f'"{s}"'
    return f"'{s}'"


def serialize_alloc(alloc):
    """Convert Python ALLOC dict back to JS source."""
    lines = ["const ALLOC={"]
    for player, teams in alloc.items():
        team_strs = []
        for t in teams:
            parts = [f"n:{js_quote(t['n'])}", f"s:{t['s']}", f"r:'{t['r']}'"]
            if t.get('elim'):
                parts.append("elim:true")
            if t.get('acq'):
                parts.append("acq:true")
            if t.get('from'):
                parts.append(f"from:'{t['from']}'")
            if t.get('transferred'):
                parts.append("transferred:true")
            team_strs.append("{" + ",".join(parts) + "}")
        lines.append(f"  {player}:[")
        for ts in team_strs:
            lines.append(f"    {ts},")
        lines.append("  ],")
    lines.append("};")
    return "\n".join(lines)


def serialize_regions(regions):
    """Convert Python REGIONS back to JS source."""
    lines = ["const REGIONS=["]
    for reg in regions:
        lines.append(f"  {{id:'{reg['id']}',name:'{reg['name']}',games:[")
        for g in reg['games']:
            top_s = g['top']['s'] if g['top']['s'] is not None else 'null'
            bot_s = g['bot']['s'] if g['bot']['s'] is not None else 'null'
            sp = js_quote(g['sp']) if g['sp'] else 'null'
            score_part = f",score:'{g['score']}'" if g.get('score') else ''
            top_n = js_quote(g['top']['n'])
            bot_n = js_quote(g['bot']['n'])
            lines.append(
                f"    {{id:'{g['id']}',rd:{g['rd']},"
                f"top:{{s:{top_s},n:{top_n}}},"
                f"bot:{{s:{bot_s},n:{bot_n}}},"
                f"sp:{sp},st:'{g['st']}'"
                f"{score_part}}},"
            )
        lines.append("  ]},")
    lines.append("];")
    return "\n".join(lines)


def serialize_log(log):
    """Convert Python LOG back to JS source."""
    lines = ["const LOG=["]
    for e in log:
        acq = js_quote(e['acq']) if e.get('acq') else 'null'
        lines.append(
            f"  {{date:'{e['date']}',round:'{e['round']}',"
            f"top:{js_quote(e['top'])},bot:{js_quote(e['bot'])},"
            f"score:'{e['score']}',spread:{js_quote(e['spread'])},"
            f"result:'{e['result']}',acq:{acq}}},"
        )
    lines.append("];")
    return "\n".join(lines)


# ─── Spread Update ───────────────────────────────────────
def update_spreads(regions, dk_spreads, mapping):
    """Update spread fields in REGIONS for upcoming games."""
    if not dk_spreads:
        return 0

    count = 0
    for reg in regions:
        for game in reg['games']:
            if game['sp'] is not None:
                continue  # Already has a spread
            if game['st'] != 'p':
                continue  # Already played
            if game['top']['n'] == 'TBD' or game['bot']['n'] == 'TBD':
                continue  # Teams not yet determined

            # Try to find a spread for this matchup
            top_name = game['top']['n']
            bot_name = game['bot']['n']

            for dk_team, dk_spread in dk_spreads.items():
                html_name = resolve_team_name(dk_team, mapping)
                if html_name == top_name:
                    game['sp'] = f"{top_name} {dk_spread}"
                    count += 1
                    break
                elif html_name == bot_name:
                    game['sp'] = f"{bot_name} {dk_spread}"
                    count += 1
                    break

    return count


# ─── Validation ──────────────────────────────────────────
def validate(alloc, regions, log):
    """Run all validation checks. Returns list of errors (empty = pass)."""
    errors = []

    # 1. Check ownership consistency
    own = build_ownership(alloc)
    for reg in regions:
        for game in reg['games']:
            for pos in ['top', 'bot']:
                team = game[pos]
                if team['n'] == 'TBD':
                    continue
                if team['n'] not in own:
                    errors.append(f"Team '{team['n']}' in bracket but not in ALLOC")

    # 2. Check active team counts are positive for at least some players
    active_counts = {}
    for player, teams in alloc.items():
        active = [t for t in teams if not t.get('elim') and not t.get('transferred')]
        active_counts[player] = len(active)

    total_active = sum(active_counts.values())
    if total_active == 0:
        errors.append("No active teams found — something is wrong")

    # 3. Check that eliminated teams are actually losers in completed games
    completed_losers = set()
    for reg in regions:
        for game in reg['games']:
            if game['st'] == 'p':
                continue
            if game['st'] in ('wt', 'ct', 'xt'):
                completed_losers.add(game['bot']['n'])
            elif game['st'] in ('wb', 'cb', 'xb'):
                completed_losers.add(game['top']['n'])

    # 4. Check LOG entries match completed games count
    completed_games = sum(
        1 for reg in regions
        for game in reg['games']
        if game['st'] != 'p'
    )
    if len(log) != completed_games:
        errors.append(f"LOG has {len(log)} entries but {completed_games} completed games in bracket")

    # 5. Check for duplicate team entries (same team active in multiple players)
    active_teams_all = {}
    for player, teams in alloc.items():
        for t in teams:
            if not t.get('elim') and not t.get('transferred'):
                if t['n'] in active_teams_all:
                    errors.append(
                        f"Team '{t['n']}' is active under both "
                        f"{active_teams_all[t['n']]} and {player}"
                    )
                active_teams_all[t['n']] = player

    return errors


# ─── Main ────────────────────────────────────────────────
def main():
    print("=" * 50)
    print("March Madness Pool Auto-Updater")
    print("=" * 50)

    # Load data
    mapping = load_team_mapping()
    html = read_html()

    # Parse current state
    print("\nParsing current HTML state...")
    alloc, alloc_start, alloc_end = parse_alloc(html)
    regions, regions_start, regions_end = parse_regions(html)
    log, log_start, log_end = parse_log(html)

    print(f"  Players: {list(alloc.keys())}")
    print(f"  Regions: {[r['id'] for r in regions]}")
    print(f"  Log entries: {len(log)}")

    # Track if anything changed
    changes = []

    # Fetch scores from ESPN
    print("\nFetching scores from ESPN...")
    espn_games = fetch_espn_tournament_scores()
    print(f"  Found {len(espn_games)} ESPN games")

    completed_espn = [g for g in espn_games if g['completed']]
    print(f"  Completed: {len(completed_espn)}")

    # Process each completed game
    new_results = 0
    for espn_game in completed_espn:
        match = match_espn_to_bracket(espn_game, regions, mapping)
        if not match:
            continue

        ri, gi, bracket_game = match
        if bracket_game['st'] != 'p':
            continue  # Already processed

        # Get team scores mapped to top/bot
        teams = espn_game['teams']
        top_html = resolve_team_name(teams[0]['name'], mapping) or resolve_team_name(teams[0]['full_name'], mapping)
        bot_html = resolve_team_name(teams[1]['name'], mapping) or resolve_team_name(teams[1]['full_name'], mapping)

        # Match ESPN teams to bracket positions
        if top_html == bracket_game['top']['n']:
            game_data = {
                'top_html': top_html, 'bot_html': bot_html,
                'top_score': teams[0]['score'], 'bot_score': teams[1]['score'],
                'top_seed': bracket_game['top']['s'], 'bot_seed': bracket_game['bot']['s'],
            }
        elif top_html == bracket_game['bot']['n']:
            game_data = {
                'top_html': bot_html, 'bot_html': top_html,
                'top_score': teams[1]['score'], 'bot_score': teams[0]['score'],
                'top_seed': bracket_game['top']['s'], 'bot_seed': bracket_game['bot']['s'],
            }
        else:
            print(f"  Warning: Could not match ESPN teams to bracket positions for {bracket_game['id']}")
            continue

        if not bracket_game['sp']:
            print(f"  Warning: No spread set for {bracket_game['id']} — skipping (need spread to determine result)")
            continue

        result = determine_game_result(game_data, bracket_game['sp'])
        if not result:
            print(f"  Warning: Could not determine result for {bracket_game['id']}")
            continue

        acq = apply_game_result(
            alloc, regions, log, bracket_game['id'], result,
            espn_game.get('start_time', ''), bracket_game
        )

        new_results += 1
        emoji = "📦" if result['result'] == 'COVER' else "⚡" if result['result'] == 'UPSET' else "✅"
        print(f"  {emoji} {bracket_game['id']}: {result['winner']} def. {result['loser']} "
              f"({result['score']}) — {result['result']}"
              f"{f' | {acq}' if acq else ''}")

    if new_results > 0:
        changes.append(f"{new_results} new game results")

    # Fetch and update spreads — only if there are games needing spreads
    # Spreads lock at 9AM EST and should NOT be updated after that
    games_needing_spreads = sum(
        1 for reg in regions for g in reg['games']
        if g['st'] == 'p' and g['sp'] is None
        and g['top']['n'] != 'TBD' and g['bot']['n'] != 'TBD'
    )

    # Check if we're past the 9:30AM EST lock window (14:30 UTC)
    now_utc = datetime.now(timezone.utc)
    est_hour = (now_utc.hour - 5) % 24  # Rough EST conversion
    past_lock = est_hour >= 10  # After 10AM EST = well past the 9AM lock

    if games_needing_spreads > 0 and not past_lock:
        print(f"\nFetching DraftKings spreads ({games_needing_spreads} games need spreads)...")
        dk_spreads = fetch_draftkings_spreads()
        if dk_spreads:
            spread_count = update_spreads(regions, dk_spreads, mapping)
            if spread_count > 0:
                changes.append(f"{spread_count} spreads updated")
                print(f"  Updated {spread_count} spreads")
        else:
            print("  No spreads available (API key missing or no upcoming lines)")
    elif games_needing_spreads > 0 and past_lock:
        print(f"\nPast 9AM EST lock — {games_needing_spreads} games still need spreads but won't fetch (locked)")
    else:
        print("\nAll games have spreads — skipping Odds API call")

    # Validate
    print("\nRunning validation...")
    errors = validate(alloc, regions, log)
    if errors:
        print("  VALIDATION FAILED:")
        for err in errors:
            print(f"    ❌ {err}")
        print("\n  NOT updating HTML due to validation errors.")
        sys.exit(1)
    else:
        print("  ✅ All validation checks passed")

    # Write back if changes
    if not changes:
        print("\n  No changes detected — HTML is up to date.")
        return

    print(f"\nApplying changes: {', '.join(changes)}")

    # Serialize updated data
    new_alloc = serialize_alloc(alloc)
    new_regions = serialize_regions(regions)
    new_log = serialize_log(log)

    # Replace blocks in HTML (work backwards to preserve offsets)
    # Order: LOG comes after REGIONS which comes after ALLOC in the file
    # We need to re-extract positions since they may shift

    # Re-read and replace each block
    html = read_html()

    # Replace LOG
    old_log_str, ls, le = extract_js_block(html, 'LOG')
    html = html[:ls] + new_log + html[le:]

    # Replace REGIONS
    old_reg_str, rs, re_ = extract_js_block(html, 'REGIONS')
    html = html[:rs] + new_regions + html[re_:]

    # Replace ALLOC
    old_alloc_str, as_, ae = extract_js_block(html, 'ALLOC')
    html = html[:as_] + new_alloc + html[ae:]

    write_html(html)
    print("✅ index.html updated successfully!")
    print(f"   Changes: {', '.join(changes)}")


if __name__ == "__main__":
    main()
