# -*- Mode: Python; test-case-name: morituri.test.test_common_program -*-
# vi:si:et:sw=4:sts=4:ts=4

# Morituri - for those about to RIP

# Copyright (C) 2009 Thomas Vander Stichele

# This file is part of morituri.
# 
# morituri is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# morituri is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with morituri.  If not, see <http://www.gnu.org/licenses/>.

"""
Common functionality and class for all programs using morituri.
"""

import os

from morituri.common import common, log
from morituri.result import result
from morituri.program import cdrdao, cdparanoia

import gst

class MusicBrainzException(Exception):
    def __init__(self, exc):
        self.args = (exc, )
        self.exception = exc

class TrackMetadata(object):
    artist = None
    title = None

class DiscMetadata(object):
    """
    @param release: earliest release date, in YYYY-MM-DD
    @type  release: unicode
    """
    artist = None
    title = None
    various = False
    tracks = None
    release = None

    def __init__(self):
        self.tracks = []

def filterForPath(text):
    return "-".join(text.split("/"))

def getMetadata(release):
    metadata = DiscMetadata()

    isSingleArtist = release.isSingleArtistRelease()
    metadata.various = not isSingleArtist
    metadata.title = release.title
    # getUniqueName gets disambiguating names like Muse (UK rock band)
    metadata.artist = release.artist.name
    metadata.release = release.getEarliestReleaseDate()

    for t in release.tracks:
        track = TrackMetadata()
        if isSingleArtist:
            track.artist = metadata.artist
            track.title = t.title
        else:
            track.artist = t.artist.name
            track.title = t.title
        metadata.tracks.append(track)

    return metadata


def musicbrainz(discid):
    #import musicbrainz2.disc as mbdisc
    import musicbrainz2.webservice as mbws


    # Setup a Query object.
    service = mbws.WebService()
    query = mbws.Query(service)


    # Query for all discs matching the given DiscID.
    # FIXME: let mbws.WebServiceError go through for now
    try:
        rfilter = mbws.ReleaseFilter(discId=discid)
        results = query.getReleases(rfilter)
    except mbws.WebServiceError, e:
        raise MusicBrainzException(e)

    # No disc matching this DiscID has been found.
    if len(results) == 0:
        return None

    # Display the returned results to the user.
    ret = []

    for result in results:
        release = result.release
        # The returned release object only contains title and artist, but no
        # tracks.  Query the web service once again to get all data we need.
        try:
            inc = mbws.ReleaseIncludes(artist=True, tracks=True,
                releaseEvents=True)
            release = query.getReleaseById(release.getId(), inc)
        except mbws.WebServiceError, e:
            raise MusicBrainzException(e)

        ret.append(getMetadata(release))

    return ret

def getPath(outdir, template, metadata, mbdiscid, i):
    """
    Based on the template, get a complete path for the given track,
    minus extension.
    Also works for the disc name, using disc variables for the template.

    @param outdir:   the directory where to write the files
    @type  outdir:   str
    @param template: the template for writing the file
    @type  template: str
    @param metadata:
    @type  metadata: L{DiscMetadata}
    @param i:        track number (0 for HTOA)
    @type  i:        int
    """
    # returns without extension

    v = {}

    v['t'] = '%02d' % i

    # default values
    v['A'] = 'Unknown Artist'
    v['d'] = mbdiscid

    v['a'] = v['A']
    v['n'] = 'Unknown Track %d' % i

    if metadata:
        v['A'] = filterForPath(metadata.artist)
        v['d'] = filterForPath(metadata.title)
        if i > 0:
            try:
                v['a'] = filterForPath(metadata.tracks[i - 1].artist)
                v['n'] = filterForPath(metadata.tracks[i - 1].title)
            except IndexError, e:
                print 'ERROR: no track %d found, %r' % (i, e)
                raise
        else:
            # htoa defaults to disc's artist
            v['a'] = filterForPath(metadata.artist)
            v['n'] = filterForPath('Hidden Track One Audio')

    import re
    template = re.sub(r'%(\w)', r'%(\1)s', template)

    return os.path.join(outdir, template % v)


