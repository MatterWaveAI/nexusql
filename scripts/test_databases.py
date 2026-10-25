#!/usr/bin/env python
"""Start, stop and inspect the local database containers the test suite uses.

The integration tests read TEST_POSTGRESQL_URL, TEST_MYSQL_URL and
TEST_MSSQL_URL (tests/conftest.py); without a reachable server behind a URL
the matching parametrisations skip. This script brings up one container per
backend with the credentials and ports those defaults expect, so the whole
suite runs locally, not just the SQLite half.

Works with podman or docker, whichever is on PATH (NEXUSQL_CONTAINER_RUNTIME
overrides the choice).

    python scripts/test_databases.py up            # all three
    python scripts/test_databases.py up mysql mssql
    python scripts/test_databases.py status        # containers + reachability
    python scripts/test_databases.py env           # export lines for your shell
    python scripts/test_databases.py env --powershell
    python scripts/test_databases.py down          # stop and remove

A port that is already bound by something else (for example another test
Postgres) is left alone and reported, since the suite can point at it
through the TEST_*_URL variable instead.

SQL Server also needs an ODBC driver on the host: Microsoft's "ODBC Driver
18 for SQL Server" (winget install Microsoft.msodbcsql.18 on Windows, the
msodbcsql18 apt/yum package on Linux). The legacy "SQL Server" driver that
ships with Windows connects but mishandles newer types such as DATETIME2.

The SQL Server image is built from tests/mssql-fts.Dockerfile on first use,
so it carries the full-text search component the stock image lacks.
"""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Service:
    name: str
    image: str
    host_port: int
    container_port: int
    env: dict = field(default_factory=dict)
    url: str = ""
    env_var: str = ""
    # Arguments passed to the image's entrypoint (the server command line).
    args: tuple = ()
    # Dockerfile (relative to the repo root) to build ``image`` from when it
    # is not present locally.
    dockerfile: str = ""

    @property
    def container(self) -> str:
        return f"nexusql-test-{self.name}"


SERVICES = {
    "postgres": Service(
        name="postgres",
        image="docker.io/library/postgres:16-alpine",
        host_port=15432,
        container_port=5432,
        env={"POSTGRES_USER": "testuser", "POSTGRES_PASSWORD": "testpass",
             "POSTGRES_DB": "nexusql_test"},
        url="postgresql://testuser:testpass@127.0.0.1:15432/nexusql_test",
        env_var="TEST_POSTGRESQL_URL",
    ),
    "mysql": Service(
        name="mysql",
        image="docker.io/library/mysql:8",
        host_port=13306,
        container_port=3306,
        env={"MYSQL_ROOT_PASSWORD": "rootpass", "MYSQL_USER": "testuser",
             "MYSQL_PASSWORD": "testpass", "MYSQL_DATABASE": "nexusql_test"},
        url="mysql://testuser:testpass@127.0.0.1:13306/nexusql_test",
        env_var="TEST_MYSQL_URL",
        # MySQL 8 enables binary logging by default and then refuses to let a
        # user without SUPER create triggers or stored functions, which the
        # change-data-capture and row-level-security features need.
        args=("--log-bin-trust-function-creators=1",),
    ),
    "mssql": Service(
        name="mssql",
        image="localhost/nexusql-test-mssql-fts:2022",
        dockerfile="tests/mssql-fts.Dockerfile",
        host_port=11433,
        container_port=1433,
        env={"ACCEPT_EULA": "Y", "MSSQL_SA_PASSWORD": "TestPass123!",
             "MSSQL_PID": "Express"},
        url="mssql://sa:TestPass123!@127.0.0.1:11433/nexusql_test?TrustServerCertificate=yes",
        env_var="TEST_MSSQL_URL",
    ),
}


def runtime() -> str:
    chosen = os.environ.get("NEXUSQL_CONTAINER_RUNTIME")
    candidates = [chosen] if chosen else ["podman", "docker"]
    for name in candidates:
        if name and shutil.which(name):
            return name
    sys.exit("no container runtime found: install podman or docker, or set "
             "NEXUSQL_CONTAINER_RUNTIME")


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, check=check)


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def image_present(rt: str, image: str) -> bool:
    return run(rt, "image", "exists", image, check=False).returncode == 0


def ensure_image(rt: str, service: Service) -> None:
    """Build the service's image from its Dockerfile when it is not local."""
    if not service.dockerfile or image_present(rt, service.image):
        return
    print(f"{service.name}: building {service.image} from {service.dockerfile} "
          f"(a few minutes the first time)")
    subprocess.run([rt, "build", "-t", service.image, "-f",
                    os.path.join(REPO_ROOT, service.dockerfile), REPO_ROOT], check=True)


