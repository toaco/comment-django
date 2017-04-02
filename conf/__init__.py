"""
Settings and configuration for Django.

Values will be read from the module specified by the DJANGO_SETTINGS_MODULE environment
variable, and then from django.conf.global_settings; see the global settings file for
a list of all possible variables.
"""

import importlib
import os
import time  # Needed for Windows,因为windows没有tzset
import warnings

from django.conf import global_settings
from django.core.exceptions import ImproperlyConfigured
from django.utils import six
from django.utils.deprecation import RemovedInDjango110Warning
from django.utils.functional import LazyObject, empty

ENVIRONMENT_VARIABLE = "DJANGO_SETTINGS_MODULE"


class LazySettings(LazyObject):
    """
    A lazy proxy for either global Django settings or a custom settings object.
    The user can manually configure settings prior to using them. Otherwise,
    Django uses the settings module pointed to by DJANGO_SETTINGS_MODULE.
    """

    def _setup(self, name=None):
        """
        初始化的过程

        Load the settings module pointed to by the environment variable. This
        is used the first time we need any settings at all, if the user has not
        previously configured the settings manually.
        """
        settings_module = os.environ.get(ENVIRONMENT_VARIABLE)
        if not settings_module:
            desc = ("setting %s" % name) if name else "settings"
            raise ImproperlyConfigured(
                "Requested %s, but settings are not configured. "
                "You must either define the environment variable %s "
                "or call settings.configure() before accessing settings."
                % (desc, ENVIRONMENT_VARIABLE))

        self._wrapped = Settings(settings_module)

    def __getattr__(self, name):
        """该方法父类写了的,多余的吗?"""
        if self._wrapped is empty:
            self._setup(name)
        return getattr(self._wrapped, name)

    # todo:该方法的作用,该方法如果通过wsgi的过程,那么不会被执行?,但是其他时候需要调用该方法?,因此此方法用于开发?
    def configure(self, default_settings=global_settings, **options):
        """
        手动加载设置.主要是可以通过代码调用添加关键字参数进行设置.此时使用的不是Settting而是UserSettingsHolder
        而且可以设置default_settings为非global_setting,方便测试吗?
        Called to manually configure the settings. The 'default_settings'
        parameter sets where to retrieve any unspecified values from (its
        argument must support attribute access (__getattr__)).
        """
        if self._wrapped is not empty:
            raise RuntimeError('Settings already configured.')
        holder = UserSettingsHolder(default_settings)
        for name, value in options.items():
            setattr(holder, name, value)
        self._wrapped = holder

    @property
    def configured(self):
        """
        是否已经访问过setting了
        Returns True if the settings have already been configured.
        """
        return self._wrapped is not empty


class BaseSettings(object):
    """
    主要就是设置特殊属性时必须满足的规则
    Common logic for settings whether set by a module or by the user.
    """

    def __setattr__(self, name, value):
        # 如果是"MEDIA_URL", "STATIC_URL",那么必须有值且以/结尾.
        if name in ("MEDIA_URL", "STATIC_URL") and value and not value.endswith('/'):
            raise ImproperlyConfigured("If set, %s must end with a slash" % name)
        object.__setattr__(self, name, value)


