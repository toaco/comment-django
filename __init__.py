from django.utils.version import get_version

VERSION = (1, 8, 13, 'final', 0)

__version__ = get_version(VERSION)


def setup():
    """
    初始化Django
    Configure the settings (this happens as a side effect of accessing the
    first setting), configure logging and populate the app registry.
    """
    # TODO: 为什么在函数内部导入,减少导入模块的时间,不调用不导入.
    from django.apps import apps
    from django.conf import settings
    from django.utils.log import configure_logging

    configure_logging(settings.LOGGING_CONFIG, settings.LOGGING)
    apps.populate(settings.INSTALLED_APPS)
