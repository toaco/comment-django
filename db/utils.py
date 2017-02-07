import inspect
import os
import pkgutil
import warnings
from importlib import import_module
from threading import local

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils import six
from django.utils._os import upath
from django.utils.deprecation import (
    RemovedInDjango19Warning, RemovedInDjango110Warning,
)
from django.utils.functional import cached_property
from django.utils.inspect import HAS_INSPECT_SIGNATURE
from django.utils.module_loading import import_string

DEFAULT_DB_ALIAS = 'default'
DJANGO_VERSION_PICKLE_KEY = '_django_version'


class Error(Exception if six.PY3 else StandardError):  # NOQA: StandardError undefined on PY3
    pass


class InterfaceError(Error):
    pass


class DatabaseError(Error):
    pass


class DataError(DatabaseError):
    pass


class OperationalError(DatabaseError):
    pass


class IntegrityError(DatabaseError):
    pass


class InternalError(DatabaseError):
    pass


class ProgrammingError(DatabaseError):
    pass


class NotSupportedError(DatabaseError):
    pass


class DatabaseErrorWrapper(object):
    """
    Context manager and decorator that re-throws backend-specific database
    exceptions using Django's common wrappers.
    """

    def __init__(self, wrapper):
        """
        wrapper is a database wrapper.

        It must have a Database attribute defining PEP-249 exceptions.
        """
        self.wrapper = wrapper

    def __enter__(self):
        pass

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            return
        for dj_exc_type in (
                DataError,
                OperationalError,
                IntegrityError,
                InternalError,
                ProgrammingError,
                NotSupportedError,
                DatabaseError,
                InterfaceError,
                Error,
        ):
            db_exc_type = getattr(self.wrapper.Database, dj_exc_type.__name__)
            if issubclass(exc_type, db_exc_type):
                dj_exc_value = dj_exc_type(*exc_value.args)
                dj_exc_value.__cause__ = exc_value
                # Only set the 'errors_occurred' flag for errors that may make
                # the connection unusable.
                if dj_exc_type not in (DataError, IntegrityError):
                    self.wrapper.errors_occurred = True
                six.reraise(dj_exc_type, dj_exc_value, traceback)

    def __call__(self, func):
        # Note that we are intentionally not using @wraps here for performance
        # reasons. Refs #21109.
        def inner(*args, **kwargs):
            with self:
                return func(*args, **kwargs)

        return inner


def load_backend(backend_name):
    # Look for a fully qualified database backend name
    try:
        # 直接导入,加一个.base后缀
        return import_module('%s.base' % backend_name)
    except ImportError as e_user:
        # 没有找到抛出异常,这里只是让报错信息更加有用.暂不深入
        # The database backend wasn't found. Display a helpful error message
        # listing all possible (built-in) database backends.
        backend_dir = os.path.join(os.path.dirname(upath(__file__)), 'backends')
        try:
            builtin_backends = [
                name for _, name, ispkg in pkgutil.iter_modules([backend_dir])
                if ispkg and name != 'dummy']
        except EnvironmentError:
            builtin_backends = []
        if backend_name not in ['django.db.backends.%s' % b for b in
                                builtin_backends]:
            backend_reprs = map(repr, sorted(builtin_backends))
            error_msg = ("%r isn't an available database backend.\n"
                         "Try using 'django.db.backends.XXX', where XXX "
                         "is one of:\n    %s\nError was: %s" %
                         (backend_name, ", ".join(backend_reprs), e_user))
            raise ImproperlyConfigured(error_msg)
        else:
            # If there's some other error, this must be an error in Django
            raise


class ConnectionDoesNotExist(Exception):
    pass


