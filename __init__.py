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

    # 这里就开始访问setting的属性了,那么setting的延迟初始化在这里并没有体现出优势,难道是其他路径不用setting的很多?
    # # The callable to use to configure logging,这个东西是ligging模块里面的,表示用字典配置(将配置放在字典然后解析??)
    # LOGGING_CONFIG = 'logging.config.dictConfig'
    # # Custom logging configuration.
    # LOGGING = {}

    configure_logging(settings.LOGGING_CONFIG, settings.LOGGING)
    apps.populate(settings.INSTALLED_APPS)
