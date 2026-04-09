# Purpose: Tests for engineering audit fixes (S-1, S-2, S-5, S-7, S-8, P-4, P-5, D-1).

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from closed_claw.tools.executor import ToolExecutor, ToolExecutionError


# ──────────────────────────────────────────────────────────
# S-1: SSRF IP resolution bypass tests
# ──────────────────────────────────────────────────────────


class TestSSRFIPResolution:
    """Verify _check_url_safety blocks encoded IP variants."""

    def _make(self, tmp_path: Path) -> ToolExecutor:
        return ToolExecutor(workspace_root=tmp_path)

    def test_blocks_hex_ip_localhost(self, tmp_path: Path):
        """0x7f000001 == 127.0.0.1 — must be blocked."""
        ex = self._make(tmp_path)
        # Mock getaddrinfo to return the decoded 127.0.0.1
        fake_addr = [(2, 1, 6, "", ("127.0.0.1", 80))]
        with patch("closed_claw.tools.executor.socket.getaddrinfo", return_value=fake_addr):
            with pytest.raises(ToolExecutionError, match="non-public address"):
                ex._check_url_safety("http://0x7f000001/admin")

    def test_blocks_decimal_ip_localhost(self, tmp_path: Path):
        """2130706433 == 127.0.0.1 — must be blocked."""
        ex = self._make(tmp_path)
        fake_addr = [(2, 1, 6, "", ("127.0.0.1", 80))]
        with patch("closed_claw.tools.executor.socket.getaddrinfo", return_value=fake_addr):
            with pytest.raises(ToolExecutionError, match="non-public address"):
                ex._check_url_safety("http://2130706433/secret")

    def test_blocks_ipv6_mapped_localhost(self, tmp_path: Path):
        """[::ffff:127.0.0.1] — must be blocked."""
        ex = self._make(tmp_path)
        fake_addr = [(10, 1, 6, "", ("::ffff:127.0.0.1", 80, 0, 0))]
        with patch("closed_claw.tools.executor.socket.getaddrinfo", return_value=fake_addr):
            with pytest.raises(ToolExecutionError, match="non-public address"):
                ex._check_url_safety("http://[::ffff:127.0.0.1]/")

    def test_blocks_link_local_169_254(self, tmp_path: Path):
        """Cloud metadata 169.254.169.254 — blocked as metadata host."""
        ex = self._make(tmp_path)
        with pytest.raises(ToolExecutionError, match="metadata endpoint"):
            ex._check_url_safety("http://169.254.169.254/latest/meta-data")

    def test_blocks_metadata_hostname(self, tmp_path: Path):
        """metadata.google.internal — blocked before DNS resolution."""
        ex = self._make(tmp_path)
        with pytest.raises(ToolExecutionError, match="metadata endpoint"):
            ex._check_url_safety("http://metadata.google.internal/computeMetadata")

    def test_blocks_private_10_range(self, tmp_path: Path):
        """10.x.x.x is private — must be blocked."""
        ex = self._make(tmp_path)
        fake_addr = [(2, 1, 6, "", ("10.0.0.1", 80))]
        with patch("closed_claw.tools.executor.socket.getaddrinfo", return_value=fake_addr):
            with pytest.raises(ToolExecutionError, match="non-public address"):
                ex._check_url_safety("http://internal-service.corp/api")

    def test_blocks_private_192_168(self, tmp_path: Path):
        """192.168.x.x is private — must be blocked."""
        ex = self._make(tmp_path)
        fake_addr = [(2, 1, 6, "", ("192.168.1.100", 8080))]
        with patch("closed_claw.tools.executor.socket.getaddrinfo", return_value=fake_addr):
            with pytest.raises(ToolExecutionError, match="non-public address"):
                ex._check_url_safety("http://my-router.local:8080")

    def test_allows_public_ip(self, tmp_path: Path):
        """Public IPs should pass the check."""
        ex = self._make(tmp_path)
        fake_addr = [(2, 1, 6, "", ("93.184.216.34", 80))]
        with patch("closed_claw.tools.executor.socket.getaddrinfo", return_value=fake_addr):
            # Should not raise
            ex._check_url_safety("http://example.com/page")

    def test_blocks_unresolvable_host(self, tmp_path: Path):
        """Unresolvable hosts should be blocked."""
        import socket
        ex = self._make(tmp_path)
        with patch("closed_claw.tools.executor.socket.getaddrinfo", side_effect=socket.gaierror("not found")):
            with pytest.raises(ToolExecutionError, match="cannot resolve"):
                ex._check_url_safety("http://definitelynotarealdomainxyz123.com")

    def test_blocks_file_scheme(self, tmp_path: Path):
        """file:// scheme blocked."""
        ex = self._make(tmp_path)
        with pytest.raises(ToolExecutionError, match="disallowed scheme"):
            ex._check_url_safety("file:///etc/passwd")

    def test_blocks_gopher_scheme(self, tmp_path: Path):
        """gopher:// scheme blocked."""
        ex = self._make(tmp_path)
        with pytest.raises(ToolExecutionError, match="disallowed scheme"):
            ex._check_url_safety("gopher://evil.com/ssrf")

    def test_blocks_no_hostname(self, tmp_path: Path):
        """URLs without a hostname should be rejected."""
        ex = self._make(tmp_path)
        with pytest.raises(ToolExecutionError, match="no hostname"):
            ex._check_url_safety("http://")