def container_state(rt: str, service: Service) -> str | None:
    """'running', 'exited', ... or None when no such container exists."""
    result = run(rt, "inspect", "--format", "{{.State.Status}}", service.container,
                 check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def port_open(port: int, timeout: float = 1.0) -> bool:
    with socket.socket() as sock:
        sock.settimeout(timeout)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def reachable(service: Service) -> tuple[bool, str]:
    """Try a real driver connection, since an open port is not a ready server."""
    try:
        if service.name == "postgres":
            import psycopg
            psycopg.connect(service.url, connect_timeout=5).close()
        elif service.name == "mysql":
            import pymysql
            pymysql.connect(host="127.0.0.1", port=service.host_port,
                            user=service.env["MYSQL_USER"],
                            password=service.env["MYSQL_PASSWORD"],
                            database=service.env["MYSQL_DATABASE"],
                            connect_timeout=5).close()
        elif service.name == "mssql":
            import pyodbc
            drivers = [d for d in pyodbc.drivers() if "SQL Server" in d]
            preferred = [d for d in drivers if "ODBC Driver" in d] or drivers
            if not preferred:
                return False, "no SQL Server ODBC driver installed"
            pyodbc.connect(
                f"DRIVER={{{preferred[0]}}};SERVER=127.0.0.1,{service.host_port};"
                f"DATABASE=master;UID=sa;PWD={service.env['MSSQL_SA_PASSWORD']};"
                "TrustServerCertificate=yes", timeout=5).close()
        return True, "ok"
    except ImportError as exc:
        return False, f"driver not installed ({exc.name})"
    except Exception as exc:
        return False, str(exc).splitlines()[0][:120]


def up(rt: str, names: list[str], wait: int) -> None:
    for name in names:
        service = SERVICES[name]
        state = container_state(rt, service)
        if state == "running":
            print(f"{service.container}: already running")
        elif state is not None:
            run(rt, "start", service.container)
            print(f"{service.container}: started")
        elif port_open(service.host_port):
            print(f"{name}: port {service.host_port} is already in use by something "
                  f"else; not starting a container. Point {service.env_var} at it.")
            continue
        else:
            ensure_image(rt, service)
            args = [rt, "run", "-d", "--name", service.container,
                    "-p", f"127.0.0.1:{service.host_port}:{service.container_port}"]
            for key, value in service.env.items():
                args += ["-e", f"{key}={value}"]
            args.append(service.image)
            args.extend(service.args)
            run(*args)
            print(f"{service.container}: created")

    deadline = time.monotonic() + wait
    pending = [SERVICES[n] for n in names if container_state(rt, SERVICES[n]) == "running"]
    while pending and time.monotonic() < deadline:
        still = []
        for service in pending:
            ok, detail = reachable(service)
            if ok:
                print(f"{service.name}: ready")
            else:
                still.append(service)
        pending = still
        if pending:
            time.sleep(3)
    for service in pending:
        ok, detail = reachable(service)
        print(f"{service.name}: NOT READY after {wait}s: {detail}")


def down(rt: str, names: list[str]) -> None:
    for name in names:
        service = SERVICES[name]
        if container_state(rt, service) is None:
            print(f"{service.container}: not present")
            continue
        run(rt, "rm", "-f", service.container)
        print(f"{service.container}: removed")


def status(rt: str, names: list[str]) -> None:
    for name in names:
        service = SERVICES[name]
        state = container_state(rt, service) or "absent"
        ok, detail = reachable(service) if port_open(service.host_port) else (False, "port closed")
        print(f"{name:9} container={state:8} port={service.host_port} "
              f"reachable={'yes' if ok else 'no'}  {detail if not ok else ''}".rstrip())


def env(names: list[str], powershell: bool) -> None:
    for name in names:
        service = SERVICES[name]
        if powershell:
            print(f'$env:{service.env_var} = "{service.url}"')
        else:
            print(f'export {service.env_var}="{service.url}"')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["up", "down", "status", "env"])
    parser.add_argument("services", nargs="*", choices=[*SERVICES, []],
                        help="subset of: postgres mysql mssql (default: all)")
    parser.add_argument("--wait", type=int, default=180,
                        help="seconds to wait for servers to accept connections")
    parser.add_argument("--powershell", action="store_true",
                        help="env: print PowerShell assignments instead of exports")
    args = parser.parse_args()
    names = args.services or list(SERVICES)

    if args.command == "env":
        env(names, args.powershell)
        return
    rt = runtime()
    if args.command == "up":
        up(rt, names, args.wait)
    elif args.command == "down":
        down(rt, names)
    else:
        status(rt, names)


if __name__ == "__main__":
    main()
