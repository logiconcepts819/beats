"""
This is an implementation of packet-by-packet Generalized Processor Sharing, as
described in the following paper:

Abhay K. Parekh and Robert G. Gallager. 1993. A generalized processor sharing
approach to flow control in integrated services networks: the single-node case.
IEEE/ACM Trans. Netw. 1, 3 (June 1993), 344-357. DOI=10.1109/90.234856
http://dx.doi.org/10.1109/90.234856
"""

from collections import deque
from config import config
from ConfigParser import NoOptionError
from db import Session, Song, PlayHistory, Packet, Vote
from sets import Set
import song
from youtube import get_youtube_video_details, YouTubeVideo
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import FlushError
from sqlalchemy.sql.expression import not_, func
import threading
import time
import player

PLAYER_NAME = config.get('Player', 'player_name')
try:
    DONT_REPEAT_FOR = config.getfloat('Player', 'dont_repeat_for')
except NoOptionError:
    DONT_REPEAT_FOR = 0.0
try:
    MAX_DONT_REPEAT_FOR = config.getint('Player', 'max_dont_repeat_for')
except NoOptionError:
    MAX_DONT_REPEAT_FOR = None

SCHEDULER_INTERVAL_SEC = 0.25
"""Interval at which to run the scheduler loop"""


class Scheduler(object):
    virtual_time = 0.0
    discard_pile = deque([])
    discard_set = Set([])

    active_sessions = 0
    """Number of users with currently queued songs"""

    def __init__(self):
        self._initialize_virtual_time()
        self._update_active_sessions()
        self._update_finish_times()

    def trim_list(self, max_list_size):
        while len(self.discard_pile) > max_list_size:
            self.remove_song_from_discard_pile()

    def add_song_to_discard_pile(self, song):
        if not song in self.discard_set:
            self.discard_set.add(song)
            self.discard_pile.append(song)

    def remove_song_from_discard_pile(self):
        song = self.discard_pile.popleft()
        if song in self.discard_set:
            self.discard_set.remove(song)

    @staticmethod
    def compute_max_discard_pile_size(db_size):
        max_discard_size = int(DONT_REPEAT_FOR * db_size)
        if MAX_DONT_REPEAT_FOR is not None:
            max_discard_size = min(MAX_DONT_REPEAT_FOR, max_discard_size)
        return max_discard_size

    def update_discard_pile_with_song(self, session, song):
        if DONT_REPEAT_FOR != 0.0 and MAX_DONT_REPEAT_FOR != 0:
            count = session.query(func.count(Song.id)).scalar()
            max_discard_size = self.compute_max_discard_pile_size(count)
            if max_discard_size != 0:
                self.add_song_to_discard_pile(song)
                self.trim_list(max_discard_size)

    def get_random_song(self):
        # Algorithm based on http://stackoverflow.com/questions/5467174

        # If the condition below holds true, then we only need to fetch the
        # next song naively
        if DONT_REPEAT_FOR == 0.0 or MAX_DONT_REPEAT_FOR == 0:
            return song.random_songs(limit=1)['results']

        # Query the database for the list of songs and a list of at most one
        # random song that doesn't exist in the discard pile
        table = Song.__table__
        session = Session()
        db_filenames = session.query(table.c.path).all()
        random_song = session.query(Song).order_by(func.rand())
        if len(self.discard_pile) != 0:
            random_song = random_song.filter(
                not_(Song.path.in_(self.discard_pile)))
        random_song = random_song.limit(1).all()
        session.commit()

        # Clean up the discard pile
        max_discard_size = self.compute_max_discard_pile_size(
            len(db_filenames))
        if max_discard_size == 0:
            # Everything gets weeded out in the discard pile in this case
            self.discard_pile.clear()
        else:
            # Efficiently weed out the filenames in the discard pile that don't
            # exist in the database anymore
            filename_set = Set(db_filenames)
            num_filenames = len(self.discard_pile)
            for k in xrange(num_filenames):
                filename = self.discard_pile.pop()
                if (filename, ) in filename_set:
                    self.discard_pile.appendleft(filename)
            # Update the discard pile
            self.trim_list(max_discard_size)

        # Obtain random song and update the discard pile
        song = [s.dictify() for s in random_song]
        if len(song) == 1 and max_discard_size != 0:
            self.add_song_to_discard_pile(song[0]['path'])
            self.trim_list(max_discard_size)

        return song

    def vote_song(self, user, song_id=None, video_url=None):
        """Vote for a song"""
        session = Session()

        if video_url:
            packet = session.query(Packet).filter_by(
                video_url=video_url, player_name=PLAYER_NAME).first()
        elif song_id is not None:
            packet = session.query(Packet).filter_by(
                song_id=song_id, player_name=PLAYER_NAME).first()
        else:
            raise Exception('Must specify either song_id or video_url')

        if packet:  # Song is already queued; add a vote
            if user == packet.user:
                session.rollback()
                raise Exception('User %s has already voted for this song' %
                                user)
            try:
                packet.additional_votes.append(Vote(user=user))
                session.commit()
            except FlushError:
                session.rollback()
                raise Exception('User %s has already voted for this song' %
                                user)
            self._update_finish_times(packet.user)
        else:  # Song is not queued; queue it
            if video_url:
                if 'www.youtube.com' in video_url:
                    try:
                        video_details = get_youtube_video_details(video_url)
                        packet = Packet(video_url=video_url,
                                        video_title=video_details['title'],
                                        video_length=video_details['length'],
                                        user=user,
                                        arrival_time=self.virtual_time,
                                        player_name=PLAYER_NAME)
                        session.add(packet)
                        session.commit()
                    except Exception, e:
                        session.rollback()
                        raise e
                else:
                    session.rollback()
                    raise Exception('Unsupported website')
            else:
                try:
                    packet = Packet(song_id=song_id,
                                    user=user,
                                    arrival_time=self.virtual_time,
                                    player_name=PLAYER_NAME)
                    session.add(packet)
                    session.commit()
                except IntegrityError:
                    session.rollback()
                    raise Exception('Song with id %d does not exist' % song_id)
            self._update_finish_times(user)
            self._update_active_sessions()
        return self.get_queue()

    @staticmethod
    def num_songs_queued():
        """Returns the number of songs that are queued"""
        session = Session()
        num_songs = session.query(Packet).filter_by(
            player_name=PLAYER_NAME).count()
        session.commit()
        return num_songs

    @staticmethod
    def get_queue(user=None):
        """
        Returns the current ordering of songs

        If there is a song currently playing, puts it at the front of the list.
        If user is specified, returns whether or not the user has voted for
        each song.
        """
        session = Session()
        packets = (session.query(Packet).filter_by(player_name=PLAYER_NAME)
                   .order_by(Packet.finish_time).all())
        session.commit()

        queue = []
        for packet in packets:
            if packet.video_url:
                video = YouTubeVideo(packet)
                video_obj = video.dictify()
                video_obj['packet'] = {
                    'num_votes': packet.num_votes(),
                    'user': packet.user,
                    'has_voted': packet.has_voted(user),
                }
                queue.append(video_obj)
            else:
                song = session.query(Song).get(packet.song_id)
                song_obj = song.dictify()
                song_obj['packet'] = {
                    'num_votes': packet.num_votes(),
                    'user': packet.user,
                    'has_voted': packet.has_voted(user),
                }
                queue.append(song_obj)

        # Put now playing song at front of list
        if player.now_playing:
            for i, song in enumerate(queue):
                try:
                    if player.now_playing.id == song['id']:
                        return {'queue': [queue[i]] + queue[:i] + queue[i+1:]}
                except:
                    pass
                try:
                    if player.now_playing.url == song['url']:
                        return {'queue': [queue[i]] + queue[:i] + queue[i+1:]}
                except:
                    pass

        return {'queue': queue}

    def clear(self):
        session = Session()
        session.query(Packet).filter_by(player_name=PLAYER_NAME).delete()
        session.commit()
        player.stop()
        return self.get_queue()

    def remove_song(self, song_id, skip=False):
        """Removes the packet with the given id"""
        session = Session()
        packet = session.query(Packet).filter_by(
            song_id=song_id, player_name=PLAYER_NAME).first()
        if (isinstance(player.now_playing, Song) and
                player.now_playing.id == song_id):
            player.stop()
            if skip:
                self.virtual_time = packet.finish_time
        session.delete(packet)
        session.commit()
        self._update_active_sessions()
        return self.get_queue()

    def remove_video(self, url, skip=False):
        """Removes the packet with the given video_url"""
        session = Session()
        packet = session.query(Packet).filter_by(
            video_url=url, player_name=PLAYER_NAME).first()
        if (isinstance(player.now_playing, YouTubeVideo) and
                player.now_playing.url == url):
            player.stop()
            if skip:
                self.virtual_time = packet.finish_time
        session.delete(packet)
        session.commit()
        self._update_active_sessions()
        return self.get_queue()

    def play_next(self, skip=False):
        if self.empty():
            random_song = self.get_random_song()
            if len(random_song) == 1:
                self.vote_song('RANDOM', random_song[0]['id'])

        if not self.empty():
            if player.now_playing:
                if isinstance(player.now_playing, YouTubeVideo):
                    self.remove_video(player.now_playing.url, skip=skip)
                else:
                    self.remove_song(player.now_playing.id, skip=skip)
            session = Session()
            next_packet = (session.query(Packet)
                           .filter_by(player_name=PLAYER_NAME)
                           .order_by(Packet.finish_time).first())
            if next_packet:
                if next_packet.video_url:
                    video = YouTubeVideo(next_packet)
                    player.play_media(video)
                    session.commit()
                    return video.dictify()
                else:
                    next_song = session.query(Song).get(next_packet.song_id)
                    self.update_discard_pile_with_song(session, next_song.path)
                    player.play_media(next_song)
                    next_song.history.append(
                        PlayHistory(user=next_packet.user,
                                    player_name=PLAYER_NAME))
                    session.commit()
                    return next_song.dictify()

    def empty(self):
        """Returns true if there are no queued songs"""
        # If there are no queued songs, there are also no active sessions
        return self.active_sessions == 0

    @staticmethod
    def _update_finish_times(user=None):
        """
        Updates finish times for packets

        If a user is specified, only update given user's queue.
        """
        session = Session()

        if user:
            packets = (session.query(Packet)
                       .filter_by(user=user, player_name=PLAYER_NAME)
                       .order_by(Packet.arrival_time).all())
        else:
            packets = (session.query(Packet).filter_by(player_name=PLAYER_NAME)
                       .order_by(Packet.arrival_time).all())

        last_finish_time = {}
        for packet in packets:
            length = (packet.video_length or
                      session.query(Song).get(packet.song_id).length)
            user = packet.user

            if user in last_finish_time:
                last_finish = max(last_finish_time[user], packet.arrival_time)
                packet.finish_time = last_finish + length / packet.weight()
                last_finish_time[user] = packet.finish_time
            else:
                packet.finish_time = (
                    packet.arrival_time + length / packet.weight())
                last_finish_time[user] = packet.finish_time

        session.commit()

    def _update_active_sessions(self):
        """Updates the active_sessions member variable"""
        session = Session()
        self.active_sessions = session.query(Packet.user).filter_by(
            player_name=PLAYER_NAME).distinct().count()
        session.commit()

    def _initialize_virtual_time(self):
        """Initializes virtual time to the latest packet arrival time"""
        session = Session()
        last_arrived_packet = (session.query(Packet)
                               .filter_by(player_name=PLAYER_NAME)
                               .order_by(Packet.arrival_time.desc()).first())
        if last_arrived_packet:
            self.virtual_time = last_arrived_packet.arrival_time
        session.commit()

    def _increment_virtual_time(self):
        """Increments the virtual time"""
        if not self.empty():
            self.virtual_time += SCHEDULER_INTERVAL_SEC / self.active_sessions

    def _scheduler_thread(self):
        """Main scheduler loop"""
        while True:
            # print 'Virtual time: %.3f\tActive sessions: %d' % (
            #     self.virtual_time, self.active_sessions)
            if player.has_ended():
                self.play_next()
            self._increment_virtual_time()
            time.sleep(SCHEDULER_INTERVAL_SEC)

    def start(self):
        """Starts the scheduler"""
        thread = threading.Thread(target=self._scheduler_thread)
        thread.daemon = True
        thread.start()

if __name__ == '__main__':
    s = Scheduler()
    s.start()
    if s.empty():
        s.vote_song('klwang3', 1)
        s.vote_song('klwang3', 2)
        s.vote_song('bezault2', 3)
        s.vote_song('bezault2', 2)
    while True:
        if player.now_playing:
            print player.now_playing
        time.sleep(SCHEDULER_INTERVAL_SEC)
