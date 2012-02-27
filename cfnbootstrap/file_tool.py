#==============================================================================
# Copyright 2011 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Amazon Software License (the "License"). You may not use
# this file except in compliance with the License. A copy of the License is
# located at
#
#       http://aws.amazon.com/asl/
#
# or in the "license" file accompanying this file. This file is distributed on
# an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, express or
# implied. See the License for the specific language governing permissions
# and limitations under the License.
#==============================================================================
from __future__ import with_statement
import logging
from cfnbootstrap.construction_errors import ToolError
import os
import base64
import stat
import shutil
from cfnbootstrap import util, security
import urllib2
import gzip
import tempfile
from contextlib import contextmanager
try:
    import simplejson as json
except ImportError:
    import json

log = logging.getLogger("cfn.init")

class FileTool(object):
    """
    Writes files to disk

    """

    _compare_buffer = 8*1024

    @classmethod
    def is_same_file(cls, f1, f2):
        if os.name == "posix":
            return os.path.samefile(f1, f2)
        else:
            #Crude workaround for os.path.samefile only existing on Unix
            return os.path.normcase(os.path.abspath(f1)) == os.path.normcase(os.path.abspath(f2))

    @classmethod
    def compare_file_contents(cls, f1, f2):
        """
        Return true if f1 and f2 have the same content.
        """
        if os.path.getsize(f1) != os.path.getsize(f2):
            return False

        if cls.is_same_file(f1, f2):
            return True

        # Borrowed from filecmp
        with file(f1, 'rb') as fp1:
            with file(f2, 'rb') as fp2:
                bufsize = 8*1024
                while True:
                    b1 = fp1.read(bufsize)
                    b2 = fp2.read(bufsize)
                    if b1 != b2:
                        return False
                    if not b1:
                        return True

    def apply(self, action, auth_config):
        """
        Write a set of files to disk, returning a list of the files that have changed.

        Arguments:
        action -- a dict of pathname to attributes, such as owner, group, mode, content, and encoding
        auth_config -- an AuthenticationConfig object for managing authenticated downloads

        Exceptions:
        ToolError -- on expected failures
        """

        files_changed = []

        if not action.keys():
            log.debug("No files specified")
            return files_changed

        for (filename, attribs) in sorted(action.iteritems(), key=lambda pair: pair[0]):
            # The only difference between a file and a symlink is hidden in the mode
            file_is_link = "mode" in attribs and stat.S_ISLNK(int(attribs["mode"], 8))

            if file_is_link:
                if "content" not in attribs:
                    raise ToolError("Symbolic link specified without a destination")
                elif os.path.exists(filename) and FileTool.is_same_file(os.path.realpath(filename), attribs["content"]):
                    log.info("Symbolic link %s already exists", filename)
                    continue

            parent = os.path.dirname(filename)
            if not os.path.isdir(parent):
                if not os.path.exists(parent):
                    log.debug("Parent directory %s does not exist, creating", parent)
                    os.makedirs(parent)
                else:
                    raise ToolError("Parent directory %s exists and is a file" % parent)

            with self.backup(filename, files_changed):
                if file_is_link:
                    log.debug("%s is specified as a symbolic link to %s", filename, attribs['content'])
                    os.symlink(attribs["content"], filename)
                else:
                    with file(filename, 'wb') as f:
                        log.debug("Writing content to %s", filename)
                        self._write_file(f, attribs, auth_config)

                if "mode" in attribs:
                    log.debug("Setting mode for %s to %s", filename, attribs["mode"])
                    os.chmod(filename, stat.S_IMODE(int(attribs["mode"], 8)))
                else:
                    log.debug("No mode specified for %s", filename)

                security.set_owner_and_group(filename, attribs.get("owner"), attribs.get("group"))

        return files_changed

    @contextmanager
    def backup(self, filename, files_changed):
        backup_file = None
        backup_backup_file = None
        if os.path.exists(filename):
            log.debug("%s already exists", filename)
            backup_file = filename + '.bak'
            if os.path.exists(backup_file):
                backup_backup_file = backup_file + "2"
                self._backup_file(backup_file, backup_backup_file)
            self._backup_file(filename, backup_file)

        try:
            yield backup_file
        except Exception:
            if backup_file:
                try:
                    self._backup_file(backup_file, filename)
                    if backup_backup_file:
                        self._backup_file(backup_backup_file, backup_file)
                except ToolError, t:
                    log.warn("Error restoring %s from backup", filename)
            raise
        else:
            linkmode = backup_file and os.path.islink(backup_file) or os.path.islink(filename)
            # we assume any symbolic links changed because we short-circuit links to the same files early on
            if not backup_file or linkmode or not FileTool.compare_file_contents(backup_file, filename):
                files_changed.append(filename)
                if backup_backup_file:
                    os.remove(backup_backup_file)
            elif backup_file and backup_backup_file:
                try:
                    self._backup_file(backup_backup_file, backup_file)
                except ToolError, t:
                    log.warn("Error restoring backup file %s: %s", backup_file, str(t))

    def _backup_file(self, source, dest):
        try:
            log.debug("Moving %s to %s", source, dest)
            os.rename(source, dest)
        except OSError, e:
            log.error("Could not move %s to %s", source, dest)
            raise ToolError("Could not rename %s: %s" % (source, str(e)))

    def _write_file(self, dest, attribs, auth_config):
        content = attribs.get("content", "")
        if content:
            self._write_inline_content(dest, content, attribs.get("encoding", "plain") == "base64")
        else:
            source = attribs.get("source", "")
            if not source:
                raise ToolError("File specified without source or content")
            log.debug("Retrieving contents from %s", source)

            try:
                remote_contents = util.urlopen_withretry(urllib2.Request(source, headers={'Accept-Encoding' : 'gzip'}),
                                                            opener = auth_config.get_opener(attribs.get('authentication', None)))
            except IOError, e:
                raise ToolError(e.strerror)

            if remote_contents.info().get('Content-Encoding') == 'gzip':
                self._write_gzip_response(remote_contents, dest)
            else:
                shutil.copyfileobj(remote_contents, dest)


    def _write_inline_content(self, dest, content, is_base64):
        if not isinstance(content, basestring):
            log.debug('Content will be serialized as a JSON structure')
            json.dump(content, dest)
            return

        if is_base64:
            try:
                log.debug("Decoding base64 content")
                content = base64.b64decode(content.strip())
            except TypeError:
                raise ToolError("Malformed base64: %s" % content)

        dest.write(content)

    def _write_gzip_response(self, resp, dest):
        with tempfile.TemporaryFile() as tf:
            #We yank the file to disk before gzip decoding it, as
            #The gzip decoder expects to get a real file (it uses tell())
            #Whereas an HTTP response really can only be read from
            shutil.copyfileobj(resp, tf)
            tf.seek(0, 0)
            gz = gzip.GzipFile(fileobj=tf, mode='r')
            try:
                shutil.copyfileobj(gz, dest)
            finally:
                gz.close()
