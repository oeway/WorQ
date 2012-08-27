# WorQ - asynchronous Python task queue.
#
# Copyright (c) 2012 Daniel Miller
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import logging
from collections import defaultdict
from cPickle import dumps, loads, HIGHEST_PROTOCOL
from uuid import uuid4
from weakref import ref as weakref

from worq.const import DEFAULT, HOUR, MINUTE, DAY, STATUS_VALUES, TASK_EXPIRED
from worq.task import (Queue, TaskSet, TaskSpace, FunctionTask, DeferredResult,
    TaskFailure, TaskExpired)

log = logging.getLogger(__name__)

class Broker(object):

    task_options = set([
        'result_status',
        'result_timeout',
        'heartrate',
        'taskset',
        'on_error',
        'size',
    ])

    def __init__(self, message_queue):
        self.messages = message_queue
        self.tasks = {_stop_task.name: _stop_task}
        self.name = message_queue.name

    @property
    def url(self):
        return self.messages.url

    def expose(self, obj):
        """Expose a TaskSpace or task callable.

        :param obj: A TaskSpace or task callable.
        """
        if isinstance(obj, TaskSpace):
            space = obj
        else:
            space = TaskSpace()
            space.task(obj)
        for name, func in space.tasks.iteritems():
            if name in self.tasks:
                raise ValueError('task %r conflicts with existing task' % name)
            self.tasks[name] = func

    def start_worker(self, max_wait=None):
        """Start a single worker

        TODO move to a utility class (this is a single-threaded worker pool).

        :param max_wait: Maximum number of seconds to wait for a task before
            stopping the worker. A value of None (the default) makes this a
            blocking call.
        """
        try:
            while True:
                task = self.next_task(timeout=max_wait)
                if task is None:
                    break
                task.invoke(self)
        except _StopWorker:
            log.info('worker stopped')

    def stop(self):
        """Stop a random worker.

        WARNING this is only meant for testing purposes. It will likely not do
        what you expect in an environment with more than one worker.
        """
        stop = FunctionTask(_stop_task.name, (), {}, {})
        stop.id = 'stop'
        self.enqueue(stop)

    def discard_pending_tasks(self):
        """Discard pending tasks from queue"""
        self.messages.discard_pending()

    def queue(self, target=''):
        return Queue(self, target)

    def enqueue(self, task):
        queue = self.name # TODO remove this
        options = task.options
        unknown_options = set(options) - self.task_options
        if unknown_options:
            raise ValueError('unrecognized task options: %s'
                % ', '.join(unknown_options))
        log.debug('enqueue %s [%s:%s]', task.name, queue, task.id)
        message = self.serialize(task)
        if options.get('result_status', False) or 'result_timeout' in options:
            result = DeferredResult(self, task.id, task.name, task.heartrate)
        else:
            result = None
        self.messages.enqueue_task(task.id, message, result)
        return result

    def set_status(self, task, value):
        """Set the status of a task"""
        if value not in STATUS_VALUES:
            value = self.serialize(value)
        self.messages.set_status(task.id, value)

    def status(self, result):
        """Get the status of a deferred result"""
        message = self.messages.get_status(result.id)
        if message is None:
            return message
        if message in STATUS_VALUES:
            return message
        return self.deserialize(message)

    def next_task(self, timeout=None):
        """Get the next task from the queue.

        :param timeout: See ``AbstractMessageQueue.get``.
        :returns: A task object. None on timeout expiration or if the task
            could not be deserialized.
        """
        message = self.messages.get(timeout=timeout)
        if message is None:
            return message
        task_id, message = message
        try:
            return self.deserialize(message)
        except Exception:
            log.error('cannot deserialize task [%s:%s]',
                self.name, task_id, exc_info=True)
            return None

    def invoke(self, task):
        """Invoke the given task (normally only called by a worker)"""
        task.invoke(self)

    def heartbeat(self, task):
        """Extend task result timeout"""
        self.messages.set_task_timeout(task.id, task.heartrate * 2 + 5)

    def serialize(self, obj):
        return dumps(obj, HIGHEST_PROTOCOL)

    def deserialize(self, message):
        return loads(message)

    def set_result(self, task, result):
        """Persist result object.

        :param task: Task object for which to set the result.
        :param result: Result object.
        """
        message = self.serialize(result)
        self.messages.set_result(task.id, message, task.result_timeout)

    def pop_result(self, task, timeout=0):
        """Pop and deserialize a task's result object

        :param task: An object with ``id`` and ``name`` attributes
            representing the task.
        :param timeout: Length of time to wait for the result. The default
            behavior is to return immediately (no wait). Wait indefinitely
            if None.
        :returns: The deserialized result object.
        :raises: KeyError if the result was not available.
        :raises: TaskExpired if the task expired before a result was returned.
            A task normally only expires if the pool loses its ability
            to communicate with the worker performing the task.
        """
        if timeout < 0:
            raise ValueError('negative timeout not supported')
        message = self.messages.pop_result(task.id, timeout)
        if message is None:
            raise KeyError(task.id)
        if message is TASK_EXPIRED:
            result = message
        else:
            result = self.deserialize(message)
        if result is TASK_EXPIRED:
            raise TaskExpired(task.name, self.name, task.id,
                'task expired before a result was returned')
        return result

    def task_failed(self, task):
        """Signal that the given task has failed."""
        self.messages.discard_result(task.id, self.serialize(TASK_EXPIRED))

    def init_taskset(self, taskset):
        """Initialize taskset result storage

        :returns: A DeferredResult object.
        """
        result = DeferredResult(
            self, taskset.id, taskset.name, taskset.heartrate)
        self.messages.init_taskset(taskset.id, result)
        return result


