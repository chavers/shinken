#!/usr/bin/env python

# -*- coding: utf-8 -*-

# Copyright (C) 2009-2012:
#     Gabes Jean, naparuba@gmail.com
#     Gerhard Lausser, Gerhard.Lausser@consol.de
#     Gregory Starck, g.starck@gmail.com
#     Hartmut Goebel, h.goebel@goebel-consult.de
#
# This file is part of Shinken.
#
# Shinken is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Shinken is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with Shinken.  If not, see <http://www.gnu.org/licenses/>.

import select
import errno
import time
import socket
import select
import copy
import cPickle
import inspect
import json
import zlib
import threading
try:
    import ssl
except ImportError:
    ssl = None

try:
    from cherrypy import wsgiserver as cheery_wsgiserver
except ImportError:
    cheery_wsgiserver = None
    
from wsgiref import simple_server

from log import logger

# Let's load bottlecore! :)
from shinken.webui import bottlecore as bottle
bottle.debug(True)



class InvalidWorkDir(Exception):
    pass


class PortNotFree(Exception):
    pass





# CherryPy is allowing us to have a HTTP 1.1 server, and so have a KeepAlive
class CherryPyServer(bottle.ServerAdapter):
    def run(self, handler):  # pragma: no cover
        daemon_thread_pool_size = self.options['daemon_thread_pool_size']
        server = cheery_wsgiserver.CherryPyWSGIServer((self.host, self.port), handler, numthreads=daemon_thread_pool_size, shutdown_timeout=1)
        logger.info('Initializing a CherryPy backend with %d threads' % daemon_thread_pool_size)
        use_ssl = self.options['use_ssl']
        ca_cert = self.options['ca_cert']
        ssl_cert = self.options['ssl_cert']
        ssl_key = self.options['ssl_key']
        
        
        if use_ssl:            
            server.ssl_certificate = ssl_cert
            server.ssl_private_key = ssl_key
        return server

    

class CherryPyBackend(object):
    def __init__(self, host, port, use_ssl, ca_cert, ssl_key, ssl_cert, hard_ssl_name_check, daemon_thread_pool_size):
        self.port = port
        try:
            self.srv = bottle.run(host=host, port=port, server=CherryPyServer, quiet=False, use_ssl=use_ssl, ca_cert=ca_cert, ssl_key=ssl_key, ssl_cert=ssl_cert, daemon_thread_pool_size=daemon_thread_pool_size)
        except socket.error, exp:
            msg = "Error: Sorry, the port %d is not free: %s" % (self.port, str(exp))
            raise PortNotFree(msg)
        except Exception, e:
            # must be a problem with pyro workdir:
            raise InvalidWorkDir(e)

    
    # When call, it do not have a socket
    def get_sockets(self):
        return []


    # We stop our processing, but also try to hard close our socket as cherrypy is not doing it...
    def stop(self):
        try:
            self.srv.stop()
        except Exception, exp:
            logger.warning('Cannot stop the CherryPy backend : %s' % exp)


    # Will run and LOCK
    def run(self):
        try:
            self.srv.start()
        except socket.error, exp:
            msg = "Error: Sorry, the port %d is not free: %s" % (self.port, str(exp))
            raise PortNotFree(msg)
        finally:
            self.srv.stop()


# WSGIRef is the default HTTP server, it CAN manage HTTPS, but at a Huge cost for the client, because it's only HTTP1.0
# so no Keep-Alive, and in HTTPS it's just a nightmare
class WSGIREFAdapter (bottle.ServerAdapter):
    def run (self, handler):
        daemon_thread_pool_size = self.options['daemon_thread_pool_size']
        from wsgiref.simple_server import WSGIRequestHandler
        LoggerHandler = WSGIRequestHandler
        if self.quiet:
            class QuietHandler(WSGIRequestHandler):
                def log_request(*args, **kw): pass
            LoggerHandler = QuietHandler

        srv = simple_server.make_server(self.host, self.port, handler, handler_class=LoggerHandler)
        logger.info('Initializing a wsgiref backend with %d threads' % daemon_thread_pool_size)
        use_ssl = self.options['use_ssl']
        ca_cert = self.options['ca_cert']
        ssl_cert = self.options['ssl_cert']
        ssl_key = self.options['ssl_key']
        
        if use_ssl:
            if not ssl:
                logger.error("Missing python-openssl librairy, please install it to open a https backend")
                raise Exception("Missing python-openssl librairy, please install it to open a https backend")
            srv.socket = ssl.wrap_socket(srv.socket,
                                         keyfile=ssl_key, certfile=ssl_cert, server_side=True)
        return srv


