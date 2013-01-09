#
# Copyright (c) SAS Institute Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#


"""
Cache of chroots.
"""

import errno
import os
import subprocess
import tempfile

from conary.lib import sha1helper, util
sha1ToString = sha1helper.sha1ToString

class ChrootCacheInterface(object):
    """
    ChrootCacheInterface defines the standard interface for a chroot
    cache.  It should never be instantiated.
    """
    def store(self, chrootFingerprint, root):
        """
        Store the chroot currently located at C{root} in the
        filesystem using the given chroot fingerprint.

        @param chrootFingerprint: The fingerprint (a SHA1 sum) to use
        when storing the chroot
        @type chrootFingerprint: str of length 20
        @param root: The location of the chroot in the filesystem
        @type root: str
        @return: None
        """
        raise NotImplementedError

    def restore(self, chrootFingerprint, root):
        """
        Return the cached chroot with the given chroot fingerprint to
        the directory specified by C{root}

        @param chrootFingerprint: The fingerprint (a SHA1 sum) to use
        when restoring the chroot
        @type chrootFingerprint: str of length 20
        @param root: The location to restore the chroot in the filesystem
        @type root: str
        @return: None
        """
        raise NotImplementedError

    def remove(self, chrootFingerprint):
        """
        Delete a cached chroot archive.

        @param chrootFingerprint: The fingerprint (a SHA1 sum) to delete
        @type  chrootFingerprint: str of length 20
        """
        raise NotImplementedError

    def hasChroot(self, chrootFingerprint):
        """
        Check to see if the chroot cache contains an entry for the given
        chroot fingerprint

        @param chrootFingerprint: The fingerprint (a SHA1 sum) to check
        @type chrootFingerprint: str of length 20
        @return: bool
        """
        raise NotImplementedError


class LocalChrootCache(ChrootCacheInterface):
    """
    The LocalChrootCache class implements a chroot cache that uses the
    local file system to store tar archive of chroots.
    """
    def __init__(self, cacheDir):
        """
        Instanciate a LocalChrootCache object
        @param cacheDir: The base directory for the chroot cache files
        @type cacheDir: str
        """
        self.cacheDir = cacheDir

    def store(self, chrootFingerprint, root):
        path = self._fingerPrintToPath(chrootFingerprint)
        prefix = sha1ToString(chrootFingerprint) + '.'
        util.mkdirChain(self.cacheDir)
        fd, fn = tempfile.mkstemp('.tar.gz', prefix, self.cacheDir)
        os.close(fd)
        try:
            subprocess.call('tar cSpf - -C %s . | gzip -1 - > %s' %(root, fn),
                            shell=True)
            os.rename(fn, path)
        finally:
            util.removeIfExists(fn)

    def restore(self, chrootFingerprint, root):
        path = self._fingerPrintToPath(chrootFingerprint)
        subprocess.call('zcat %s | tar xSpf - -C %s' %(path, root),
                        shell=True)

    def remove(self, chrootFingerprint):
        path = self._fingerPrintToPath(chrootFingerprint)
        try:
            os.unlink(path)
        except OSError, err:
            if err.errno != errno.ENOENT:
                raise

    def hasChroot(self, chrootFingerprint):
        path = self._fingerPrintToPath(chrootFingerprint)
        return os.path.isfile(path)

    def _fingerPrintToPath(self, chrootFingerprint):
        tar = sha1ToString(chrootFingerprint) + '.tar.gz'
        return os.path.join(self.cacheDir, tar)
