#!/usr/bin/env python3.9
# -*- coding: utf-8 -*-

# if you're interested in development, my test server is
# usually up at 51.161.34.235. just switch the ip of any
# switcher to the one above, toggle it off and on again, and
# you should be connected. registration is done ingame with
# osu!'s built-in registration.
# certificate: https://akatsuki.pw/static/ca.crt

import utils.misc
utils.misc.install_excepthook()

import os
import sys
from pathlib import Path

import aiohttp
import cmyui
import datadog
import orjson # go zoom
import geoip2.database
from cmyui import Ansi
from cmyui import log

import bg_loops
from constants.privileges import Privileges
from objects import glob
from objects.achievement import Achievement
from objects.collections import PlayerList
from objects.collections import MatchList
from objects.collections import ChannelList
from objects.collections import ClanList
from objects.collections import MapPoolList
from objects.player import Player
from utils.updater import Updater

__all__ = ()

# current version of gulag
# NOTE: this is used internally for the updater, it may be
# worth reading through it's code before playing with it.
glob.version = cmyui.Version(3, 2, 9)

GEOLOC_DB_FILE = Path.cwd() / 'ext/GeoLite2-City.mmdb'

async def setup_collections() -> None:
    """Setup & cache many global collections (mostly from sql)."""
    glob.players = PlayerList() # online players
    glob.matches = MatchList() # active multiplayer matches

    glob.channels = await ChannelList.prepare() # active channels
    glob.clans = await ClanList.prepare() # active clans
    glob.pools = await MapPoolList.prepare() # active mappools

    # create our bot & append it to the global player list.
    bot_name = (await glob.db.fetch(
        'SELECT name FROM users '
        'WHERE id = 1', _dict=False
    ))[0]

    glob.bot = Player(
        id = 1, name = bot_name, priv = Privileges.Normal,
        login_time = float(0x7fffffff), # never auto-dc
        bot_client = True
    )
    glob.players.append(glob.bot)

    # global achievements (sorted by vn gamemodes)
    glob.achievements = {0: [], 1: [], 2: [], 3: []}
    async for row in glob.db.iterall('SELECT * FROM achievements'):
        # NOTE: achievement conditions are stored as
        # stringified python expressions in the database
        # to allow for easy custom achievements.
        condition = eval(f'lambda score: {row.pop("cond")}')
        achievement = Achievement(**row, cond=condition)

        # NOTE: achievements are grouped by modes internally.
        glob.achievements[row['mode']].append(achievement)

    # static api keys
    glob.api_keys = {
        row['api_key']: row['id']
        for row in await glob.db.fetchall(
            'SELECT id, api_key FROM users '
            'WHERE api_key IS NOT NULL'
        )
    }

async def before_serving() -> None:
    """Called before the server begins serving connections."""
    # retrieve a client session to use for http connections.
    glob.http = aiohttp.ClientSession(json_serialize=orjson.dumps)

    # retrieve a pool of connections to use for mysql interaction.
    glob.db = cmyui.AsyncSQLPool()
    await glob.db.connect(glob.config.mysql)

    # run the sql & submodule updater (uses http & db).
    updater = Updater(glob.version)
    await updater.run()
    await updater.log_startup()

    # open a connection to our local geoloc database,
    # if the database file is present.
    if GEOLOC_DB_FILE.exists():
        glob.geoloc_db = geoip2.database.Reader(str(GEOLOC_DB_FILE))
    else:
        glob.geoloc_db = None

    # cache many global collections/objects from sql,
    # such as channels, mappools, clans, bot, etc.
    await setup_collections()

    new_coros = []

    # create a task for each donor expiring in 30d.
    new_coros.extend(await bg_loops.donor_expiry())

    # setup a loop to kick inactive ghosted players.
    new_coros.append(bg_loops.disconnect_ghosts())

    # if the surveillance webhook has a value, run
    # automatic (still very primitive) detections on
    # replays deemed by the server's configurable values.
    if glob.config.webhooks['surveillance']:
        new_coros.append(bg_loops.replay_detections())

    # reroll the bot's random status every `interval` sec.
    new_coros.append(bg_loops.reroll_bot_status(interval=300))

    for coro in new_coros:
        glob.app.add_pending_task(coro)