class ConnectionHandler(object):
    def __init__(self, databases=None):
        """
        databases is an optional dictionary of database definitions (structured
        like settings.DATABASES).
        """
        # databases就是一个setting中DATABASES设置那个字典格式
        self._databases = databases
        self._connections = local()

    @cached_property
    def databases(self):
        # 没设置默认为setting
        # 设置了但是是空则为dummy
        # 必须有default
        if self._databases is None:
            self._databases = settings.DATABASES
        if self._databases == {}:
            self._databases = {
                # DEFAULT_DB_ALIAS:'default'
                DEFAULT_DB_ALIAS: {
                    'ENGINE': 'django.db.backends.dummy',
                },
            }
        if DEFAULT_DB_ALIAS not in self._databases:
            raise ImproperlyConfigured("You must define a '%s' database" % DEFAULT_DB_ALIAS)
        return self._databases

    def ensure_defaults(self, alias):
        """
        保证设置字典中的所有的设置都被正确设置
        Puts the defaults into the settings dictionary for a given connection
        where no settings is provided.
        """
        try:
            # 拿到连接的配置,如:
            # {
            #     'ENGINE': 'django.db.backends.sqlite3',
            #     'NAME': os.path.join(BASE_DIR, 'db.sqlite3'),
            # }
            # NOTE:因为拿到的是字典,所以是可变对象,因此会正确设置
            conn = self.databases[alias]
        except KeyError:
            raise ConnectionDoesNotExist("The connection %s doesn't exist" % alias)

        # setdefault返回键的值,然后如果不在字典中,会设置
        conn.setdefault('ATOMIC_REQUESTS', False)
        conn.setdefault('AUTOCOMMIT', True)
        # 如果没有键,设置成backends.或者空,那么都设置为dummy
        conn.setdefault('ENGINE', 'django.db.backends.dummy')
        if conn['ENGINE'] == 'django.db.backends.' or not conn['ENGINE']:
            conn['ENGINE'] = 'django.db.backends.dummy'
        conn.setdefault('CONN_MAX_AGE', 0)
        conn.setdefault('OPTIONS', {})
        # 时区设置,如果设置了tz为True则使用UTC否则使用settin的timezone,即setting中use_tz最重要,
        conn.setdefault('TIME_ZONE', 'UTC' if settings.USE_TZ else settings.TIME_ZONE)
        # 下面几个设置为默认的
        for setting in ['NAME', 'USER', 'PASSWORD', 'HOST', 'PORT']:
            conn.setdefault(setting, '')
    # 下面几个是测试使用的
    TEST_SETTING_RENAMES = {
        'CREATE': 'CREATE_DB',
        'USER_CREATE': 'CREATE_USER',
        'PASSWD': 'PASSWORD',
    }
    TEST_SETTING_RENAMES_REVERSE = {v: k for k, v in TEST_SETTING_RENAMES.items()}

    def prepare_test_settings(self, alias):
        """
        Makes sure the test settings are available in the 'TEST' sub-dictionary.
        """
        try:
            # 同样是先拿到字典
            conn = self.databases[alias]
        except KeyError:
            raise ConnectionDoesNotExist("The connection %s doesn't exist" % alias)
        # 是否设置过test
        test_dict_set = 'TEST' in conn
        # 如果没设置设置默认值,并拿到键值
        test_settings = conn.setdefault('TEST', {})

        # 老式的设置
        old_test_settings = {}
        for key, value in six.iteritems(conn):
            if key.startswith('TEST_'):
                new_key = key[5:]
                new_key = self.TEST_SETTING_RENAMES.get(new_key, new_key)
                old_test_settings[new_key] = value
        # 如果有老式的设置
        if old_test_settings:
            # 如果同时设置了新式的,但两个不一样,那么久抛出异常
            if test_dict_set:
                if test_settings != old_test_settings:
                    raise ImproperlyConfigured(
                        "Connection '%s' has mismatched TEST and TEST_* "
                        "database settings." % alias)
            # 用老式的更新新式的,因此如果同时设置那么会覆盖,并且发出RemovedInDjango19Warning警告
            else:
                test_settings.update(old_test_settings)
                for key, _ in six.iteritems(old_test_settings):
                    warnings.warn("In Django 1.9 the TEST_%s connection setting will be moved "
                                  "to a %s entry in the TEST setting" %
                                  (self.TEST_SETTING_RENAMES_REVERSE.get(key, key), key),
                                  RemovedInDjango19Warning, stacklevel=2)
        # 删除老式的,因为已更新在新式的字典里面
        for key in list(conn.keys()):
            if key.startswith('TEST_'):
                del conn[key]
        # Check that they didn't just use the old name with 'TEST_' removed
        # 确保设置的键没有老式的
        for key, new_key in six.iteritems(self.TEST_SETTING_RENAMES):
            if key in test_settings:
                warnings.warn("Test setting %s was renamed to %s; specified value (%s) ignored" %
                              (key, new_key, test_settings[key]), stacklevel=2)
        # 给TEST键设置这几个键的默认值.
        for key in ['CHARSET', 'COLLATION', 'NAME', 'MIRROR']:
            test_settings.setdefault(key, None)

    def __getitem__(self, alias):
        # todo
        if hasattr(self._connections, alias):
            return getattr(self._connections, alias)

        self.ensure_defaults(alias)
        # 为什么不命名为ensure_test_defaults
        self.prepare_test_settings(alias)
        # 拿到的db是一个配置字典
        db = self.databases[alias]
        # 加上.base后缀,然后导入那个模块
        backend = load_backend(db['ENGINE'])
        # 根据字典创建连接,同时保存了别名
        conn = backend.DatabaseWrapper(db, alias)
        # 然后将创建的连接保存在_connection上,线程缓存.
        setattr(self._connections, alias, conn)
        return conn

    def __setitem__(self, key, value):
        setattr(self._connections, key, value)

    def __delitem__(self, key):
        delattr(self._connections, key)

    def __iter__(self):
        # 迭代databases字典,
        # DATABASES = {
        #     'default': {
        #         'ENGINE': 'django.db.backends.sqlite3',
        #         'NAME': os.path.join(BASE_DIR, 'db.sqlite3'),
        #     }
        # }
        return iter(self.databases)

    def all(self):
        # 拿到所有连接的wrapper
        return [self[alias] for alias in self]

    def close_all(self):
        for alias in self:
            try:
                connection = getattr(self._connections, alias)
            except AttributeError:
                continue
            connection.close()


