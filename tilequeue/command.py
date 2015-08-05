from collections import namedtuple
from contextlib import closing
from itertools import chain
from multiprocessing.pool import ThreadPool
from tilequeue.cache import RedisCacheIndex
from tilequeue.config import make_config_from_argparse
from tilequeue.format import lookup_format_by_extension
from tilequeue.metro_extract import city_bounds
from tilequeue.metro_extract import parse_metro_extract
from tilequeue.query import DataFetcher
from tilequeue.queue import make_sqs_queue
from tilequeue.tile import coord_int_zoom_up
from tilequeue.tile import coord_marshall_int
from tilequeue.tile import coord_unmarshall_int
from tilequeue.tile import parse_expired_coord_string
from tilequeue.tile import seed_tiles
from tilequeue.tile import tile_generator_for_multiple_bounds
from tilequeue.tile import tile_generator_for_single_bounds
from tilequeue.tile import zoom_mask
from tilequeue.top_tiles import parse_top_tiles
from tilequeue.worker import DataFetch
from tilequeue.worker import ProcessAndFormatData
from tilequeue.worker import QueuePrint
from tilequeue.worker import S3Storage
from tilequeue.worker import SqsQueueReader
from tilequeue.worker import SqsQueueWriter
from TileStache import parseConfigfile
from urllib2 import urlopen
import argparse
import logging
import logging.config
import multiprocessing
import os
import Queue
import signal
import sys
import threading
import time


def create_command_parser(fn):
    def create_parser_fn(parser):
        parser.add_argument('--config', required=True,
                            help='The path to the tilequeue config file.')
        parser.set_defaults(func=fn)
        return parser
    return create_parser_fn


def create_coords_generator_from_tiles_file(fp, logger=None):
    for line in fp:
        line = line.strip()
        if not line:
            continue
        coord = parse_expired_coord_string(line)
        if coord is None:
            if logger is not None:
                logger.warning('Could not parse coordinate from line: ' % line)
            continue
        yield coord


def lookup_formats(format_extensions):
    formats = []
    for extension in format_extensions:
        format = lookup_format_by_extension(extension)
        assert format is not None, 'Unknown extension: %s' % extension
        formats.append(format)
    return formats


def uniquify_generator(generator):
    s = set(generator)
    for tile in s:
        yield tile


def make_queue(queue_type, queue_name, redis_client, cfg):
    if queue_type == 'sqs':
        return make_sqs_queue(queue_name, redis_client,
                              cfg.aws_access_key_id, cfg.aws_secret_access_key)
    elif queue_type == 'mem':
        from tilequeue.queue import MemoryQueue
        return MemoryQueue()
    elif queue_type == 'file':
        from tilequeue.queue import OutputFileQueue
        if os.path.exists(queue_name):
            assert os.path.isfile(queue_name), \
                'Could not create file queue. `./{}` is not a file!'.format(
                    queue_name)

        # The mode here is important: if `tilequeue seed` is being run, then
        # new tile coordinates will get appended to the queue file due to the
        # `a`. Otherwise, if it's something like `tilequeue process`,
        # coordinates will be read from the beginning of the file thanks to the
        # `+`.
        fp = open(queue_name, 'a+')
        return OutputFileQueue(fp)
    elif queue_type == 'stdout':
        # only support writing
        from tilequeue.queue import OutputFileQueue
        return OutputFileQueue(sys.stdout)
    elif queue_type == 'redis':
        from tilequeue.queue import make_redis_queue
        return make_redis_queue(redis_client, queue_name)
    else:
        raise ValueError('Unknown queue type: %s' % queue_type)


def make_redis_client(cfg):
    from redis import StrictRedis
    redis_client = StrictRedis(cfg.redis_host, cfg.redis_port, cfg.redis_db)
    return redis_client


def make_redis_cache_index(redis_client, cfg):
    redis_cache_index = RedisCacheIndex(redis_client, cfg.redis_cache_set_key)
    return redis_cache_index


def make_logger(cfg, logger_name):
    if getattr(cfg, 'logconfig') is not None:
        logging.config.fileConfig(cfg.logconfig)
    logger = logging.getLogger(logger_name)
    return logger


