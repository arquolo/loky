###############################################################################
# Basic context management with LokyContext and  provides
# compat for UNIX 2.7 and 3.3
#
# author: Thomas Moreau and Olivier Grisel
#
# adapted from multiprocessing/context.py
#  * Create a context ensuring loky uses only objects that are compatible
#  * Add LokyContext to the list of context of multiprocessing so loky can be
#    used with multiprocessing.set_start_method
#
import os
import sys
import math
import subprocess
import traceback
import warnings
import multiprocessing as mp
from multiprocessing import get_context as mp_get_context
from multiprocessing.context import BaseContext

from .process import LokyProcess, LokyInitMainProcess

START_METHODS = ['loky', 'loky_init_main', 'spawn']
if sys.platform != 'win32':
    START_METHODS += ['fork', 'forkserver']

_DEFAULT_START_METHOD = None

# Cache for the number of physical cores to avoid repeating subprocess calls.
# It should not change during the lifetime of the program.
physical_cores_cache = None


def get_context(method=None):
    # Try to overload the default context
    method = method or _DEFAULT_START_METHOD or "loky"
    if method == "fork":
        # If 'fork' is explicitly requested, warn user about potential issues.
        warnings.warn("`fork` start method should not be used with "
                      "`loky` as it does not respect POSIX. Try using "
                      "`spawn` or `loky` instead.", UserWarning)
    try:
        return mp_get_context(method)
    except ValueError:
        raise ValueError(
            f"Unknown context '{method}'. Value should be in "
            f"{START_METHODS}."
        )


def set_start_method(method, force=False):
    global _DEFAULT_START_METHOD
    if _DEFAULT_START_METHOD is not None and not force:
        raise RuntimeError('context has already been set')
    assert method is None or method in START_METHODS, (
        f"'{method}' is not a valid start_method. It should be in "
        f"{START_METHODS}"
    )

    _DEFAULT_START_METHOD = method


def get_start_method():
    return _DEFAULT_START_METHOD


def cpu_count(only_physical_cores=False):
    """Return the number of CPUs the current process can use.

    The returned number of CPUs accounts for:
     * the number of CPUs in the system, as given by
       ``multiprocessing.cpu_count``;
     * the CPU affinity settings of the current process
       (available on some Unix systems);
     * CFS scheduler CPU bandwidth limit (available on Linux only, typically
       set by docker and similar container orchestration systems);
     * the value of the LOKY_MAX_CPU_COUNT environment variable if defined.
    and is given as the minimum of these constraints.

    If ``only_physical_cores`` is True, return the number of physical cores
    instead of the number of logical cores (hyperthreading / SMT). Note that
    this option is not enforced if the number of usable cores is controlled in
    any other way such as: process affinity, restricting CFS scheduler policy
    or the LOKY_MAX_CPU_COUNT environment variable. If the number of physical
    cores is not found, return the number of logical cores.

    It is also always larger or equal to 1.
    """
    # Note: os.cpu_count() is allowed to return None in its docstring
    os_cpu_count = os.cpu_count() or 1

    cpu_count_user = _cpu_count_user(os_cpu_count)
    aggregate_cpu_count = max(min(os_cpu_count, cpu_count_user), 1)

    if not only_physical_cores:
        return aggregate_cpu_count

    if cpu_count_user < os_cpu_count:
        # Respect user setting
        return max(cpu_count_user, 1)

    cpu_count_physical, exception = _count_physical_cores()
    if cpu_count_physical != "not found":
        return cpu_count_physical

    # Fallback to default behavior
    if exception is not None:
        # warns only the first time
        warnings.warn(
            "Could not find the number of physical cores for the "
            f"following reason:\n{exception}\n"
            "Returning the number of logical cores instead. You can "
            "silence this warning by setting LOKY_MAX_CPU_COUNT to "
            "the number of cores you want to use.")
        traceback.print_tb(exception.__traceback__)

    return aggregate_cpu_count


def _cpu_count_user(os_cpu_count):
    """Number of user defined available CPUs"""
    # Number of available CPUs given affinity settings
    cpu_count_affinity = os_cpu_count
    if hasattr(os, 'sched_getaffinity'):
        try:
            cpu_count_affinity = len(os.sched_getaffinity(0))
        except NotImplementedError:
            pass

    # CFS scheduler CPU bandwidth limit
    # available in Linux since 2.6 kernel
    cpu_count_cfs = os_cpu_count
    cfs_quota_fname = "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"
    cfs_period_fname = "/sys/fs/cgroup/cpu/cpu.cfs_period_us"
    if os.path.exists(cfs_quota_fname) and os.path.exists(cfs_period_fname):
        with open(cfs_quota_fname) as fh:
            cfs_quota_us = int(fh.read())
        with open(cfs_period_fname) as fh:
            cfs_period_us = int(fh.read())

        if cfs_quota_us > 0 and cfs_period_us > 0:
            cpu_count_cfs = math.ceil(cfs_quota_us / cfs_period_us)

    # User defined soft-limit passed as a loky specific environment variable.
    cpu_count_loky = int(os.environ.get('LOKY_MAX_CPU_COUNT', os_cpu_count))

    return min(cpu_count_affinity, cpu_count_cfs, cpu_count_loky)


