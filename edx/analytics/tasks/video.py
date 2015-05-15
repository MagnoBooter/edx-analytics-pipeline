
from collections import namedtuple
import datetime
import logging
import math
import xml.etree.cElementTree as ET
import urllib

import ciso8601
import luigi

from edx.analytics.tasks.mapreduce import MapReduceJobTask, MapReduceJobTaskMixin
from edx.analytics.tasks.pathutil import EventLogSelectionMixin, EventLogSelectionDownstreamMixin
from edx.analytics.tasks.url import get_target_from_url, ExternalURL
from edx.analytics.tasks.util import eventlog
from edx.analytics.tasks.util.hive import WarehouseMixin, HivePartition, HiveTableTask, HiveQueryToMysqlTask

log = logging.getLogger(__name__)


VIDEO_PLAYED = 'play_video'
VIDEO_PAUSED = 'pause_video'
VIDEO_EVENT_TYPES = frozenset([
    VIDEO_PLAYED,
    VIDEO_PAUSED,
])
VIDEO_SESSION_END_INDICATORS = frozenset([
    'seq_next',
    'seq_prev',
    'seq_goto',
    'page_close',
])
VIDEO_SESSION_UNKNOWN_DURATION = -1
VIDEO_SESSION_SECONDS_PER_SEGMENT = 5


VideoSession = namedtuple('VideoSession', [
    'start_timestamp', 'course_id', 'encoded_module_id', 'start_offset', 'video_duration'])


class UserVideoSessionTask(EventLogSelectionMixin, MapReduceJobTask):

    output_root = luigi.Parameter()

    def mapper(self, line):
        value = self.get_event_and_date_string(line)
        if value is None:
            return
        event, _date_string = value

        username = event.get('username')
        if username is None:
            log.error("encountered event with no username: %s", event)
            return

        if len(username) == 0:
            return

        event_type = event.get('event_type')
        if event_type is None:
            log.error("encountered event with no event_type: %s", event)
            return

        timestamp = eventlog.get_event_time_string(event)
        if timestamp is None:
            log.error("encountered event with bad timestamp: %s", event)
            return

        course_id = eventlog.get_course_id(event)

        encoded_module_id = None
        current_time = None
        youtube_id = None
        if event_type in VIDEO_EVENT_TYPES:
            event_data = eventlog.get_event_data(event)
            encoded_module_id = event_data.get('id')
            current_time = event_data.get('currentTime')
            code = event_data.get('code')
            if code in ('html5', 'mobile') or course_id is None:
                return
            if event_type == VIDEO_PLAYED:
                youtube_id = code
                if current_time is None:
                    log.warn('Play video without valid currentTime: {0}'.format(line))
                    return
            elif event_type == VIDEO_PAUSED:
                if current_time is None:
                    current_time = 0
        elif event_type in VIDEO_SESSION_END_INDICATORS:
            pass
        else:
            return

        if course_id is not None:
            course_id = course_id.encode('utf8')

        if encoded_module_id is not None:
            encoded_module_id = encoded_module_id.encode('utf8')

        if youtube_id is not None:
            youtube_id = youtube_id.encode('utf8')

        print "OUT: ", username.encode('utf8'), timestamp, event_type, course_id, encoded_module_id, current_time, youtube_id

        yield (username.encode('utf8'), (timestamp, event_type, course_id, encoded_module_id, current_time, youtube_id))

    def reducer(self, username, events):
        sorted_events = sorted(events)
        session = None
        video_durations = {}
        for event in sorted_events:
            timestamp, event_type, course_id, encoded_module_id, current_time, youtube_id = event
            parsed_timestamp = ciso8601.parse_datetime(timestamp)
            if current_time:
                current_time = float(current_time)

            def start_session():
                video_duration = VIDEO_SESSION_UNKNOWN_DURATION
                if youtube_id:
                    video_duration = video_durations.get(youtube_id)
                    if not video_duration:
                        video_duration = self.get_video_duration(youtube_id)
                        video_durations[youtube_id] = video_duration

                return VideoSession(
                    start_timestamp=parsed_timestamp,
                    course_id=course_id,
                    encoded_module_id=encoded_module_id,
                    start_offset=current_time,
                    video_duration=video_duration
                )

            def end_session(end_time):
                if end_time is None or session.start_offset is None:
                    log.error(
                        'Invalid session ending, {0}, {1}, {2}, {3}'.format(
                            username,
                            str(event),
                            session.start_offset,
                            end_time
                        )
                    )
                    return None

                if session.video_duration == VIDEO_SESSION_UNKNOWN_DURATION or end_time > session.video_duration:
                    return None

                if (end_time - session.start_offset) < 0.5:
                    return None
                else:

                    print "OUTT: ", username,session.course_id,session.encoded_module_id,session.video_duration,session.start_timestamp.isoformat(),session.start_offset,end_time,event_type

                    return (
                        username,
                        session.course_id,
                        session.encoded_module_id,
                        session.video_duration,
                        session.start_timestamp.isoformat(),
                        session.start_offset,
                        end_time,
                        event_type,
                    )

            if session:
                if event_type == VIDEO_PLAYED:
                    time_diff = parsed_timestamp - session.start_timestamp
                    if time_diff < datetime.timedelta(seconds=0.5):
                        # log.warn(
                        #     'Play video events detected %f seconds apart, second one is ignored.',
                        #     time_diff.total_seconds()
                        # )
                        continue
                    else:
                        record = end_session(current_time)
                        if record:
                            yield record

                    session = start_session()

                elif event_type == VIDEO_PAUSED:
                    record = end_session(current_time)
                    if record:
                        yield record
                    session = None

                elif event_type in VIDEO_SESSION_END_INDICATORS:
                    if session.start_offset is None:
                        log.error(
                            'Invalid session, {0}, {1}, {2}'.format(username, str(event), session.start_offset)
                        )
                    else:
                        session_length = (parsed_timestamp - session.start_timestamp).total_seconds()
                        session_end = session.start_offset + session_length
                        record = end_session(session_end)
                        if record:
                            yield record
                    session = None
            else:
                if event_type == VIDEO_PLAYED:
                    session = start_session()

    def output(self):
        return get_target_from_url(self.output_root)

    def get_video_duration(self, youtube_id):
        duration = VIDEO_SESSION_UNKNOWN_DURATION
        try:
            f = urllib.urlopen("http://gdata.youtube.com/feeds/api/videos/{0}".format(youtube_id))
            xml_string = f.read()
            tree = ET.fromstring(xml_string)
            for item in tree.iter('{http://gdata.youtube.com/schemas/2007}duration'):
                if 'seconds' in item.attrib:
                    duration = int(item.attrib['seconds'])
        except IOError:
            pass
        except ET.ParseError:
            pass
        finally:
            f.close()

        return duration