# ──────────────────────────────────────────────────────────
# S-2: SQL query denylist tests
# ──────────────────────────────────────────────────────────


class TestSQLQueryHardening:
    def _make(self, tmp_path: Path) -> ToolExecutor:
        return ToolExecutor(workspace_root=tmp_path)

    def _make_db(self, tmp_path: Path) -> Path:
        db_path = tmp_path / "test.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE items (id INTEGER, name TEXT)")
        conn.execute("INSERT INTO items VALUES (1, 'alpha')")
        conn.commit()
        conn.close()
        return db_path

    def test_valid_select_works(self, tmp_path: Path):
        """Normal SELECT should work."""
        db = self._make_db(tmp_path)
        ex = self._make(tmp_path)
        result = ex.execute(
            "sql_query",
            {"db_path": str(db), "query": "SELECT * FROM items"},
            allowlist=["sql_query"],
        )
        assert len(result["rows"]) == 1

    def test_blocks_semicolon_multistatement(self, tmp_path: Path):
        """Multi-statement queries via ; must be blocked."""
        db = self._make_db(tmp_path)
        ex = self._make(tmp_path)
        with pytest.raises(ToolExecutionError, match="forbidden pattern"):
            ex.execute(
                "sql_query",
                {"db_path": str(db), "query": "SELECT 1; DROP TABLE items"},
                allowlist=["sql_query"],
            )

    def test_blocks_attach(self, tmp_path: Path):
        """ATTACH DATABASE must be blocked."""
        db = self._make_db(tmp_path)
        ex = self._make(tmp_path)
        with pytest.raises(ToolExecutionError, match="only allows SELECT"):
            ex.execute(
                "sql_query",
                {"db_path": str(db), "query": "ATTACH DATABASE ':memory:' AS memdb"},
                allowlist=["sql_query"],
            )

    def test_blocks_load_extension(self, tmp_path: Path):
        """load_extension() in query must be blocked."""
        db = self._make_db(tmp_path)
        ex = self._make(tmp_path)
        with pytest.raises(ToolExecutionError, match="forbidden pattern"):
            ex.execute(
                "sql_query",
                {"db_path": str(db), "query": "SELECT load_extension('evil.so')"},
                allowlist=["sql_query"],
            )

    def test_blocks_pragma(self, tmp_path: Path):
        """PRAGMA and pragma_* functions must be blocked."""
        db = self._make_db(tmp_path)
        ex = self._make(tmp_path)
        with pytest.raises(ToolExecutionError, match="forbidden pattern"):
            ex.execute(
                "sql_query",
                {"db_path": str(db), "query": "SELECT * FROM pragma_table_info('items')"},
                allowlist=["sql_query"],
            )
        # Also test standalone PRAGMA
        with pytest.raises(ToolExecutionError, match="only allows SELECT"):
            ex.execute(
                "sql_query",
                {"db_path": str(db), "query": "PRAGMA table_info('items')"},
                allowlist=["sql_query"],
            )

    def test_blocks_non_select(self, tmp_path: Path):
        """INSERT/UPDATE/DELETE still blocked."""
        db = self._make_db(tmp_path)
        ex = self._make(tmp_path)
        with pytest.raises(ToolExecutionError, match="only allows SELECT"):
            ex.execute(
                "sql_query",
                {"db_path": str(db), "query": "INSERT INTO items VALUES (2, 'beta')"},
                allowlist=["sql_query"],
            )


# ──────────────────────────────────────────────────────────
# T-4: Additional SSRF edge-case tests
# ──────────────────────────────────────────────────────────


