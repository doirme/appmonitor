"""Optional Streamlit viewer for AppMonitor SQLite databases."""

# ruff: noqa: ANN401, PLC0415

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from appmonitor.reporting import ReportDatabase, ReportDatabaseError, ReportPage

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

_DATABASE_ENVIRONMENT = "APPMONITOR_VIEWER_DATABASE"
_DEFAULT_DATABASE = Path(".appmonitor/runs.sqlite3")


def main(argv: Sequence[str] | None = None) -> int:
    """Launch the viewer through Streamlit's supported CLI runner."""
    parser = argparse.ArgumentParser(prog="appmonitor-viewer")
    parser.add_argument("--database", type=Path, default=_DEFAULT_DATABASE)
    arguments = parser.parse_args(argv)
    database = arguments.database.expanduser().resolve()
    return _run_streamlit(["run", str(Path(__file__).resolve()), "--", "--database", str(database)])


def render_app(database: str | Path) -> None:
    """Render all read-only operational views for one database."""
    st = _streamlit()
    st.set_page_config(page_title="AppMonitor", page_icon=":material/monitor_heart:", layout="wide")
    _apply_style(st)
    st.title("AppMonitor")
    path = Path(database).expanduser().resolve()
    st.caption(str(path))

    try:
        report = ReportDatabase(path)
    except ReportDatabaseError as error:
        st.error(str(error))
        return

    if st.button("Refresh", icon=":material/refresh:", help="Refresh database results"):
        st.cache_data.clear()

    tabs = st.tabs(
        [
            "Overview",
            "Runs",
            "Runtime",
            "LLM",
            "Maintenance",
            "Git and recovery",
            "Tables",
        ]
    )
    with tabs[0]:
        _overview(st, report)
    with tabs[1]:
        _runs(st, report)
    with tabs[2]:
        _runtime(st, report)
    with tabs[3]:
        _page_view(st, _cached(st, report, "llm_stats"), empty="No LLM calls recorded.")
    with tabs[4]:
        _page_view(
            st,
            _cached(st, report, "maintenance"),
            empty="No patch decisions recorded.",
        )
    with tabs[5]:
        _page_view(
            st,
            _cached(st, report, "git_recovery"),
            empty="No Git maintenance recorded.",
        )
    with tabs[6]:
        _tables(st, report)


def _overview(st: Any, report: ReportDatabase) -> None:
    """Render top-level operational counters."""
    stats = _cached(st, report, "overview")
    first = st.columns(5)
    first[0].metric("Runs", stats.runs)
    first[1].metric("Succeeded", stats.succeeded)
    first[2].metric("Failed", stats.failed)
    first[3].metric("Timed out", stats.timed_out)
    first[4].metric("Latest activity", _short_timestamp(stats.latest_activity))

    second = st.columns(5)
    second[0].metric("LLM calls", stats.llm_calls)
    second[1].metric("LLM cost", f"${stats.llm_cost_usd:.4f}")
    second[2].metric("LLM success", _percent(stats.llm_success_rate))
    second[3].metric(
        "Average latency",
        _seconds(stats.average_llm_latency_seconds),
    )
    second[4].metric("Accepted patches", stats.accepted_patches)

    third = st.columns(4)
    third[0].metric("Rejected patches", stats.rejected_patches)
    third[1].metric("Pushed branches", stats.pushed_branches)
    third[2].metric("Restarts", stats.restarts)
    third[3].metric("Stop decisions", stats.stop_decisions)

    recent = _cached(st, report, "runs", limit=10)
    st.subheader("Recent runs")
    _page_view(st, recent, empty="No runs recorded.")


