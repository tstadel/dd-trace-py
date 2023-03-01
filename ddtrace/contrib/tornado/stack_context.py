import tornado
from tornado.ioloop import IOLoop

from ...provider import BaseContextProvider
from ...provider import DefaultContextProvider
from ...span import Span


# tornado.stack_context deprecated in Tornado 5 removed in Tornado 6
# instead use DefaultContextProvider with ContextVarContextManager for asyncio
_USE_STACK_CONTEXT = not tornado.version_info >= (5, 0)

if _USE_STACK_CONTEXT:
    from tornado.stack_context import StackContextInconsistentError
    from tornado.stack_context import _state

    class TracerStackContext(DefaultContextProvider):
        """
        A context manager that manages ``Context`` instances in a thread-local state.
        It must be used every time a Tornado's handler or coroutine is used within a
        tracing Context. It is meant to work like a traditional ``StackContext``,
        preserving the state across asynchronous calls.

        Every time a new manager is initialized, a new ``Context()`` is created for
        this execution flow. A context created in a ``TracerStackContext`` is not
        shared between different threads.

        This implementation follows some suggestions provided here:
        https://github.com/tornadoweb/tornado/issues/1063
        """

        def __init__(self):
            # type: (...) -> None
            # HACK(jd): this should be using super(), but calling DefaultContextProvider.__init__
            # sets the context to `None` which breaks this code.
            # We therefore skip DefaultContextProvider.__init__ and call only BaseContextProvider.__init__.
            BaseContextProvider.__init__(self)
            self._context = None

        def enter(self):
            """
            Required to preserve the ``StackContext`` protocol.
            """
            pass

        def exit(self, type, value, traceback):  # noqa: A002
            """
            Required to preserve the ``StackContext`` protocol.
            """
            pass

        def __enter__(self):
            self.old_contexts = _state.contexts
            self.new_contexts = (self.old_contexts[0] + (self,), self)
            _state.contexts = self.new_contexts
            return self

        def __exit__(self, type, value, traceback):  # noqa: A002
            final_contexts = _state.contexts
            _state.contexts = self.old_contexts

            if final_contexts is not self.new_contexts:
                raise StackContextInconsistentError(
                    "stack_context inconsistency (may be caused by yield " 'within a "with TracerStackContext" block)'
                )

            # break the reference to allow faster GC on CPython
            self.new_contexts = None

        def _has_io_loop(self):
            """Helper to determine if we are currently in an IO loop"""
            return getattr(IOLoop._current, "instance", None) is not None

        def _has_active_context(self):
            """Helper to determine if we have an active context or not"""
            if not self._has_io_loop():
                return super(TracerStackContext, self)._has_active_context()
            else:
                # we're inside a Tornado loop so the TracerStackContext is used
                return self._get_state_active_context() is not None

        def _get_state_active_context(self):
            """Helper to get the currently active context from the TracerStackContext"""
            # we're inside a Tornado loop so the TracerStackContext is used
            for stack in reversed(_state.contexts[0]):
                if isinstance(stack, self.__class__):
                    ctx = stack._context
                    if isinstance(ctx, Span):
                        return self._update_active(ctx)
                    return ctx
            return None

        def active(self):
            """
            Return the ``Context`` from the current execution flow. This method can be
            used inside a Tornado coroutine to retrieve and use the current tracing context.
            If used in a separated Thread, the `_state` thread-local storage is used to
            propagate the current Active context from the `MainThread`.
            """
            if not self._has_io_loop():
                # if a Tornado loop is not available, it means that this method
                # has been called from a synchronous code, so we can rely in a
                # thread-local storage
                return super(TracerStackContext, self).active()
            else:
                # we're inside a Tornado loop so the TracerStackContext is used
                return self._get_state_active_context()

        def activate(self, ctx):
            """
            Set the active ``Context`` for this async execution. If a ``TracerStackContext``
            is not found, the context is discarded.
            If used in a separated Thread, the `_state` thread-local storage is used to
            propagate the current Active context from the `MainThread`.
            """
            if not self._has_io_loop():
                # because we're outside of an asynchronous execution, we store
                # the current context in a thread-local storage
                super(TracerStackContext, self).activate(ctx)
            else:
                # we're inside a Tornado loop so the TracerStackContext is used
                for stack_ctx in reversed(_state.contexts[0]):
                    if isinstance(stack_ctx, self.__class__):
                        stack_ctx._context = ctx
            return ctx


else:
    # no-op when not using stack_context
    class TracerStackContext(DefaultContextProvider):
        def __enter__(self):
            pass

        def __exit__(self, *exc):
            pass


def run_with_trace_context(func, *args, **kwargs):
    """
    Run the given function within a traced StackContext. This function is used to
    trace Tornado web handlers, but can be used in your code to trace coroutines
    execution.
    """
    with TracerStackContext():
        return func(*args, **kwargs)
