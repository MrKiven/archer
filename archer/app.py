# -*- coding: utf-8 -*-

import os
import threading
import time
import functools
import sys
import thriftpy
from thriftpy.protocol import TBinaryProtocolFactory
from thriftpy.rpc import make_server
from thriftpy.server import TThreadedServer
from thriftpy.thrift import TProcessor
from thriftpy.transport import TBufferedTransportFactory, TServerSocket

from .config import ConfigAttribute
from .observer import before_api_call, after_api_call, tear_down_api_call
from ._server import run_simple
from .logger import log


class ApiMeta(object):
    def __init__(self, app, name, f, args, kwargs):
        self.app = app
        self.name = name
        self.f = f
        self.args = args
        self.kwargs = kwargs
        self.start_time = time.time()


class APIResultMeta(object):
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.end_time = time.time()


class Archer(object):
    debug = ConfigAttribute('DEBUG')
    testing = ConfigAttribute('TESTING')
    logger_name = ConfigAttribute('LOGGER_NAME')
    default_config = {}

    def __init__(self, name, thrift_file, service):
        # if not os.path.exists(thrift_file):
        # raise
        thrift_module = thriftpy.load(thrift_file)

        self.thrift_file = thrift_file
        self.service = getattr(thrift_module, service)
        self.app = TProcessor(getattr(thrift_module, service), self)

        self.default_error_handler = None

        self.before_api_call_funcs = []

        self.after_api_call_funcs = []

        self.tear_down_api_funcs = []

        self.extensions = {}

        self.error_handlers = {}

        self.api_map = {}

    def run(self, host='127.0.0.1', port=6000, use_reloader=True, **options):

        run_simple(host, port, self, extra_files=[self.thrift_file],
                   use_reloader=use_reloader, **options)


    def errorhandler(self, exception):
        def decorator(f):
            self.register_error_handler(exception, f)
            return f

        return decorator

    def register_error_handler(self, exception, f):
        assert exception not in (Exception, BaseException), ValueError(
            "Please Register with `register_default_exc_handler`")
        if exception in self.exception_handlers:
            existing = self.exception_handlers[exception]
            raise KeyError("Handler (%s) already registered in %s" % (
                exception, existing))
        self.error_handlers[exception] = f

    def before_api_call(self, f):
        self.before_api_call_funcs.append(f)
        return f

    def after_api_call(self, f):
        self.after_api_call_funcs.append(f)
        return f

    def tear_down_api_call(self, f):
        self.teardown_api_funcs.append(f)
        return f

    def api(self, f=None, name=None, **kwargs):
        if f is None:
            return functools.partial(self.api, name=name, **kwargs)

        self.register_api(name or f.func_name, f)
        return f

    def register_api(self, name, f):
        if name in self.api_map:
            raise KeyError("Api_name(%s) already registered in %s" % (
                name, self.api_map[name].__module__
            ))
        self.api_map[name] = self.wrap_api(name, f)

    def preprocess_api(self, api_meta):
        for handler in self.before_api_call_funcs:
            ret_val = handler(api_meta)
            if ret_val is not None:
                return ret_val

    def postprocess_api(self, api_meta, result_meta):
        for handler in self.after_api_call_funcs:
            handler(api_meta, result_meta)

    def tear_down_api(self, api_meta, result_meta):
        for handler in self.tear_down_api_funcs:
            handler(api_meta, result_meta)

    def wrap_api(self, name, f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            api_meta = ApiMeta(self, name, f, args, kwargs)
            before_api_call.notify(api_meta)
            ret_val = self.preprocess_api(api_meta)
            if ret_val is not None:
                return ret_val
            try:
                ret_val = f(*args, **kwargs)
            except Exception as e:
                result_meta = APIResultMeta(error=e)
                for exc_type, handler in self.error_handlers.iteritems():
                    if isinstance(e, exc_type):
                        return handler(api_meta, result_meta)
                return self.handle_uncaught_exception(api_meta, result_meta)
            else:
                result_meta = APIResultMeta(ret_val)
                after_api_call.notify(api_meta, result_meta)
                self.postprocess_api(api_meta, result_meta)
                return ret_val
            finally:
                tear_down_api_call.notify(api_meta, result_meta)
                self.tear_down_api(api_meta, result_meta)

        return wrapper

    def handle_uncaught_exception(self, api_meta, result_info):
        if self.default_error_handler is not None:
            return self.default_error_handler(api_meta, result_info)
        raise

    def processor(self, iprot, oprot):
        return self.app.processor(iprot, oprot)

    def __getattr__(self, name):
        if name not in self.api_map:
            raise AttributeError("Api {} not registered".format(name))
        return self.api_map[name]
