from contextlib import closing, contextmanager
import os
import pathlib
import subprocess
from fixtures.log_helper import log
import time
import requests
import signal

from fixtures.zenith_fixtures import PgProtocol, PortDistributor, ZenithEnvBuilder, zenith_binpath, pg_distrib_dir


@contextmanager
def new_pageserver_helper(new_pageserver_dir: pathlib.Path,
                          pageserver_bin: pathlib.Path,
                          remote_storage_mock_path: pathlib.Path,
                          pg_port: int,
                          http_port: int):
    """
    cannot use ZenithPageserver yet bacause it depends on zenith cli
    which currently lacks support for multiple pageservers
    """
    cmd = [
        str(pageserver_bin),
        '--init',
        '--listen-pg',
        f'localhost:{pg_port}',
        '--listen-http',
        f'localhost:{http_port}',
        '--workdir',
        str(new_pageserver_dir),
        '--postgres-distrib',
        pg_distrib_dir,
    ]

    subprocess.check_output(cmd, text=True)

    new_pageserver_config_file_path = new_pageserver_dir / 'pageserver.toml'

    # add remote storage mock to pageserver config
    with new_pageserver_config_file_path.open('a') as f:
        f.write(f'\n[remote_storage]\nlocal_path = "{remote_storage_mock_path}"')

    # actually run new pageserver TODO kill it after the test
    cmd = [
        str(pageserver_bin),
        '--workdir',
        str(new_pageserver_dir),
        '--daemonize',
    ]
    log.info("starting new pageserver %s", cmd)
    out = subprocess.check_output(cmd, text=True)
    log.info("started new pageserver %s", out)
    yield
    log.info("stopping new pageserver")
    pid = int((new_pageserver_dir / 'pageserver.pid').read_text())
    os.kill(pid, signal.SIGQUIT)


def test_tenant_relocation(zenith_env_builder: ZenithEnvBuilder, port_distributor: PortDistributor):
    zenith_env_builder.num_safekeepers = 1

    zenith_env_builder.set_auto_start_pageserver(False)

    env = zenith_env_builder.init()

    # create folder for remote storage mock
    remote_storage_mock_path = env.repo_dir / 'remote_storage_mock'
    remote_storage_mock_path.mkdir()

    pageserver_config_file_path = env.repo_dir / 'pageserver.toml'

    # add remote storage mock to pageserver config
    with pageserver_config_file_path.open('a') as f:
        f.write(f'\n[remote_storage]\nlocal_path = "{remote_storage_mock_path}"')

    env.pageserver.start()

    tenant = env.create_tenant()
    log.info("tenant to relocate %s", tenant)

    time.sleep(10)

    env.zenith_cli(["branch", "test_tenant_relocation", "main", f"--tenantid={tenant}"])

    tenant_pg = env.postgres.create_start(
        "test_tenant_relocation",
        "main",  # None,  # branch name, None means same as node name
        tenant_id=tenant,
    )

    # insert some data
    with closing(tenant_pg.connect()) as conn:
        with conn.cursor() as cur:
            # save timeline for later gc call
            cur.execute("SHOW zenith.zenith_timeline")
            timeline = cur.fetchone()[0]
            log.info("timeline to relocate %s", timeline)

            # we rely upon autocommit after each statement
            # as waiting for acceptors happens there
            cur.execute("CREATE TABLE t(key int primary key, value text)")
            cur.execute("INSERT INTO t SELECT generate_series(1,1000), 'some payload'")
            cur.execute("SELECT sum(key) FROM t")
            assert cur.fetchone() == (500500, )

    # run checkpoint manually to be sure that data landed in remote storage
    with closing(env.pageserver.connect()) as psconn:
        with psconn.cursor() as pscur:
            pscur.execute(f"do_gc {tenant} {timeline}")

    log.info("wait for upload")  # TODO api to check if upload is done
    time.sleep(2)

    log.info("inititalizing new pageserver")
    # bootstrap second pageserver
    new_pageserver_dir = env.repo_dir / 'new_pageserver'
    new_pageserver_dir.mkdir()

    new_pageserver_pg_port = port_distributor.get_port()
    new_pageserver_http_port = port_distributor.get_port()
    log.info("new pageserver ports pg %s http %s", new_pageserver_pg_port, new_pageserver_http_port)
    pageserver_bin = pathlib.Path(zenith_binpath) / 'pageserver'

    with new_pageserver_helper(new_pageserver_dir,
                               pageserver_bin,
                               remote_storage_mock_path,
                               new_pageserver_pg_port,
                               new_pageserver_http_port):
        # call to attach timeline to new timeline TODO use pageserver http client
        # /v1/timeline/:tenant_id/:timeline_id
        r = requests.get(
            f'http://localhost:{new_pageserver_http_port}/v1/timeline/{tenant}/{timeline}/attach')
        log.info("attach timeline response %s", r.text)
        r.raise_for_status()

        time.sleep(5)  # let the new pageserver learn all the new things
        log.info("wait complete")
        # callmemaybe to start replication from safekeeper to the new pageserver
        with closing(PgProtocol(host='localhost',
                                port=new_pageserver_pg_port).connect()) as new_pageserver_pg:
            with new_pageserver_pg.cursor() as cur:
                # "callmemaybe {} {} host={} port={} options='-c ztimelineid={} ztenantid={}'"
                safekeeper_connstring = f"host=localhost port={env.safekeepers[0].port.pg} options='-c ztimelineid={timeline} ztenantid={tenant}'"
                cur.execute("callmemaybe {} {} {}".format(tenant, timeline, safekeeper_connstring))

        # TODO wait for pageserver to catch up
        r = requests.get(
            f'http://localhost:{new_pageserver_http_port}/v1/timeline/{tenant}/{timeline}')
        r.raise_for_status()
        assert r.json()['timeline_state'] == 'Ready'
        # start restart compute with changed pageserver
        tenant_pg.stop()
        tenant_pg_config_file_path = pathlib.Path(tenant_pg.config_file_path())
        tenant_pg_config_file_path.open('a').write(
            f"\nzenith.page_server_connstring = 'postgresql://no_user:@localhost:{new_pageserver_pg_port}'"
        )
        tenant_pg.start()

        # detach tenant from old pageserver before we check
        # that all the data is there to be sure that old pageserver
        # is no longer involved in there, and if it is, we will see the errors
        # TODO detach tenant from old pageserver
        env.pageserver.stop(immediate=True)

        with closing(tenant_pg.connect()) as conn:
            with conn.cursor() as cur:
                # check that data is still there
                cur.execute("SELECT sum(key) FROM t")
                assert cur.fetchone() == (500500, )
                # check that we can write new data
                cur.execute("INSERT INTO t SELECT generate_series(1001,2000), 'some payload'")
                cur.execute("SELECT sum(key) FROM t")
                assert cur.fetchone() == (2001000, )