def make_seed_tile_generator(cfg):
    if cfg.seed_all_zoom_start is not None:
        assert cfg.seed_all_zoom_until is not None
        all_tiles = seed_tiles(cfg.seed_all_zoom_start,
                               cfg.seed_all_zoom_until)
    else:
        all_tiles = ()

    if cfg.seed_metro_extract_url:
        assert cfg.seed_metro_extract_zoom_start is not None
        assert cfg.seed_metro_extract_zoom_until is not None
        with closing(urlopen(cfg.seed_metro_extract_url)) as fp:
            # will raise a MetroExtractParseError on failure
            metro_extracts = parse_metro_extract(fp)

        city_filter = cfg.seed_metro_extract_cities
        if city_filter is not None:
            metro_extracts = [
                city for city in metro_extracts if city.city in city_filter]

        multiple_bounds = city_bounds(metro_extracts)
        metro_extract_tiles = tile_generator_for_multiple_bounds(
            multiple_bounds, cfg.seed_metro_extract_zoom_start,
            cfg.seed_metro_extract_zoom_until)
    else:
        metro_extract_tiles = ()

    if cfg.seed_top_tiles_url:
        assert cfg.seed_top_tiles_zoom_start is not None
        assert cfg.seed_top_tiles_zoom_until is not None
        with closing(urlopen(cfg.seed_top_tiles_url)) as fp:
            top_tiles = parse_top_tiles(
                fp, cfg.seed_top_tiles_zoom_start,
                cfg.seed_top_tiles_zoom_until)
    else:
        top_tiles = ()

    if cfg.seed_custom_bboxes:
        assert cfg.seed_custom_zoom_start is not None
        assert cfg.seed_custom_zoom_until is not None
        custom_tiles = tile_generator_for_multiple_bounds(
            cfg.seed_custom_bboxes, cfg.seed_custom_zoom_start,
            cfg.seed_custom_zoom_until)
    else:
        custom_tiles = ()

    combined_tiles = chain(
        all_tiles, metro_extract_tiles, top_tiles, custom_tiles)
    tile_generator = uniquify_generator(combined_tiles)

    return tile_generator


def tilequeue_drain(cfg, peripherals):
    queue = peripherals.queue
    logger = make_logger(cfg, 'drain')
    logger.info('Draining queue ...')
    n = queue.clear()
    logger.info('Draining queue ... done')
    logger.info('Removed %d messages' % n)


def explode_and_intersect(coord_ints, tiles_of_interest, until=0):
    next_coord_ints = coord_ints
    coord_ints_at_parent_zoom = set()
    while True:
        for coord_int in next_coord_ints:
            if coord_int in tiles_of_interest:
                yield coord_int
            zoom = zoom_mask & coord_int
            if zoom > until:
                parent_coord_int = coord_int_zoom_up(coord_int)
                coord_ints_at_parent_zoom.add(parent_coord_int)
        if not coord_ints_at_parent_zoom:
            return
        next_coord_ints = coord_ints_at_parent_zoom
        coord_ints_at_parent_zoom = set()


def coord_ints_from_paths(paths):
    coord_set = set()
    for path in paths:
        with open(path) as fp:
            coords = create_coords_generator_from_tiles_file(fp)
            for coord in coords:
                coord_int = coord_marshall_int(coord)
                coord_set.add(coord_int)
    return coord_set


