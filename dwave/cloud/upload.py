# Copyright 2019 D-Wave Systems Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Multipart upload helpers."""

from __future__ import division

import io
import os
import math
import logging
import threading

from abc import abstractmethod
try:
    import collections.abc as abc
except ImportError:     # pragma: no cover
    import collections as abc

import six

__all__ = ['FileBuffer', 'ChunkedData']

logger = logging.getLogger(__name__)


class RandomAccessIOBaseBuffer(abc.Sized):
    """Abstract base class for random access file-like object buffers.

    Concrete subclasses must provide __len__ and __getitem__.
    """

    __slots__ = ()

    @abstractmethod
    def __getitem__(self, key):     # pragma: no cover
        raise KeyError


class FileBuffer(RandomAccessIOBaseBuffer):
    """Provide thread-safe memory buffer-like random access to a file-like
    object via an item getter interface.

    Args:
        fp (:class:`io.BufferedIOBase`/binary-file-like):
            A file-like object that supports seek and readinto operations.
            Thread-safety of these operations is not assumed.

        strict (bool, default=True):
            Require file-like object to be a :class:`io.BufferedIOBase`
            subclass.

    Note:
        :class:`FileBuffer` requires a file-like object to support buffered
        binary read access.

    Example:
        Access overlapping segments of a file from multiple threads::

            with open('/path/to/file', 'rb') as fp:   # binary mode, read access
                fb = FileBuffer(fp):

                # in thread 1:
                seg = fb[0:10]

                # in thread 2:
                seg = fb[5:15]

    """

    def __init__(self, fp, strict=True):
        if strict:
            valid = lambda f: (
                isinstance(f, io.BufferedIOBase) and f.seekable() and f.readable())
        else:
            valid = lambda f: all([hasattr(f, 'readinto'), hasattr(f, 'seek')])

        if not valid(fp):
            raise TypeError("expected file-like, seekable, readable object")

        # store file size, assuming it won't change
        self._size = fp.seek(0, os.SEEK_END)
        self._fp = fp

        # multiple threads will be accessing the underlying file 
        self._lock = threading.RLock()

    def __len__(self):
        return self._size

    def _getkey_to_range(self, key):
        """Resolve slice/int key to start-stop range bounds."""

        if isinstance(key, slice):
            start, stop, stride = key.indices(len(self))
            if stride != 1:
                raise NotImplementedError("stride of 1 required")
        else:
            try:
                start = int(key)
            except:
                raise TypeError("slice or integral key expected")

            # negative indices wrap around
            if start < 0:
                start %= len(self)

            stop = start + 1

        return start, stop

    def getinto(self, key, b):
        """Copy a slice of file's content into a pre-allocated bytes-like
        object b. For example, b might be a `bytearray`.

        Args:
            key (slice/int):
                Source data address coded as a `slice` object or int position.
            b (bytes-like):
                Target pre-allocated bytes-like object.

        Returns:
            int:
                The number of bytes read (0 for EOF).
        """

        start, stop = self._getkey_to_range(key)

        # empty slice
        if stop <= start:
            return 0

        # read source[start:stop] => target[0:stop-start]
        size = stop - start
        target = memoryview(b)[:size]

        # slice is an atomic "seek and read" operation
        with self._lock:
            self._fp.seek(start)
            return self._fp.readinto(target)

    def __getitem__(self, key):
        """Fetch a slice of file's content into a pre-allocated bytes-like
        object b.

        Returns:
            :class:`bytes`/:class:`str`
        """

        start, stop = self._getkey_to_range(key)
        size = stop - start
        if size <= 0:
            return bytes()

        b = bytearray(size)
        n = self.getinto(key, b)
        del b[n:]

        return bytes(b)


class ChunkedData(object):
    """Unifying and performant streaming file-like interface to (large problem)
    data chunks.

    Handles streaming (not yet), in-file and in-memory data. Provides access to
    chunk data.

    Args:
        data (bytes/str/binary-file-like):
            Encoded problem data, in-memory or in-file.

        chunk_size (int):
            Chunk size in bytes.

    """

    def __init__(self, data, chunk_size):
        self.data = data
        self.view = None
        self.chunk_size = int(chunk_size)

        if self.chunk_size <= 0:
            raise ValueError("positive integer required for chunk size")

        # convenience string handler
        if isinstance(data, six.string_types):
            data = data.encode('ascii')

        if isinstance(data, bytes):
            data = io.BytesIO(data)
            # use non-locking memory view over bytes if available
            try:
                self.view = data.getbuffer()
            except AttributeError:  # pragma: no cover
                # python 2: fallback to FileBuffer
                pass

        if self.view is None and isinstance(data, io.IOBase):
            # use locking file view if possible
            if not data.seekable():
                raise ValueError("seekable file-like data object expected")
            if not data.readable():
                raise ValueError("readable file-like data object expected")
            self.view = FileBuffer(data)

        # TODO: use stream view if possible

        if self.view is None:
            raise TypeError("bytes/str/IOBase-subclass data required")

    @property
    def num_chunks(self):
        """Total number of chunks."""

        total_size = len(self.view)
        return math.ceil(total_size / self.chunk_size)

    def __len__(self):
        return self.num_chunks

    def chunk(self, idx):
        """Return zero-indexed chunk data as a binary stream."""

        start = idx * self.chunk_size
        stop = start + self.chunk_size
        return io.BytesIO(self.view[start:stop])

    def __iter__(self):
        for idx in range(len(self)):
            yield self.chunk(idx)