def _runs(st: Any, report: ReportDatabase) -> None:
    """Render filterable run history and portable report details."""
    filters = st.columns((1, 2, 1))
    outcome = filters[0].selectbox(
        "Outcome",
        ("All", "succeeded", "failed", "timed_out", "cancelled"),
    )
    search = filters[1].text_input("Search")
    page_size = filters[2].selectbox("Rows", (25, 50, 100), index=1)
    page = _cached(
        st,
        report,
        "runs",
        outcome=None if outcome == "All" else outcome,
        search=search,
        limit=page_size,
    )
    _page_view(st, page, empty="No matching runs.")
    if not page.rows:
        return
    run_ids = [str(row["run_id"]) for row in page.rows]
    selected = st.selectbox("Run detail", run_ids)
    detail = _cached(st, report, "run_detail", run_id=selected)["report"]
    output_tabs = st.tabs(["Summary", "stdout", "stderr", "States", "Artifacts"])
    with output_tabs[0]:
        st.json(_without_large_sections(detail))
    with output_tabs[1]:
        st.code(_line_messages(detail, "stdout"), language="text")
    with output_tabs[2]:
        st.code(_line_messages(detail, "stderr"), language="text")
    with output_tabs[3]:
        st.dataframe(_mapping_value(detail, "transitions", []), width="stretch", hide_index=True)
    with output_tabs[4]:
        st.json(_mapping_value(detail, "artifacts", {}))


def _runtime(st: Any, report: ReportDatabase) -> None:
    """Render a selected run's process metric timeline."""
    runs = _cached(st, report, "runs", limit=200)
    if not runs.rows:
        st.info("No runs recorded.")
        return
    selected = st.selectbox(
        "Run",
        [str(row["run_id"]) for row in runs.rows],
        key="runtime_run",
    )
    metrics = _cached(st, report, "runtime_metrics", run_id=selected, limit=1_000)
    if not metrics.rows:
        st.info("No runtime metrics recorded for this run.")
        return
    peak_rss = max(int(_required_cell(row["rss_bytes"])) for row in metrics.rows)
    peak_cpu = max(float(_required_cell(row["cpu_percent"])) for row in metrics.rows)
    summary = st.columns(3)
    summary[0].metric("Samples", metrics.total)
    summary[1].metric("Peak RSS", _bytes(peak_rss))
    summary[2].metric("Peak CPU", f"{peak_cpu:.1f}%")
    chart_rows = [
        {
            "timestamp": row["timestamp"],
            "RSS MiB": int(_required_cell(row["rss_bytes"])) / (1024 * 1024),
            "CPU %": row["cpu_percent"],
            "Processes": row["process_count"],
            "Threads": row["thread_count"],
        }
        for row in reversed(metrics.rows)
    ]
    st.line_chart(chart_rows, x="timestamp", y=["RSS MiB", "CPU %", "Processes", "Threads"])
    st.dataframe(metrics.rows, width="stretch", hide_index=True)


def _tables(st: Any, report: ReportDatabase) -> None:
    """Render the allow-listed raw table browser and CSV export."""
    tables = _cached(st, report, "tables")
    if not tables:
        st.info("No AppMonitor tables found.")
        return
    controls = st.columns((2, 1))
    selected = controls[0].selectbox("Table", tables)
    page_size = controls[1].selectbox("Rows", (25, 50, 100, 250), index=2, key="table_rows")
    page = _cached(st, report, "table", name=selected, limit=page_size)
    _page_view(st, page, empty="This table is empty.")
    st.download_button(
        "Download CSV",
        data=_csv(page),
        file_name=f"{selected}.csv",
        mime="text/csv",
        icon=":material/download:",
        disabled=not page.rows,
    )


def _page_view(st: Any, page: ReportPage, *, empty: str) -> None:
    """Render one bounded report page."""
    if not page.rows:
        st.info(empty)
        return
    st.dataframe(page.rows, width="stretch", hide_index=True)
    st.caption(f"{len(page.rows)} of {page.total} rows")


def _without_large_sections(payload: object) -> object:
    """Exclude streams and artifacts already rendered in dedicated views."""
    if not isinstance(payload, dict):
        return payload
    return {
        key: value
        for key, value in payload.items()
        if key not in {"stdout", "stderr", "transitions", "artifacts", "metrics"}
    }


def _line_messages(payload: object, stream: str) -> str:
    """Join captured stream messages into a readable text block."""
    if not isinstance(payload, dict):
        return ""
    lines = payload.get(stream, [])
    if not isinstance(lines, list):
        return ""
    return "\n".join(str(line.get("message", "")) for line in lines if isinstance(line, dict))