def tilequeue_intersect(cfg, peripherals):
    logger = make_logger(cfg, 'intersect')
    logger.info("Intersecting expired tiles with tiles of interest")
    sqs_queue = peripherals.queue

    assert cfg.intersect_expired_tiles_location, \
        'Missing tiles expired-location configuration'
    assert os.path.isdir(cfg.intersect_expired_tiles_location), \
        'tiles expired-location is not a directory'

    file_names = os.listdir(cfg.intersect_expired_tiles_location)
    if not file_names:
        logger.info('No expired tiles found, terminating.')
        return
    file_names.sort()
    # cap the total number of files that we process in one shot
    # this will limit memory usage, as well as keep progress moving
    # along more consistently rather than bursts
    expired_tile_files_cap = 20
    file_names = file_names[:expired_tile_files_cap]
    expired_tile_paths = [os.path.join(cfg.intersect_expired_tiles_location, x)
                          for x in file_names]

    logger.info('Fetching tiles of interest ...')
    tiles_of_interest = peripherals.redis_cache_index.fetch_tiles_of_interest()
    logger.info('Fetching tiles of interest ... done')

    logger.info('Will process %d expired tile files.'
                % len(expired_tile_paths))

    lock = threading.Lock()
    totals = dict(enqueued=0, in_flight=0)
    thread_queue_buffer_size = 1000
    thread_queue = Queue.Queue(thread_queue_buffer_size)

    # each thread will enqueue coords to sqs
    def enqueue_coords():
        buf = []
        buf_size = 10
        done = False
        while not done:
            coord = thread_queue.get()
            if coord is None:
                done = True
            else:
                buf.append(coord)
            if len(buf) >= buf_size or (done and buf):
                n_queued, n_in_flight = sqs_queue.enqueue_batch(buf)
                with lock:
                    totals['enqueued'] += n_queued
                    totals['in_flight'] += n_in_flight
                del buf[:]

    # clamp number of threads between 5 and 20
    n_threads = max(min(len(expired_tile_paths), 20), 5)
    # start up threads
    threads = []
    for i in range(n_threads):
        thread = threading.Thread(target=enqueue_coords)
        thread.start()
        threads.append(thread)

    for expired_tile_path in expired_tile_paths:
        stat_result = os.stat(expired_tile_path)
        file_size = stat_result.st_size
        file_size_in_kilobytes = file_size / 1024
        logger.info('Processing %s. Size: %dK' %
                    (expired_tile_path, file_size_in_kilobytes))

    # This will store all coords from all paths as integers in a
    # set. A set is used because if the same tile has been expired in
    # more than one file, we only process it once
    all_coord_ints_set = coord_ints_from_paths(expired_tile_paths)
    logger.info('Unique expired tiles read to process: %d' %
                len(all_coord_ints_set))
    for coord_int in explode_and_intersect(
            all_coord_ints_set, tiles_of_interest,
            until=cfg.intersect_zoom_until):
        coord = coord_unmarshall_int(coord_int)
        thread_queue.put(coord)

    for thread in threads:
        # threads stop on None sentinel
        thread_queue.put(None)

    # wait for all threads to terminate
    for thread in threads:
        thread.join()

    # print results
    for expired_tile_path in expired_tile_paths:
        logger.info('Processing complete: %s' % expired_tile_path)
        os.remove(expired_tile_path)
        logger.info('Removed: %s' % expired_tile_path)

    logger.info('%d tiles enqueued. %d tiles in flight.' %
                (totals['enqueued'], totals['in_flight']))

    logger.info('Intersection complete.')


def parse_layer_data_layers(tilestache_config, layer_names):
    layers = tilestache_config.layers
    layer_data = []
    for layer_name in layer_names:
        assert layer_name in layers, \
            ('Layer not found in config: %s' % layer_name)
        layer = layers[layer_name]
        layer_datum = dict(
            name=layer_name,
            queries=layer.provider.queries,
            is_clipped=layer.provider.clip,
            geometry_types=layer.provider.geometry_types,
            simplify_until=layer.provider.simplify_until,
            suppress_simplification=layer.provider.suppress_simplification,
            transform_fn_names=layer.provider.transform_fn_names,
            sort_fn_name=layer.provider.sort_fn_name,
            simplify_before_intersect=layer.provider.simplify_before_intersect
        )
        layer_data.append(layer_datum)
    return layer_data


def parse_layer_data(tilestache_config):
    layers = tilestache_config.layers
    all_layer = layers.get('all')
    assert all_layer is not None, 'All layer is expected in tilestache config'
    layer_names = all_layer.provider.names
    layer_data = parse_layer_data_layers(tilestache_config, layer_names)
    return layer_data


def make_store(store_type, store_name, cfg):
    if store_type == 'directory':
        from tilequeue.store import make_tile_file_store
        return make_tile_file_store(store_name)

    elif store_type == 's3':
        from tilequeue.store import make_s3_store
        return make_s3_store(
            cfg.s3_bucket, cfg.aws_access_key_id, cfg.aws_secret_access_key,
            path=cfg.s3_path, reduced_redundancy=cfg.s3_reduced_redundancy)

    else:
        raise ValueError('Unrecognized store type: `{}`'.format(store_type))


