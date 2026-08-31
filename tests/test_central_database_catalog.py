import json
import sqlite3
from pathlib import Path

from session_migrate.catalog import Catalog, auto_roots, discover_roots
from session_migrate.cli import main
from session_migrate.conversion import (
    load_devin_session,
    load_hermes_session,
    load_mastracode_session,
)
from session_migrate.formats import claude, devin, hermes, mastracode
from session_migrate.model import AgentFormat

FIXTURES = Path(__file__).parent / "fixtures"
HERMES_IDS = ("20260830_120000_a1b2c3", "20260830_120100_d4e5f6")
MASTRA_IDS = (
    "16161616-1616-4616-8616-161616161616",
    "17171717-1717-4717-8717-171717171717",
)
DEVIN_IDS = ("fix-timeline-merging", "review-auth-boundary")


def _hermes_store(home: Path) -> Path:
    home.mkdir(parents=True)
    path = home / hermes.HERMES_STATE_FILENAME
    with sqlite3.connect(path) as database:
        database.executescript(
            """
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, source TEXT NOT NULL, user_id TEXT,
                model TEXT, model_config TEXT, parent_session_id TEXT,
                started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
                message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0,
                cwd TEXT, billing_provider TEXT, title TEXT, title_source TEXT,
                last_activity_at REAL, archived INTEGER DEFAULT 0,
                hidden INTEGER DEFAULT 0
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
                role TEXT NOT NULL, content TEXT, tool_call_id TEXT, tool_calls TEXT,
                tool_name TEXT, timestamp REAL NOT NULL, finish_reason TEXT,
                reasoning TEXT, reasoning_content TEXT, reasoning_details TEXT,
                observed INTEGER DEFAULT 0, _compressed_summary INTEGER DEFAULT 0,
                active INTEGER DEFAULT 1, compacted INTEGER DEFAULT 0,
                api_content TEXT, display_kind TEXT, display_metadata TEXT
            );
            INSERT INTO schema_version VALUES (26);
            """
        )
        for offset, (session_id, title) in enumerate(
            zip(HERMES_IDS, ("Fix parser boundary", "Trace scheduler race"), strict=True)
        ):
            started = 1_788_093_296.0 + offset * 60
            database.execute(
                """INSERT INTO sessions(
                       id,source,model,model_config,started_at,ended_at,end_reason,
                       message_count,tool_call_count,cwd,billing_provider,title,
                       title_source,last_activity_at,archived,hidden
                   ) VALUES (?, 'cli', 'loopback/fixture', '{}', ?, ?, 'cli_close',
                             1, 0, ?, 'loopback', ?, 'user', ?, 0, 0)""",
                (session_id, started, started + 1, str(home), title, started + 1),
            )
            database.execute(
                """INSERT INTO messages(
                       session_id,role,content,timestamp,observed,_compressed_summary,
                       active,compacted
                   ) VALUES (?, 'user', ?, ?, 0, 0, 1, 0)""",
                (session_id, f"private Hermes body {offset}", started),
            )
    return path