class Program(object):
    """
    I maintain program state and functionality.
    """

    cuePath = None
    logPath = None

    def __init__(self):
        self.result = result.RipResult()

    def _getTableCachePath(self):
        path = os.path.join(os.path.expanduser('~'), '.morituri', 'cache',
            'table')
        return path

    def unmountDevice(self, device):
        """
        Unmount the given device if it is mounted, as happens with automounted
        data tracks.
        """
        proc = open('/proc/mounts').read()
        if device in proc:
            print 'Device %s is mounted, unmounting' % device
            os.system('umount %s' % device)
        
    def getTable(self, runner, cddbdiscid, device):
        """
        Retrieve the Table either from the cache or the drive.

        @rtype: L{table.Table}
        """
        path = self._getTableCachePath()

        pcache = common.PersistedCache(path)
        ptable = pcache.get(cddbdiscid)

        if not ptable.object:
            t = cdrdao.ReadTableTask(device=device)
            runner.run(t)
            ptable.persist(t.table)
        itable = ptable.object
        assert itable.hasTOC()

        self.result.table = itable

        return itable

    def getTagList(self, metadata, i):
        """
        Based on the metadata, get a gst.TagList for the given track.

        @param metadata:
        @type  metadata: L{DiscMetadata}
        @param i:        track number (0 for HTOA)
        @type  i:        int

        @rtype: L{gst.TagList}
        """
        artist = u'Unknown Artist'
        disc = u'Unknown Disc'
        title = u'Unknown Track'

        if metadata:
            artist = metadata.artist
            disc = metadata.title
            if i > 0:
                try:
                    artist = metadata.tracks[i - 1].artist
                    title = metadata.tracks[i - 1].title
                except IndexError, e:
                    print 'ERROR: no track %d found, %r' % (i, e)
                    raise
            else:
                # htoa defaults to disc's artist
                title = 'Hidden Track One Audio'

        ret = gst.TagList()

        # gst-python 0.10.15.1 does not handle unicode -> utf8 string conversion
        # see http://bugzilla.gnome.org/show_bug.cgi?id=584445
        ret[gst.TAG_ARTIST] = artist.encode('utf-8')
        ret[gst.TAG_TITLE] = title.encode('utf-8')
        ret[gst.TAG_ALBUM] = disc.encode('utf-8')

        # gst-python 0.10.15.1 does not handle tags that are UINT
        # see gst-python commit 26fa6dd184a8d6d103eaddf5f12bd7e5144413fb
        # FIXME: no way to compare against 'master' version after 0.10.15
        if gst.pygst_version >= (0, 10, 15):
            ret[gst.TAG_TRACK_NUMBER] = i
        if metadata:
            # works, but not sure we want this
            # if gst.pygst_version >= (0, 10, 15):
            #     ret[gst.TAG_TRACK_COUNT] = len(metadata.tracks)
            # hack to get a GstDate which we cannot instantiate directly in
            # 0.10.15.1
            # FIXME: The dates are strings and must have the format 'YYYY',
            # 'YYYY-MM' or 'YYYY-MM-DD'.
            # GstDate expects a full date, so default to Jan and 1st if MM and DD
            # are missing
            date = metadata.release
            if date:
                log.debug('metadata',
                    'Converting release date %r to structure', date)
                if len(date) == 4:
                    date += '-01'
                if len(date) == 7:
                    date += '-01'

                s = gst.structure_from_string('hi,date=(GstDate)%s' %
                    str(date))
                ret[gst.TAG_DATE] = s['date']
            
        # FIXME: gst.TAG_ISRC 

        return ret

    def getHTOA(self):
        """
        Check if we have hidden track one audio.

        @returns: tuple of (start, stop), or None
        """
        track = self.result.table.tracks[0]
        try:
            index = track.getIndex(0)
        except KeyError:
            return None

        start = index.absolute
        stop = track.getIndex(1).absolute
        return (start, stop)

    def ripTrack(self, runner, trackResult, path, number, offset, device, profile, taglist):
        """
        @param number: track number (1-based)
        """
        t = cdparanoia.ReadVerifyTrackTask(path, self.result.table,
            self.result.table.getTrackStart(number),
            self.result.table.getTrackEnd(number),
            offset=offset,
            device=device,
            profile=profile,
            taglist=taglist)
        t.description = 'Reading Track %d' % (number)

        runner.run(t)

        trackResult.testcrc = t.testchecksum
        trackResult.copycrc = t.copychecksum
        trackResult.peak = t.peak
        trackResult.quality = t.quality

    def writeCue(self, discName):
        assert self.result.table.canCue()

        cuePath = '%s.cue' % discName
        handle = open(cuePath, 'w')
        # FIXME: do we always want utf-8 ?
        handle.write(self.result.table.cue().encode('utf-8'))
        handle.close()

        self.cuePath = cuePath

        return cuePath

    def writeLog(self, discName, logger):
        logPath = '%s.log' % discName
        handle = open(logPath, 'w')
        handle.write(logger.log(self.result).encode('utf-8'))
        handle.close()

        self.logPath = logPath

        return logPath
