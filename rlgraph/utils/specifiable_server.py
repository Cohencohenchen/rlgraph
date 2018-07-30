# Copyright 2018 The YARL-Project, All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import multiprocessing
import tensorflow as tf

from rlgraph import get_backend, RLGraphError
from rlgraph.spaces.space import Space
from rlgraph.utils.util import force_list, dtype


class SpecifiableServer(object):
    """
    A class that creates a separate python process ("server") which runs an arbitrary Specifiable object
    (wrapped as a `SpaceInfoCarrier` object to ascertain an API to get Space- and dtype-specs for
    the Specifiable).

    This is useful - for example - to run RLgraph Environments (which are Specifiables) in a highly parallelized and
    in-graph fashion for faster Agent-Environment stepping.
    """

    COLLECTION = "rlgraph_specifiable_server"

    def __init__(self, class_, spec, output_spaces, shutdown_method=None):
        """
        Args:
            class_ (type): The class to use for constructing the Specifiable from spec. This class needs to be
                a child class of Specifiable (with a __lookup_classes__ property).
            spec (dict): The specification dict that will be used to construct the Specifiable.
            output_spaces (Union[callable,Dict[str,Space]]): A callable that takes a method_name as argument
                and returns the Space(s) that this method (on the Specifiable object) would return. Alternatively:
                A dict with key=method name and value=Space(s).
            shutdown_method (Optional[str]): An optional name of a shutdown method that will be called on the
                Specifiable object before "server" shutdown to give the Specifiable a chance to clean up.
                The Specifiable must implement this method.
        """
        self.class_ = class_
        self.spec = spec
        # If dict: Process possible specs so we don't have to do this during calls.
        if isinstance(output_spaces, dict):
            self.output_spaces = dict()
            for method_name, space_spec in output_spaces.items():
                space_spec_list = force_list(space_spec)
                self.output_spaces[method_name] = [Space.from_spec(spec) if spec is not None else
                                                   None for spec in space_spec_list]
        else:
            self.output_spaces = output_spaces
        self.shutdown_method = shutdown_method

        # The process in which the Specifiable will run.
        self.process = None
        # The out-pipe to send commands (method calls) to the server process.
        self.out_pipe = None
        # The in-pipe to receive "ready" signal from the server process.
        self.in_pipe = None

        # Register this process in a special collection so we can shut it down once the tf.Session ends
        # (via session hook).
        if get_backend() == "tf":
            tf.add_to_collection(SpecifiableServer.COLLECTION, self)

    def __getattr__(self, method_name):
        """
        Returns a function that will create a server-call (given method_name must be one of the Specifiable object)
        from within the backend-specific graph.

        Args:
            method_name (str): The method to call on the Specifiable.
            #return_slots (Optional[List[int]]): An optional list of return slots to use. None for using all return
            #    values.

        Returns:
            callable: The callable to be executed when getting the given method name (of the Specifiable object
                (running inside the SpecifiableServer).
        """
        def call(*args):
            if isinstance(self.output_spaces, dict):
                specs = self.output_spaces[method_name]
            else:
                specs = self.output_spaces(method_name)

            if specs is None:
                raise RLGraphError("No Space information received for method '{}:{}'".format(self.class_.__name__,
                                                                                             method_name))

            dtypes = list()
            shapes = list()
            return_slots = list()
            for i, space in enumerate(specs):
                # Expecting an op (space 0).
                if space == 0:
                    dtypes.append(0)
                    shapes.append(0)
                    return_slots.append(i)
                # Expecting a tensor.
                elif space is not None:
                    # TODO: weird tf bug where floats coming from the py-func are interpreted as tf-doubles and then won't match the Tout types.
                    dt = dtype(space.dtype)
                    dtypes.append(dt if space.dtype != "float" else tf.float64)
                    shapes.append(space.shape)
                    return_slots.append(i)

            if get_backend() == "tf":
                # This function will send the method-call-comment via the out-pipe to the remote (server) Specifiable
                # object - all in-graph - and return the results to be used further by other graph ops.
                def py_call(*args_):
                    try:
                        self.out_pipe.send(args_)
                        result_ = self.out_pipe.recv()
                        # If an error occurred, it'll be passed back through the pipe.
                        if isinstance(result_, Exception):
                            raise result_
                        # Regular result. Filter out the return values according to return_slots.
                        elif isinstance(result_, tuple):  # is not None:
                            return tuple(r for slot, r in enumerate(result_) if slot in return_slots)
                        else:
                            return result_
                    except Exception as e:
                        if isinstance(e, IOError):
                            raise StopIteration()  # Clean exit.
                        else:
                            raise
                results = tf.py_func(py_call, (method_name,) + tuple(args), dtypes, name=method_name)

                # Force known shapes on the returned tensors.
                for result, shape in zip(results, shapes):
                    # Not an op (which have shape=0).
                    if shape != 0:
                        result.set_shape(shape)
            else:
                raise NotImplementedError

            return results

        return call

    def start(self):
        # Create the in- and out- pipes to communicate with the proxy-Specifiable.
        self.out_pipe, self.in_pipe = multiprocessing.Pipe()
        # Create and start the process passing it the spec to construct the desired Specifiable object..
        self.process = multiprocessing.Process(target=self.run, args=(self.class_, self.spec, self.in_pipe))
        self.process.start()

        # Wait for the "ready" signal (which is None).
        result = self.out_pipe.recv()

        # Check whether there were construction errors.
        if isinstance(result, Exception):
            raise result

    def close(self):  #, session):
        try:
            self.out_pipe.send(None)
            self.out_pipe.close()
        except IOError:
            pass
        self.process.join()

    @staticmethod
    def run(class_, spec, in_pipe, shutdown_method=None):
        proxy_object = None
        try:
            # Construct the Specifiable object.
            proxy_object = class_.from_spec(spec)

            # Send the ready signal (no errors).
            in_pipe.send(None)

            # Start a server-loop waiting for method call requests.
            while True:
                command = in_pipe.recv()

                # "close" signal (None) -> End this process.
                if command is None:
                    # Give the proxy_object a chance to clean up via some `shutdown_method`.
                    if shutdown_method is not None and hasattr(proxy_object, shutdown_method):
                        getattr(proxy_object, shutdown_method)()
                    in_pipe.close()
                    return

                # Call the method with the given args.
                method_name = command[0].decode()  # must decode here as method_name comes in as bytes
                inputs = command[1:]
                results = getattr(proxy_object, method_name)(*inputs)

                # Send return values back to caller.
                in_pipe.send(results)

        # If something happens during the construction and proxy run phase, pass the exception back through our pipe.
        except Exception as e:
            # Try to clean up.
            if proxy_object is not None and shutdown_method is not None and hasattr(proxy_object, shutdown_method):
                try:
                    getattr(proxy_object, shutdown_method)()
                except:
                    pass
            # Send the exception back so the main process knows what's going on.
            in_pipe.send(e)


class SpecifiableServerHook(tf.train.SessionRunHook):
    """
    A hook for a tf.MonitoredSession that takes care of automatically starting and stopping
    SpecifiableServer objects.
    """
    def begin(self):
        """
        Starts all registered RLGraphProxyProcess processes.
        """
        tp = multiprocessing.pool.ThreadPool()
        tp.map(lambda server: server.start(),
               tf.get_collection(SpecifiableServer.COLLECTION))
        tp.close()
        tp.join()

    def end(self, session):
        tp = multiprocessing.pool.ThreadPool()
        tp.map(lambda server: server.close(),
               tf.get_collection(SpecifiableServer.COLLECTION))
        tp.close()
        tp.join()
