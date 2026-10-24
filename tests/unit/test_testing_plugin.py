"""
The nexusql pytest plugin, run through pytester.

The plugin is registered through the pytest11 entry point of an installed
nexusql, which a source checkout does not have, so each inner session loads
it explicitly from its conftest the way an uninstalled user would.
"""

import pytest

pytest_plugins = ["pytester"]

LOAD_PLUGIN = "pytest_plugins = ['nexusql.testing.pytest_plugin']\n"


@pytest.fixture
def project(pytester, monkeypatch):
    monkeypatch.delenv("NEXUSQL_TEST_URL", raising=False)
    pytester.makeconftest(LOAD_PLUGIN)
    return pytester


def test_fixtures_are_registered(project):
    result = project.runpytest("--fixtures")
    result.stdout.fnmatch_lines(["nexusql_sqlite*", "nexusql_db*", "nexusql_tx*"])


def test_nexusql_sqlite_is_a_fresh_connected_manager(project):
    project.makepyfile(
        """
        from nexusql import DatabaseManager

        def test_one(nexusql_sqlite, tmp_path):
            assert isinstance(nexusql_sqlite, DatabaseManager)
            assert nexusql_sqlite.database_url.startswith("sqlite:///")
            assert str(tmp_path) in nexusql_sqlite.database_url
            nexusql_sqlite.execute("CREATE TABLE t (id SERIAL PRIMARY KEY, v TEXT)")
            assert nexusql_sqlite.execute(
                "INSERT INTO t (v) VALUES (:v)", {"v": "x"}, return_id=True) == 1

        def test_two(nexusql_sqlite):
            # A new file per test: nothing from test_one is visible.
            assert not nexusql_sqlite.table_exists("t")
        """
    )
    project.runpytest().assert_outcomes(passed=2)


def test_nexusql_db_defaults_to_sqlite_in_tmp_path(project):
    project.makepyfile(
        """
        def test_default(nexusql_db, tmp_path):
            assert nexusql_db.database_url == f"sqlite:///{tmp_path / 'nexusql.db'}"
            assert nexusql_db.fetch_one("SELECT 1 AS one") == {"one": 1}
        """
    )
    project.runpytest().assert_outcomes(passed=1)


def test_nexusql_db_takes_the_command_line_option(project, tmp_path):
    url = f"sqlite:///{tmp_path / 'option.db'}"
    project.makepyfile(
        f"""
        def test_option(nexusql_db):
            assert nexusql_db.database_url == {url!r}
        """
    )
    project.runpytest("--nexusql-url", url).assert_outcomes(passed=1)


def test_nexusql_db_takes_the_environment_variable(project, tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'env.db'}"
    monkeypatch.setenv("NEXUSQL_TEST_URL", url)
    project.makepyfile(
        f"""
        def test_env(nexusql_db):
            assert nexusql_db.database_url == {url!r}
        """
    )
    project.runpytest().assert_outcomes(passed=1)


def test_option_wins_over_environment(project, tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUSQL_TEST_URL", f"sqlite:///{tmp_path / 'env.db'}")
    url = f"sqlite:///{tmp_path / 'option.db'}"
    project.makepyfile(
        f"""
        def test_precedence(nexusql_db):
            assert nexusql_db.database_url == {url!r}
        """
    )
    project.runpytest("--nexusql-url", url).assert_outcomes(passed=1)


def test_unreachable_server_skips(project):
    pytest.importorskip("pymysql")
    project.makepyfile(
        """
        def test_server(nexusql_db):
            raise AssertionError("must not run")
        """
    )
    # Port 1 refuses immediately, so the skip is reported without a wait.
    result = project.runpytest("--nexusql-url", "mysql://u:p@127.0.0.1:1/db", "-rs")
    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines(["*mysql not reachable at 127.0.0.1:1/db*"])


SHARED_FILE_CONFTEST = LOAD_PLUGIN + """
import pytest

@pytest.fixture
def nexusql_url(tmp_path_factory):
    # One file for the whole session, so a later test can see whether an
    # earlier test's rows were rolled back.
    return f"sqlite:///{tmp_path_factory.mktemp('shared') / 'shared.db'}"
"""


def test_nexusql_tx_rolls_back_what_the_test_wrote(project):
    project.makeconftest(SHARED_FILE_CONFTEST)
    project.makepyfile(
        """
        import pytest

        @pytest.fixture
        def schema(nexusql_db):
            if not nexusql_db.table_exists("t"):
                nexusql_db.execute("CREATE TABLE t (id SERIAL PRIMARY KEY, v TEXT)")

        def test_a_writes(schema, nexusql_tx):
            assert nexusql_tx._in_transaction
            nexusql_tx.execute("INSERT INTO t (v) VALUES (:v)", {"v": "x"})
            assert nexusql_tx.fetch_one("SELECT COUNT(*) AS n FROM t") == {"n": 1}

        def test_b_sees_nothing(schema, nexusql_db):
            assert nexusql_db.fetch_one("SELECT COUNT(*) AS n FROM t") == {"n": 0}
        """
    )
    project.runpytest().assert_outcomes(passed=2)


def test_nexusql_tx_rolls_back_even_when_the_test_fails(project):
    project.makeconftest(SHARED_FILE_CONFTEST)
    project.makepyfile(
        """
        import pytest

        @pytest.fixture
        def schema(nexusql_db):
            if not nexusql_db.table_exists("t"):
                nexusql_db.execute("CREATE TABLE t (id SERIAL PRIMARY KEY, v TEXT)")

        def test_a_fails_after_writing(schema, nexusql_tx):
            nexusql_tx.execute("INSERT INTO t (v) VALUES (:v)", {"v": "x"})
            assert False

        def test_b_sees_nothing(schema, nexusql_db):
            assert nexusql_db.fetch_one("SELECT COUNT(*) AS n FROM t") == {"n": 0}
        """
    )
    project.runpytest().assert_outcomes(passed=1, failed=1)
