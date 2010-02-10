from logging import handlers
import logging
import os

from conary.lib import util

_loggers = []
LOGSIZE = 10 * 1024 * 1024
BACKUPS = 3

def shutdown():
    for logger in _loggers:
        logger.close()
        logger.__class__._dict = {}

class Logger(object):

    name = ''
    consoleDateFormat = '%X'
    consoleFormat = '%(asctime)s - [%(name)s] - %(message)s'
    isCopy = False

    formatterClass = logging.Formatter
    dateFormat = '%x %X %Z'
    fileFormat = '%(asctime)s - [%(name)s] - %(message)s'

    def __init__(self, name=None, logPath=None):
        # do some borg magic to ensure there's only one Logger instance per
        # class + name
        if not hasattr(self.__class__, '_dict'):
            self.__class__._dict = {}
        if name is not None:
            self.name = name

        self._loggers = []
        if self.name in self.__class__._dict:
            self.__dict__ = self.__class__._dict[self.name]
            self.isCopy = True
            return
        else:
            _loggers.append(self)
            self.__class__._dict[self.name] = self.__dict__

        self.fileHandler = None


        # set up for output to the console - everything above debug
        self.console = logging.StreamHandler()
        self.console.setFormatter(self.formatterClass(self.consoleFormat,
                                                      self.consoleDateFormat))
        self.console.setLevel(logging.INFO)
        logger = logging.getLogger(self.name)
        logger.parent = None
        for handler in logger.handlers:
            logger.removeHandler(handler)
        logger.setLevel(logging.DEBUG)
        self.logger = logger
        if logPath:
            self.logToFile(logPath)
        else:
            self.enableConsole()
        self._loggers.append(logger)

    def close(self):
        for logger in self._loggers:
            for handler in logger.handlers:
                # if handler not in this list, it's already been cleaned up.
                if handler in logging._handlers:
                    handler.close()
                logger.removeHandler(handler)

    def setQuietMode(self):
        for logger in self._loggers:
            logger.setLevel(logging.ERROR)

    def logToFile(self, logPath):
        if not self.fileHandler:
            util.mkdirChain(os.path.dirname(logPath))
            fileHandler = handlers.RotatingFileHandler(logPath,
                                                      maxBytes=LOGSIZE,
                                                      backupCount=BACKUPS)
            fileHandler.setFormatter(self.formatterClass(self.fileFormat,
                                                         self.dateFormat))
            self.fileHandler = fileHandler
        self.logger.addHandler(self.fileHandler)

    def info(self, message, *args, **kw):
        self.logger.info(message, *args, **kw)

    def error(self, message, *args, **kw):
        self.logger.error('error: ' + str(message), *args, **kw)

    def warning(self, message, *args, **kw):
        self.logger.warning('warning: ' + str(message), *args, **kw)

    def debug(self, message, *args, **kw):
        self.logger.debug('debug: ' + str(message), *args, **kw)

    def exception(self, message, *args, **kw):
        self.logger.exception('fatal error: ' + str(message), *args, **kw)

    def enableConsole(self, level=logging.INFO):
        self.logger.addHandler(self.console)
        self.console.setLevel(level)

    def disableConsole(self):
        self.logger.removeHandler(self.console)

class ServerLogger(Logger):

    rpcConsoleFormat = '%(asctime)s %(message)s'
    rpcFormat = '%(asctime)s - %(message)s'
    maxParamLength = 300

    def __init__(self, name=None, logPath=None):
        Logger.__init__(self, name=name, logPath=logPath)
        if self.isCopy:
            return
        self.xmlrpcLogger = logging.getLogger(self.name + '-rpc')
        self.xmlrpcLogger.parent = None
        self.xmlrpcLogger.setLevel(logging.DEBUG)
        self._loggers.append(self.xmlrpcLogger)
        self.xmlrpcConsole = logging.StreamHandler()
        self.xmlrpcConsole.setFormatter(
                                     self.formatterClass(self.rpcConsoleFormat,
                                                         self.consoleDateFormat))
        self.xmlrpcConsole.setLevel(logging.INFO)
        self.rpcFileHandler = None
        self.enableRPCConsole()

    def enableRPCConsole(self):
        self.xmlrpcLogger.addHandler(self.xmlrpcConsole)

    def disableRPCConsole(self):
        self.xmlrpcLogger.removeHandler(self.xmlrpcConsole)

    def logRPCToFile(self, rpcPath):
        if not self.rpcFileHandler:
            fileHandler = handlers.RotatingFileHandler(rpcPath, 
                                                     maxBytes=LOGSIZE,
                                                     backupCount=BACKUPS)
            fileHandler.setFormatter(self.formatterClass(self.rpcFormat,
                                                       self.dateFormat))
            self.rpcFileHandler = fileHandler
        self.xmlrpcLogger.addHandler(self.rpcFileHandler)

    def logRPCCall(self, callData, methodname, args):
        self.xmlrpcLogger.info('%-15s - %s' % (methodname, callData.getAuth()))

    def logRPCDetails(self, methodname, **kw):
        params = []
        for param, value in kw.items():
            value = str(value)
            if len(value) > self.maxParamLength:
                value = value[:self.maxParamLength] + '<truncated>'
            params.append('='.join((param, value)))
        params = ', '.join(sorted(params))
        self.xmlrpcLogger.info(' ->  %s(%s)' % (methodname, params))


FORMATS = {
        'apache': ('[%(asctime)s] [%(levelname)s] (%(name)s) %(message)s',
            '%a %b %d %T %Y'),
        'console': ('%(levelname)s: %(message)s', None),
        'file': ('%(asctime)s %(levelname)s %(name)s : %(message)s', None),
        }


def setupLogging(logPath=None, consoleLevel=logging.WARNING,
        consoleFormat='console', fileLevel=logging.INFO, fileFormat='file',
        logger=''):

    logger = logging.getLogger(logger)
    logger.handlers = []
    logger.propagate = False
    level = 100

    # Console handler
    if consoleLevel is not None:
        if consoleFormat in FORMATS:
            consoleFormat = FORMATS[consoleFormat]
        consoleFormatter = logging.Formatter(*consoleFormat)
        consoleHandler = logging.StreamHandler()
        consoleHandler.setFormatter(consoleFormatter)
        consoleHandler.setLevel(consoleLevel)
        logger.addHandler(consoleHandler)
        level = min(level, consoleLevel)

    # File handler
    if logPath and fileLevel is not None:
        if fileFormat in FORMATS:
            fileFormat = FORMATS[fileFormat]
        logfileFormatter = logging.Formatter(*fileFormat)
        logfileHandler = logging.FileHandler(logPath)
        logfileHandler.setFormatter(logfileFormatter)
        logfileHandler.setLevel(fileLevel)
        logger.addHandler(logfileHandler)
        level = min(level, fileLevel)

    logger.setLevel(level)
    return logger
