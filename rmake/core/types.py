#
# Copyright (c) 2010 rPath, Inc.
#
# This program is distributed under the terms of the Common Public License,
# version 1.0. A copy of this license should have been distributed with this
# source file in a file called LICENSE. If it is not present, the license
# is always available at http://www.rpath.com/permanent/licenses/CPL-1.0.
#
# This program is distributed in the hope that it will be useful, but
# without any warranty; without even the implied warranty of merchantability
# or fitness for a particular purpose. See the Common Public License for
# full details.
#

import copy
import cPickle
from rmake.lib import uuid
from rmake.lib.ninamori.types import namedtuple

from twisted.python import reflect

NAMESPACE_TASK = uuid.UUID('14dfcf54-40e4-11df-b434-33d2b616adec')

IMMUTABLE_TYPES = (int, long, basestring, uuid.UUID, tuple, frozenset)


def freezify(cls):
    """Returns a 'frozen' namedtuple type of the given SlotCompare subclass."""
    assert issubclass(cls, _SlotCompare)
    frozenName = 'Frozen' + cls.__name__

    # namedtuple constructs the base class.
    baseType = namedtuple(frozenName, cls.__slots__)

    # Subclass the namedtuple to add a thaw() mixin.
    frozenDict = {'__slots__': (), '_thawedType': cls}
    frozenType = type(frozenName, (baseType, _Thawable), frozenDict)

    # Stash forward and backward type references.
    cls._frozenType = frozenType
    frozenType._thawedType = cls

    return frozenType


class _SlotCompare(object):
    """Base class for types that can be easily compared using their slots.

    Types can also be freezified to make them freezable to a namedtuple form.
    """
    __slots__ = ()
    _frozenType = None

    def __eq__(self, other):
        if type(self) != type(other):
            return False
        for slot in self.__slots__:
            if getattr(self, slot) != getattr(other, slot):
                return False
        return True

    def __ne__(self, other):
        return not self.__eq__(other)

    def __copy__(self):
        cls = type(self)
        new = cls.__new__(cls)
        for name in self.__slots__:
            setattr(new, name, getattr(self, name))
        return new

    def __deepcopy__(self, memo):
        cls = type(self)
        new = cls.__new__(cls)
        for name in self.__slots__:
            setattr(new, name, copy.deepcopy(getattr(self, name), memo))
        return new

    def freeze(self):
        if not self._frozenType:
            raise TypeError("Object of type %s cannot be frozen" %
                    reflect.qual(type(self)))
        vals = {}
        for name in self.__slots__:
            value = getattr(self, name)
            if value is None or isinstance(value, IMMUTABLE_TYPES):
                vals[name] = value
            elif isinstance(value, _SlotCompare):
                vals[name] = value.freeze()
            else:
                raise TypeError("Can't freeze field %r of type %r as it is "
                        "not a type known to be immutable" % (name,
                            type(value).__name__))
        return self._frozenType(**vals)


class _Thawable(object):
    """Mixin class used by freezify to add thawing support to named tuples."""
    __slots__ = ()

    def thaw(self):
        ret = object.__new__(self._thawedType)
        for name, value in zip(self._fields, self):
            if isinstance(value, _Thawable):
                value = value.thaw()
            setattr(ret, name, value)
        return ret


class RmakeJob(_SlotCompare):
    __slots__ = ('job_uuid', 'job_type', 'owner', 'status', 'times', 'data')

    def __init__(self, job_uuid, job_type, owner, status=None, times=None,
            data=None):
        self.job_uuid = job_uuid
        self.job_type = job_type
        self.owner = owner
        self.status = status or JobStatus()
        self.times = times or JobTimes()
        self.data = data


FrozenRmakeJob = freezify(RmakeJob)


class RmakeTask(_SlotCompare):
    __slots__ = ('task_uuid', 'job_uuid', 'task_name', 'task_type',
            'task_data', 'node_assigned', 'status', 'times')

    def __init__(self, task_uuid, job_uuid, task_name, task_type,
            task_data=None, node_assigned=None, status=None, times=None):
        if not task_uuid:
            task_uuid = uuid.uuid5(NAMESPACE_TASK,
                    str(job_uuid) + str(task_name))
        self.task_uuid = task_uuid
        self.job_uuid = job_uuid
        self.task_name = task_name
        self.task_type = task_type
        self.task_data = task_data
        self.node_assigned = node_assigned
        self.status = status or JobStatus()
        self.times = times or JobTimes()


FrozenRmakeTask = freezify(RmakeTask)


class JobStatus(_SlotCompare):
    __slots__ = ('code', 'text', 'detail')

    def __init__(self, code=0, text='', detail=None):
        self.code = code
        self.text = text
        self.detail = detail

    @property
    def completed(self):
        return 200 <= self.code < 300

    @property
    def failed(self):
        return 400 <= self.code < 500

    @property
    def final(self):
        return self.completed or self.failed

    @classmethod
    def from_failure(cls, reason, text="Fatal error", code=400):
        text = "%s: %s: %s" % (text,
                reflect.qual(reason.type),
                reflect.safe_str(reason.value))
        return cls(code, text, reason.getTraceback())


FrozenJobStatus = freezify(JobStatus)


class JobTimes(_SlotCompare):
    __slots__ = ('started', 'updated', 'finished', 'expires_after', 'ticks')

    def __init__(self, started=None, updated=None, finished=None,
            expires_after=None, ticks=-1):
        self.started = started
        self.updated = updated
        self.finished = finished
        self.expires_after = expires_after
        self.ticks = ticks


FrozenJobTimes = freezify(JobTimes)


class TaskCapability(namedtuple('TaskCapability', 'taskType')):
    pass


class FrozenObject(namedtuple('FrozenObject', 'data')):
    """Encapsulated pickled object."""

    @classmethod
    def fromObject(cls, obj):
        return cls('pickle:' + cPickle.dumps(obj, 2))

    def thaw(self):
        idx = self.data.index(':')
        kind = self.data[:idx]
        if kind == 'pickle':
            return cPickle.loads(self.data[idx+1:])
        else:
            raise RuntimeError("Unrecognized serialization format %s" % kind)

    def asBuffer(self):
        return buffer(self.data)

    def __deepcopy__(self, memo=None):
        return self
    __copy__ = __deepcopy__