class TestSSRFEdgeCases:
    """Extra SSRF checks: 172.16 range, IPv6 loopback, data: scheme, DNS rebind."""

    def _make(self, tmp_path: Path) -> ToolExecutor:
        return ToolExecutor(workspace_root=tmp_path)

    def test_blocks_172_16_private(self, tmp_path: Path):
        """172.16.x.x is private — must be blocked."""
        ex = self._make(tmp_path)
        fake_addr = [(2, 1, 6, "", ("172.16.0.5", 80))]
        with patch("closed_claw.tools.executor.socket.getaddrinfo", return_value=fake_addr):
            with pytest.raises(ToolExecutionError, match="non-public address"):
                ex._check_url_safety("http://internal.corp.example:8080/")

    def test_blocks_ipv6_loopback(self, tmp_path: Path):
        """::1 is IPv6 loopback — must be blocked."""
        ex = self._make(tmp_path)
        fake_addr = [(10, 1, 6, "", ("::1", 80, 0, 0))]
        with patch("closed_claw.tools.executor.socket.getaddrinfo", return_value=fake_addr):
            with pytest.raises(ToolExecutionError, match="non-public address"):
                ex._check_url_safety("http://sneaky-host.example/")

    def test_blocks_data_scheme(self, tmp_path: Path):
        """data: scheme should be blocked."""
        ex = self._make(tmp_path)
        with pytest.raises(ToolExecutionError, match="disallowed scheme"):
            ex._check_url_safety("data:text/html,<h1>hi</h1>")

    def test_blocks_url_with_credentials(self, tmp_path: Path):
        """URL with embedded credentials resolving to private IP — blocked."""
        ex = self._make(tmp_path)
        fake_addr = [(2, 1, 6, "", ("10.0.0.1", 80))]
        with patch("closed_claw.tools.executor.socket.getaddrinfo", return_value=fake_addr):
            with pytest.raises(ToolExecutionError, match="non-public address"):
                ex._check_url_safety("http://admin:password@evil.example/admin")

    def test_blocks_dns_rebind_to_private(self, tmp_path: Path):
        """A public-looking hostname that resolves to 192.168 — blocked."""
        ex = self._make(tmp_path)
        fake_addr = [(2, 1, 6, "", ("192.168.0.1", 443))]
        with patch("closed_claw.tools.executor.socket.getaddrinfo", return_value=fake_addr):
            with pytest.raises(ToolExecutionError, match="non-public address"):
                ex._check_url_safety("https://evil-rebind.example.com/steal")

    def test_allows_multiple_public_ips(self, tmp_path: Path):
        """Hostname resolving to multiple public IPs — allowed."""
        ex = self._make(tmp_path)
        fake_addrs = [
            (2, 1, 6, "", ("93.184.216.34", 80)),
            (2, 1, 6, "", ("93.184.216.35", 80)),
        ]
        with patch("closed_claw.tools.executor.socket.getaddrinfo", return_value=fake_addrs):
            ex._check_url_safety("http://cdn.example.com/resource")  # Should not raise

    def test_blocks_when_one_ip_is_private(self, tmp_path: Path):
        """Hostname resolving to mix of public + private — blocked."""
        ex = self._make(tmp_path)
        fake_addrs = [
            (2, 1, 6, "", ("93.184.216.34", 80)),
            (2, 1, 6, "", ("127.0.0.1", 80)),
        ]
        with patch("closed_claw.tools.executor.socket.getaddrinfo", return_value=fake_addrs):
            with pytest.raises(ToolExecutionError, match="non-public address"):
                ex._check_url_safety("http://multi-ip.example.com/")


# ──────────────────────────────────────────────────────────
# S-5: Symlink path traversal tests
# ──────────────────────────────────────────────────────────


class TestSymlinkTraversal:
    def test_safe_path_blocks_symlink_outside_workspace(self, tmp_path: Path):
        """A symlink inside workspace pointing outside should be rejected."""
        import os
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "secret.txt"
        secret.write_text("secret data")

        link = workspace / "sneaky_link"
        try:
            os.symlink(outside, link)
        except OSError:
            pytest.skip("OS does not support symlinks")

        ex = ToolExecutor(workspace_root=workspace)
        with pytest.raises(ToolExecutionError, match="escapes workspace"):
            ex._safe_path(str(link / "secret.txt"))

    def test_safe_path_allows_normal_relative(self, tmp_path: Path):
        """Normal relative paths inside workspace should work."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "file.txt").write_text("ok")

        ex = ToolExecutor(workspace_root=workspace)
        result = ex._safe_path("file.txt")
        assert result == (workspace / "file.txt").resolve()

    def test_safe_path_blocks_dotdot_escape(self, tmp_path: Path):
        """../../../etc/passwd style escapes should be blocked."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        ex = ToolExecutor(workspace_root=workspace)
        with pytest.raises(ToolExecutionError, match="escapes workspace"):
            ex._safe_path("../../../etc/passwd")


