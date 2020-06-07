"""
Support for sharing and saving models and data structures.
"""

import sys
import os
import pathlib
import warnings
from abc import abstractmethod, ABC
from contextlib import contextmanager
import tempfile
import threading
import logging
import pickle
try:
    import multiprocessing.shared_memory as shm
except ImportError:
    shm = None

import binpickle

_log = logging.getLogger(__name__)

_store_state = threading.local()


def _save_mode():
    return getattr(_store_state, 'mode', 'save')


def _active_stores():
    if not hasattr(_store_state, 'active'):
        _store_state.active = []
    return _store_state.active


@contextmanager
def sharing_mode():
    """
    Context manager to tell models that pickling will be used for cross-process
    sharing, not model persistence.
    """
    old = _save_mode()
    _store_state.mode = 'share'
    try:
        yield
    finally:
        _store_state.mode = old


def in_share_context():
    """
    Query whether sharing mode is active.  If ``True``, we are currently in a
    :func:`sharing_mode` context, which means model pickling will be used for
    cross-process sharing.
    """
    return _save_mode() == 'share'


class PersistedModel(ABC):
    """
    A persisted model for inter-process model sharing.

    These objects can be pickled for transmission to a worker process.

    .. note::
        Subclasses need to override the pickling protocol to implement the
        proper pickling implementation.
    """

    @abstractmethod
    def get(self):
        """
        Get the persisted model, reconstructing it if necessary.
        """
        pass

    @abstractmethod
    def close(self):
        """
        Release the persisted model resources.  Should only be called in the
        parent process (will do nothing in a child process).
        """
        pass


def persist(model):
    """
    Persist a model for cross-process sharing.

    This will return a persiste dmodel that can be used to reconstruct the model
    in a worker process (using :func:`reconstruct`).

    This function automatically selects a model persistence strategy from the
    the following, in order:

    1. If `LK_TEMP_DIR` is set, use :mod:`binpickle` in shareable mode to save
       the object into the LensKit temporary directory.
    2. If :mod:`multiprocessing.shared_memory` is available, use :mod:`pickle`
       to save the model, placing the buffers into shared memory blocks.
    3. Otherwise, use :mod:`binpickle` in shareable mode to save the object
       into the system temporary directory.

    Args:
        model(obj): the model to persist.

    Returns:
        PersistedModel: The persisted object.
    """
    lk_tmp = os.environ.get('LK_TEMP_DIR', None)
    if lk_tmp is not None:
        return persist_binpickle(model, lk_tmp)
    elif shm is not None:
        return persist_shm(model)
    else:
        return persist_binpickle(model)


def persist_binpickle(model, dir=None):
    """
    Persist a model using binpickle.

    Args:
        model: The model to persist.
        dir: The temporary directory for persisting the model object.

    Returns:
        PersistedModel: The persisted object.
    """
    fd, path = tempfile.mkstemp(suffix='.bpk', prefix='lkpy-', dir=dir)
    os.close(fd)
    path = pathlib.Path(path)
    _log.info('persisting %s to %s', model, path)
    with binpickle.BinPickler.mappable(path) as bp, sharing_mode():
        bp.dump(model)
    return BPKPersisted(path)


class BPKPersisted(PersistedModel):
    def __init__(self, path):
        self.path = path
        self.is_owner = True
        self._bpk_file = None
        self._model = None

    def get(self):
        if self._bpk_file is None:
            self._bpk_file = binpickle.BinPickleFile(self.path, direct=True)
            self._model = self._bpk_file.load()
        return self._model

    def close(self, unlink=True):
        if self._bpk_file is not None:
            self._model = None
            try:
                self._bpk_file.close()
            except IOError as e:
                _log.warn('error closing %s: %s', self.path, e)
            self._bpk_file = None

        if self.is_owner and unlink:
            assert self._model is None
            if unlink:
                self.path.unlink()
            self.is_owner = False

    def __getstate__(self):
        d = dict(self.__dict__)
        d['is_owner'] = False
        return d

    def __del___(self):
        self.close(False)


def persist_shm(model, dir=None):
    """
    Persist a model using binpickle.

    Args:
        model: The model to persist.
        dir: The temporary directory for persisting the model object.

    Returns:
        PersistedModel: The persisted object.
    """
    if shm is None:
        raise ImportError('multiprocessing.shared_memory')

    buffers = []
    buf_keys = []

    def buf_cb(buf):
        ba = buf.raw()
        block = shm.SharedMemory(create=True, size=ba.nbytes)
        _log.debug('serializing %d bytes to %s', ba.nbytes, block.name)
        # blit the buffer into shared memory
        block.buf[:ba.nbytes] = ba
        buffers.append(block)
        buf_keys.append((block.name, ba.nbytes))

    with sharing_mode():
        data = pickle.dumps(model, protocol=5, buffer_callback=buf_cb)
        shm_bytes = sum(b.size for b in buffers)
        _log.info('serialized %s to %d pickle bytes with %d buffers of %d bytes',
                  model, len(data), len(buffers), shm_bytes)

    return SHMPersisted(data, buf_keys, buffers)


class SHMPersisted(PersistedModel):
    buffers = []
    _model = None

    def __init__(self, data, buf_specs, buffers):
        self.pickle_data = data
        self.buffer_specs = buf_specs
        self.buffers = buffers
        self.is_owner = True

    def get(self):
        if self._model is None:
            buffers = []
            shm_bufs = []
            for bn, bs in self.buffer_specs:
                # funny business with buffer sizes
                block = shm.SharedMemory(name=bn)
                _log.debug('%s: %d bytes (%d used)', block.name, bs, block.size)
                buffers.append(block.buf[:bs])
                shm_bufs.append(block)

            self.buffers = shm_bufs
            self._model = pickle.loads(self.pickle_data, buffers=buffers)

        return self._model

    def close(self, unlink=True):
        self._model = None
        if self.is_owner:
            for buf in self.buffers:
                buf.close()
                buf.unlink()
            del self.buffers
            self.is_owner = False

    def __getstate__(self):
        return {
            'pickle_data': self.pickle_data,
            'buffer_specs': self.buffer_specs,
            'is_owner': False
        }