async def after_serving() -> None:
    """Called after the server stops serving connections."""
    if hasattr(glob, 'http'):
        await glob.http.close()

    if hasattr(glob, 'db') and glob.db.pool is not None:
        await glob.db.close()

    if hasattr(glob, 'geoloc_db') and glob.geoloc_db is not None:
        glob.geoloc_db.close()

    if hasattr(glob, 'datadog') and glob.datadog is not None:
        glob.datadog.stop() # stop thread
        glob.datadog.flush() # flush any leftover

def detect_mysqld_running() -> None:
    """Detect whether theres a mysql server running locally."""
    for path in (
        '/var/run/mysqld/mysqld.pid',
        '/var/run/mariadb/mariadb.pid'
    ):
        if os.path.exists(path):
            # path found
            return True
    else:
        # not found, try pgrep
        return os.system('pgrep mysqld') == 0

if __name__ == '__main__':
    # attempt to start up gulag.
    if sys.platform != 'linux':
        log('gulag currently only supports linux', Ansi.LRED)
        if sys.platform == 'win32':
            log("you could also try wsl(2), i'd recommend ubuntu 18.04 "
                "(i use it to test gulag)", Ansi.LBLUE)
        sys.exit()

    if sys.version_info < (3, 9):
        sys.exit('gulag uses many modern python features, '
                 'and the minimum python version is 3.9.')

    # make sure nginx & mysqld are running.
    if (
        glob.config.mysql['host'] in ('localhost', '127.0.0.1') and
        not detect_mysqld_running()
    ):
        sys.exit('Please start your mysqld server.')

    if not os.path.exists('/var/run/nginx.pid'):
        sys.exit('Please start your nginx server.')

    # warn if gulag is running on root.
    if os.geteuid() == 0:
        log('It is not recommended to run gulag as root, '
            'especially in production..', Ansi.LYELLOW)

        if glob.config.advanced:
            log('The risk is even greater with features '
                'such as config.advanced enabled.', Ansi.LRED)

    # set cwd to /gulag.
    os.chdir(os.path.dirname(os.path.realpath(__file__)))

    # create /.data and its subdirectories.
    data_path = Path.cwd() / '.data'
    data_path.mkdir(exist_ok=True)

    for sub_dir in ('avatars', 'logs', 'osu', 'osr', 'ss'):
        subdir = data_path / sub_dir
        subdir.mkdir(exist_ok=True)

    achievements_path = data_path / 'assets/medals/client'
    if not achievements_path.exists():
        # create directory & download achievement images
        achievements_path.mkdir(parents=True)
        utils.misc.download_achievement_images(achievements_path)

    # make sure oppai-ng is built and ready.
    glob.oppai_built = (Path.cwd() / 'oppai-ng/oppai').exists()

    if not glob.oppai_built:
        log('No oppai-ng compiled binary found. PP for all '
            'std & taiko scores will be set to 0; instructions '
            'can be found in the README file.', Ansi.LRED)

    # create a server object, which serves as a map of domains.
    app = glob.app = cmyui.Server(
        name=f'gulag v{glob.version}',
        gzip=4, debug=glob.config.debug
    )

    # add our endpoint's domains to the server;
    # each may potentially hold many individual endpoints.
    from domains.cho import domain as cho_domain # c[e4-6]?.ppy.sh
    from domains.osu import domain as osu_domain # osu.ppy.sh
    from domains.ava import domain as ava_domain # a.ppy.sh
    from domains.map import domain as map_domain # b.ppy.sh
    app.add_domains({cho_domain, osu_domain,
                     ava_domain, map_domain})

    # enqueue tasks to run once the server
    # begins, and stops serving connections.
    # these make sure we set everything up
    # and take it down nice and graceful.
    app.before_serving = before_serving
    app.after_serving = after_serving

    # support for https://datadoghq.com
    if all(glob.config.datadog.values()):
        datadog.initialize(**glob.config.datadog)
        glob.datadog = datadog.ThreadStats()
        glob.datadog.start(flush_in_thread=True,
                           flush_interval=15)

        # wipe any previous stats from the page.
        glob.datadog.gauge('gulag.online_players', 0)
    else:
        glob.datadog = None

    # start up the server; this starts an event loop internally,
    # using uvloop if it's installed. it uses SIGUSR1 for restarts.
    # NOTE: eventually the event loop creation will likely be
    # moved into the gulag codebase for increased flexibility.
    app.run(glob.config.server_addr, handle_restart=True)