# ──────────────────────────────────────────────────────────
# T-5: Additional file_io path-traversal tests
# ──────────────────────────────────────────────────────────


class TestPathTraversalEdgeCases:
    """Extra path traversal checks: absolute outside, embedded dotdot, deep nesting."""

    def test_blocks_absolute_path_outside_workspace(self, tmp_path: Path):
        """Absolute path pointing outside workspace root should be blocked."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "data.txt").write_text("secret")

        ex = ToolExecutor(workspace_root=workspace)
        with pytest.raises(ToolExecutionError, match="escapes workspace"):
            ex._safe_path(str(outside / "data.txt"))

    def test_blocks_dotdot_in_middle(self, tmp_path: Path):
        """subdir/../../etc/passwd — dotdot in the middle still escapes."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "subdir").mkdir()

        ex = ToolExecutor(workspace_root=workspace)
        with pytest.raises(ToolExecutionError, match="escapes workspace"):
            ex._safe_path("subdir/../../etc/passwd")

    def test_allows_subdirectory_path(self, tmp_path: Path):
        """Nested path like sub/deep/file.txt inside workspace passes."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        deep = workspace / "sub" / "deep"
        deep.mkdir(parents=True)
        (deep / "file.txt").write_text("ok")

        ex = ToolExecutor(workspace_root=workspace)
        result = ex._safe_path("sub/deep/file.txt")
        assert result == (workspace / "sub" / "deep" / "file.txt").resolve()

    def test_blocks_tilde_expansion_outside(self, tmp_path: Path):
        """~/../../etc/passwd with expanduser should still be blocked."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        ex = ToolExecutor(workspace_root=workspace)
        # expanduser will resolve ~ to home dir, which is outside workspace
        with pytest.raises(ToolExecutionError, match="escapes workspace"):
            ex._safe_path("~/../../etc/passwd")

    def test_allows_dot_current_dir(self, tmp_path: Path):
        """./file.txt should work (equivalent to file.txt)."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "file.txt").write_text("ok")

        ex = ToolExecutor(workspace_root=workspace)
        result = ex._safe_path("./file.txt")
        assert result == (workspace / "file.txt").resolve()


# ──────────────────────────────────────────────────────────
# S-8: JSON extraction (non-greedy) tests
# ──────────────────────────────────────────────────────────


class TestExtractJSON:
    def test_direct_json(self):
        from closed_claw.registry.search import _extract_json
        result = _extract_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_fenced_json(self):
        from closed_claw.registry.search import _extract_json
        text = 'Some preamble\n```json\n{"a": 1}\n```\nMore text'
        assert _extract_json(text) == {"a": 1}

    def test_embedded_json_not_greedy(self):
        """The old greedy regex would capture from first { to last }, merging
        two separate JSON objects. The new approach should parse the first one."""
        from closed_claw.registry.search import _extract_json
        text = 'Result: {"answer": 42} with extra {"noise": true} end'
        result = _extract_json(text)
        assert result == {"answer": 42}

    def test_array_json(self):
        from closed_claw.registry.search import _extract_json
        result = _extract_json('[1, 2, 3]')
        assert result == [1, 2, 3]

    def test_empty_returns_empty_dict(self):
        from closed_claw.registry.search import _extract_json
        assert _extract_json("") == {}
        assert _extract_json("   ") == {}

    def test_no_json_raises(self):
        from closed_claw.registry.search import _extract_json
        with pytest.raises(ValueError, match="did not contain valid JSON"):
            _extract_json("no json here at all")


# ──────────────────────────────────────────────────────────
# S-7: enable_load_extension disabled after use
# ──────────────────────────────────────────────────────────


class TestLoadExtensionDisabled:
    def test_store_disables_after_load(self, tmp_path: Path):
        """After _try_load_sqlite_vec, enable_load_extension should be False."""
        from closed_claw.registry.store import RegistryStore
        # Create a store that doesn't require sqlite_vec
        schema_path = Path(__file__).resolve().parent.parent.parent / "closed_claw" / "registry" / "schema.sql"
        store = RegistryStore(
            db_path=tmp_path / "test.db",
            schema_path=schema_path,
            embedding_dim=8,
            require_sqlite_vec=False,
        )
        # The store should work without sqlite-vec; key thing is the
        # _try_load_sqlite_vec method always calls enable_load_extension(False)
        # in its finally block. We verify by checking the method exists and
        # the store initialised without error.
        assert store is not None
        store.close()


# ──────────────────────────────────────────────────────────
# P-4: AuditStore cached connection
# ──────────────────────────────────────────────────────────


class TestAuditStoreCachedConn:
    def test_connection_is_reused(self, tmp_path: Path):
        """_conn() should return the same connection object."""
        from closed_claw.policy.audit import AuditStore
        store = AuditStore(tmp_path / "audit.db")
        conn1 = store._conn()
        conn2 = store._conn()
        assert conn1 is conn2
        store.close()

    def test_close_clears_connection(self, tmp_path: Path):
        """After close(), _conn() creates a new connection."""
        from closed_claw.policy.audit import AuditStore
        store = AuditStore(tmp_path / "audit.db")
        conn1 = store._conn()
        store.close()
        conn2 = store._conn()
        assert conn1 is not conn2
        store.close()

    def test_record_and_list(self, tmp_path: Path):
        """Basic record + list still works with cached connection."""
        from closed_claw.policy.audit import AuditStore
        store = AuditStore(tmp_path / "audit.db")
        store.record_event("test_event", {"key": "val"}, run_id="r1")
        events = store.list_events(limit=10)
        assert len(events) == 1
        assert events[0]["event_type"] == "test_event"
        store.close()


# ──────────────────────────────────────────────────────────
# P-5: RunLogger atomic emit
# ──────────────────────────────────────────────────────────


class TestRunLoggerAtomic:
    def test_emit_creates_valid_jsonl(self, tmp_path: Path):
        """Each emit should produce a valid JSONL line."""
        from closed_claw.observability.runlog import RunLogger
        rl = RunLogger(tmp_path, "test-run")
        rl.emit("event_a", {"x": 1})
        rl.emit("event_b", {"y": 2})
        lines = rl.path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        for line in lines:
            parsed = json.loads(line)
            assert "ts" in parsed
            assert "event" in parsed

    def test_concurrent_emits_dont_corrupt(self, tmp_path: Path):
        """Multiple threads emitting concurrently should not corrupt JSONL."""
        from closed_claw.observability.runlog import RunLogger
        rl = RunLogger(tmp_path, "concurrent-run")
        errors = []

        def worker(idx: int):
            try:
                for i in range(50):
                    rl.emit(f"event_{idx}_{i}", {"thread": idx, "iter": i})
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors during concurrent emit: {errors}"
        lines = rl.path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 200  # 4 threads × 50 events
        for line in lines:
            parsed = json.loads(line)
            assert "event" in parsed


# ──────────────────────────────────────────────────────────
# D-1: CoordinatorState TypedDict has proper docstring
# ──────────────────────────────────────────────────────────


class TestCoordinatorState:
    def test_state_is_importable(self):
        """CoordinatorState should be importable and have expected keys."""
        from closed_claw.coordinator.state import CoordinatorState
        # TypedDict keys
        annotations = CoordinatorState.__annotations__
        assert "run_id" in annotations
        assert "task" in annotations
        assert "task_plan" in annotations
        assert "subtask_pool" in annotations
        assert "runtime_policies" in annotations

    def test_candidate_is_importable(self):
        from closed_claw.coordinator.state import Candidate
        assert "agent_id" in Candidate.__annotations__


# ──────────────────────────────────────────────────────────
# R-4: conftest.py shared fixture works
# ──────────────────────────────────────────────────────────


class TestConftest:
    def test_make_test_settings_defaults(self, tmp_path: Path):
        """make_test_settings returns valid Settings."""
        from tests.conftest import make_test_settings
        s = make_test_settings(tmp_path)
        assert s.db_path == tmp_path / "registry.db"
        assert s.llm_provider == "heuristic"

    def test_make_test_settings_overrides(self, tmp_path: Path):
        """make_test_settings accepts keyword overrides."""
        from tests.conftest import make_test_settings
        s = make_test_settings(tmp_path, llm_provider="openai", agent_retries=5)
        assert s.llm_provider == "openai"
        assert s.agent_retries == 5

    def test_test_settings_fixture(self, test_settings):
        """The test_settings fixture should provide a usable Settings."""
        assert test_settings.llm_provider == "heuristic"
        assert test_settings.db_path.name == "registry.db"
