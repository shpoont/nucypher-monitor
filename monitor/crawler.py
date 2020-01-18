import os
import time
from datetime import datetime, timedelta

import requests
from flask import Flask, jsonify
from hendrix.deploy.base import HendrixDeploy
from influxdb import InfluxDBClient
from maya import MayaDT

from nucypher.blockchain.economics import TokenEconomicsFactory
from nucypher.blockchain.eth.agents import (
    ContractAgency,
    StakingEscrowAgent,
)
from nucypher.blockchain.eth.registry import InMemoryContractRegistry, BaseContractRegistry
from nucypher.blockchain.eth.token import StakeList, NU
from nucypher.blockchain.eth.utils import datetime_at_period
from nucypher.config.constants import DEFAULT_CONFIG_ROOT
from nucypher.config.storages import SQLiteForgetfulNodeStorage
from nucypher.network.nodes import FleetStateTracker
from nucypher.network.nodes import Learner
from twisted.internet import task, reactor
from twisted.logger import Logger


class CrawlerNodeStorage(SQLiteForgetfulNodeStorage):
    _name = 'crawler'

    DB_FILE_NAME = 'crawler-storage.sqlite'
    DEFAULT_DB_FILEPATH = os.path.join(DEFAULT_CONFIG_ROOT, DB_FILE_NAME)

    STATE_DB_NAME = 'fleet_state'
    STATE_DB_SCHEMA = [('nickname', 'text primary key'),
                       ('symbol', 'text'),
                       ('color_hex', 'text'),
                       ('color_name', 'text'),
                       ('updated', 'text')]

    TEACHER_DB_NAME = 'teacher'
    TEACHER_ID = 'current_teacher'
    TEACHER_DB_SCHEMA = [('id', 'text primary key'), ('checksum_address', 'text')]

    def __init__(self, storage_filepath: str = DEFAULT_DB_FILEPATH, *args, **kwargs):
        super().__init__(db_filepath=storage_filepath, federated_only=False, *args, **kwargs)

    def init_db_tables(self):
        with self.db_conn:

            # ensure table is empty
            for table in [self.STATE_DB_NAME, self.TEACHER_DB_NAME]:
                self.db_conn.execute(f"DROP TABLE IF EXISTS {table}")

            # create fresh new state table (same column names as FleetStateTracker.abridged_state_details)
            state_schema = ", ".join(f"{schema[0]} {schema[1]}" for schema in self.STATE_DB_SCHEMA)
            self.db_conn.execute(f"CREATE TABLE {self.STATE_DB_NAME} ({state_schema})")

            # create new teacher table
            teacher_schema = ", ".join(f"{schema[0]} {schema[1]}" for schema in self.TEACHER_DB_SCHEMA)
            self.db_conn.execute(f"CREATE TABLE {self.TEACHER_DB_NAME} ({teacher_schema})")
        super().init_db_tables()

    def clear(self, metadata: bool = True, certificates: bool = True) -> None:
        if metadata is True:
            with self.db_conn:
                # TODO Clear the states table here?
                for table in [self.STATE_DB_NAME, self.TEACHER_DB_NAME]:
                    self.db_conn.execute(f"DELETE FROM {table}")

        super().clear(metadata=metadata, certificates=certificates)

    def store_state_metadata(self, state: dict):
        # TODO Limit the size of this table - no reason to store really old state values
        # TODO: Convert updated timestamp format for supported sqlite3 sorting
        # state['updated'] = state['updated'].rfc3339()
        db_row = (state['nickname'],
                  state['symbol'],
                  state['color_hex'],
                  state['color_name'],
                  state['updated'])
        sql = f'REPLACE INTO {self.STATE_DB_NAME} VALUES(?,?,?,?,?)'
        with self.db_conn:
            self.db_conn.execute(sql, db_row)

    def store_current_teacher(self, teacher_checksum: str):
        sql = f'REPLACE INTO {self.TEACHER_DB_NAME} VALUES (?,?)'
        with self.db_conn:
            self.db_conn.execute(sql, (self.TEACHER_ID, teacher_checksum))


