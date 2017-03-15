"""
Cross Site Request Forgery Middleware.

This module provides a middleware that implements protection
against request forgeries from other sites.
"""
from __future__ import unicode_literals

import logging
import re

from django.conf import settings
from django.core.urlresolvers import get_callable
from django.utils.cache import patch_vary_headers
from django.utils.crypto import constant_time_compare, get_random_string
from django.utils.encoding import force_text
from django.utils.http import same_origin

logger = logging.getLogger('django.request')

REASON_NO_REFERER = "Referer checking failed - no Referer."
REASON_BAD_REFERER = "Referer checking failed - %s does not match %s."
REASON_NO_CSRF_COOKIE = "CSRF cookie not set."
REASON_BAD_TOKEN = "CSRF token missing or incorrect."

CSRF_KEY_LENGTH = 32


# ########
# # CSRF #
# ########
#
# # Dotted path to callable to be used as view when a request is
# # rejected by the CSRF middleware.
# CSRF_FAILURE_VIEW = 'django.views.csrf.csrf_failure'
#
# # Settings for CSRF cookie.
# CSRF_COOKIE_NAME = 'csrftoken'
# CSRF_COOKIE_AGE = 60 * 60 * 24 * 7 * 52
# CSRF_COOKIE_DOMAIN = None
# CSRF_COOKIE_PATH = '/'
# CSRF_COOKIE_SECURE = False
# CSRF_COOKIE_HTTPONLY = False

def _get_failure_view():
    """
    返回CSRF验证失败的视图
    Returns the view to be used for CSRF rejections
    """
    return get_callable(settings.CSRF_FAILURE_VIEW)


def _get_new_csrf_key():
    """生成新的CSRF值,使用了random,基于SECRET,且全部是数字和字母"""
    return get_random_string(CSRF_KEY_LENGTH)


def get_token(request):
    """
    Returns the CSRF token required for a POST form. The token is an
    alphanumeric value.

    A side effect of calling this function is to make the csrf_protect
    decorator and the CsrfViewMiddleware add a CSRF cookie and a 'Vary: Cookie'
    header to the outgoing response.  For this reason, you may need to use this
    function lazily, as is done by the csrf context processor.
    """
    request.META["CSRF_COOKIE_USED"] = True
    return request.META.get("CSRF_COOKIE", None)


def rotate_token(request):
    """
    Changes the CSRF token in use for a request - should be done on login
    for security purposes.

    循环利用,但是改变了token值,为什么要改变呢
    # TODO:假如攻击者获得了之前的csrftoken,那么用户登录之后,他获得的就失效,这就是rotate的意义吗?rotate至少可以避免这种情况...
    """
    request.META.update({
        "CSRF_COOKIE_USED": True,
        # 这个其实作为响应中csrftoken的值
        "CSRF_COOKIE": _get_new_csrf_key(),
    })


def _sanitize_token(token):
    # Allow only alphanum,只允许数字,字母
    if len(token) > CSRF_KEY_LENGTH:
        return _get_new_csrf_key()
    token = re.sub('[^a-zA-Z0-9]+', '', force_text(token))
    if token == "":
        # In case the cookie has been truncated to nothing at some point.
        return _get_new_csrf_key()
    return token


