#!/usr/bin/env python3
"""Generate privacy-safe GitHub contribution SVGs for a profile README.

Only aggregate contribution-calendar data is queried. Repository names,
commit messages, pull-request titles, and source code are never requested.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
API_URL = "https://api.github.com/graphql"
REST_USER_URL = "https://api.github.com/user"
USERNAME = os.getenv("GITHUB_USERNAME", "salahsaeed19")
TZ_NAME = os.getenv("CONTRIBUTION_TIMEZONE", "Asia/Hebron")
PALETTE = ("#172337", "#34294e", "#714B67", "#5278b7", "#88b8e8")

QUERY = """
query ProfileContributions($login: String!, $from: DateTime!, $to: DateTime!) {
  viewer { login }
  user(login: $login) {
    login
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar {
        totalContributions
        weeks {
          contributionDays { date contributionCount weekday }
        }
      }
    }
  }
}
"""


class AnalyticsError(RuntimeError):
    """A safe, user-facing analytics failure."""


@dataclass(frozen=True)
class CalendarWindow:
    """GitHub query bounds and the exact calendar dates they cover."""

    start: datetime
    end: datetime
    start_day: date
    end_day: date

    @property
    def dates(self) -> list[date]:
        return [self.start_day + timedelta(days=offset) for offset in range((self.end_day - self.start_day).days + 1)]


@dataclass(frozen=True)
class ApiResponse:
    data: dict[str, Any]


@dataclass(frozen=True)
class AuthenticationContext:
    """Identity and OAuth-scope evidence from GitHub's documented REST headers."""

    login: str | None
    oauth_scopes: frozenset[str]
    scopes_observed: bool
    verification_error: str | None = None


def resolve_timezone(timezone_name: str) -> tzinfo:
    """Resolve the configured IANA timezone without silently changing it."""
    if timezone_name.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise AnalyticsError(
            f"Timezone data for {timezone_name!r} is unavailable. Install IANA tzdata or use CONTRIBUTION_TIMEZONE=UTC."
        ) from exc


def contribution_window(
    today: date | None = None, timezone_name: str = TZ_NAME, now: datetime | None = None
) -> CalendarWindow:
    """Return an exact 365-day, calendar-aligned [from, to) GitHub query window."""
    zone = resolve_timezone(timezone_name)
    current_day = today or (now.astimezone(zone).date() if now else datetime.now(zone).date())
    start_day = current_day - timedelta(days=364)
    return CalendarWindow(
        start=datetime.combine(start_day, time.min, zone),
        end=datetime.combine(current_day + timedelta(days=1), time.min, zone),
        start_day=start_day,
        end_day=current_day,
    )


def date_window(today: date | None = None, timezone_name: str = TZ_NAME) -> tuple[datetime, datetime, date]:
    """Backward-compatible helper used by tests and local integrations."""
    window = contribution_window(today, timezone_name)
    return window.start, window.end, window.end_day


def parse_oauth_scopes(value: str | None) -> frozenset[str]:
    return frozenset(scope.strip().casefold() for scope in (value or "").split(",") if scope.strip())