def tilequeue_process(cfg, peripherals):
    logger = make_logger(cfg, 'process')
    logger.warn('tilequeue processing started')

    assert os.path.exists(cfg.tilestache_config), \
        'Invalid tilestache config path'

    formats = lookup_formats(cfg.output_formats)

    sqs_queue = peripherals.queue

    tilestache_config = parseConfigfile(cfg.tilestache_config)
    layer_data = parse_layer_data(tilestache_config)

    store = make_store(cfg.store_type, cfg.s3_bucket, cfg)

    assert cfg.postgresql_conn_info, 'Missing postgresql connection info'

    n_cpu = multiprocessing.cpu_count()
    sqs_messages_per_batch = 10
    n_simultaneous_query_sets = cfg.n_simultaneous_query_sets
    if not n_simultaneous_query_sets:
        # default to number of databases configured
        n_simultaneous_query_sets = len(cfg.postgresql_conn_info['dbnames'])
    assert n_simultaneous_query_sets > 0
    default_queue_buffer_size = 256
    n_layers = len(layer_data)
    n_formats = len(formats)
    n_simultaneous_s3_storage = cfg.n_simultaneous_s3_storage
    if not n_simultaneous_s3_storage:
        n_simultaneous_s3_storage = max(n_cpu / 2, 1)
    assert n_simultaneous_s3_storage > 0

    # thread pool used for queries and uploading to s3
    n_total_needed_query = n_layers * n_simultaneous_query_sets
    n_total_needed_s3 = n_formats * n_simultaneous_s3_storage
    n_total_needed = n_total_needed_query + n_total_needed_s3
    n_max_io_workers = 50
    n_io_workers = min(n_total_needed, n_max_io_workers)
    io_pool = ThreadPool(n_io_workers)

    feature_fetcher = DataFetcher(cfg.postgresql_conn_info, layer_data,
                                  io_pool, n_layers)

    # create all queues used to manage pipeline

    sqs_input_queue_buffer_size = sqs_messages_per_batch
    # holds coord messages from sqs
    sqs_input_queue = Queue.Queue(sqs_input_queue_buffer_size)

    # holds raw sql results - no filtering or processing done on them
    sql_data_fetch_queue = multiprocessing.Queue(default_queue_buffer_size)

    # holds data after it has been filtered and processed
    # this is where the cpu intensive part of the operation will happen
    # the results will be data that is formatted for each necessary format
    processor_queue = multiprocessing.Queue(default_queue_buffer_size)

    # holds data after it has been sent to s3
    s3_store_queue = Queue.Queue(default_queue_buffer_size)

    # create worker threads/processes
    thread_sqs_queue_reader_stop = threading.Event()
    sqs_queue_reader = SqsQueueReader(sqs_queue, sqs_input_queue, logger,
                                      thread_sqs_queue_reader_stop)

    data_fetch = DataFetch(
        feature_fetcher, sqs_input_queue, sql_data_fetch_queue, io_pool,
        peripherals.redis_cache_index, logger)

    data_processor = ProcessAndFormatData(formats, sql_data_fetch_queue,
                                          processor_queue, logger)

    s3_storage = S3Storage(processor_queue, s3_store_queue, io_pool,
                           store, logger)

    thread_sqs_writer_stop = threading.Event()
    sqs_queue_writer = SqsQueueWriter(sqs_queue, s3_store_queue, logger,
                                      thread_sqs_writer_stop)

    def create_and_start_thread(fn, *args):
        t = threading.Thread(target=fn, args=args)
        t.start()
        return t

    thread_sqs_queue_reader = create_and_start_thread(sqs_queue_reader)

    threads_data_fetch = []
    threads_data_fetch_stop = []
    for i in range(n_simultaneous_query_sets):
        thread_data_fetch_stop = threading.Event()
        thread_data_fetch = create_and_start_thread(data_fetch,
                                                    thread_data_fetch_stop)
        threads_data_fetch.append(thread_data_fetch)
        threads_data_fetch_stop.append(thread_data_fetch_stop)

    # create a data processor per cpu
    n_data_processors = n_cpu
    data_processors = []
    data_processors_stop = []
    for i in range(n_data_processors):
        data_processor_stop = multiprocessing.Event()
        process_data_processor = multiprocessing.Process(
            target=data_processor, args=(data_processor_stop,))
        process_data_processor.start()
        data_processors.append(process_data_processor)
        data_processors_stop.append(data_processor_stop)

    threads_s3_storage = []
    threads_s3_storage_stop = []
    for i in range(n_simultaneous_s3_storage):
        thread_s3_storage_stop = threading.Event()
        thread_s3_storage = create_and_start_thread(s3_storage,
                                                    thread_s3_storage_stop)
        threads_s3_storage.append(thread_s3_storage)
        threads_s3_storage_stop.append(thread_s3_storage_stop)

    thread_sqs_writer = create_and_start_thread(sqs_queue_writer)

    if cfg.log_queue_sizes:
        assert(cfg.log_queue_sizes_interval_seconds > 0)
        queue_data = (
            (sqs_input_queue, 'sqs'),
            (sql_data_fetch_queue, 'sql'),
            (processor_queue, 'proc'),
            (s3_store_queue, 's3'),
        )
        queue_printer_thread_stop = threading.Event()
        queue_printer = QueuePrint(
            cfg.log_queue_sizes_interval_seconds, queue_data, logger,
            queue_printer_thread_stop)
        queue_printer_thread = create_and_start_thread(queue_printer)
    else:
        queue_printer_thread = None
        queue_printer_thread_stop = None

    def stop_all_workers(signum, stack):
        logger.warn('tilequeue processing shutdown ...')

        logger.info('requesting all workers (threads and processes) stop ...')

        # each worker guards its read loop with an event object
        # ask all these to stop first

        thread_sqs_queue_reader_stop.set()
        for thread_data_fetch_stop in threads_data_fetch_stop:
            thread_data_fetch_stop.set()
        for data_processor_stop in data_processors_stop:
            data_processor_stop.set()
        for thread_s3_storage_stop in threads_s3_storage_stop:
            thread_s3_storage_stop.set()
        thread_sqs_writer_stop.set()

        if queue_printer_thread_stop:
            queue_printer_thread_stop.set()

        logger.info('requesting all workers (threads and processes) stop ... '
                    'done')

        # Once workers receive a stop event, they will keep reading
        # from their queues until they receive a sentinel value. This
        # is mandatory so that no messages will remain on queues when
        # asked to join. Otherwise, we never terminate.

        logger.info('joining all workers ...')

        logger.info('joining sqs queue reader ...')
        thread_sqs_queue_reader.join()
        logger.info('joining sqs queue reader ... done')
        logger.info('enqueueing sentinels for data fetchers ...')
        for i in range(len(threads_data_fetch)):
            sqs_input_queue.put(None)
        logger.info('enqueueing sentinels for data fetchers ... done')
        logger.info('joining data fetchers ...')
        for thread_data_fetch in threads_data_fetch:
            thread_data_fetch.join()
        logger.info('joining data fetchers ... done')
        logger.info('enqueueing sentinels for data processors ...')
        for i in range(len(data_processors)):
            sql_data_fetch_queue.put(None)
        logger.info('enqueueing sentinels for data processors ... done')
        logger.info('joining data processors ...')
        for data_processor in data_processors:
            data_processor.join()
        logger.info('joining data processors ... done')
        logger.info('enqueueing sentinels for s3 storage ...')
        for i in range(len(threads_s3_storage)):
            processor_queue.put(None)
        logger.info('enqueueing sentinels for s3 storage ... done')
        logger.info('joining s3 storage ...')
        for thread_s3_storage in threads_s3_storage:
            thread_s3_storage.join()
        logger.info('joining s3 storage ... done')
        logger.info('enqueueing sentinel for sqs queue writer ...')
        s3_store_queue.put(None)
        logger.info('enqueueing sentinel for sqs queue writer ... done')
        logger.info('joining sqs queue writer ...')
        thread_sqs_writer.join()
        logger.info('joining sqs queue writer ... done')
        if queue_printer_thread:
            logger.info('joining queue printer ...')
            queue_printer_thread.join()
            logger.info('joining queue printer ... done')

        logger.info('joining all workers ... done')

        logger.info('joining io pool ...')
        io_pool.close()
        io_pool.join()
        logger.info('joining io pool ... done')

        logger.info('joining multiprocess data fetch queue ...')
        sql_data_fetch_queue.close()
        sql_data_fetch_queue.join_thread()
        logger.info('joining multiprocess data fetch queue ... done')

        logger.info('joining multiprocess process queue ...')
        processor_queue.close()
        processor_queue.join_thread()
        logger.info('joining multiprocess process queue ... done')

        logger.warn('tilequeue processing shutdown ... done')
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop_all_workers)
    signal.signal(signal.SIGINT, stop_all_workers)
    signal.signal(signal.SIGQUIT, stop_all_workers)

    logger.warn('all tilequeue threads and processes started')

    # this is necessary for the main thread to receive signals
    # when joining on threads/processes, the signal is never received
    # http://www.luke.maurits.id.au/blog/post/threads-and-signals-in-python.html
    while True:
        time.sleep(1024)


