# Software License Agreement (BSD License)
#
# Copyright (c) 2012, Willow Garage, Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above
#    copyright notice, this list of conditions and the following
#    disclaimer in the documentation and/or other materials provided
#    with the distribution.
#  * Neither the name of Willow Garage, Inc. nor the names of its
#    contributors may be used to endorse or promote products derived
#    from this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

import sys
import threading
import traceback
import uuid
from collections import deque
from functools import partial, wraps
from typing import Any

import rclpy
from rosbridge_library.rosbridge_protocol import RosbridgeProtocol
from rosbridge_library.util import bson, json
from tornado import version_info as tornado_version_info
from tornado.gen import BadYieldError, coroutine
from tornado.ioloop import IOLoop
from tornado.iostream import StreamClosedError
from tornado.websocket import WebSocketClosedError, WebSocketHandler

from rosbridge_msgs.msg import AuthenticationField
from rosbridge_msgs.srv import Authentication

_io_loop = IOLoop.instance()


def _log_exception():
    """Log the most recent exception to ROS."""
    exc = traceback.format_exception(*sys.exc_info())
    RosbridgeWebSocket.node_handle.get_logger().error("".join(exc))


def log_exceptions(f):
    """Decorator for logging exceptions to ROS."""

    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception:
            _log_exception()
            raise

    return wrapper


class IncomingQueue(threading.Thread):
    """Decouples incoming messages from the Tornado thread.

    This mitigates cases where outgoing messages are blocked by incoming,
    and vice versa.
    """

    def __init__(self, protocol):
        threading.Thread.__init__(self)
        self.daemon = True
        self.queue = deque()
        self.protocol = protocol

        self.cond = threading.Condition()
        self._finished = False

    def finish(self):
        """Clear the queue and do not accept further messages."""
        with self.cond:
            self._finished = True
            while len(self.queue) > 0:
                self.queue.popleft()
            self.cond.notify()

    def push(self, msg):
        with self.cond:
            self.queue.append(msg)
            self.cond.notify()

    def run(self):
        while True:
            with self.cond:
                if len(self.queue) == 0 and not self._finished:
                    self.cond.wait()

                if self._finished:
                    break

                msg = self.queue.popleft()

            self.protocol.incoming(msg)

        self.protocol.finish()