class ConnectionRouter(object):
    def __init__(self, routers=None):
        """
        If routers is not specified, will default to settings.DATABASE_ROUTERS.
        """
        self._routers = routers

    @cached_property
    def routers(self):
        if self._routers is None:
            self._routers = settings.DATABASE_ROUTERS
        routers = []
        for r in self._routers:
            if isinstance(r, six.string_types):
                router = import_string(r)()
            else:
                router = r
            routers.append(router)
        return routers

    def _router_func(action):
        def _route_db(self, model, **hints):
            chosen_db = None
            for router in self.routers:
                try:
                    method = getattr(router, action)
                except AttributeError:
                    # If the router doesn't have a method, skip to the next one.
                    pass
                else:
                    chosen_db = method(model, **hints)
                    if chosen_db:
                        return chosen_db
            instance = hints.get('instance')
            if instance is not None and instance._state.db:
                return instance._state.db
            return DEFAULT_DB_ALIAS

        return _route_db

    db_for_read = _router_func('db_for_read')
    db_for_write = _router_func('db_for_write')

    def allow_relation(self, obj1, obj2, **hints):
        for router in self.routers:
            try:
                method = router.allow_relation
            except AttributeError:
                # If the router doesn't have a method, skip to the next one.
                pass
            else:
                allow = method(obj1, obj2, **hints)
                if allow is not None:
                    return allow
        return obj1._state.db == obj2._state.db

    def allow_migrate(self, db, app_label, **hints):
        for router in self.routers:
            try:
                try:
                    method = router.allow_migrate
                except AttributeError:
                    method = router.allow_syncdb
                    has_deprecated_signature = True
                    warnings.warn(
                        'Router.allow_syncdb has been deprecated and will stop working in Django 1.9. '
                        'Rename the method to allow_migrate.',
                        RemovedInDjango19Warning, stacklevel=2)
                else:
                    if HAS_INSPECT_SIGNATURE:
                        sig = inspect.signature(method)
                        has_deprecated_signature = not any(
                            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
                        )
                    else:
                        argspec = inspect.getargspec(method)
                        has_deprecated_signature = len(argspec.args) == 3 and not argspec.keywords
                    if has_deprecated_signature:
                        # Raised here because allow_syncdb has to be called with
                        # the deprecated signature but shouldn't show this
                        # warning (only the deprecated method one)
                        warnings.warn(
                            "The signature of allow_migrate has changed from "
                            "allow_migrate(self, db, model) to "
                            "allow_migrate(self, db, app_label, model_name=None, **hints). "
                            "Support for the old signature will be removed in Django 1.10.",
                            RemovedInDjango110Warning)
            except AttributeError:
                # If the router doesn't have a method, skip to the next one.
                continue

            if has_deprecated_signature:
                model = hints.get('model')
                allow = None if model is None else method(db, model)
            else:
                allow = method(db, app_label, **hints)

            if allow is not None:
                return allow
        return True

    def allow_migrate_model(self, db, model):
        return self.allow_migrate(
            db,
            model._meta.app_label,
            model_name=model._meta.model_name,
            model=model,
        )

    def get_migratable_models(self, app_config, db, include_auto_created=False):
        """
        Return app models allowed to be synchronized on provided db.
        """
        models = app_config.get_models(include_auto_created=include_auto_created)
        return [model for model in models if self.allow_migrate_model(db, model)]
