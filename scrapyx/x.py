import asyncio
import json
import logging
import os
import sys
import traceback

import coloredlogs
import redis
import scrapydo
from fastapi import FastAPI
from scrapy.commands import ScrapyCommand
from scrapy.utils.project import get_project_settings
from uvicorn import Config, Server

from . import routes, utils


class Command(ScrapyCommand):
    requires_project = True
    version = "3.0"

    def __init__(self):
        command_name = os.path.basename(__file__).split('.')[0]

        if len(sys.argv) < 2:
            return

        if sys.argv[1] is not command_name:
            return

        self.boot()

        if len(self.settings.get('SPIDER_MODULES', [])) < 1 or len(self.spiders) < 1:
            self.logger.critical(
                'there is no need to call me here, I cannot find any project/spider there!'
            )

            return

        for queue_name_suffix, workers_count in self.queue_workers_count.items():
            utils.threads(workers_count, self.consumer, queue_name_suffix)

        loop = asyncio.get_event_loop()

        self.server(loop)

    def boot(self):
        self.logger = logging.getLogger("scrapy-x")

        scrapydo.setup()

        coloredlogs.install(
            fmt="[%(levelname)s] | %(asctime)s |  %(message)s",
            logger=self.logger
        )

        self.settings = get_project_settings()

        self.debug = self.settings.getbool('X_DEBUG', False)

        self.queue_name = self.settings.get('X_QUEUE_NAME', 'SCRAPY_X_QUEUE')

        self.queue_workers_count = self.settings.getdict(
            'X_QUEUE_WORKERS_COUNT', {'default': os.cpu_count()}
        )

        self.server_workers_count = self.settings.getint(
            'X_SERVER_WORKERS_COUNT', os.cpu_count()
        )

        self.server_listen_port = self.settings.getint(
            'X_SERVER_LISTEN_PORT', 6800
        )

        self.server_listen_host = self.settings.get(
            'X_SERVER_LISTEN_HOST', '0.0.0.0'
        )

        self.enable_access_log = self.settings.getbool(
            'X_ENABLE_ACCESS_LOG', True
        )

        self.redis_config = {
            'host': self.settings.get('X_REDIS_HOST', 'localhost'),
            'port': self.settings.getint('X_REDIS_PORT', 6379),
            'db': self.settings.getint('X_REDIS_DB', 0),
            'password': self.settings.get('X_REDIS_PASSWORD', ''),
        }

        self.spiders = utils.discover_spiders(self.settings)

        self.redis_conn = redis.Redis(
            host=self.redis_config["host"],
            port=self.redis_config["port"],
            password=self.redis_config["password"],
            db=self.redis_config["db"]
        )

        self.queue_backlog_names = {}
        self.queue_finished_counter_names = {}
        self.queue_consumers_rpm_names = {}

        for queue_name_suffix in self.queue_workers_count.keys():
            backlog_full_name = self.queue_name + '.' + queue_name_suffix + '.BACKLOG'
            counter_full_name = self.queue_name + '.' + queue_name_suffix + '.C.FINISHED.'
            rpm_full_name = self.queue_name + '.' + queue_name_suffix + '.C.RPM.'

            self.queue_backlog_names[queue_name_suffix] = backlog_full_name
            self.queue_finished_counter_names[queue_name_suffix] = counter_full_name
            self.queue_consumers_rpm_names[queue_name_suffix] = rpm_full_name

    def consumer(self, queue_name_suffix):
        """
        start a single redis consumer worker
        """

        self.logger.info(
            "a new consumer (thread) has been started for queue {}".format(
                queue_name_suffix
            )
        )

        try:
            try:
                r = redis.Redis(
                    host=self.redis_config["host"],
                    port=self.redis_config["port"],
                    password=self.redis_config["password"],
                    db=self.redis_config["db"]
                )
            except Exception as e:
                self.logger.critical("[redis] {}".format(str(e)))
                os._exit(-1)

            while True:
                try:
                    _, payload = r.blpop(
                        self.queue_backlog_names[queue_name_suffix])
                except Exception as e:
                    self.logger.critical("queue error {}".format(str(e)))
                    os._exit(-1)

                try:
                    task = json.loads(payload)
                except Exception as e:
                    self.logger.critical(
                        "invalid task payload {}".format(str(e)))
                    continue

                self.logger.info(
                    "detected new job and started working on it ...")

                spider_name = task.get("spider", None)
                spider = self.spiders.get(spider_name, None)
                args = task.get("args", {})

                if not isinstance(args, dict):
                    self.logger.warning(
                        "invalid args object, replacing it with empty one {}".format(
                            args
                        )
                    )

                    args = {}

                if not spider:
                    self.logger.critical(
                        "unknwon spider {}".format(spider_name))
                    continue

                try:
                    utils.crawl(spider, self.settings, args)
                except Exception as e:
                    if str(e).strip():
                        self.logger.critical(
                            "exception from scrapy {}".format(str(e)))

                # increment the finish queue
                r.incr(
                    self.queue_finished_counter_names[queue_name_suffix],
                    amount=1
                )

                # increment our RPM stat (which expires after 60 seconds)
                if r.incr(self.queue_consumers_rpm_names[queue_name_suffix], amount=1) == 1:
                    r.expire(
                        self.queue_consumers_rpm_names[queue_name_suffix],
                        60
                    )

        except Exception as e:
            self.logger.critical(
                """ QueueWorkerExit({}) due to the following error ({}), and here is the details:
                ------------
                    {}
                ------------
                """.format(queue_name_suffix, str(e), traceback.format_exc())
            )
            os._exit(-1)

    def server(self, loop):
        """
            prepare the server and initialize it
            so it be ready to run inside a thread

            this code is inspired by the following link:
                - https://github.com/encode/uvicorn/issues/706#issuecomment-652220153
        """

        app = FastAPI()
        app.x = self

        app.include_router(routes.router)

        config = Config(
            app=app,
            loop=loop,
            workers=self.server_workers_count,
            port=self.server_listen_port,
            host=self.server_listen_host,
            access_log=self.enable_access_log,
            debug=self.debug,
        )

        server = Server(config)

        loop.run_until_complete(server.serve())

    def short_desc(self):
        return "start the x server and queue manager - v {}".format(self.version)

    def run(self, op_ts, args):
        """
        we don't want to be executed inside scrapy event loop because we have our own!
        """
        pass