class RosbridgeWebSocket(WebSocketHandler):
    clients_connected = 0
    use_compression = False

    # The following are passed on to RosbridgeProtocol
    # defragmentation.py:
    fragment_timeout = 600  # seconds
    # protocol.py:
    delay_between_messages = 0  # seconds
    max_message_size = 10000000  # bytes
    unregister_timeout = 10.0  # seconds
    bson_only_mode = False
    node_handle = None

    # Optional service name that is called to authenticate each client
    # connection
    authentication_service = None

    @log_exceptions
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        cls = self.__class__
        if self.authentication_service is not None:
            self.auth_client = cls.node_handle.create_client(
                Authentication, self.authentication_service
            )
            if not self.auth_client.wait_for_service(timeout_sec=5.0):
                cls.node_handle.get_logger().warn(
                    "Authentication service %s not available" % self.authentication_service
                )

    @log_exceptions
    def open(self):
        cls = self.__class__
        parameters = {
            "fragment_timeout": cls.fragment_timeout,
            "delay_between_messages": cls.delay_between_messages,
            "max_message_size": cls.max_message_size,
            "unregister_timeout": cls.unregister_timeout,
            "bson_only_mode": cls.bson_only_mode,
        }
        try:
            self.client_id = uuid.uuid4()
            self.protocol = RosbridgeProtocol(
                self.client_id, cls.node_handle, parameters=parameters
            )
            self.incoming_queue = IncomingQueue(self.protocol)
            self.incoming_queue.start()
            self.protocol.outgoing = self.send_message
            self.authenticated = False
            self.set_nodelay(True)
            self._write_lock = threading.RLock()
            cls.clients_connected += 1
            if cls.client_manager:
                cls.client_manager.add_client(self.client_id, self.request.remote_ip)
        except Exception as exc:
            cls.node_handle.get_logger().error(
                f"Unable to accept incoming connection.  Reason: {exc}"
            )

        cls.node_handle.get_logger().info(
            f"Client connected. {cls.clients_connected} clients total."
        )

    @log_exceptions
    def on_message(self, message):
        cls = self.__class__
        if isinstance(message, bytes):
            message = message.decode("utf-8")
        if not message.strip():
            return
        msg = json.loads(message)
        # check if we need to authenticate
        failure = None
        if self.authenticated or self.authentication_service is None:
            # no authentication required
            if msg["op"] != "auth":
                self.incoming_queue.push(message)
        elif msg["op"] == "auth":
            if self.check_authentication(message):
                self.authenticated = True
            else:
                failure = "Authentication failed for client: %s" % (self.client_id)
        else:
            failure = "Authentication required for client: %s" % (self.client_id)
        if failure is not None:
            cls.node_handle.get_logger().warn(failure)
            self.close()

    @log_exceptions
    def on_close(self):
        cls = self.__class__
        cls.clients_connected -= 1
        if cls.client_manager:
            cls.client_manager.remove_client(self.client_id, self.request.remote_ip)
        cls.node_handle.get_logger().info(
            f"Client disconnected. {cls.clients_connected} clients total."
        )
        self.incoming_queue.finish()

    def send_message(self, message):
        if isinstance(message, bson.BSON):
            binary = True
        elif isinstance(message, bytearray):
            binary = True
            message = bytes(message)
        else:
            binary = False

        with self._write_lock:
            _io_loop.add_callback(partial(self.prewrite_message, message, binary))

    @coroutine
    def prewrite_message(self, message, binary):
        cls = self.__class__
        # Use a try block because the log decorator doesn't cooperate with @coroutine.
        try:
            with self._write_lock:
                future = self.write_message(message, binary)

                # When closing, self.write_message() return None even if it's an undocument output.
                # Consider it as WebSocketClosedError
                # For tornado versions <4.3.0 self.write_message() does not have a return value
                if future is None and tornado_version_info >= (4, 3, 0, 0):
                    raise WebSocketClosedError

                yield future
        except WebSocketClosedError:
            cls.node_handle.get_logger().warn(
                "WebSocketClosedError: Tried to write to a closed websocket",
                throttle_duration_sec=1.0,
            )
            raise
        except StreamClosedError:
            cls.node_handle.get_logger().warn(
                "StreamClosedError: Tried to write to a closed stream",
                throttle_duration_sec=1.0,
            )
            raise
        except BadYieldError:
            # Tornado <4.5.0 doesn't like its own yield and raises BadYieldError.
            # This does not affect functionality, so pass silently only in this case.
            if tornado_version_info < (4, 5, 0, 0):
                pass
            else:
                _log_exception()
                raise
        except:  # noqa: E722  # Will log and raise
            _log_exception()
            raise

    @log_exceptions
    def check_origin(self, origin):
        return True

    @log_exceptions
    def check_authentication(self, message) -> bool:
        cls = self.__class__
        msg = json.loads(message)
        if msg["op"] != "auth":
            return False
        authenticated = False
        # check the authorization information
        auth_req = Authentication.Request(
            client_connection_id=str(self.client_id), remote_ip=self.request.remote_ip
        )
        if "fields" in msg and isinstance(msg["fields"], dict):
            for k, v in msg["fields"].items():
                auth_req.fields.append(AuthenticationField(name=k, value=v))
        try:
            auth_future = self.auth_client.call_async(auth_req)
            rclpy.spin_until_future_complete(cls.node_handle, auth_future, timeout_sec=90.0)
        except Exception as e:
            cls.node_handle.get_logger().error(
                "Authentication service call to %s failed %r" % (self.authentication_service, e)
            )
        else:
            if auth_future.done():
                auth_response = auth_future.result()
                authenticated = auth_response.authenticated
            else:
                cls.node_handle.get_logger().error(
                    "Authentication service call timed out while waiting for response from %s"
                    % self.authentication_service
                )
        return authenticated

    @log_exceptions
    def get_compression_options(self):
        # If this method returns None (the default), compression will be disabled.
        # If it returns a dict (even an empty one), it will be enabled.
        cls = self.__class__

        if not cls.use_compression:
            return None

        return {}