def queue_generator(queue):
    while True:
        data = queue.get()
        if data is None:
            break
        yield data


def tilequeue_seed(cfg, peripherals):
    logger = make_logger(cfg, 'seed')
    logger.info('Seeding tiles ...')
    queue = peripherals.queue
    tile_generator = make_seed_tile_generator(cfg)

    # updating sqs and updating redis happen in background threads
    def sqs_enqueue(tile_gen):
        n_enqueued, n_in_flight = queue.enqueue_batch(tile_gen)

    def redis_add(tile_gen):
        peripherals.redis_cache_index.index_coords(tile_gen)

    queue_buf_size = 1000
    queue_sqs_coords = Queue.Queue(queue_buf_size)
    queue_redis_coords = Queue.Queue(queue_buf_size)

    # suppresses checking the in flight list while seeding
    queue.is_seeding = True

    # use multiple sqs threads
    n_sqs_threads = 3

    sqs_threads = [threading.Thread(target=sqs_enqueue,
                                    args=(queue_generator(queue_sqs_coords),))
                   for x in range(n_sqs_threads)]
    thread_redis = threading.Thread(
        target=redis_add, args=(queue_generator(queue_redis_coords),))

    logger.info('Sqs ... ')
    if cfg.seed_should_add_to_tiles_of_interest:
        logger.info('Tiles of interest ...')

    for thread_sqs in sqs_threads:
        thread_sqs.start()
    if cfg.seed_should_add_to_tiles_of_interest:
        thread_redis.start()

    n_tiles = 0
    for tile in tile_generator:
        n_tiles += 1
        queue_sqs_coords.put(tile)
        if cfg.seed_should_add_to_tiles_of_interest:
            queue_redis_coords.put(tile)

    # None is sentinel value
    for i in range(n_sqs_threads):
        queue_sqs_coords.put(None)
    if cfg.seed_should_add_to_tiles_of_interest:
        queue_redis_coords.put(None)

    if cfg.seed_should_add_to_tiles_of_interest:
        thread_redis.join()
        logger.info('Tiles of interest ... done')
    for thread_sqs in sqs_threads:
        thread_sqs.join()
    logger.info('Sqs ... done')
    logger.info('Seeding tiles ... done')
    logger.info('%d tiles enqueued' % n_tiles)


