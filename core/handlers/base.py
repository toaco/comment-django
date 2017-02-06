from __future__ import unicode_literals

import logging
import sys
import types

from django import http
from django.conf import settings
from django.core import signals, urlresolvers
from django.core.exceptions import (
    MiddlewareNotUsed, PermissionDenied, SuspiciousOperation,
)
from django.db import connections, transaction
from django.http.multipartparser import MultiPartParserError
from django.utils import six
from django.utils.encoding import force_text
from django.utils.module_loading import import_string
from django.views import debug

logger = logging.getLogger('django.request')


class BaseHandler(object):
    # Changes that are always applied to a response (in this order).
    # 修复一下响应使其更加符合rfc规范,暂时不用关心
    response_fixes = [
        http.fix_location_header,
        http.conditional_content_removal,
    ]

    def __init__(self):
        # Django的五种中间件.todo:为什么不初始化为空列表
        self._request_middleware = None
        self._view_middleware = None
        self._template_response_middleware = None
        self._response_middleware = None
        self._exception_middleware = None

    def load_middleware(self):
        """
        Populate middleware lists from settings.MIDDLEWARE_CLASSES.

        Must be called after the environment is fixed (see __call__ in subclasses).
        """
        self._view_middleware = []
        self._template_response_middleware = []
        self._response_middleware = []
        self._exception_middleware = []

        request_middleware = []
        for middleware_path in settings.MIDDLEWARE_CLASSES:
            # 这里使用了import_string方法.TODO: AppConfig为什么不适用import_string方法导入
            mw_class = import_string(middleware_path)
            try:
                mw_instance = mw_class()
            # 对于没有正确导入的中间件将直接忽略,但是在Debug模式下会显示结果
            except MiddlewareNotUsed as exc:
                if settings.DEBUG:
                    if six.text_type(exc):
                        logger.debug('MiddlewareNotUsed(%r): %s', middleware_path, exc)
                    else:
                        logger.debug('MiddlewareNotUsed: %r', middleware_path)
                continue

            if hasattr(mw_instance, 'process_request'):
                request_middleware.append(mw_instance.process_request)
            if hasattr(mw_instance, 'process_view'):
                self._view_middleware.append(mw_instance.process_view)
            if hasattr(mw_instance, 'process_template_response'):
                self._template_response_middleware.insert(0, mw_instance.process_template_response)
            if hasattr(mw_instance, 'process_response'):
                self._response_middleware.insert(0, mw_instance.process_response)
            if hasattr(mw_instance, 'process_exception'):
                self._exception_middleware.insert(0, mw_instance.process_exception)

        # We only assign to this when initialization is complete as it is used
        # as a flag for initialization being complete.
        # 在最后才赋值,确保的确是正确初始化了
        self._request_middleware = request_middleware

    # TODO:深入了解实现方式
    def make_view_atomic(self, view):
        """确保原子的执行视图函数."""
        non_atomic_requests = getattr(view, '_non_atomic_requests', set())
        # 遍历所有连接了的数据库,然后每个连接的数据库包装原子执行的视图函数,即执行一个视图函数占据所有的数据库资源?,然后返回该视图函数
        for db in connections.all():
            if (db.settings_dict['ATOMIC_REQUESTS']
                and db.alias not in non_atomic_requests):
                view = transaction.atomic(using=db.alias)(view)
        return view

    def get_exception_response(self, request, resolver, status_code):
        try:
            # 拿到错误处理hander,错误handler接受request参数,返回响应,该响应必须返回DjangoResponse,而不是包含render的模板
            callback, param_dict = resolver.resolve_error_handler(status_code)
            response = callback(request, **param_dict)
        except:
            signals.got_request_exception.send(sender=self.__class__, request=request)
            response = self.handle_uncaught_exception(request, resolver, sys.exc_info())

        return response

    def get_response(self, request):
        "Returns an HttpResponse object for the given HttpRequest"

        # Setup default url resolver for this thread, this code is outside
        # the try/except so we don't get a spurious "unbound local
        # variable" exception in the event an exception is raised before
        # resolver is set
        # 这个resolver本来就要放在外面啊=.=
        urlconf = settings.ROOT_URLCONF
        #　根URLconfig设置在当前线程默认为/,但是请求中一般有urlconf,会重写
        urlresolvers.set_urlconf(urlconf)
        # 拿到了全局的解析器,TODO:为什么不将该resolver直接放在线程中,而是在这里创建,需要reverse的时候又创建
        resolver = urlresolvers.RegexURLResolver(r'^/', urlconf)
        try:
            response = None
            # Apply request middleware,一旦有请求中间件返回response就停止
            for middleware_method in self._request_middleware:
                response = middleware_method(request)
                if response:
                    break
            # 如果返回的是空,说明所有中间件执行了,但是没有返回response,就继续处理
            if response is None:
                # 如果请求有urlconf属性,则重新配置resolver
                if hasattr(request, 'urlconf'):
                    # Reset url resolver with a custom urlconf.
                    urlconf = request.urlconf
                    urlresolvers.set_urlconf(urlconf)
                    # 　路径以／开头
                    resolver = urlresolvers.RegexURLResolver(r'^/', urlconf)
                # 这里可能抛出resolve404,是HTTP404的子类,会被正确处理
                resolver_match = resolver.resolve(request.path_info)
                callback, callback_args, callback_kwargs = resolver_match
                # 在request上设置匹配到的ResolverMatch
                request.resolver_match = resolver_match

                # Apply view middleware
                # 应用视图方法的中间件
                for middleware_method in self._view_middleware:
                    response = middleware_method(request, callback, callback_args, callback_kwargs)
                    if response:
                        break

            # TODO:make_view_atomic是什么
            # 调用视图方法，如果调用过程中抛出异常，则使用异常中间件
            if response is None:
                wrapped_callback = self.make_view_atomic(callback)
                try:
                    response = wrapped_callback(request, *callback_args, **callback_kwargs)
                except Exception as e:
                    # If the view raised an exception, run it through exception
                    # middleware, and if the exception middleware returns a
                    # response, use that. Otherwise, reraise the exception.
                    for middleware_method in self._exception_middleware:
                        response = middleware_method(request, e)
                        if response:
                            break
                    if response is None:
                        raise

            # Complain if the view returned None (a common error).
            # 视图函数不应该返回None
            if response is None:
                if isinstance(callback, types.FunctionType):  # FBV
                    view_name = callback.__name__
                else:  # CBV
                    view_name = callback.__class__.__name__ + '.__call__'
                raise ValueError("The view %s.%s didn't return an HttpResponse object. It returned None instead."
                                 % (callback.__module__, view_name))

            # If the response supports deferred rendering, apply template
            # response middleware and then render the response
            # 如果响应支持延迟的渲染,那么就使用模板响应中间件然后渲染响应.
            if hasattr(response, 'render') and callable(response.render):
                for middleware_method in self._template_response_middleware:
                    response = middleware_method(request, response)
                    # Complain if the template response middleware returned None (a common error).
                    if response is None:
                        raise ValueError(
                            "%s.process_template_response didn't return an "
                            "HttpResponse object. It returned None instead."
                            % (middleware_method.__self__.__class__.__name__))
                response = response.render()

        # 可能在resolve的过程中抛出的
        except http.Http404 as e:
            logger.warning('Not Found: %s', request.path,
                           extra={
                               'status_code': 404,
                               'request': request
                           })
            if settings.DEBUG:
                response = debug.technical_404_response(request, e)
            else:
                response = self.get_exception_response(request, resolver, 404)
        # 权限中间件可以使用
        except PermissionDenied:
            logger.warning(
                'Forbidden (Permission denied): %s', request.path,
                extra={
                    'status_code': 403,
                    'request': request
                })
            response = self.get_exception_response(request, resolver, 403)

        # TODO
        except MultiPartParserError:
            logger.warning(
                'Bad request (Unable to parse request body): %s', request.path,
                extra={
                    'status_code': 400,
                    'request': request
                })
            response = self.get_exception_response(request, resolver, 400)
        # 可疑的操作
        except SuspiciousOperation as e:
            # The request logger receives events for any problematic request
            # The security logger receives events for all SuspiciousOperations
            # request logger就是本模块的全局logger
            security_logger = logging.getLogger('django.security.%s' %
                                                e.__class__.__name__)
            security_logger.error(
                force_text(e),
                extra={
                    'status_code': 400,
                    'request': request
                })
            if settings.DEBUG:
                return debug.technical_500_response(request, *sys.exc_info(), status_code=400)

            response = self.get_exception_response(request, resolver, 400)

        except SystemExit:
            # Allow sys.exit() to actually exit. See tickets #1023 and #4701
            raise

        except:  # Handle everything else.
            # Get the exception info now, in case another exception is thrown later.
            # 把信号发送给出去
            signals.got_request_exception.send(sender=self.__class__, request=request)
            response = self.handle_uncaught_exception(request, resolver, sys.exc_info())

        try:
            # Apply response middleware, regardless of the response
            # 依次执行响应中间件,如果此时发生异常,则会视为500异常
            for middleware_method in self._response_middleware:
                response = middleware_method(request, response)
                # Complain if the response middleware returned None (a common error).
                if response is None:
                    raise ValueError(
                        "%s.process_response didn't return an "
                        "HttpResponse object. It returned None instead."
                        % (middleware_method.__self__.__class__.__name__))
            # 执行完之后执行response_fixes
            response = self.apply_response_fixes(request, response)
        except:  # Any exception should be gathered and handled
            signals.got_request_exception.send(sender=self.__class__, request=request)
            response = self.handle_uncaught_exception(request, resolver, sys.exc_info())

        # 这个是什么,是添加可关闭的对象,然后wsgi会关闭这些对象吗,request方法的close好像是关闭使用的文件
        response._closable_objects.append(request)

        return response

    def handle_uncaught_exception(self, request, resolver, exc_info):
        """
        Processing for any otherwise uncaught exceptions (those that will
        generate HTTP 500 responses). Can be overridden by subclasses who want
        customised 500 handling.

        Be *very* careful when overriding this because the error could be
        caused by anything, so assuming something like the database is always
        available would be an error.
        """
        # 设置了DEBUG_PROPAGATE_EXCEPTIONS之后,会自动抛出异常,不传播但是开启DEBUG模式会返回异常页面,否则使用HTTP500handler
        if settings.DEBUG_PROPAGATE_EXCEPTIONS:
            raise

        # 日志记录，严重的５００错误
        logger.error('Internal Server Error: %s', request.path,
                     exc_info=exc_info,
                     extra={
                         'status_code': 500,
                         'request': request
                     }
                     )

        if settings.DEBUG:
            return debug.technical_500_response(request, *exc_info)

        # If Http500 handler is not installed, re-raise last exception
        # 如果resolver没有module,那么抛出异常,否则使用500handler
        if resolver.urlconf_module is None:
            six.reraise(*exc_info)
        # Return an HttpResponse that displays a friendly error message.
        callback, param_dict = resolver.resolve_error_handler(500)
        return callback(request, **param_dict)

    def apply_response_fixes(self, request, response):
        """
        Applies each of the functions in self.response_fixes to the request and
        response, modifying the response in the process. Returns the new
        response.
        """
        for func in self.response_fixes:
            response = func(request, response)
        return response