class CsrfViewMiddleware(object):
    """
    Middleware that requires a present and correct csrfmiddlewaretoken
    for POST requests that have a CSRF cookie, and sets an outgoing
    CSRF cookie.

    This middleware should be used in conjunction with the csrf_token template
    tag.
    """

    # The _accept and _reject methods currently only exist for the sake of the
    # requires_csrf_token decorator.
    # 接受主要是设置了csrf_process_done
    def _accept(self, request):
        # Avoid checking the request twice by adding a custom attribute to
        # request.  This will be relevant when both decorator and middleware
        # are used.
        request.csrf_processing_done = True
        return None

    def _reject(self, request, reason):
        logger.warning('Forbidden (%s): %s', reason, request.path,
                       extra={
                           'status_code': 403,
                           'request': request,
                       }
                       )
        # reason参数
        return _get_failure_view()(request, reason=reason)

    # TODO:如果攻击者盗取了Cookie,好像就可以构造middlewaretoken或HTTP_X_CSRFTOKEN绕过CSRF机制了
    def process_view(self, request, callback, callback_args, callback_kwargs):
        # 如果请求里面设置了csrf_done,则没有任何处理,一旦这个设置了,那么整个中间件都
        # 不会产生作用
        if getattr(request, 'csrf_processing_done', False):
            return None

        try:
            # 从cookie里面拿出之前设置的cookie,将其净化
            csrf_token = _sanitize_token(
                request.COOKIES[settings.CSRF_COOKIE_NAME])
            # Use same token next time
            request.META['CSRF_COOKIE'] = csrf_token
        # 没有设置过,则在request中设置一个CSRF_COOKIE,这次请求必须是一个GET请求.
        # 如果开发的网站使用GET,HEAD等请求更新数据库(不符合规范),那么CSRF中间层不会
        # 正确的验证,因此绝对不要这样.
        except KeyError:
            csrf_token = None
            # Generate token and store it in the request, so it's
            # available to the view.
            request.META["CSRF_COOKIE"] = _get_new_csrf_key()

        # TODO:设置了就跳过这里的处理,下面的注释的意思
        # Wait until request.META["CSRF_COOKIE"] has been manipulated before
        # bailing out, so that get_token still works
        if getattr(callback, 'csrf_exempt', False):
            return None

        # Assume that anything not defined as 'safe' by RFC2616 needs protection
        if request.method not in ('GET', 'HEAD', 'OPTIONS', 'TRACE'):
            # 如果请求中设置了不要开启csrf检查则直接ok,应该是其他中间件可以控制的,如
            # 果这个设置了,中间件只是处理response验证.以及设置个Cookie
            if getattr(request, '_dont_enforce_csrf_checks', False):
                # Mechanism to turn off CSRF checks for test suite.
                # It comes after the creation of CSRF cookies, so that
                # everything else continues to work exactly the same
                # (e.g. cookies are sent, etc.), but before any
                # branches that call reject().
                return self._accept(request)

            # 如果使用HTTPS,那么必须设置了refer并且和目标HOST一致.
            if request.is_secure():
                # Suppose user visits http://example.com/
                # An active network attacker (man-in-the-middle, MITM) sends a
                # POST form that targets https://example.com/detonate-bomb/ and
                # submits it via JavaScript.
                #
                # The attacker will need to provide a CSRF cookie and token, but
                # that's no problem for a MITM and the session-independent
                # nonce we're using. So the MITM can circumvent the CSRF
                # protection. This is true for any HTTP connection, but anyone
                # using HTTPS expects better! For this reason, for
                # https://example.com/ we need additional protection that treats
                # http://example.com/ as completely untrusted. Under HTTPS,
                # Barth et al. found that the Referer header is missing for
                # same-domain requests in only about 0.2% of cases or less, so
                # we can use strict Referer checking.
                referer = force_text(
                    request.META.get('HTTP_REFERER'),
                    strings_only=True,
                    errors='replace'
                )
                if referer is None:
                    return self._reject(request, REASON_NO_REFERER)

                # Note that request.get_host() includes the port.
                good_referer = 'https://%s/' % request.get_host()
                if not same_origin(referer, good_referer):
                    reason = REASON_BAD_REFERER % (referer, good_referer)
                    return self._reject(request, reason)

            # 没有csrf_token则拒绝,也就是说对网站的第一个请求必须是get,因为之后会设置csrf.
            if csrf_token is None:
                # No CSRF cookie. For POST requests, we insist on a CSRF cookie,
                # and in this way we can avoid all CSRF attacks, including login
                # CSRF.
                return self._reject(request, REASON_NO_CSRF_COOKIE)

            # Check non-cookie token for match.
            # 拿到其中的另一个csrf设置,对于POST,是csrfmt,对于其他,是HTTP_X_CSRFTOKEN
            # 判断这两个和POST中的是否一致.  可以POST.因为存在cookie中,而其他程序拿不到cookie值
            # 能发送但是不能拿到,然后在加上另外的值其他程序就没办法了
            request_csrf_token = ""
            if request.method == "POST":
                try:
                    # 是Django模板系统设置的.
                    request_csrf_token = request.POST.get('csrfmiddlewaretoken',
                                                          '')
                except IOError:
                    # Handle a broken connection before we've completed reading
                    # the POST data. process_view shouldn't raise any
                    # exceptions, so we'll ignore and serve the user a 403
                    # (assuming they're still listening, which they probably
                    # aren't because of the error).
                    pass

            if request_csrf_token == "":
                # Fall back to X-CSRFToken, to make things easier for AJAX,
                # and possible for PUT/DELETE.
                # TODO: 是哪里设置的?:视图,模板等设置的.
                request_csrf_token = request.META.get('HTTP_X_CSRFTOKEN', '')
            if not constant_time_compare(request_csrf_token, csrf_token):
                return self._reject(request, REASON_BAD_TOKEN)

        return self._accept(request)

    def process_response(self, request, response):
        # 同上
        if getattr(response, 'csrf_processing_done', False):
            return response

        # 如果没有cookie,那么说明直接跳过了process_view,因此response也不处理
        # If CSRF_COOKIE is unset, then CsrfViewMiddleware.process_view was
        # never called, probably because a request middleware returned a
        # response (for example, contrib.auth redirecting to a login page).
        if request.META.get("CSRF_COOKIE") is None:
            return response

        # 这个值只会被rotate_token和get_token设置,因此必须掉用过这两个,否则跳过了
        # response的处理
        if not request.META.get("CSRF_COOKIE_USED", False):
            return response

        # Set the CSRF cookie even if it's already set, so we renew
        # the expiry timer.
        response.set_cookie(settings.CSRF_COOKIE_NAME,
                            request.META["CSRF_COOKIE"],
                            max_age=settings.CSRF_COOKIE_AGE,
                            domain=settings.CSRF_COOKIE_DOMAIN,
                            path=settings.CSRF_COOKIE_PATH,
                            secure=settings.CSRF_COOKIE_SECURE,
                            httponly=settings.CSRF_COOKIE_HTTPONLY
                            )
        # Content varies with the CSRF cookie, so set the Vary header.
        patch_vary_headers(response, ('Cookie',))
        response.csrf_processing_done = True
        return response