def tilequeue_enqueue_tiles_of_interest(cfg, peripherals):
    logger = make_logger(cfg, 'enqueue_tiles_of_interest')
    logger.info('Enqueueing tiles of interest')

    sqs_queue = peripherals.queue
    logger.info('Fetching tiles of interest ...')
    tiles_of_interest = peripherals.redis_cache_index.fetch_tiles_of_interest()
    logger.info('Fetching tiles of interest ... done')

    thread_queue_buffer_size = 5000
    thread_queue = Queue.Queue(thread_queue_buffer_size)
    n_threads = 50

    lock = threading.Lock()
    totals = dict(enqueued=0, in_flight=0)

    def enqueue_coords_thread():
        buf = []
        buf_size = 10
        done = False
        while not done:
            coord = thread_queue.get()
            if coord is None:
                done = True
            else:
                buf.append(coord)
            if len(buf) >= buf_size or (done and buf):
                n_queued, n_in_flight = sqs_queue.enqueue_batch(buf)
                with lock:
                    totals['enqueued'] += n_queued
                    totals['in_flight'] += n_in_flight
                del buf[:]

    logger.info('Starting %d enqueueing threads ...' % n_threads)
    threads = []
    for i in xrange(n_threads):
        thread = threading.Thread(target=enqueue_coords_thread)
        thread.start()
        threads.append(thread)
    logger.info('Starting %d enqueueing threads ... done' % n_threads)

    logger.info('Starting to enqueue coordinates - will process %d tiles'
                % len(tiles_of_interest))

    def log_totals():
        with lock:
            logger.info('%d processed - %d enqueued, %d in flight' % (
                totals['enqueued'] + totals['in_flight'],
                totals['enqueued'], totals['in_flight']))

    progress_queue = Queue.Queue()
    progress_interval_seconds = 120

    def log_progress_thread():
        while True:
            try:
                progress_queue.get(timeout=progress_interval_seconds)
            except:
                log_totals()
            else:
                break

    progress_thread = threading.Thread(target=log_progress_thread)
    progress_thread.start()

    for tile_of_interest_value in tiles_of_interest:
        coord = coord_unmarshall_int(tile_of_interest_value)
        # don't enqueue coords with zoom > 18
        if coord.zoom <= 18:
            thread_queue.put(coord)

    for i in xrange(n_threads):
        thread_queue.put(None)

    for thread in threads:
        thread.join()

    progress_queue.put(None)
    progress_thread.join()

    logger.info('All tiles of interest processed')
    log_totals()


