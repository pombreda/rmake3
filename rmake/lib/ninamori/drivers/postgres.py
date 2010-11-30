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

import psycopg2
from psycopg2 import extensions

from rmake.lib.ninamori.connection import DatabaseConnection


class PostgresConnection(DatabaseConnection):
    __slots__ = ()
    driver = 'postgres'

    @classmethod
    def connect(cls, connectString):
        args = connectString.asDict(exclude=('driver',))
        args['database'] = args.pop('dbname')

        conn = psycopg2.connect(**args)
        conn.set_isolation_level(extensions.ISOLATION_LEVEL_AUTOCOMMIT)
        extensions.register_type(extensions.UNICODE, conn)
        return cls(conn)