class VideoTableDownstreamMixin(WarehouseMixin, EventLogSelectionDownstreamMixin, MapReduceJobTaskMixin):
    pass


class UserVideoSessionTableTask(VideoTableDownstreamMixin, HiveTableTask):

    @property
    def table(self):
        return 'user_video_session'

    @property
    def columns(self):
        return [
            ('username', 'STRING'),
            ('course_id', 'STRING'),
            ('encoded_module_id', 'STRING'),
            ('start_timestamp', 'STRING'),
            ('start_offset', 'FLOAT'),
            ('end_offset', 'FLOAT'),
            ('end_reason', 'STRING'),
        ]

    @property
    def partition(self):
        return HivePartition('dt', self.interval.date_b.isoformat())  # pylint: disable=no-member

    def requires(self):
        return UserVideoSessionTask(
            mapreduce_engine=self.mapreduce_engine,
            n_reduce_tasks=self.n_reduce_tasks,
            source=self.source,
            interval=self.interval,
            pattern=self.pattern,
            output_root=self.partition_location,
        )

    def output(self):
        return self.requires().output()


class VideoUsageTask(EventLogSelectionDownstreamMixin, WarehouseMixin, MapReduceJobTask):

    output_root = luigi.Parameter()
    input_path = luigi.Parameter(default=None)

    def requires(self):
        if self.input_path:
            return ExternalURL(self.input_path)
        else:
            return UserVideoSessionTableTask(
                mapreduce_engine=self.mapreduce_engine,
                n_reduce_tasks=self.n_reduce_tasks,
                source=self.source,
                interval=self.interval,
                pattern=self.pattern,
                warehouse_path=self.warehouse_path
            )

    def mapper(self, line):
        username, course_id, encoded_module_id, video_duration, start_timestamp_str, start_offset, end_offset, reason = line.split('\t')
        yield ((course_id, encoded_module_id), (username, start_offset, end_offset, video_duration))

    def reducer(self, key, sessions):
        course_id, encoded_module_id = key
        pipeline_video_id = '{0}|{1}'.format(course_id, encoded_module_id)
        usage_map = {}

        sessions_by_duration = {}
        for session in sessions:
            username, start_offset, end_offset, video_duration = session
            sessions_by_duration.setdefault(video_duration, []).append((username, start_offset, end_offset))

        video_duration = 0
        max_count = -1
        for duration, indexed_sessions in sessions_by_duration.iteritems():
            if len(indexed_sessions) > max_count:
                video_duration = duration

        for session in sessions_by_duration[video_duration]:
            username, start_offset, end_offset = session
            first_second = int(math.floor(float(start_offset)))
            start_segment = self.snap_to_last_segment_boundary(first_second)
            last_second = int(math.ceil(float(end_offset)))
            last_segment = self.snap_to_last_segment_boundary(last_second)
            stats = usage_map.setdefault(start_segment, {})
            stats['starts'] = stats.get('starts', 0) + 1
            stats = usage_map.setdefault(last_segment, {})
            stats['stops'] = stats.get('stops', 0) + 1
            for segment in xrange(start_segment, last_second, VIDEO_SESSION_SECONDS_PER_SEGMENT):
                stats = usage_map.setdefault(segment, {})
                users = stats.setdefault('users', set())
                users.add(username)
                stats['views'] = stats.get('views', 0) + 1

        final_segment = self.snap_to_last_segment_boundary(float(video_duration))
        start_views = usage_map.get(0, {}).get('views', 0)
        end_views = usage_map.get(final_segment, {}).get('views', 0)
        partial_views = abs(start_views - end_views)
        for segment in sorted(usage_map.keys()):
            stats = usage_map[segment]
            yield (
                pipeline_video_id,
                course_id,
                encoded_module_id,
                video_duration,
                VIDEO_SESSION_SECONDS_PER_SEGMENT,
                start_views,
                end_views,
                partial_views,
                segment,
                len(stats.get('users', [])),
                stats.get('views', 0),
            )

    def snap_to_last_segment_boundary(self, second):
        return (second / VIDEO_SESSION_SECONDS_PER_SEGMENT) * VIDEO_SESSION_SECONDS_PER_SEGMENT

    def output(self):
        return get_target_from_url(self.output_root)


