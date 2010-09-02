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


import os
from rmake.core import types
from rmake.lib import ninamori
from rmake.lib.ninamori.types import SQL
from rmake.lib.uuid import UUID


def populateDatabase(dburl):
    db = ninamori.connect(dburl)
    timeline = os.path.join(os.path.dirname(__file__), 'schema')
    db.attach(timeline)


class CoreDB(object):

    def __init__(self, pool):
        self.pool = pool

    ## Jobs

    def getJobs(self, job_uuids):
        uuids = _castUUIDS(job_uuids)
        d = self.pool.runQuery("""
            SELECT job_uuid, job_type, owner, status_code, status_text,
                status_detail, time_started, time_updated, time_finished,
                expires_after, time_ticks, frozen_data
            FROM jobs.jobs WHERE job_uuid IN %s
            """, (tuple(uuids),))
        d.addCallback(_mergeThings, pkeys=job_uuids, func=_oneJob)
        return d

    def createJob(self, job, frozen_handler, callback=None):
        if callback is None:
            # It's faster not to use an interaction since the interaction has
            # to set up and tear down an explicit transaction.
            return self._createJob(self.pool.runQuery, job, frozen_handler)

        def interaction(cu, *args):
            d = self._createJob(cu.query, job, frozen_handler)

            def cb_do_callback(newJob):
                # Invoke the callback, but discard its result (unless it errors)
                dx = callback(newJob, cu)
                dx.addCallback(lambda _: newJob)
                return dx
            d.addCallback(cb_do_callback)

            return d
        return self.pool.runInteraction(interaction)

    def _createJob(self, do_query, job, frozen_handler):
        d = do_query("""
            INSERT INTO jobs.jobs ( job_uuid, job_type, owner,
                status_code, status_text, status_detail,
                expires_after,
                frozen_data, frozen_handler )
            VALUES ( %s, %s, %s, %s, %s, %s, %s, %s, %s )
            RETURNING jobs.jobs.*
            """, (job.job_uuid, job.job_type, job.owner,
                job.status.code, job.status.text, job.status.detail,
                job.times.expires_after,
                job.data, frozen_handler,
                ))
        d.addCallback(_grabOne, func=_oneJob)
        return d

    def updateJob(self, job, frozen_handler=None):
        stmt = SQL("""
            UPDATE jobs.jobs SET
                status_code = %s, status_text = %s, status_detail = %s,
                time_updated = now(), time_ticks = %s, frozen_data = %s
                """, job.status.code, job.status.text, job.status.detail,
                job.times.ticks, job.data)
        if job.status.final:
            stmt += SQL(", time_finished = now()")
        elif frozen_handler is not None:
            stmt += SQL(", frozen_handler = %s", frozen_handler)

        stmt += SQL(" WHERE job_uuid = %s", job.job_uuid)
        if job.times.ticks != types.JobTimes.TICK_OVERRIDE:
            stmt += SQL(" AND time_ticks < %s", job.times.ticks)
        stmt += SQL(" RETURNING jobs.jobs.*")

        d = self.pool.runQuery(stmt)
        d.addCallback(_grabOne, func=_oneJob)
        return d

    ## Tasks

    def createTask(self, task):
        d = self.pool.runQuery("""
            INSERT INTO jobs.tasks ( task_uuid, job_uuid, task_name, task_type,
                task_zone, task_data, status_code, status_text, status_detail,
                node_assigned )
            VALUES ( %s, %s, %s, %s, %s, %s, %s, %s, %s, %s )
            RETURNING jobs.tasks.*
            """, (task.task_uuid, task.job_uuid, task.task_name,
                task.task_type, task.task_zone, task.task_data,
                task.status.code, task.status.text, task.status.detail,
                task.node_assigned))
        d.addCallback(_grabOne, func=_oneTask)
        return d

    def updateTask(self, task):
        stmt = SQL("""
            UPDATE jobs.tasks SET status_code = %s, status_text = %s,
                status_detail = %s, time_updated = now(), time_ticks = %s,
                node_assigned = %s
                """, task.status.code, task.status.text, task.status.detail,
                task.times.ticks, task.node_assigned)
        if task.status.final:
            stmt += SQL(", time_finished = now()")
        if task.task_data is not None:
            stmt += SQL(", task_data = %s", task.task_data)

        stmt += SQL(" WHERE task_uuid = %s", task.task_uuid)
        if task.times.ticks != types.JobTimes.TICK_OVERRIDE:
            stmt += SQL(" AND time_ticks < %s", task.times.ticks)
        stmt += SQL(" RETURNING jobs.tasks.*")

        d = self.pool.runQuery(stmt)
        d.addCallback(_grabOne, func=_oneTask)
        return d


def _popStatus(kwargs):
    return types.FrozenJobStatus(
            kwargs.pop('status_code'),
            kwargs.pop('status_text'),
            kwargs.pop('status_detail'),
            )


def _popTimes(kwargs):
    return types.FrozenJobTimes(
            kwargs.pop('time_started'),
            kwargs.pop('time_updated'),
            kwargs.pop('time_finished'),
            kwargs.pop('expires_after', None),
            kwargs.pop('time_ticks', None),
            )


def _castUUIDS(raw_uuids):
    uuids = []
    for uuid in raw_uuids:
        if not isinstance(uuid, UUID):
            uuid = UUID(uuid)
        uuids.append(uuid)
    return uuids


def _mergeThings(rows, pkeys, func):
    """Zip a list of primary keys with a non-sorted list of rows where the
    first column is the primary key and return the list of sorted objects.
    """
    mapping = dict((x[0], x) for x in rows)
    out = []
    for pkey in pkeys:
        row = mapping.get(pkey)
        if row is not None:
            row = func(row)
        out.append(row)
    return out


def _grabOne(rows, func):
    if not rows:
        return None
    return func(rows[0])


def _oneJob(row):
    kwargs = dict(row)
    kwargs['status'] = _popStatus(kwargs)
    kwargs['times'] = _popTimes(kwargs)
    kwargs['data'] = types.FrozenObject(str(kwargs.pop('frozen_data')))
    kwargs.pop('frozen_handler', None)
    return types.FrozenRmakeJob(**kwargs)


def _oneTask(row):
    kwargs = dict(row)
    kwargs['status'] = _popStatus(kwargs)
    kwargs['times'] = _popTimes(kwargs)
    kwargs['task_data'] = types.FrozenObject(str(kwargs['task_data']))
    return types.FrozenRmakeTask(**kwargs)