def authenticated_context(token: str) -> AuthenticationContext:
    """Read token identity and scopes from GET /user without exposing either."""
    request = urllib.request.Request(
        REST_USER_URL,
        headers={
            "Authorization": f"bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "salahsaeed19-profile-analytics",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.load(response)
            scope_header = response.headers.get("X-OAuth-Scopes")
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError, OSError):
        # The calendar may still be public and usable, but its private scope is
        # deliberately treated as unknown rather than inferred.
        return AuthenticationContext(None, frozenset(), False, "REST authentication verification unavailable")
    if not isinstance(result, dict) or not isinstance(result.get("login"), str):
        return AuthenticationContext(None, frozenset(), False, "REST authentication response was incomplete")
    return AuthenticationContext(
        login=result["login"],
        oauth_scopes=parse_oauth_scopes(scope_header),
        scopes_observed=scope_header is not None,
    )


def graphql(token: str, login: str, window: CalendarWindow) -> ApiResponse:
    payload = json.dumps(
        {
            "query": QUERY,
            "variables": {"login": login, "from": window.start.isoformat(), "to": window.end.isoformat()},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        API_URL,
        data=payload,
        headers={
            "Authorization": f"bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
            "User-Agent": "salahsaeed19-profile-analytics",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise AnalyticsError(f"GitHub GraphQL returned HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise AnalyticsError("Could not reach GitHub GraphQL.") from exc
    except (json.JSONDecodeError, OSError) as exc:
        raise AnalyticsError("GitHub GraphQL returned an invalid response.") from exc

    if not isinstance(result, dict):
        raise AnalyticsError("GitHub GraphQL returned an unexpected response.")
    if result.get("errors"):
        messages = "; ".join(str(item.get("message", "GraphQL error")) for item in result["errors"])
        raise AnalyticsError(f"GitHub GraphQL error: {messages}")
    data = result.get("data")
    if not isinstance(data, dict) or not data.get("user"):
        raise AnalyticsError("GitHub user was not returned by GraphQL.")
    return ApiResponse(data=data)


def month_keys_between(start_day: date, end_day: date) -> list[str]:
    """Return every calendar month intersecting an exact date window."""
    cursor = date(start_day.year, start_day.month, 1)
    final = date(end_day.year, end_day.month, 1)
    keys: list[str] = []
    while cursor <= final:
        keys.append(cursor.strftime("%Y-%m"))
        cursor = date(cursor.year + (cursor.month == 12), (cursor.month % 12) + 1, 1)
    return keys


def private_access_verified(data: dict[str, Any], login: str, auth: AuthenticationContext | None, token_source: str) -> bool:
    """Private inclusion requires owner identity and observed REST read:user scope."""
    viewer_login = str(data.get("viewer", {}).get("login", "")).casefold()
    return bool(
        auth
        and token_source == "personal"
        and auth.scopes_observed
        and auth.login is not None
        and auth.login.casefold() == login.casefold()
        and viewer_login == login.casefold()
        and "read:user" in auth.oauth_scopes
    )


def normalize(
    data: dict[str, Any],
    window: CalendarWindow,
    login: str,
    auth: AuthenticationContext | None = None,
    token_source: str = "workflow",
) -> dict[str, Any]:
    """Validate the returned calendar and calculate exact-window aggregates."""
    try:
        collection = data["user"]["contributionsCollection"]
        calendar = collection["contributionCalendar"]
        raw_days = [day for week in calendar["weeks"] for day in week["contributionDays"]]
    except (KeyError, TypeError) as exc:
        raise AnalyticsError("GitHub returned an incomplete contribution calendar.") from exc

    returned: dict[date, int] = {}
    for item in raw_days:
        try:
            calendar_day = date.fromisoformat(item["date"])
            count = int(item["contributionCount"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AnalyticsError("GitHub returned an invalid contribution day.") from exc
        if count < 0 or calendar_day in returned:
            raise AnalyticsError("GitHub returned an invalid contribution calendar.")
        returned[calendar_day] = count

    expected = set(window.dates)
    missing = expected.difference(returned)
    if missing:
        raise AnalyticsError(f"GitHub contribution calendar is incomplete ({len(missing)} requested dates missing).")

    days = {calendar_day: returned[calendar_day] for calendar_day in window.dates}
    calculated_total = sum(days.values())
    try:
        api_total = int(calendar["totalContributions"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AnalyticsError("GitHub returned an invalid contribution total.") from exc
    if api_total != calculated_total:
        raise AnalyticsError("GitHub contribution total does not match the returned calendar.")

    months = {key: 0 for key in month_keys_between(window.start_day, window.end_day)}
    for calendar_day, count in days.items():
        months[calendar_day.strftime("%Y-%m")] += count
    if sum(months.values()) != calculated_total:
        raise AnalyticsError("Monthly aggregation does not match the contribution calendar.")

    private_verified = private_access_verified(data, login, auth, token_source)
    scope_label = "Public + eligible private activity" if private_verified else "Private inclusion unverified"
    return {
        "total": calculated_total,
        "today": days[window.end_day],
        "last30": sum(days[window.end_day - timedelta(days=offset)] for offset in range(30)),
        "days": days,
        "months": months,
        "window_start": window.start_day.isoformat(),
        "window_end": window.end_day.isoformat(),
        "private_access_verified": private_verified,
        "scope_label": scope_label,
    }


def svg_document(content: str, width: int, height: int, label: str) -> str:
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(label)}">
  <defs>
    <linearGradient id="accent" x1="0" x2="1"><stop stop-color="#714B67"/><stop offset="1" stop-color="#83b8ee"/></linearGradient>
    <linearGradient id="panel" x1="0" x2="1"><stop stop-color="#111d31"/><stop offset="1" stop-color="#0d1728"/></linearGradient>
  </defs>
  <rect width="100%" height="100%" rx="20" fill="#091321"/>
  <rect x="1" y="1" width="{width - 2}" height="{height - 2}" rx="19" fill="none" stroke="#243858"/>
  {content}
</svg>'''


def text(x: float, y: float, value: str, size: int, color: str = "#f4f8ff", weight: int = 400, anchor: str = "start") -> str:
    return f'<text x="{x}" y="{y}" fill="{color}" font-family="Arial, Helvetica, sans-serif" font-size="{size}" font-weight="{weight}" text-anchor="{anchor}">{html.escape(str(value))}</text>'


def stats_svg(metrics: dict[str, Any] | None, message: str | None = None) -> str:
    if metrics is None:
        body = text(36, 54, "LIVE DEVELOPMENT ACTIVITY", 13, "#95a9c7", 700)
        body += text(36, 108, "Awaiting secure GitHub API access", 25, "#f4f8ff", 700)
        body += text(36, 141, message or "Run the scheduled workflow to publish live counts.", 14, "#bac8db")
        return svg_document(body, 920, 178, "Live development activity unavailable")

    cards = (("TODAY", metrics["today"]), ("LAST 30 DAYS", metrics["last30"]), ("WINDOW", "365 DAYS"))
    body = text(36, 42, "LIVE DEVELOPMENT ACTIVITY", 13, "#95a9c7", 700)
    body += text(36, 98, f'{metrics["total"]:,}', 44, "#f4f8ff", 700)
    body += text(36, 126, "contributions in the exact 365-day calendar window", 14, "#bac8db")
    body += '<rect x="36" y="146" width="840" height="4" rx="2" fill="url(#accent)"/>'
    body += text(36, 177, metrics["scope_label"], 13, "#95a9c7")
    body += text(884, 177, f'{metrics["window_start"]} → {metrics["window_end"]}', 13, "#95a9c7", 400, "end")
    for index, (label, value) in enumerate(cards):
        x = 368 + index * 170
        body += f'<rect x="{x}" y="48" width="150" height="84" rx="13" fill="url(#panel)" stroke="#2a4063"/>'
        body += text(x + 14, 77, label, 10, "#95a9c7", 700)
        body += text(x + 14, 112, value, 25 if isinstance(value, int) else 19, "#f4f8ff", 700)
    return svg_document(body, 920, 204, "Live development activity")


def trend_svg(metrics: dict[str, Any] | None, message: str | None = None) -> str:
    if metrics is None:
        body = text(30, 42, "EXACT-WINDOW ACTIVITY TREND", 13, "#95a9c7", 700)
        body += text(30, 88, message or "No contribution data has been published yet.", 16, "#bac8db")
        return svg_document(body, 920, 122, "Activity trend unavailable")

    items = list(metrics["months"].items())
    maximum = max(metrics["months"].values()) or 1
    count = len(items)
    chart_left, chart_width, bar_width = 38, 844, 40
    gap = (chart_width - count * bar_width) / max(count - 1, 1)
    body = text(30, 36, "EXACT-WINDOW ACTIVITY TREND", 13, "#95a9c7", 700)
    body += text(890, 36, f'{metrics["window_start"]} → {metrics["window_end"]}', 12, "#95a9c7", 400, "end")
    body += '<path d="M38 162H882" stroke="#233754" stroke-width="1"/>'
    for index, (key, value) in enumerate(items):
        x = chart_left + index * (bar_width + gap)
        height = round((value / maximum) * 96)
        y = 162 - height
        fill = "url(#accent)" if value else "#1a2a43"
        label = datetime.strptime(key, "%Y-%m").strftime("%b")
        body += f'<rect x="{x:.1f}" y="{y}" width="{bar_width}" height="{height}" rx="5" fill="{fill}"><title>{key}: {value} contributions</title></rect>'
        body += text(x + bar_width / 2, 183, label, 10, "#95a9c7", 400, "middle")
        if value:
            body += text(x + bar_width / 2, y - 7, str(value), 10, "#dbe9ff", 700, "middle")
    return svg_document(body, 920, 205, "Exact 365-day contribution activity trend")


def heatmap_svg(metrics: dict[str, Any] | None, message: str | None = None) -> str:
    if metrics is None:
        body = text(30, 42, "CONTRIBUTION HEATMAP", 13, "#95a9c7", 700)
        body += text(30, 88, message or "No contribution data has been published yet.", 16, "#bac8db")
        return svg_document(body, 920, 122, "Contribution heatmap unavailable")

    start_day = date.fromisoformat(metrics["window_start"])
    end_day = date.fromisoformat(metrics["window_end"])
    grid_start = start_day - timedelta(days=(start_day.weekday() + 1) % 7)  # Sunday
    max_count = max(metrics["days"].values()) or 1
    body = text(30, 35, "CONTRIBUTION HEATMAP", 13, "#95a9c7", 700)
    body += text(890, 35, "exact window · no repository details", 12, "#95a9c7", 400, "end")
    for row, label in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        body += text(29, 69 + row * 16, label, 10, "#95a9c7")
    for column in range(53):
        for row in range(7):
            current = grid_start + timedelta(days=column * 7 + row)
            x, y = 63 + column * 16, 48 + row * 16
            if current < start_day or current > end_day:
                body += f'<rect x="{x}" y="{y}" width="12" height="12" rx="3" fill="#0b1626"/>'
                continue
            count = metrics["days"][current]
            level = 0 if count == 0 else min(4, max(1, (count * 4 + max_count - 1) // max_count))
            body += f'<rect x="{x}" y="{y}" width="12" height="12" rx="3" fill="{PALETTE[level]}"><title>{current.isoformat()}: {count} contributions</title></rect>'
    legend_x = 728
    body += text(legend_x, 179, "Less", 10, "#95a9c7")
    for index, colour in enumerate(PALETTE):
        body += f'<rect x="{legend_x + 34 + index * 16}" y="169" width="12" height="12" rx="3" fill="{colour}"/>'
    body += text(864, 179, "More", 10, "#95a9c7")
    return svg_document(body, 920, 204, "GitHub contribution heatmap")


def technology_svg() -> str:
    technologies = (("Py", "Python"), ("Od", "Odoo"), ("Dj", "Django"), ("DRF", "REST"), ("Pg", "PostgreSQL"), ("JS", "JavaScript"), ("Ow", "OWL"), ("<>", "XML"), ("Git", "Git"), ("Li", "Linux"), ("QW", "QWeb"), ("API", "Integrations"))
    body = text(30, 38, "ENGINEERING TOOLKIT", 13, "#95a9c7", 700)
    for index, (mark, label) in enumerate(technologies):
        column, row = index % 6, index // 6
        x, y = 28 + column * 148, 56 + row * 82
        body += f'<rect x="{x}" y="{y}" width="132" height="60" rx="12" fill="url(#panel)" stroke="#2a4063"/>'
        body += f'<rect x="{x + 10}" y="{y + 12}" width="36" height="36" rx="9" fill="#714B67"/>'
        body += text(x + 28, y + 35, mark, 11 if len(mark) > 2 else 14, "#ffffff", 700, "middle")
        body += text(x + 56, y + 36, label, 12, "#e7f0ff", 700)
    return svg_document(body, 920, 222, "Engineering technology stack")


def atomic_write(path: Path, content: str) -> None:
    ET.fromstring(content)  # Validate before replacing a published asset.
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=path.parent, suffix=".tmp") as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(path)


def write_assets(metrics: dict[str, Any] | None, message: str | None = None) -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    atomic_write(ASSETS / "live-development-activity.svg", stats_svg(metrics, message))
    atomic_write(ASSETS / "activity-trend.svg", trend_svg(metrics, message))
    atomic_write(ASSETS / "contribution-heatmap.svg", heatmap_svg(metrics, message))
    atomic_write(ASSETS / "technology-stack.svg", technology_svg())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--placeholder", action="store_true", help="Create honest no-data activity assets without calling GitHub.")
    args = parser.parse_args()
    if args.placeholder:
        write_assets(None, "Awaiting a scheduled GitHub GraphQL refresh.")
        return 0

    personal_token = os.getenv("GH_CONTRIBUTIONS_TOKEN")
    token = personal_token or os.getenv("GITHUB_TOKEN")
    if not token:
        print("GH_CONTRIBUTIONS_TOKEN or GITHUB_TOKEN is required for a live refresh.", file=sys.stderr)
        return 2
    try:
        window = contribution_window()
        auth = authenticated_context(token)
        response = graphql(token, USERNAME, window)
        metrics = normalize(response.data, window, USERNAME, auth, "personal" if personal_token else "workflow")
        write_assets(metrics)
        print(f"Updated assets for {USERNAME}: {metrics['total']} contributions in the exact 365-day window.")
        return 0
    except AnalyticsError as exc:
        print(f"Analytics refresh failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