class Crawler(Learner):
    """
    Obtain Blockchain information for Monitor and output to a DB.
    """

    _SHORT_LEARNING_DELAY = .5
    _LONG_LEARNING_DELAY = 30
    _ROUNDS_WITHOUT_NODES_AFTER_WHICH_TO_SLOW_DOWN = 25

    LEARNING_TIMEOUT = 10
    DEFAULT_REFRESH_RATE = 60  # seconds

    # InfluxDB Line Protocol Format (note the spaces, commas):
    # +-----------+--------+-+---------+-+---------+
    # |measurement|,tag_set| |field_set| |timestamp|
    # +-----------+--------+-+---------+-+---------+
    NODE_MEASUREMENT = 'crawler_node_info'

    # TODO: Needs Cleanup
    # _NODE_FIELD_SET = ('staker_address={staker_address}',
    #                    'worker_address="{worker_address}"',
    #                    'start_date={start_date}',
    #                    'end_date={end_date}',
    #                    'stake={stake}',
    #                    'locked_stake={locked_stake}',
    #                    'current_period={current_period}i',
    #                    'last_confirmed_period={last_confirmed_period}i',
    #                    'work_orders={work_orders}i')
    # NODE_LINE_PROTOCOL = '{measurement},' + ','.join(_NODE_FIELD_SET) + ',{timestamp}'

    NODE_LINE_PROTOCOL = '{measurement},staker_address={staker_address} ' \
                                      'worker_address="{worker_address}",' \
                                      'start_date={start_date},' \
                                      'end_date={end_date},' \
                                      'stake={stake},' \
                                      'locked_stake={locked_stake},' \
                                      'current_period={current_period}i,' \
                                      'last_confirmed_period={last_confirmed_period}i ' \
                                  '{timestamp}'

    INFLUX_DB_NAME = 'network'
    INFLUX_RETENTION_POLICY_NAME = 'network_info_retention'

    # TODO: review defaults for retention policy
    RETENTION = '5w'  # Weeks
    REPLICATION = '1'

    def __init__(self,
                 influx_host: str,
                 influx_port: int,
                 registry: BaseContractRegistry = None,
                 node_storage_filepath: str = CrawlerNodeStorage.DEFAULT_DB_FILEPATH,
                 refresh_rate=DEFAULT_REFRESH_RATE,
                 restart_on_error=True,
                 *args, **kwargs):

        # Settings
        self.federated_only = False  # Nope - for compatibility with Leaner TODO # nucypher/466
        self.registry = registry or InMemoryContractRegistry.from_latest_publication()
        self._refresh_rate = refresh_rate
        self._restart_on_error = restart_on_error

        # TODO: Needs cleanup
        # Tracking
        node_storage = CrawlerNodeStorage(storage_filepath=node_storage_filepath)
        class MonitoringTracker(FleetStateTracker):
            def record_fleet_state(self, *args, **kwargs):
                new_state_or_none = super().record_fleet_state(*args, **kwargs)
                if new_state_or_none:
                    _, new_state = new_state_or_none
                    state = self.abridged_state_details(new_state)
                    node_storage.store_state_metadata(state)
        self.tracker_class = MonitoringTracker

        super().__init__(save_metadata=True, node_storage=node_storage, *args, **kwargs)
        self.log = Logger(self.__class__.__name__)
        self.log.info(f"Storing node metadata in DB: {node_storage.db_filepath}")
        self.log.info(f"Storing blockchain metadata in DB: {influx_host}:{influx_port}")

        # In-memory Metrics
        self._stats = {'status': 'initializing'}
        self._crawler_client = None

        # Initialize InfluxDB
        self._db_host = influx_host
        self._db_port = influx_port
        self._influx_client = None

        # Agency
        self.staking_agent = ContractAgency.get_agent(StakingEscrowAgent, registry=self.registry)

        # Crawler Tasks
        self._node_details_task = task.LoopingCall(self._learn_about_nodes)
        self._stats_collection_task = task.LoopingCall(self._collect_stats)

        # JSON Endpoint
        self._flask = None

    def _initialize_influx(self):
        try:
            db_list = self._influx_client.get_list_database()
        except requests.exceptions.ConnectionError:
            raise ConnectionError(f"No connection to InfluxDB at {self._db_host}:{self._db_port}")
        found_db = (list(filter(lambda db: db['name'] == self.INFLUX_DB_NAME, db_list)))
        if len(found_db) == 0:
            # db not previously created
            self.log.info(f'Database {self.INFLUX_DB_NAME} not found, creating it')
            self._influx_client.create_database(self.INFLUX_DB_NAME)
            self._influx_client.create_retention_policy(name=self.INFLUX_RETENTION_POLICY_NAME,
                                                        duration=self.RETENTION,
                                                        replication=self.REPLICATION,
                                                        database=self.INFLUX_DB_NAME,
                                                        default=True)
        else:
            self.log.info(f'Database {self.INFLUX_DB_NAME} already exists, no need to create it')

    def learn_from_teacher_node(self, *args, **kwargs):
        try:
            current_teacher = self.current_teacher_node(cycle=False)
        except self.NotEnoughTeachers as e:
            self.log.warn("Can't learn right now: {}".format(e.args[0]))
            return

        new_nodes = super().learn_from_teacher_node(*args, **kwargs)

        # update metadata of teacher - not just in memory but in the underlying storage system (db in this case)
        self.node_storage.store_node_metadata(current_teacher)
        self.node_storage.store_current_teacher(current_teacher.checksum_address)

        return new_nodes

    @property
    def stats(self) -> dict:
        return self._stats

    def _measure_future_locked_tokens(self, periods: int = 365):
        period_range = range(1, periods + 1)
        token_counter = dict()
        for day in period_range:
            tokens, stakers = self.staking_agent.get_all_active_stakers(periods=day)
            token_counter[day] = (float(NU.from_nunits(tokens).to_tokens()), len(stakers))
        return dict(token_counter)

    def _measure_staker_activity(self) -> dict:
        confirmed, pending, inactive = self.staking_agent.partition_stakers_by_activity()
        stakers = dict()
        stakers['active'] = len(confirmed)
        stakers['pending'] = len(pending)
        stakers['inactive'] = len(inactive)
        return stakers

    def _measure_time_remaining(self):
        # TODO: Measure against period time with datetime to period utils
        tomorrow = datetime.utcnow() + timedelta(days=1)
        midnight = datetime(year=tomorrow.year, month=tomorrow.month,
                            day=tomorrow.day, hour=0, minute=0, second=0, microsecond=0)
        seconds_remaining = MayaDT.from_datetime(midnight).slang_time()
        return seconds_remaining

    def make_flask_server(self):
        """JSON Endpoint"""
        flask = Flask('nucypher-monitor')
        self._flask = flask
        self._flask.config["JSONIFY_PRETTYPRINT_REGULAR"] = True

        @flask.route('/stats', methods=['GET'])
        def stats():
            response = jsonify(self._stats)
            return response

    def _collect_stats(self, threaded: bool = True) -> None:
        if threaded:
            return reactor.callInThread(self._collect_stats, threaded=False)

        self.log.info("Collecting Statistics...")

        #
        # Read
        #

        # Time
        block_time = self.staking_agent.blockchain.client.w3.eth.getBlock('latest').timestamp  # epoch
        current_period = self.staking_agent.get_current_period()
        time_remaining = self._measure_time_remaining()

        # Nodes
        teacher = self._crawler_client.get_current_teacher_checksum()
        states = self._crawler_client.get_previous_states_metadata()
        # known_nodes = self._crawler_client.get_known_nodes_metadata()
        activity = self._measure_staker_activity()
        # historical_stakers = self._influx_client.get_historical_num_stakers_over_range()

        # Stake
        # past_locked_tokens = self._influx_client.get_historical_locked_tokens_over_range()
        future_locked_tokens = self._measure_future_locked_tokens()
        global_locked_tokens = self.staking_agent.get_global_locked_tokens()

        #
        # Write
        #

        self._stats = {'blocktime': block_time,
                       'current_period': current_period,
                       'next_period': time_remaining,

                       'prev_states': states,
                       'current_teacher': teacher,
                       'known_nodes': len(self.known_nodes),
                       'activity': activity,
                       # 'node_details': known_nodes,
                       # 'historical_stakers': historical_stakers,

                       'global_locked_tokens': global_locked_tokens,
                       # 'past_locked_tokens': past_locked_tokens,
                       'future_locked_tokens': future_locked_tokens
                       }

    def _learn_about_nodes(self, threaded: bool = True):
        if threaded:
            return reactor.callInThread(self._learn_about_nodes, threaded=False)

        agent = self.staking_agent
        known_nodes = list(self.known_nodes)

        block_time = agent.blockchain.client.w3.eth.getBlock('latest').timestamp  # precision in seconds
        current_period = agent.get_current_period()

        log = f'Processing {len(known_nodes)} nodes at {MayaDT(epoch=block_time)} | Period {current_period}'
        self.log.info(log)

        data = list()
        for node in known_nodes:
            time.sleep(0.1)  # chill out and share the damn machine will ya

            staker_address = node.checksum_address
            worker = agent.get_worker_from_staker(staker_address)

            stake = agent.owned_tokens(staker_address)
            staked_nu_tokens = float(NU.from_nunits(stake).to_tokens())
            locked_nu_tokens = float(NU.from_nunits(agent.get_locked_tokens(staker_address=staker_address)).to_tokens())

            economics = TokenEconomicsFactory.get_economics(registry=self.registry)
            stakes = StakeList(checksum_address=staker_address, registry=self.registry)
            stakes.refresh()

            start_date = datetime_at_period(stakes.initial_period, seconds_per_period=economics.seconds_per_period)
            start_date = start_date.datetime().timestamp()
            end_date = datetime_at_period(stakes.terminal_period, seconds_per_period=economics.seconds_per_period)
            end_date = end_date.datetime().timestamp()

            last_confirmed_period = agent.get_last_active_period(staker_address)

            num_work_orders = 0 # len(node.work_orders())  # TODO: Only works for is_me with datastore attached

            # TODO: do we need to worry about how much information is in memory if number of nodes is
            #  large i.e. should I check for size of data and write within loop if too big
            data.append(self.NODE_LINE_PROTOCOL.format(
                measurement=self.NODE_MEASUREMENT,
                staker_address=staker_address,
                worker_address=worker,
                start_date=start_date,
                end_date=end_date,
                stake=staked_nu_tokens,
                locked_stake=locked_nu_tokens,
                current_period=current_period,
                last_confirmed_period=last_confirmed_period,
                timestamp=block_time,
                work_orders=num_work_orders
            ))

        if not self._influx_client.write_points(data,
                                                database=self.INFLUX_DB_NAME,
                                                time_precision='s',
                                                batch_size=10000,
                                                protocol='line'):
            # TODO: What do we do here - Event hook for alerting?
            self.log.warn(f'Unable to write to database {self.INFLUX_DB_NAME} at '
                          f'{MayaDT(epoch=block_time)} | Period {current_period}')

    def _handle_errors(self, *args, **kwargs):
        failure = args[0]
        cleaned_traceback = failure.getTraceback().replace('{', '').replace('}', '')
        if self._restart_on_error:
            self.log.warn(f'Unhandled error: {cleaned_traceback}. Attempting to restart crawler')
            if not self._node_details_task.running:
                self.start()
        else:
            self.log.critical(f'Unhandled error: {cleaned_traceback}')

    def start(self):
        """Start the crawler if not already running"""
        if not self.is_running:
            self.log.info('Starting Crawler...')
            if self._influx_client is None:
                self._influx_client = InfluxDBClient(host=self._db_host, port=self._db_port, database=self.INFLUX_DB_NAME)
                self._initialize_influx()

            if self._crawler_client is None:
                from monitor.db import CrawlerStorageClient
                self._crawler_client = CrawlerStorageClient()

                # TODO: Maybe?
                # from monitor.db import CrawlerInfluxClient
                # self.crawler_influx_client = CrawlerInfluxClient()

            # start tasks
            collection_deferred = self._stats_collection_task.start(interval=self._refresh_rate, now=False)
            node_learner_deferred = self._node_details_task.start(interval=self._refresh_rate, now=False)

            # hookup error callbacks
            node_learner_deferred.addErrback(self._handle_errors)
            collection_deferred.addErrback(self._handle_errors)

            # Start up
            self.start_learning_loop(now=False)
            self.make_flask_server()
            hx_deployer = HendrixDeploy(action="start", options={"wsgi": self._flask, "http_port": 9555})
            hx_deployer.run()  # <--- Blocking Call to Reactor

    def stop(self):
        """Stop the crawler if currently running"""
        if self.is_running:
            self.log.info('Stopping Monitor Crawler')

            # stop tasks
            self._node_details_task.stop()

            if self._influx_client is not None:
                self._influx_client.close()
                self._influx_client = None

            # TODO: should I delete the NodeStorage to close the sqlite db connection here?

    @property
    def is_running(self):
        """Returns True if currently running, False otherwise"""
        return self._node_details_task.running