class WSGIREFBackend(object):
    def __init__(self, host, port, use_ssl, ca_cert, ssl_key, ssl_cert, hard_ssl_name_check, daemon_thread_pool_size):
        self.daemon_thread_pool_size = daemon_thread_pool_size
        try:
            self.srv = bottle.run(host=host, port=port, server=WSGIREFAdapter, quiet=False, use_ssl=use_ssl, ca_cert=ca_cert, ssl_key=ssl_key, ssl_cert=ssl_cert, daemon_thread_pool_size=daemon_thread_pool_size)
        except socket.error, exp:
            msg = "Error: Sorry, the port %d is not free: %s" % (port, str(exp))
            raise PortNotFree(msg)
        except Exception, e:
            # must be a problem with pyro workdir:
            raise e


    def get_sockets(self):
        return [self.srv.socket]


    def get_socks_activity(self, socks, timeout):
        try:
            ins, _, _ = select.select(socks, [], [], timeout)
        except select.error, e:
            errnum, _ = e
            if errnum == errno.EINTR:
                return []
            raise
        return ins


    # We are asking us to stop, so we close our sockets
    def stop(self):
        for s in self.get_sockets():
            try:
                s.close()
            except:
                pass
        

    # Manually manage the number of threads
    def run(self):
        # Ok create the thread
        nb_threads = self.daemon_thread_pool_size
        # Keep a list of our running threads
        threads = []
        logger.info('Using a %d http pool size' % nb_threads)
        while True:
            # We must not run too much threads, so we will loop until
            # we got at least one free slot available
            free_slots = 0
            while free_slots <= 0:
                to_del = [t for t in threads if not t.is_alive()]
                _ = [t.join() for t in to_del]
                for t in to_del:
                    threads.remove(t)
                free_slots = nb_threads - len(threads)
                if free_slots <= 0:
                    time.sleep(0.01)
            
            socks = self.get_sockets()
            # Blocking for 0.1 s max here
            ins = self.get_socks_activity(socks, 0.1)
            if len(ins) == 0:  # trivial case: no fd activity:
                continue
            # If we got activity, Go for a new thread!
            for sock in socks:
                if sock in ins:
                    # GO!
                    t = threading.Thread(None, target=self.handle_one_request_thread, name='http-request', args=(sock,))
                    # We don't want to hang the master thread just because this one is still alive
                    t.daemon = True
                    t.start()
                    threads.append(t)
                                        

    def handle_one_request_thread(self, sock):
        self.srv.handle_request()



class HTTPDaemon(object):
        def __init__(self, host, port, http_backend, use_ssl, ca_cert, ssl_key, ssl_cert, hard_ssl_name_check, daemon_thread_pool_size):
            self.port = port
            self.host = host
            # Port = 0 means "I don't want HTTP server"
            if self.port == 0:
                return

            protocol = 'http'
            if use_ssl:
                protocol = 'https'
            self.uri = '%s://%s:%s' % (protocol, self.host, self.port)
            logger.info("Opening HTTP socket at %s" % self.uri)
            
            # Hack the BaseHTTPServer so only IP will be looked by wsgiref, and not names
            __import__('BaseHTTPServer').BaseHTTPRequestHandler.address_string = lambda x:x.client_address[0]

            if http_backend == 'cherrypy' or http_backend == 'auto' and cheery_wsgiserver:
                self.srv = CherryPyBackend(host, port, use_ssl, ca_cert, ssl_key, ssl_cert, hard_ssl_name_check, daemon_thread_pool_size)
            else:
                self.srv = WSGIREFBackend(host, port, use_ssl, ca_cert, ssl_key, ssl_cert, hard_ssl_name_check, daemon_thread_pool_size)
            
            self.lock = threading.RLock()

            #@bottle.error(code=500)
            #def error500(err):
            #    print err.__dict__
            #    return 'FUCKING ERROR 500', str(err)

            
        
        # Get the server socket but not if disabled or closed
        def get_sockets(self):
            if self.port == 0 or self.srv is None:
                return []
            return self.srv.get_sockets()


        def run(self):
            self.srv.run()


        def register(self, obj):
            methods = inspect.getmembers(obj, predicate=inspect.ismethod)
            for (fname, f) in methods:
                # Slip private functions
                if fname.startswith('_'):
                    continue
                # Get the args of the function to catch them in the queries
                argspec = inspect.getargspec(f)
                args = argspec.args
                varargs = argspec.varargs
                keywords = argspec.keywords
                defaults = argspec.defaults
                # remove useless self in args, because we alredy got a bonded method f
                if 'self' in args:
                    args.remove('self')
                print "Registering", fname, args
                # WARNING : we MUST do a 2 levels function here, or the f_wrapper
                # will be uniq and so will link to the last function again
                # and again
                def register_callback(fname, args, f, obj, lock):
                    def f_wrapper():
                        need_lock = getattr(f, 'need_lock', True)
                        
                        # Warning : put the bottle.response set inside the wrapper
                        # because outside it will break bottle
                        d = {}
                        method = getattr(f, 'method', 'get').lower()
                        for aname in args:
                            v = None
                            if method == 'post':
                                v = bottle.request.forms.get(aname, None)
                                # Post args are zlibed and cPickled
                                if v is not None:
                                    v = zlib.decompress(v)
                                    v = cPickle.loads(v)
                            elif method == 'get':
                                v = bottle.request.GET.get(aname, None)
                            if v is None:
                                raise Exception('Missing argument %s' % aname)
                            d[aname] = v
                        if need_lock:
                            logger.debug("HTTP: calling lock for %s" % fname)
                            lock.acquire()

                        ret = f(**d)

                        # Ok now we can release the lock
                        if need_lock:
                            lock.release()

                        encode = getattr(f, 'encode', 'json').lower()
                        j = json.dumps(ret)
                        return j
                    # Ok now really put the route in place
                    bottle.route('/'+fname, callback=f_wrapper, method=getattr(f, 'method', 'get').upper())
                register_callback(fname, args, f, obj, self.lock)

            # Add a simple / page
            def slash():
                return "OK"
            bottle.route('/', callback=slash)


        def unregister(self, obj):
            return


        def handleRequests(self, s):
            self.srv.handle_request()
            
            
        def create_uri(address, port, obj_name, use_ssl=False):
            return "PYRO:%s@%s:%d" % (obj_name, address, port)


        def set_timeout(con, timeout):
            con._pyroTimeout = timeout


        # Close all sockets and delete the server object to be sure
        # no one is still alive
        def shutdown(self):
            self.srv.stop()
            self.srv = None


        def get_socks_activity(self, timeout):
            try:
                ins, _, _ = select.select(self.get_sockets(), [], [], timeout)
            except select.error, e:
                errnum, _ = e
                if errnum == errno.EINTR:
                    return []
                raise
            return ins


daemon_inst = None