def _mapping_value(payload: object, key: str, default: object) -> object:
    """Read one value from a dynamically loaded JSON object."""
    return payload.get(key, default) if isinstance(payload, dict) else default


def _required_cell(value: bytes | float | str | None) -> bytes | float | str:
    """Narrow a non-null numeric report cell."""
    if value is None:
        message = "required report cell is null"
        raise ReportDatabaseError(message)
    return value


def _cached(st: Any, report: ReportDatabase, operation: str, **kwargs: object) -> Any:
    """Cache a serializable report result briefly, never its SQLite connection."""
    cached_query = st.cache_data(ttl=5, show_spinner=False)(_query)
    return cached_query(str(report.database), operation, json.dumps(kwargs, sort_keys=True))


def _query(database: str, operation: str, arguments_json: str) -> object:
    """Dispatch one allow-listed reporting query for Streamlit's data cache."""
    report = ReportDatabase(database)
    arguments = json.loads(arguments_json)
    operations: dict[str, Callable[..., object]] = {
        "overview": report.overview,
        "runs": report.runs,
        "run_detail": report.run_detail,
        "runtime_metrics": report.runtime_metrics,
        "llm_stats": report.llm_stats,
        "maintenance": report.maintenance,
        "git_recovery": report.git_recovery,
        "tables": report.tables,
        "table": report.table,
    }
    try:
        query = operations[operation]
    except KeyError as error:
        message = f"unknown viewer operation: {operation}"
        raise ValueError(message) from error
    return query(**arguments)


def _csv(page: ReportPage) -> str:
    """Serialize one visible report page to CSV."""
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=page.columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(page.rows)
    return output.getvalue()


def _short_timestamp(value: str | None) -> str:
    """Format an ISO timestamp compactly without timezone conversion."""
    return value.replace("T", " ")[:19] if value else "-"


def _percent(value: float | None) -> str:
    """Format a nullable ratio."""
    return f"{value:.1%}" if value is not None else "-"


def _seconds(value: float | None) -> str:
    """Format nullable seconds."""
    return f"{value:.2f} s" if value is not None else "-"


def _bytes(value: int) -> str:
    """Format bytes as a compact binary unit."""
    return f"{value / (1024 * 1024):.1f} MiB"


def _apply_style(st: Any) -> None:
    """Apply restrained operational styling without altering Streamlit behavior."""
    st.markdown(
        """
        <style>
        .stApp { background: #f7f8fa; color: #1f2933; }
        [data-testid="stHeader"] { background: #f7f8fa; }
        [data-testid="stMetric"] {
            background: #ffffff;
            border: 1px solid #d9dee7;
            border-radius: 6px;
            padding: 0.75rem;
        }
        .stTabs [data-baseweb="tab-list"] { gap: 1.25rem; }
        .stTabs [data-baseweb="tab"] { padding-left: 0; padding-right: 0; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _database_argument(argv: Sequence[str] | None = None) -> Path:
    """Resolve Streamlit script arguments with an environment fallback."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--database", type=Path)
    arguments, _ = parser.parse_known_args(argv)
    configured = arguments.database or Path(
        os.environ.get(_DATABASE_ENVIRONMENT, _DEFAULT_DATABASE)
    )
    return configured.expanduser().resolve()


def _streamlit() -> Any:
    """Import the optional viewer dependency with an actionable error."""
    try:
        import streamlit
    except ImportError as error:
        message = "viewer dependency missing; install appmonitor[viewer]"
        raise RuntimeError(message) from error
    return streamlit


def _run_streamlit(arguments: list[str]) -> int:
    """Run Streamlit while restoring process arguments for embedding callers."""
    try:
        from streamlit.web.cli import main as streamlit_main
    except ImportError as error:
        message = "viewer dependency missing; install appmonitor[viewer]"
        raise RuntimeError(message) from error
    previous = sys.argv
    try:
        sys.argv = ["streamlit", *arguments]
        result = streamlit_main()
    finally:
        sys.argv = previous
    return int(result or 0)


if __name__ == "__main__":
    render_app(_database_argument())