class VideoUsageTableTask(VideoTableDownstreamMixin, HiveTableTask):

    @property
    def table(self):
        return 'video_usage'

    @property
    def columns(self):
        return [
            ('pipeline_video_id', 'STRING'),
            ('course_id', 'STRING'),
            ('encoded_module_id', 'STRING'),
            ('duration', 'INT'),
            ('segment_length', 'INT'),
            ('start_views', 'INT'),
            ('end_views', 'INT'),
            ('partial_views', 'INT'),
            ('segment', 'INT'),
            ('num_users', 'INT'),
            ('num_views', 'INT'),
        ]

    @property
    def partition(self):
        return HivePartition('dt', self.interval.date_b.isoformat())  # pylint: disable=no-member

    def requires(self):
        return VideoUsageTask(
            mapreduce_engine=self.mapreduce_engine,
            n_reduce_tasks=self.n_reduce_tasks,
            source=self.source,
            interval=self.interval,
            pattern=self.pattern,
            warehouse_path=self.warehouse_path,
            output_root=self.partition_location
        )

    def output(self):
        return self.requires().output()


class InsertToMysqlVideoTimelineTask(VideoTableDownstreamMixin, HiveQueryToMysqlTask):

    @property
    def table(self):
        return "video_timeline"

    @property
    def query(self):
        return """
            SELECT
                pipeline_video_id,
                segment,
                num_users,
                num_views
            FROM video_usage
        """

    @property
    def columns(self):
        return [
            ('pipeline_video_id', 'VARCHAR(255)'),
            ('segment', 'INTEGER'),
            ('num_users', 'INTEGER'),
            ('num_views', 'INTEGER'),
        ]

    @property
    def required_table_tasks(self):
        return VideoUsageTableTask(
            mapreduce_engine=self.mapreduce_engine,
            n_reduce_tasks=self.n_reduce_tasks,
            source=self.source,
            interval=self.interval,
            pattern=self.pattern,
            warehouse_path=self.warehouse_path,
        )

    @property
    def indexes(self):
        return [
            ('pipeline_video_id',),
        ]

    @property
    def partition(self):
        return HivePartition('dt', self.interval.date_b.isoformat())  # pylint: disable=no-member


class InsertToMysqlVideoTask(VideoTableDownstreamMixin, HiveQueryToMysqlTask):

    @property
    def table(self):
        return "video"

    @property
    def query(self):
        return """
            SELECT DISTINCT
                pipeline_video_id,
                course_id,
                encoded_module_id,
                duration,
                segment_length,
                start_views,
                end_views,
                partial_views
            FROM video_usage
        """

    @property
    def columns(self):
        return [
            ('pipeline_video_id', 'VARCHAR(255)'),
            ('course_id', 'VARCHAR(255)'),
            ('encoded_module_id', 'VARCHAR(255)'),
            ('duration', 'INTEGER'),
            ('segment_length', 'INTEGER'),
            ('start_views', 'INTEGER'),
            ('end_views', 'INTEGER'),
            ('partial_views', 'INTEGER'),
        ]

    @property
    def required_table_tasks(self):
        return VideoUsageTableTask(
            mapreduce_engine=self.mapreduce_engine,
            n_reduce_tasks=self.n_reduce_tasks,
            source=self.source,
            interval=self.interval,
            pattern=self.pattern,
            warehouse_path=self.warehouse_path,
        )

    @property
    def indexes(self):
        return [
            ('course_id', 'encoded_module_id'),
        ]

    @property
    def partition(self):
        return HivePartition('dt', self.interval.date_b.isoformat())  # pylint: disable=no-member


class InsertToMysqlAllVideoTask(VideoTableDownstreamMixin, luigi.WrapperTask):

    def requires(self):
        kwargs = {
            'n_reduce_tasks': self.n_reduce_tasks,
            'source': self.source,
            'interval': self.interval,
            'pattern': self.pattern,
            'warehouse_path': self.warehouse_path,
        }
        yield (
            InsertToMysqlVideoTimelineTask(**kwargs),
            InsertToMysqlVideoTask(**kwargs),
        )
