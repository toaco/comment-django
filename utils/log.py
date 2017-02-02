from __future__ import unicode_literals

import logging
import sys
import warnings
# Imports kept for backwards-compatibility in Django 1.7.
from logging import NullHandler  # NOQA
from logging.config import dictConfig  # NOQA

from django.conf import settings
from django.core import mail
from django.core.mail import get_connection
from django.utils.deprecation import RemovedInNextVersionWarning
from django.utils.encoding import force_text
from django.utils.module_loading import import_string
from django.views.debug import ExceptionReporter, get_exception_reporter_filter

getLogger = logging.getLogger

# Default logging for Django. This sends an email to the site admins on every
# HTTP 500 error. Depending on DEBUG, all other log records are either sent to
# the console (DEBUG=True) or discarded by mean of the NullHandler (DEBUG=False).
# 使用字典做配置,很简洁好维护容易读懂
DEFAULT_LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'filters': {
        'require_debug_false': {
            '()': 'django.utils.log.RequireDebugFalse',
        },
        'require_debug_true': {
            '()': 'django.utils.log.RequireDebugTrue',
        },
    },
    'handlers': {
        # 3个handler,console用于开发环境,所有消息都在控制台显示,mail_admins用于产品环境
        'console': {
            'level': 'INFO',
            'filters': ['require_debug_true'],
            'class': 'logging.StreamHandler',
        },
        'null': {
            'class': 'logging.NullHandler',
        },
        'mail_admins': {
            'level': 'ERROR',
            'filters': ['require_debug_false'],
            'class': 'django.utils.log.AdminEmailHandler'
        }
    },
    'loggers': {
        # 开发环境的
        'django': {
            'handlers': ['console'],
        },
        # 真实环境的
        'django.request': {
            'handlers': ['mail_admins'],
            'level': 'ERROR',
            'propagate': False,
        },
        # 真实环境的
        'django.security': {
            'handlers': ['mail_admins'],
            'level': 'ERROR',
            'propagate': False,
        },
        # python logging框架捕获警告后,用于处理的logger,开发环境下将消息显示到控制台
        'py.warnings': {
            'handlers': ['console'],
        },
    }
}


def configure_logging(logging_config, logging_settings):
    """
    配置logging,将警告提升为default,并且转交给logging框架
    配置有默认配置,但是如果settiing有用户自己的,则使用用户自己的.
    :param logging_config:
    :param logging_settings:
    :return:
    """
    # -W参数传递进去的,日志框架不捕获警告,RemovedIn...警告会被忽略,因此会使用用户自己配置的-W选项
    # 默认是空的
    if not sys.warnoptions:
        # Route warnings through python logging
        logging.captureWarnings(True)
        # RemovedInNextVersionWarning is a subclass of DeprecationWarning which
        # is hidden by default, hence we force the "default" behavior
        # 因为是DeprecationWarining的子类,所以默认忽略的,这里改变其行为
        warnings.simplefilter("default", RemovedInNextVersionWarning)

    if logging_config:
        # First find the logging configuration function ...
        logging_config_func = import_string(logging_config)

        dictConfig(DEFAULT_LOGGING)

        # ... then invoke it with the logging settings
        if logging_settings:
            logging_config_func(logging_settings)


class AdminEmailHandler(logging.Handler):
    """An exception log handler that emails log entries to site admins.
    给管理员发送邮件

    If the request is passed as the first argument to the log record,
    request data will be provided in the email report.
    """

    def __init__(self, include_html=False, email_backend=None):
        logging.Handler.__init__(self)
        self.include_html = include_html
        self.email_backend = email_backend

    def emit(self, record):
        try:
            request = record.request
            subject = '%s (%s IP): %s' % (
                record.levelname,
                ('internal' if request.META.get('REMOTE_ADDR') in settings.INTERNAL_IPS
                 else 'EXTERNAL'),
                record.getMessage()
            )
            filter = get_exception_reporter_filter(request)
            request_repr = '\n{}'.format(force_text(filter.get_request_repr(request)))
        except Exception:
            subject = '%s: %s' % (
                record.levelname,
                record.getMessage()
            )
            request = None
            request_repr = "unavailable"
        subject = self.format_subject(subject)

        if record.exc_info:
            exc_info = record.exc_info
        else:
            exc_info = (None, record.getMessage(), None)

        message = "%s\n\nRequest repr(): %s" % (self.format(record), request_repr)
        reporter = ExceptionReporter(request, is_email=True, *exc_info)
        html_message = reporter.get_traceback_html() if self.include_html else None
        self.send_mail(subject, message, fail_silently=True, html_message=html_message)

    def send_mail(self, subject, message, *args, **kwargs):
        mail.mail_admins(subject, message, *args, connection=self.connection(), **kwargs)

    def connection(self):
        return get_connection(backend=self.email_backend, fail_silently=True)

    def format_subject(self, subject):
        """
        Escape CR and LF characters, and limit length.
        RFC 2822's hard limit is 998 characters per line. So, minus "Subject: "
        the actual subject must be no longer than 989 characters.
        """
        formatted_subject = subject.replace('\n', '\\n').replace('\r', '\\r')
        return formatted_subject[:989]


class CallbackFilter(logging.Filter):
    """
    A logging filter that checks the return value of a given callable (which
    takes the record-to-be-logged as its only parameter) to decide whether to
    log a record.

    """

    def __init__(self, callback):
        self.callback = callback

    def filter(self, record):
        if self.callback(record):
            return 1
        return 0


class RequireDebugFalse(logging.Filter):
    def filter(self, record):
        return not settings.DEBUG


class RequireDebugTrue(logging.Filter):
    def filter(self, record):
        return settings.DEBUG
