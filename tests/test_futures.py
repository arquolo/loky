import time
import pytest
import threading
import loky._base as futures
from loky._base import (PENDING, RUNNING, CANCELLED, CANCELLED_AND_NOTIFIED,
                        FINISHED, Future)


def create_future(state=PENDING, exception=None, result=None):
    f = Future()
    f._state = state
    f._exception = exception
    f._result = result
    return f


PENDING_FUTURE = create_future(state=PENDING)
RUNNING_FUTURE = create_future(state=RUNNING)
CANCELLED_FUTURE = create_future(state=CANCELLED)
CANCELLED_AND_NOTIFIED_FUTURE = create_future(state=CANCELLED_AND_NOTIFIED)
EXCEPTION_FUTURE = create_future(state=FINISHED, exception=OSError())
SUCCESSFUL_FUTURE = create_future(state=FINISHED, result=42)


@pytest.yield_fixture
def exit_on_deadlock():
    try:
        TIMEOUT = 5
        from faulthandler import dump_traceback_later
        from faulthandler import cancel_dump_traceback_later
        from sys import stderr
        dump_traceback_later(timeout=TIMEOUT, exit=True, file=stderr)
        yield
        cancel_dump_traceback_later()
    except ImportError:
        yield


class TestsFuture:
    def test_done_callback_with_result(self):
        callback_result = [None]

        def fn(callback_future):
            callback_result[0] = callback_future.result()

        f = Future()
        f.add_done_callback(fn)
        f.set_result(5)
        assert 5 == callback_result[0]

    def test_done_callback_with_exception(self):
        callback_exception = [None]

        def fn(callback_future):
            callback_exception[0] = callback_future.exception()

        f = Future()
        f.add_done_callback(fn)
        f.set_exception(Exception('test'))
        assert ('test',) == callback_exception[0].args

    def test_done_callback_with_cancel(self):
        was_cancelled = [None]

        def fn(callback_future):
            was_cancelled[0] = callback_future.cancelled()

        f = Future()
        f.add_done_callback(fn)
        assert f.cancel()
        assert was_cancelled[0]

    # @pytest.mark.skip(reason="Known failure")
    def test_done_callback_raises(self):
        # with captured_stderr() as stderr:
        raising_was_called = [False]
        fn_was_called = [False]

        def raising_fn(callback_future):
            raising_was_called[0] = True
            raise Exception('doh!')

        def fn(callback_future):
            fn_was_called[0] = True

        f = Future()
        f.add_done_callback(raising_fn)
        f.add_done_callback(fn)
        f.set_result(5)
        assert raising_was_called
        assert fn_was_called
        # assert 'Exception: doh!' in stderr.getvalue()

    def test_done_callback_already_successful(self):
        callback_result = [None]

        def fn(callback_future):
            callback_result[0] = callback_future.result()

        f = Future()
        f.set_result(5)
        f.add_done_callback(fn)
        assert 5 == callback_result[0]

    def test_done_callback_already_failed(self):
        callback_exception = [None]

        def fn(callback_future):
            callback_exception[0] = callback_future.exception()

        f = Future()
        f.set_exception(Exception('test'))
        f.add_done_callback(fn)
        assert ('test',) == callback_exception[0].args

    def test_done_callback_already_cancelled(self):
        was_cancelled = [None]

        def fn(callback_future):
            was_cancelled[0] = callback_future.cancelled()

        f = Future()
        assert f.cancel()
        f.add_done_callback(fn)
        assert was_cancelled[0]

    def test_repr(self, exit_on_deadlock):
        import re
        assert re.match('<Future at 0x[0-9a-f]+ state=pending>',
                        repr(PENDING_FUTURE)).pos > -1
        assert re.match('<Future at 0x[0-9a-f]+ state=running>',
                        repr(RUNNING_FUTURE)).pos > -1
        assert re.match('<Future at 0x[0-9a-f]+ state=cancelled>',
                        repr(CANCELLED_FUTURE)).pos > -1
        assert re.match('<Future at 0x[0-9a-f]+ state=cancelled>',
                        repr(CANCELLED_AND_NOTIFIED_FUTURE)).pos > -1
        assert re.match('<Future at 0x[0-9a-f]+ state=finished raised '
                        'OSError>', repr(EXCEPTION_FUTURE)).pos > -1
        assert re.match('<Future at 0x[0-9a-f]+ state=finished returned int>',
                        repr(SUCCESSFUL_FUTURE)).pos > -1

    def test_cancel(self, exit_on_deadlock):
        f1 = create_future(state=PENDING)
        f2 = create_future(state=RUNNING)
        f3 = create_future(state=CANCELLED)
        f4 = create_future(state=CANCELLED_AND_NOTIFIED)
        f5 = create_future(state=FINISHED, exception=OSError())
        f6 = create_future(state=FINISHED, result=5)

        assert f1.cancel()
        assert f1._state == CANCELLED

        assert not f2.cancel()
        assert f2._state == RUNNING

        assert f3.cancel()
        assert f3._state == CANCELLED

        assert f4.cancel()
        assert f4._state == CANCELLED_AND_NOTIFIED

        assert not f5.cancel()
        assert f5._state == FINISHED

        assert not f6.cancel()
        assert f6._state == FINISHED

    def test_cancelled(self, exit_on_deadlock):
        assert not PENDING_FUTURE.cancelled()
        assert not RUNNING_FUTURE.cancelled()
        assert CANCELLED_FUTURE.cancelled()
        assert CANCELLED_AND_NOTIFIED_FUTURE.cancelled()
        assert not EXCEPTION_FUTURE.cancelled()
        assert not SUCCESSFUL_FUTURE.cancelled()

    def test_done(self, exit_on_deadlock):
        assert not PENDING_FUTURE.done()
        assert not RUNNING_FUTURE.done()
        assert CANCELLED_FUTURE.done()
        assert CANCELLED_AND_NOTIFIED_FUTURE.done()
        assert EXCEPTION_FUTURE.done()
        assert SUCCESSFUL_FUTURE.done()

    def test_running(self, exit_on_deadlock):
        assert not PENDING_FUTURE.running()
        assert RUNNING_FUTURE.running()
        assert not CANCELLED_FUTURE.running()
        assert not CANCELLED_AND_NOTIFIED_FUTURE.running()
        assert not EXCEPTION_FUTURE.running()
        assert not SUCCESSFUL_FUTURE.running()

    def test_result_with_timeout(self, exit_on_deadlock):
        with pytest.raises(futures.TimeoutError):
            PENDING_FUTURE.result(timeout=0)
        with pytest.raises(futures.TimeoutError):
            RUNNING_FUTURE.result(timeout=0)
        with pytest.raises(futures.CancelledError):
            CANCELLED_FUTURE.result(timeout=0)
        with pytest.raises(futures.CancelledError):
            CANCELLED_AND_NOTIFIED_FUTURE.result(timeout=0)
        with pytest.raises(OSError):
            EXCEPTION_FUTURE.result(timeout=0)
        assert SUCCESSFUL_FUTURE.result(timeout=0) == 42

    def test_result_with_success(self, exit_on_deadlock):
        # TODO(brian@sweetapp.com): This test is timing dependent.
        def notification():
            # Wait until the main thread is waiting for the result.
            time.sleep(1)
            f1.set_result(42)

        f1 = create_future(state=PENDING)
        t = threading.Thread(target=notification)
        t.start()

        assert f1.result(timeout=5) == 42

    def test_result_with_cancel(self, exit_on_deadlock):
        # TODO(brian@sweetapp.com): This test is timing dependent.
        def notification():
            # Wait until the main thread is waiting for the result.
            time.sleep(1)
            f1.cancel()

        f1 = create_future(state=PENDING)
        t = threading.Thread(target=notification)
        t.start()

        with pytest.raises(futures.CancelledError):
            f1.result(timeout=5)

    def test_exception_with_timeout(self, exit_on_deadlock):
        with pytest.raises(futures.TimeoutError):
            PENDING_FUTURE.exception(timeout=0)
        with pytest.raises(futures.TimeoutError):
            RUNNING_FUTURE.exception(timeout=0)
        with pytest.raises(futures.CancelledError):
            CANCELLED_FUTURE.exception(timeout=0)
        with pytest.raises(futures.CancelledError):
            CANCELLED_AND_NOTIFIED_FUTURE.exception(timeout=0)
        assert isinstance(EXCEPTION_FUTURE.exception(timeout=0), OSError)
        assert SUCCESSFUL_FUTURE.exception(timeout=0) == None

    def test_exception_with_success(self, exit_on_deadlock):
        def notification():
            # Wait until the main thread is waiting for the exception.
            time.sleep(1)
            with f1._condition:
                f1._state = FINISHED
                f1._exception = OSError()
                f1._condition.notify_all()

        f1 = create_future(state=PENDING)
        t = threading.Thread(target=notification)
        t.start()

        assert isinstance(f1.exception(timeout=5), OSError)
