import django
from django.core.handlers.wsgi import WSGIHandler


def get_wsgi_application():
    """
    The public interface to Django's WSGI support. Should return a WSGI
    callable.

    Allows us to avoid making django.core.handlers.WSGIHandler public API, in
    case the internal WSGI implementation changes or moves in the future.

    NOTE: 包装一下WSGIHandler,从而方便以后变动.
    """
    django.setup()
    return WSGIHandler()