def _count_physical_cores():
    """Return a tuple (number of physical cores, exception)

    If the number of physical cores is found, exception is set to None.
    If it has not been found, return ("not found", exception).

    The number of physical cores is cached to avoid repeating subprocess calls.
    """
    exception = None

    # First check if the value is cached
    global physical_cores_cache
    if physical_cores_cache is not None:
        return physical_cores_cache, exception

    # Not cached yet, find it
    try:
        if sys.platform == "linux":
            cpu_info = subprocess.run(
                "lscpu --parse=core".split(), capture_output=True, text=True)
            cpu_info = cpu_info.stdout.splitlines()
            cpu_info = {line for line in cpu_info if not line.startswith("#")}
            cpu_count_physical = len(cpu_info)
        elif sys.platform == "win32":
            cpu_info = subprocess.run(
                "wmic CPU Get NumberOfCores /Format:csv".split(),
                capture_output=True, text=True)
            cpu_info = cpu_info.stdout.splitlines()
            cpu_info = [l.split(",")[1] for l in cpu_info
                        if (l and l != "Node,NumberOfCores")]
            cpu_count_physical = sum(map(int, cpu_info))
        elif sys.platform == "darwin":
            cpu_info = subprocess.run(
                "sysctl -n hw.physicalcpu".split(),
                capture_output=True, text=True)
            cpu_info = cpu_info.stdout
            cpu_count_physical = int(cpu_info)
        else:
            raise NotImplementedError(f"unsupported platform: {sys.platform}")

        # if cpu_count_physical < 1, we did not find a valid value
        if cpu_count_physical < 1:
            raise ValueError(
                f"found {cpu_count_physical} physical cores < 1")

    except Exception as e:
        exception = e
        cpu_count_physical = "not found"

    # Put the result in cache
    physical_cores_cache = cpu_count_physical

    return cpu_count_physical, exception


class LokyContext(BaseContext):
    """Context relying on the LokyProcess."""
    _name = 'loky'
    Process = LokyProcess
    cpu_count = staticmethod(cpu_count)

    def Queue(self, maxsize=0, reducers=None):
        '''Returns a queue object'''
        from .queues import Queue
        return Queue(maxsize, reducers=reducers,
                     ctx=self.get_context())

    def SimpleQueue(self, reducers=None):
        '''Returns a queue object'''
        from .queues import SimpleQueue
        return SimpleQueue(reducers=reducers, ctx=self.get_context())

    if sys.platform != "win32":
        """For Unix platform, use our custom implementation of synchronize
        relying on ctypes to interface with pthread semaphores.
        """
        def Semaphore(self, value=1):
            """Returns a semaphore object"""
            from .synchronize import Semaphore
            return Semaphore(value=value)

        def BoundedSemaphore(self, value):
            """Returns a bounded semaphore object"""
            from .synchronize import BoundedSemaphore
            return BoundedSemaphore(value)

        def Lock(self):
            """Returns a lock object"""
            from .synchronize import Lock
            return Lock()

        def RLock(self):
            """Returns a recurrent lock object"""
            from .synchronize import RLock
            return RLock()

        def Condition(self, lock=None):
            """Returns a condition object"""
            from .synchronize import Condition
            return Condition(lock)

        def Event(self):
            """Returns an event object"""
            from .synchronize import Event
            return Event()


class LokyInitMainContext(LokyContext):
    """Extra context with LokyProcess, which does load the main module

    This context is used for compatibility in the case ``cloudpickle`` is not
    present on the running system. This permits to load functions defined in
    the ``main`` module, using proper safeguards. The declaration of the
    ``executor`` should be protected by ``if __name__ == "__main__":`` and the
    functions and variable used from main should be out of this block.

    This mimics the default behavior of multiprocessing under Windows and the
    behavior of the ``spawn`` start method on a posix system.
    For more details, see the end of the following section of python doc
    https://docs.python.org/3/library/multiprocessing.html#multiprocessing-programming
    """
    _name = 'loky_init_main'
    Process = LokyInitMainProcess


# Register loky context so it works with multiprocessing.get_context
ctx_loky = LokyContext()
mp.context._concrete_contexts['loky'] = ctx_loky
mp.context._concrete_contexts['loky_init_main'] = LokyInitMainContext()