class AbstractMessageQueue(object):
    """Message queue abstract base class

    Task/result lifecycle
    1. Atomically store non-expiring result placeholder and enqueue task.
    2. Atomically pop task from queue and set timeout on result placeholder.
    3. Task heartbeats extend result expiration as needed.
    4. Task finishes and result value is saved.

    All methods must be thread-safe.

    :param url: URL used to identify the queue.
    :param name: Queue name.
    """

    def __init__(self, url, name=DEFAULT):
        self.url = url
        self.name = name

    def enqueue_task(self, task_id, message, result):
        """Enqueue task

        :param task_id: Task identifier.
        :param message: Serialized task message.
        :param result: A DeferredResult object for the task. None if the task
            options do not require result tracking.
        """
        raise NotImplementedError('abstract method')

    def get(self, timeout=None):
        """Atomically get a serialized task message from the queue

        Task processing has started when this method returns, which
        means that the task heartbeat must be maintained if there
        could be someone waiting on the result.

        :param timeout: Number of seconds to wait before returning None if no
            task is available in the queue. Wait forever if timeout is None
            (the default value).
        :returns: A serialized two-tuple (<task_id>, <message>) or None if
            timeout was reached before a task arrived.
        """
        raise NotImplementedError('abstract method')

    def discard_pending(self):
        """Discard pending tasks from queue"""
        raise NotImplementedError('abstract method')

    def set_task_timeout(self, task_id, timeout):
        """Set a timeout on the task result"""
        raise NotImplementedError('abstract method')

    def set_status(self, task_id, message):
        """Set the status of a task

        :param task_id: Unique task identifier string.
        :param message: A serialized task status value.
        """
        raise NotImplementedError('abstract method')

    def get_status(self, task_id):
        """Get the status of a task

        :param task_id: Unique task identifier string.
        :returns: A serialized task status object or None.
        """
        raise NotImplementedError('abstract method')

    def set_result(self, task_id, message, timeout):
        """Persist serialized result message.

        :param task_id: Unique task identifier string.
        :param message: Serialized result message.
        :param timeout: Number of seconds to persist the result before
            discarding it.
        """
        raise NotImplementedError('abstract method')

    def pop_result(self, task_id, timeout):
        """Pop serialized result message from persistent storage.

        :param task_id: Unique task identifier string.
        :param timeout: Length of time to wait for the result. Wait indefinitely
            if None. Return immediately if timeout is zero (0).
        :returns: The result message. None on timeout or
            ``worq.const.TASK_EXPIRED`` if the task expired before a result
            was available.
        """
        raise NotImplementedError('abstract method')

    def discard_result(self, task_id, task_expired_token):
        """Discard the result for the given task.

        A call to ``pop_result`` after this is invoked should return a
        task expired response.

        :param task_id: The task identifier.
        :param task_expired_token: A message that can be sent to blocking
            actors to signify that the task has expired.
        """
        raise NotImplementedError('abstract method')

    def init_taskset(self, taskset_id, result):
        """Initialize a taskset result storage

        :param taskset_id: (string) The taskset unique identifier.
        :param result: A DeferredResult object for the task.
        """
        raise NotImplementedError('abstract method')

    def update_taskset(self, taskset_id, num_tasks, message, timeout):
        """Update the result set for a task set, return all results if complete

        This operation is atomic, meaning that only one caller will ever be
        returned a value other than None for a given `taskset_id`.

        :param taskset_id: (string) The taskset unique identifier.
        :param num_tasks: (int) Number of tasks in the set.
        :param message: (string) A serialized result object to add to the
            set of results.
        :param timeout: (int) Discard results after this number of seconds.
        :returns: None if the number of updates has not reached num_tasks.
            Otherwise return an unordered list of serialized result messages.
        """
        raise NotImplementedError('abstract method')


class _StopWorker(BaseException): pass

def _stop_task():
    raise _StopWorker()
_stop_task.name = '<stop_task>'