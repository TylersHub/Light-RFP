from __future__ import annotations

from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .models import Solicitation


def _parse_datetimeish(value: str) -> datetime | None:
    if not value:
        return None

    normalized = value.strip()
    for candidate in (
        normalized,
        normalized.replace("Z", "+00:00"),
    ):
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            continue
    for pattern in (
        "%m/%d/%Y",
        "%m/%d/%Y %I:%M %p",
        "%B %d, %Y",
        "%b %d, %Y",
    ):
        try:
            return datetime.strptime(normalized, pattern)
        except ValueError:
            continue
    return None


def format_display_date(value: str) -> str:
    parsed = _parse_datetimeish(value)
    if parsed:
        return parsed.strftime("%b %d, %Y")
    return value


def format_display_datetime(value: str) -> str:
    parsed = _parse_datetimeish(value)
    if parsed:
        return parsed.strftime("%b %d, %Y %I:%M %p").replace(" 0", " ")
    return value


def render_report(
    results: list[Solicitation],
    output_path: Path,
    total_candidates: int,
    top_n: int,
    source_url: str,
) -> None:
    templates_dir = Path(__file__).resolve().parents[1] / "templates"
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    env.filters["display_date"] = format_display_date
    env.filters["display_datetime"] = format_display_datetime
    template = env.get_template("report.html.j2")

    html = template.render(
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        results=results,
        total_candidates=total_candidates,
        top_n=top_n,
        source_url=source_url,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
