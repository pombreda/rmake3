#
# Copyright (c) 2006-2007 rPath, Inc.  All Rights Reserved.
#
from email import MIMEText
import smtplib

from rmake.build import buildjob,buildtrove

from rmake.lib.subscriber import StatusSubscriber


class EmailJobLogger(StatusSubscriber):

    """
        Proof of concept simple email interface to rmake - sends out messages 
        on status changes.
    """

    protocol = 'mailto' 

    listeners = {'JOB_STATE_UPDATED'    : 'jobStateUpdated',
                 'TROVE_STATE_UPDATED'  : 'troveStateUpdated',
                 }

    fields = {
        'from'     : 'rmake@localhost', #address of the email sender
        'fromName' : 'Rmake Daemon', # Displayed name of the email sender
        'toName'   : None,           # Displayed name of the email receiver
        'prefix'   : '[rmake] ',     # Subject prefix
        }

    def _sendEmail(self, subject, body):
        msg = MIMEText.MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = self['from']
        msg['To'] = self.uri

        s = smtplib.SMTP()
        s.connect()
        s.sendmail(self['from'], [self.uri], msg.as_string())
        s.close()

    def jobStateUpdated(self, jobId, state, status):
        if state == buildjob.JOB_STATE_BUILT:
            self._sendEmail('Job %s Built' % jobId,
                            'Job %s Built' % jobId)
        if state == buildjob.JOB_STATE_FAILED:
            self._sendEmail('Job %s Failed' % jobId,
                            'Job %s Failed' % jobId)

    def troveStateUpdated(self, (jobId, troveTuple), state, status):
        if state == buildtrove.TROVE_STATE_BUILT:
            self._sendEmail('%s Built' % troveTuple[0],
                            '%s Built' % troveTuple[0])
        if state == buildtrove.TROVE_STATE_FAILED:
            self._sendEmail('%s Failed' % troveTuple[0],
                            '%s Failed' % troveTuple[0])