class Settings(BaseSettings):
    """会把所有的设置加载了,先默认的,再用户的,会验证用户的规则和设置时区"""

    def __init__(self, settings_module):
        # update this dict from global settings (but only for ALL_CAPS settings)
        # 将所有的大写的设置添加进来
        for setting in dir(global_settings):
            if setting.isupper():
                # NOTE:使用setattr和getattr是因为setting为字符串
                setattr(self, setting, getattr(global_settings, setting))

        # store the settings module in case someone later cares
        self.SETTINGS_MODULE = settings_module
        # 加载自己编写的setting模块
        mod = importlib.import_module(self.SETTINGS_MODULE)
        # 所有的元组设置的名字
        tuple_settings = (
            "ALLOWED_INCLUDE_ROOTS",
            "INSTALLED_APPS",
            "TEMPLATE_DIRS",
            "LOCALE_PATHS",
        )
        # 所有用户正确显式设置了的都加进去
        self._explicit_settings = set()
        for setting in dir(mod):
            if setting.isupper():
                setting_value = getattr(mod, setting)
                # 如果没有设置成元组会报错
                if (setting in tuple_settings and
                        isinstance(setting_value, six.string_types)):
                    raise ImproperlyConfigured("The %s setting must be a tuple. "
                                               "Please fix your settings." % setting)
                setattr(self, setting, setting_value)
                self._explicit_settings.add(setting)
        # 必须设置SECRET_KEY
        if not self.SECRET_KEY:
            raise ImproperlyConfigured("The SECRET_KEY setting must not be empty.")

        if ('django.contrib.auth.middleware.AuthenticationMiddleware' in self.MIDDLEWARE_CLASSES and
                    'django.contrib.auth.middleware.SessionAuthenticationMiddleware' not in self.MIDDLEWARE_CLASSES):
            warnings.warn(
                # 1.10强制session验证,因此需要添加session中间件
                "Session verification will become mandatory in Django 1.10. "
                "Please add 'django.contrib.auth.middleware.SessionAuthenticationMiddleware' "
                "to your MIDDLEWARE_CLASSES setting when you are ready to opt-in after "
                "reading the upgrade considerations in the 1.8 release notes.",
                RemovedInDjango110Warning
            )

        # 重置所使用的库例程的时间转换规则,只在Linux上有效,Windows上没有这个函数
        if hasattr(time, 'tzset') and self.TIME_ZONE:
            # When we can, attempt to validate the timezone. If we can't find
            # this file, no check happens and it's harmless.
            zoneinfo_root = '/usr/share/zoneinfo'
            if (os.path.exists(zoneinfo_root) and not
            os.path.exists(os.path.join(zoneinfo_root, *(self.TIME_ZONE.split('/'))))):
                raise ValueError("Incorrect timezone setting: %s" % self.TIME_ZONE)
            # Move the time zone info into os.environ. See ticket #2315 for why
            # we don't do this unconditionally (breaks Windows).
            os.environ['TZ'] = self.TIME_ZONE
            time.tzset()

    def is_overridden(self, setting):
        """是否是重载的属性."""
        return setting in self._explicit_settings


class UserSettingsHolder(BaseSettings):
    """
    加载设置,除了Base验证不做任何验证,不设置时区(因为测试在windows上,所以不设置?因为只在本机跑,不关心外部时区??).即完全信任设置的正确性且没有
    Holder for user configured settings.
    """
    # SETTINGS_MODULE doesn't make much sense in the manually configured
    # (standalone) case.
    SETTINGS_MODULE = None

    def __init__(self, default_settings):
        """
        参数default_settings默认传递进来的是global_setting
        default_settings必须支持gettattr方法
        Requests for configuration variables not in this class are satisfied
        from the module specified in default_settings (if possible).
        """
        # 这个属性就是用来更好的处理相关属性存取
        self.__dict__['_deleted'] = set()
        self.default_settings = default_settings

    def __getattr__(self, name):
        if name in self._deleted:
            raise AttributeError
        return getattr(self.default_settings, name)

    def __setattr__(self, name, value):
        # 方法discard如果存在才remove,比较方便
        self._deleted.discard(name)
        super(UserSettingsHolder, self).__setattr__(name, value)

    def __delattr__(self, name):
        self._deleted.add(name)
        if hasattr(self, name):
            super(UserSettingsHolder, self).__delattr__(name)

    def __dir__(self):
        """调用list(self.__dict__)只会显示字典的键，之后加上default_setting"""
        return list(self.__dict__) + dir(self.default_settings)

    def is_overridden(self, setting):
        """还要删除过,本地设置过,或者是默认设置的is_overridden(setting)方法为真,那么都表示修改过"""
        deleted = (setting in self._deleted)
        set_locally = (setting in self.__dict__)
        set_on_default = getattr(self.default_settings, 'is_overridden', lambda s: False)(setting)
        return (deleted or set_locally or set_on_default)


# 使用LazySetttings可以防止导入该模块就执行初始化了,启动时间加快了.方便测试?
settings = LazySettings()
