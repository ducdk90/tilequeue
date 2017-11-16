class TileProcessingStatsHandler(object):

    def __init__(self, stats):
        self.stats = stats

    def processed_coord(self, coord_proc_data):
        with self.stats.pipeline() as pipe:
            pipe.timing('process.time.fetch', coord_proc_data.timing['fetch'])
            pipe.timing('process.time.process',
                        coord_proc_data.timing['process'])
            pipe.timing('process.time.upload', coord_proc_data.timing['s3'])
            pipe.timing('process.time.ack', coord_proc_data.timing['ack'])
            pipe.timing('process.time.queue', coord_proc_data.timing['queue'])

            for layer_name, features_size in coord_proc_data.size.items():
                metric_name = 'process.size.%s' % layer_name
                pipe.gauge(metric_name, features_size)

            pipe.incr('process.storage.stored',
                      coord_proc_data.store_info['stored'])
            pipe.incr('process.storage.skipped',
                      coord_proc_data.store_info['not_stored'])

    def processed_pyramid(self, parent_tile,
                          start_time, stop_time):
        duration = stop_time - start_time
        self.stats.timing('process.pyramid', duration)

    def fetch_error(self):
        self.stats.incr('process.errors.fetch', 1)

    def proc_error(self):
        self.stats.incr('process.errors.process', 1)


class RawrTileEnqueueStatsHandler(object):

    def __init__(self, stats):
        self.stats = stats

    def __call__(self, n_coords, n_payloads, n_msgs_sent):
        with self.stats.pipeline() as pipe:
            pipe.gauge('rawr.enqueue.coords', n_coords)
            pipe.gauge('rawr.enqueue.groups', n_payloads)
            pipe.gauge('rawr.enqueue.calls', n_msgs_sent)


class RawrTilePipelineStatsHandler(object):

    def __init__(self, stats):
        self.stats = stats

    def emit_time_dict(self, pipe, timing, prefix):
        for timing_label, value in timing.items():
            metric_name = '%s.%s' % (prefix, timing_label)
            if isinstance(value, dict):
                self.emit_time_dict(pipe, value, metric_name)
            else:
                pipe.timing(metric_name, value)

    def __call__(self, intersect_metrics, n_enqueued, n_inflight, timing):
        with self.stats.pipeline() as pipe:

            pipe.incr('rawr.process.tiles', 1)

            pipe.gauge('rawr.process.intersect.toi',
                       intersect_metrics['n_toi'])
            pipe.gauge('rawr.process.intersect.candidates',
                       intersect_metrics['total'])
            pipe.gauge('rawr.process.intersect.hits',
                       intersect_metrics['hits'])
            pipe.gauge('rawr.process.intersect.misses',
                       intersect_metrics['misses'])

            pipe.gauge('rawr.process.enqueued', n_enqueued)
            pipe.gauge('rawr.process.inflight', n_inflight)

            prefix = 'rawr.process.time'
            self.emit_time_dict(pipe, timing, prefix)
