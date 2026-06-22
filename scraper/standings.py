"""
Fetch World Cup 2026 group standings from ESPN's unofficial API.
Determines which teams are eliminated or have clinched 1st place.
"""
import logging
from typing import TypedDict

import requests

logger = logging.getLogger(__name__)

ESPN_STANDINGS_URL = "https://site.api.espn.com/apis/v2/sports/soccer/fifa.world/standings"

# Points available per remaining game (max = 3)
POINTS_PER_WIN = 3
TEAMS_ADVANCING_PER_GROUP = 2  # top 2 from each group advance


class TeamStatus(TypedDict):
    team: str
    group: str
    points: int
    played: int
    wins: int
    draws: int
    losses: int
    goals_for: int
    goals_against: int
    goal_diff: int
    eliminated: bool
    clinched_first: bool
    clinched_second: bool   # locked into 2nd-place slot specifically
    clinched_advance: bool
    espn_advanced: bool     # ESPN's authoritative advancement flag (accounts for tiebreakers)
    espn_note: str          # ESPN's status description
    espn_rank: int          # ESPN's ranked position in group (1-4); used to confirm 1st vs 2nd


def fetch_standings() -> dict[str, TeamStatus]:
    """
    Fetch current World Cup 2026 group standings.
    Returns: {team_name_lower: TeamStatus}
    """
    try:
        resp = requests.get(ESPN_STANDINGS_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error(f"Failed to fetch ESPN standings: {e}")
        return {}

    team_statuses: dict[str, TeamStatus] = {}

    groups = data.get("standings", []) or data.get("children", [])

    for group_entry in groups:
        group_name = group_entry.get("name", group_entry.get("abbreviation", "?"))
        entries = group_entry.get("standings", {}).get("entries", [])
        if not entries:
            entries = group_entry.get("entries", [])

        group_teams = _parse_group_entries(entries, group_name)
        _apply_status_flags(group_teams)

        for team_data in group_teams:
            key = team_data["team"].lower()
            team_statuses[key] = team_data

    logger.info(f"Fetched standings for {len(team_statuses)} teams")
    return team_statuses


def _parse_group_entries(entries: list, group_name: str) -> list[TeamStatus]:
    teams = []

    for entry in entries:
        team_info = entry.get("team", {})
        team_name = team_info.get("displayName") or team_info.get("name") or "Unknown"

        stats_map = {}
        for stat in entry.get("stats", []):
            name = stat.get("name") or stat.get("abbreviation") or ""
            value = stat.get("value", 0)
            stats_map[name.lower()] = value

        points = int(stats_map.get("points", stats_map.get("pts", 0)))
        played = int(stats_map.get("gamesplayed", stats_map.get("gp", 0)))
        wins = int(stats_map.get("wins", stats_map.get("w", 0)))
        draws = int(stats_map.get("ties", stats_map.get("d", stats_map.get("draws", 0))))
        losses = int(stats_map.get("losses", stats_map.get("l", 0)))
        goals_for = int(stats_map.get("pointsfor", stats_map.get("gf", 0)))
        goals_against = int(stats_map.get("pointsagainst", stats_map.get("ga", 0)))

        # ESPN's authoritative advancement flag — accounts for head-to-head tiebreakers.
        # advanced=1.0 means truly locked in; 0.0 = projected but not yet confirmed.
        # note.description "Advance to Round of 32" can appear for projected-but-unconfirmed
        # teams too, so we rely ONLY on the numeric advanced stat, not the description.
        espn_advanced = bool(stats_map.get("advanced", 0))
        note_obj = entry.get("note", {}) or {}
        espn_note = note_obj.get("description", "")
        espn_rank = int(note_obj.get("rank", 0))

        teams.append({
            "team": team_name,
            "group": group_name,
            "points": points,
            "played": played,
            "wins": wins,
            "draws": draws,
            "losses": losses,
            "goals_for": goals_for,
            "goals_against": goals_against,
            "goal_diff": goals_for - goals_against,
            "eliminated": False,
            "clinched_first": False,
            "clinched_second": False,
            "clinched_advance": False,
            "espn_advanced": espn_advanced,
            "espn_note": espn_note,
            "espn_rank": espn_rank,
        })

    return teams


def _apply_status_flags(group_teams: list[TeamStatus]) -> None:
    """
    Determine elimination and clinching status within a group.
    World Cup 2026 group stage: 4 teams, 3 games each, top 2 advance.
    Total group games per team = 3.
    """
    total_games = 3  # each team plays 3 group stage games

    # Sort by points desc, then goal diff, then goals for
    ranked = sorted(
        group_teams,
        key=lambda t: (t["points"], t["goal_diff"], t["goals_for"]),
        reverse=True,
    )

    for i, team in enumerate(ranked):
        remaining = total_games - team["played"]
        max_possible_points = team["points"] + remaining * POINTS_PER_WIN

        if len(ranked) >= 2:
            second_place_points = ranked[1]["points"] if i >= 2 else 0
        else:
            second_place_points = 0

        # Eliminated: ESPN note is authoritative; fall back to points math
        if "eliminated" in team["espn_note"].lower():
            team["eliminated"] = True
        elif i >= TEAMS_ADVANCING_PER_GROUP and max_possible_points < second_place_points:
            team["eliminated"] = True

        # Clinched advance: ESPN's flag accounts for head-to-head tiebreakers
        if team["espn_advanced"]:
            team["clinched_advance"] = True
        elif i < TEAMS_ADVANCING_PER_GROUP:
            third_team = ranked[2] if len(ranked) > 2 else None
            if third_team:
                third_remaining = total_games - third_team["played"]
                third_max = third_team["points"] + third_remaining * POINTS_PER_WIN
                if third_max < team["points"]:
                    team["clinched_advance"] = True

        # Clinched 1st: ESPN advanced=1.0 AND ESPN's own rank=1.
        # ESPN computes head-to-head tiebreakers in their rank, so espn_rank=1 + advanced=1.0
        # means they've secured 1st accounting for all tiebreakers.
        # Fall back to points math (no tie possible) if ESPN data is absent.
        if i == 0:
            if team["espn_advanced"] and team["espn_rank"] == 1:
                team["clinched_first"] = True
            else:
                second_team = ranked[1] if len(ranked) > 1 else None
                if not second_team:
                    team["clinched_first"] = True
                else:
                    second_remaining = total_games - second_team["played"]
                    second_max = second_team["points"] + second_remaining * POINTS_PER_WIN
                    if second_max < team["points"]:
                        team["clinched_first"] = True

        # Clinched 2nd: ESPN advanced=1.0 AND ESPN's rank=2 (their position is also locked),
        # which requires rank-1 to also be clinched (otherwise rank could still swap).
        if i == 1:
            if team["espn_advanced"] and team["espn_rank"] == 2:
                first_team = ranked[0] if len(ranked) > 0 else None
                if first_team and first_team.get("clinched_first"):
                    team["clinched_second"] = True


def is_deadwood_match(
    home_team: str,
    away_team: str,
    statuses: dict[str, TeamStatus],
) -> bool:
    """
    Returns True when NEITHER team has anything to play for.
    A team is 'dead' if it is eliminated OR has already clinched 1st place.
    TBD teams are never considered dead.
    """
    if home_team == "TBD" or away_team == "TBD":
        return False

    def _is_dead(team_name: str) -> bool:
        s = statuses.get(team_name.lower())
        if not s:
            return False
        return s["eliminated"] or s["clinched_first"]

    return _is_dead(home_team) and _is_dead(away_team)


def is_match_worth_monitoring(
    home_team: str,
    away_team: str,
    statuses: dict[str, TeamStatus],
) -> tuple[bool, str]:
    """
    Returns (should_monitor, reason).
    Skips if either team is eliminated, or if both teams have clinched 1st.
    'TBD' teams are always monitored (we don't know the teams yet).
    """
    if home_team == "TBD" or away_team == "TBD":
        return True, "Teams TBD — monitoring until confirmed"

    home = statuses.get(home_team.lower())
    away = statuses.get(away_team.lower())

    if home and home["eliminated"]:
        return False, f"{home_team} is eliminated"
    if away and away["eliminated"]:
        return False, f"{away_team} is eliminated"

    # If both teams clinched 1st: still worth watching (it's a high-profile match)
    # Only skip if the match is genuinely irrelevant (both clinched, nothing at stake)
    # Keep it simple — only hard skip on elimination
    return True, "Match is active"


def get_group_rankings(statuses: dict[str, TeamStatus]) -> dict[str, list[str]]:
    """Returns {group_letter: [1st, 2nd, 3rd, 4th]} sorted by current standings."""
    groups: dict[str, list] = {}
    for team_data in statuses.values():
        g = team_data["group"]
        letter = g.split()[-1] if " " in g else g
        groups.setdefault(letter, []).append(team_data)
    return {
        letter: [t["team"] for t in sorted(
            teams,
            key=lambda t: (-t["points"], -t["goal_diff"], -t["goals_for"])
        )]
        for letter, teams in groups.items()
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    statuses = fetch_standings()
    print(f"\n{'Team':<25} {'Group':<8} {'Pts':>4} {'Played':>6} {'Elim':>5} {'1st':>4}")
    print("-" * 60)
    for key, s in sorted(statuses.items(), key=lambda x: (x[1]["group"], -x[1]["points"])):
        print(
            f"{s['team']:<25} {s['group']:<8} {s['points']:>4} {s['played']:>6} "
            f"{'YES' if s['eliminated'] else 'no':>5} {'YES' if s['clinched_first'] else 'no':>4}"
        )