def _shared_stores(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = claude.parse(FIXTURES / "claude-2.1.209" / "basic.jsonl")
    hermes_home = tmp_path / "hermes"
    _hermes_store(hermes_home)

    mastra_database = tmp_path / "mastracode" / "mastra.db"
    for session_id, title in zip(
        MASTRA_IDS, ("Audit cache invalidation", "Repair queue fairness"), strict=True
    ):
        artifact, _ = mastracode.serialize(
            source,
            session_id=session_id,
            cwd=tmp_path,
            timestamp="2026-08-30T12:00:00Z",
            title=title,
        )
        mastracode.install_native_bytes(artifact, mastra_database, session_id=session_id)

    devin_home = tmp_path / "devin" / "cli"
    for session_id, title in zip(
        DEVIN_IDS, ("Fix timeline merging", "Review authentication boundary"), strict=True
    ):
        artifact, _ = devin.serialize(
            source,
            session_id=session_id,
            cwd=tmp_path,
            timestamp="2026-08-30T12:00:00Z",
            title=title,
        )
        devin.install_database(artifact, devin_home, session_id)
    return hermes_home, mastra_database, devin_home


def test_catalog_indexes_searches_and_loads_every_shared_database_session(
    tmp_path: Path,
) -> None:
    hermes_home, mastra_database, devin_home = _shared_stores(tmp_path)
    catalog_path = tmp_path / "catalog" / "catalog.sqlite3"

    with Catalog(catalog_path) as catalog:
        first = catalog.refresh(
            hermes_roots=(hermes_home,),
            mastracode_roots=(mastra_database,),
            devin_roots=(devin_home,),
            include_auto=False,
            validate=True,
        )
        assert first.files_seen == 6
        assert first.scanned == 6
        assert first.root_errors == 0
        assert first.statuses == {"validated": 6}

        expected = {
            "parser boundary": (AgentFormat.HERMES, HERMES_IDS[0]),
            "cache invalidation": (AgentFormat.MASTRACODE, MASTRA_IDS[0]),
            "timeline merging": (AgentFormat.DEVIN, DEVIN_IDS[0]),
        }
        loaders = {
            AgentFormat.HERMES: load_hermes_session,
            AgentFormat.MASTRACODE: load_mastracode_session,
            AgentFormat.DEVIN: load_devin_session,
        }
        for query, (agent_format, native_id) in expected.items():
            matches = catalog.list_sessions(query=query, include_paths=True)
            assert len(matches) == 1
            entry = matches[0]
            assert entry.format == agent_format.value
            assert entry.session_id == native_id
            assert entry.path == f"{agent_format.value}:{native_id}"
            source = catalog.session_source_for_transfer(entry.catalog_id)
            assert source.is_virtual
            assert source.path is None
            loaded = loaders[agent_format](native_id, source_home=source.root)
            assert loaded.session_id == native_id
            assert loaded.title == entry.title

        second = catalog.refresh(include_auto=False, validate=True)
        assert second.files_seen == 6
        assert second.unchanged == 6
        assert second.scanned == 0


def test_shared_database_catalog_marks_removed_native_identity_missing(tmp_path: Path) -> None:
    hermes_home, mastra_database, devin_home = _shared_stores(tmp_path)
    with Catalog(tmp_path / "catalog.sqlite3") as catalog:
        catalog.refresh(
            hermes_roots=(hermes_home,),
            mastracode_roots=(mastra_database,),
            devin_roots=(devin_home,),
            include_auto=False,
        )
        with sqlite3.connect(mastra_database) as database:
            database.execute('DELETE FROM "mastra_messages" WHERE thread_id = ?', (MASTRA_IDS[1],))
            database.execute('DELETE FROM "mastra_threads" WHERE id = ?', (MASTRA_IDS[1],))

        changed = catalog.refresh(include_auto=False)
        assert changed.missing == 1
        assert catalog.list_sessions(query="queue fairness") == []
        missing = catalog.list_sessions(
            query="queue fairness", include_missing=True, statuses=("missing",)
        )
        assert len(missing) == 1
        assert missing[0].session_id == MASTRA_IDS[1]


def test_cli_dry_run_loads_each_shared_database_source(tmp_path: Path, capsys: object) -> None:
    hermes_home, mastra_database, devin_home = _shared_stores(tmp_path)
    cases = (
        ("hermes", HERMES_IDS[0], hermes_home, "mastracode"),
        ("mastracode", MASTRA_IDS[0], mastra_database, "devin"),
        ("devin", DEVIN_IDS[0], devin_home, "mastracode"),
    )
    for index, (source_format, session_id, source_home, target_format) in enumerate(cases):
        target_home = tmp_path / f"target-{index}"
        status = main(
            [
                "transfer",
                session_id,
                "--from",
                source_format,
                "--source-home",
                str(source_home),
                "--to",
                target_format,
                "--home",
                str(target_home),
                "--cwd",
                str(tmp_path),
                "--dry-run",
            ]
        )
        assert status == 0
        result = json.loads(capsys.readouterr().out)  # type: ignore[attr-defined]
        assert result["source_format"] == source_format
        assert result["target_format"] == target_format
        assert result["dry_run"] is True
        assert not target_home.exists()


def test_shared_database_roots_follow_environment_and_bounded_discovery(tmp_path: Path) -> None:
    boundary = tmp_path / "workspace"
    hermes_home, mastra_database, devin_home = _shared_stores(boundary)
    hidden_hermes = boundary / ".hermes"
    hermes_home.rename(hidden_hermes)

    roots = auto_roots(
        cwd=tmp_path,
        home=tmp_path / "home",
        environ={
            "HERMES_HOME": str(hidden_hermes),
            "MASTRA_DB_PATH": str(mastra_database),
            "XDG_DATA_HOME": str(boundary),
        },
    )
    root_set = {(agent_format, path, source) for agent_format, path, source in roots}
    assert (AgentFormat.HERMES, hidden_hermes, "environment") in root_set
    assert (AgentFormat.MASTRACODE, mastra_database, "environment") in root_set
    assert (AgentFormat.DEVIN, devin_home, "environment") in root_set

    discovered = set(discover_roots((boundary,)))
    assert (AgentFormat.HERMES, hidden_hermes, "discovered") in discovered
    assert (AgentFormat.MASTRACODE, mastra_database, "discovered") in discovered
    assert (AgentFormat.DEVIN, devin_home, "discovered") in discovered
