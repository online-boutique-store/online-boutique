"""Test support: a real, disposable PostgreSQL cluster you can stop, start and replace.

Uses TEST_DATABASE_URL if set (outage/replacement tests then skip loudly because they need to
control the server). Otherwise starts a private cluster from the postgres binaries bundled in the
`pgserver` pip package, listening on a unix socket only. No Docker, no network, no root.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(HERE)]


def _bindir():
    import pgserver
    return os.path.join(os.path.dirname(pgserver.__file__), "pginstall", "bin")


class PgCluster:
    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="pgt")
        self.data = os.path.join(self.root, "data")
        self.sock = self.root
        self.bin = _bindir()
        subprocess.run([os.path.join(self.bin, "initdb"), "-D", self.data, "-U", "postgres", "--auth=trust", "-E", "UTF8"],
                       check=True, capture_output=True)
        self.start()
        self.create_db("accounts")

    def _pgctl(self, *args):
        # Output goes to a file: a captured pipe would be inherited by the postmaster and block forever.
        with open(os.path.join(self.root, "pg_ctl.log"), "ab") as log:
            subprocess.run([os.path.join(self.bin, "pg_ctl"), "-D", self.data, "-w", "-t", "30",
                            "-l", os.path.join(self.root, "server.log"),
                            "-o", f"-c listen_addresses='' -k {self.sock} -c fsync=off", *args],
                           check=True, stdin=subprocess.DEVNULL, stdout=log, stderr=log, timeout=60)

    def start(self):
        self._pgctl("start")

    def stop(self):
        self._pgctl("stop", "-m", "fast")

    def dsn(self, db="accounts"):
        return f"host={self.sock} dbname={db} user=postgres"

    def _admin(self, sql):
        import psycopg
        with psycopg.connect(self.dsn("postgres"), autocommit=True) as c:
            c.execute(sql)

    def create_db(self, name):
        self._admin(f'CREATE DATABASE "{name}"')

    def drop_db(self, name):
        self._admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')

    def replace_db(self, name):
        """Simulate a replaced/re-initialised database: same DSN, brand-new empty database."""
        self.drop_db(name)
        self.create_db(name)

    def query(self, sql, params=(), db="accounts"):
        import psycopg
        with psycopg.connect(self.dsn(db), autocommit=True) as c:
            cur = c.execute(sql, params)
            return cur.fetchall() if cur.description else None

    def close(self):
        try:
            self.stop()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.root, ignore_errors=True)


_cluster = {"c": None}


def cluster():
    if _cluster["c"] is None:
        if os.environ.get("TEST_DATABASE_URL"):
            raise RuntimeError("SKIP: TEST_DATABASE_URL set; cluster control tests need the bundled cluster")
        _cluster["c"] = PgCluster()
        import atexit
        atexit.register(_cluster["c"].close)
    return _cluster["c"]


def dsn():
    return os.environ.get("TEST_DATABASE_URL") or cluster().dsn()


class FakeRedis:
    """Minimal in-memory redis with optional failure injection."""

    def __init__(self):
        self.d, self.ttl, self.fail_writes, self.fail_all = {}, {}, False, False

    def _chk(self, write=False):
        if self.fail_all or (write and self.fail_writes):
            raise ConnectionError("redis down")

    def get(self, k):
        self._chk()
        return self.d.get(k)

    def set(self, k, v, nx=False, xx=False):
        self._chk(True)
        if (nx and k in self.d) or (xx and k not in self.d):
            return None
        self.d[k] = v
        return True

    def setex(self, k, ttl, v):
        self._chk(True)
        self.d[k], self.ttl[k] = v, ttl

    def delete(self, k):
        self._chk(True)
        self.d.pop(k, None)
        self.ttl.pop(k, None)


def wait_ready(repo, timeout=20):
    """Drive the schema manager the way the background migrator would, until ready."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if repo.schema.state == "ready":
            return True
        repo.schema.attempt()
        time.sleep(0.1)
    return repo.schema.state == "ready"


def new_repo(retry_debounce_s=0.0, ready=True):
    import accounts_db
    repo = accounts_db.PgAccountRepository(dsn=dsn(), retry_debounce_s=retry_debounce_s)
    if ready:
        assert wait_ready(repo), repo.schema.state
    return repo


def reset_data(repo):
    cluster().query("TRUNCATE customers, login_events RESTART IDENTITY CASCADE")


def new_store(repo=None, redis=None):
    import accounts
    repo = repo or new_repo()
    return accounts.AccountStore(redis or FakeRedis(), repo=repo)