def tilequeue_tile_sizes(cfg, peripherals):
    # find averages, counts, and medians for metro extract tiles
    assert cfg.metro_extract_url
    with closing(urlopen(cfg.metro_extract_url)) as fp:
        metro_extracts = parse_metro_extract(fp)

    # zooms to get sizes for, inclusive
    zoom_start = 11
    zoom_until = 15

    bucket_name = cfg.s3_bucket

    formats = lookup_formats(cfg.output_formats)

    work_buffer_size = 1000
    work = Queue.Queue(work_buffer_size)

    from boto import connect_s3
    from boto.s3.bucket import Bucket
    s3_conn = connect_s3(cfg.aws_access_key_id, cfg.aws_secret_access_key)
    bucket = Bucket(s3_conn, bucket_name)

    lock = threading.Lock()

    def new_total_count():
        return dict(
            sum=0,
            n=0,
            elts=[],
        )

    region_counts = {}
    city_counts = {}
    zoom_counts = {}
    format_counts = {}
    grand_total_count = new_total_count()

    def update_total_count(total_count, size):
        total_count['sum'] += size
        total_count['n'] += 1
        total_count['elts'].append(size)

    def add_size(metro, coord, format, size):
        with lock:
            region_count = region_counts.get(metro.region)
            if region_count is None:
                region_counts[metro.region] = region_count = new_total_count()
            update_total_count(region_count, size)

            city_count = city_counts.get(metro.city)
            if city_count is None:
                city_counts[metro.city] = city_count = new_total_count()
            update_total_count(city_count, size)

            zoom_count = zoom_counts.get(coord.zoom)
            if zoom_count is None:
                zoom_counts[coord.zoom] = zoom_count = new_total_count()
            update_total_count(zoom_count, size)

            format_count = format_counts.get(format.extension)
            if format_count is None:
                format_counts[format.extension] = format_count = \
                    new_total_count()
            update_total_count(format_count, size)

            update_total_count(grand_total_count, size)

    from tilequeue.tile import serialize_coord

    def process_work_data():
        while True:
            work_data = work.get()
            if work_data is None:
                break
            coord = work_data['coord']
            format = work_data['format']
            key_path = 'osm/all/%s.%s' % (
                serialize_coord(coord), format.extension)
            key = bucket.get_key(key_path)
            # this shouldn't practically happen
            if key is None:
                continue
            size = key.size
            add_size(work_data['metro'], coord, format, size)

    # start all threads
    n_threads = 50
    worker_threads = []
    for i in range(n_threads):
        worker_thread = threading.Thread(target=process_work_data)
        worker_thread.start()
        worker_threads.append(worker_thread)

    # enqueue all work
    for metro_extract in metro_extracts:
        metro_tiles = tile_generator_for_single_bounds(
            metro_extract.bounds, zoom_start, zoom_until)
        for tile in metro_tiles:
            for format in formats:
                work_data = dict(
                    metro=metro_extract,
                    coord=tile,
                    format=format,
                )
                work.put(work_data)

    # tell workers to stop
    for i in range(n_threads):
        work.put(None)
    for worker_thread in worker_threads:
        worker_thread.join()

    def calc_median(elts):
        if not elts:
            return -1
        elts.sort()
        n = len(elts)
        middle = n / 2
        if n % 2 == 0:
            return (float(elts[middle]) + float(elts[middle + 1])) / float(2)
        else:
            return elts[middle]

    def calc_avg(total, n):
        if n == 0:
            return -1
        return float(total) / float(n)

    def format_commas(x):
        return '{:,}'.format(x)

    def format_kilos(size_in_bytes):
        kilos = int(float(size_in_bytes) / float(1000))
        kilos_commas = format_commas(kilos)
        return '%sK' % kilos_commas

    # print results
    def print_count(label, total_count):
        median = calc_median(total_count['elts'])
        avg = calc_avg(total_count['sum'], total_count['n'])
        if label:
            label_str = '%s -> ' % label
        else:
            label_str = ''
        print '%scount: %s - avg: %s - median: %s' % (
            label_str, format_commas(total_count['n']),
            format_kilos(avg), format_kilos(median))

    print 'Regions'
    print '*' * 80
    region_counts = sorted(region_counts.iteritems())
    for region_name, region_count in region_counts:
        print_count(region_name, region_count)

    print '\n\n'
    print 'Cities'
    print '*' * 80
    city_counts = sorted(city_counts.iteritems())
    for city_name, city_count in city_counts:
        print_count(city_name, city_count)

    print '\n\n'
    print 'Zooms'
    print '*' * 80
    zoom_counts = sorted(zoom_counts.iteritems())
    for zoom, zoom_count in zoom_counts:
        print_count(zoom, zoom_count)

    print '\n\n'
    print 'Formats'
    print '*' * 80
    format_counts = sorted(format_counts.iteritems())
    for format_extension, format_count in format_counts:
        print_count(format_extension, format_count)

    print '\n\n'
    print 'Grand total'
    print '*' * 80
    print_count(None, grand_total_count)


class TileArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        sys.stderr.write('error: %s\n' % message)
        self.print_help()
        sys.exit(2)


def tilequeue_main(argv_args=None):
    if argv_args is None:
        argv_args = sys.argv[1:]

    parser = TileArgumentParser()
    subparsers = parser.add_subparsers()

    parser_config = (
        ('process', create_command_parser(tilequeue_process)),
        ('seed', create_command_parser(tilequeue_seed)),
        ('drain', create_command_parser(tilequeue_drain)),
        ('intersect', create_command_parser(tilequeue_intersect)),
        ('enqueue-tiles-of-interest',
         create_command_parser(tilequeue_enqueue_tiles_of_interest)),
        ('tile-size', create_command_parser(tilequeue_tile_sizes)),
    )
    for parser_name, parser_func in parser_config:
        subparser = subparsers.add_parser(parser_name)
        parser_func(subparser)

    args = parser.parse_args(argv_args)
    assert os.path.exists(args.config), \
        'Config file {} does not exist!'.format(args.config)
    cfg = make_config_from_argparse(args.config)
    redis_client = make_redis_client(cfg)
    Peripherals = namedtuple('Peripherals', 'redis_cache_index queue')
    peripherals = Peripherals(make_redis_cache_index(redis_client, cfg),
                              make_queue(cfg.queue_type, cfg.queue_name,
                                         redis_client, cfg))
    args.func(cfg, peripherals)
