# Copyright 2015, 2016 OpenMarket Ltd
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

import logging
import threading
import weakref
from functools import wraps
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Collection,
    Generic,
    Iterable,
    List,
    Optional,
    Type,
    TypeVar,
    Union,
    cast,
    overload,
)

from typing_extensions import Literal, Protocol

from twisted.internet import reactor

from synapse.config import cache as cache_config
from synapse.metrics.background_process_metrics import wrap_as_background_process
from synapse.util import Clock, caches
from synapse.util.caches import CacheMetric, register_cache
from synapse.util.caches.treecache import TreeCache, iterate_tree_cache_entry

if TYPE_CHECKING:
    from synapse.server import HomeServer

logger = logging.getLogger(__name__)

try:
    from pympler.asizeof import Asizer

    def _get_size_of(val: Any, *, recurse=True) -> int:
        """Get an estimate of the size in bytes of the object.

        Args:
            val: The object to size.
            recurse: If true will include referenced values in the size,
                otherwise only sizes the given object.
        """
        # Ignore singleton values when calculating memory usage.
        if val in ((), None, ""):
            return 0

        sizer = Asizer()
        sizer.exclude_refs((), None, "")
        return sizer.asizeof(val, limit=100 if recurse else 0)


except ImportError:

    def _get_size_of(val: Any, *, recurse=True) -> int:
        return 0


# Function type: the type used for invalidation callbacks
FT = TypeVar("FT", bound=Callable[..., Any])

# Key and Value type for the cache
KT = TypeVar("KT")
VT = TypeVar("VT")

# a general type var, distinct from either KT or VT
T = TypeVar("T")


def enumerate_leaves(node, depth):
    if depth == 0:
        yield node
    else:
        for n in node.values():
            for m in enumerate_leaves(n, depth - 1):
                yield m


class _CacheEntry(Protocol):
    """A protocol for cache entries used by `_ListNode` that allows it to cause
    the cache entry to be dropped.
    """

    def drop_from_cache(self) -> None:
        """Request the entry be dropped from the cache.

        Note: This should call `.remove_from_list()` on all `_ListNodes`
        referencing it.
        """
        ...


P = TypeVar("P", bound=_CacheEntry)


class _ListNode(Generic[P]):
    """A node in a doubly linked list, with an (optional) weak reference to a
    cache entry.

    The weak reference should only be `None` for the root node.
    """

    # We don't use attrs here as in py3.6 you can't have `attr.s(slots=True)`
    # and inherit from `Generic` for some reason
    __slots__ = [
        "cache_entry",
        "prev_node",
        "next_node",
    ]

    def __init__(
        self, cache_entry: Optional["weakref.ReferenceType[P]"] = None
    ) -> None:
        self.cache_entry = cache_entry
        self.prev_node: Optional[_ListNode[P]] = self
        self.next_node: Optional[_ListNode[P]] = self

    @staticmethod
    def insert_after(
        cache_entry: "weakref.ReferenceType[P]", root: "_ListNode", clock: Clock
    ) -> "_ListNode":
        """Create a new list node that is placed after the given root node."""
        node = _ListNode(cache_entry)
        node.move_after(root, clock)
        return node

    def remove_from_list(self):
        """Remove this node from the list."""
        if self.prev_node is None or self.next_node is None:
            # We've already been removed from the list.
            return

        prev_node = self.prev_node
        next_node = self.next_node

        prev_node.next_node = next_node
        next_node.prev_node = prev_node

        # We set these to None so that we don't get circular references,
        # allowing us to be dropped without having to go via the GC.
        self.next_node = None
        self.prev_node = None

    def move_after(self, root: "_ListNode", clock: Clock):
        """Move this node from its current location in the list to after the
        given node.
        """
        self.remove_from_list()

        prev_node = root
        next_node = root.next_node

        assert prev_node is not None
        assert next_node is not None

        self.prev_node = prev_node
        self.next_node = next_node

        prev_node.next_node = self
        next_node.prev_node = self

    def get_cache_entry(self) -> Optional[P]:
        """Get the cache entry, returns None if this is the root node (i.e.
        cache_entry is None) or if the entry has been dropped.
        """

        if not self.cache_entry:
            return None

        return self.cache_entry()


class _TimedListNode(_ListNode[P]):
    """A `_ListNode` that tracks last access time."""

    __slots__ = ["last_access_ts_secs"]

    def __init__(
        self, clock: Clock, cache_entry: Optional["weakref.ReferenceType[P]"]
    ) -> None:
        super().__init__(cache_entry=cache_entry)

        self.last_access_ts_secs = int(clock.time())

    @staticmethod
    def insert_after(
        cache_entry: "weakref.ReferenceType[P]", root: "_ListNode", clock: Clock
    ) -> "_TimedListNode":
        node = _TimedListNode(clock, cache_entry)
        node.move_after(root, clock)
        return node

    def move_after(self, root: "_ListNode", clock: Clock):
        self.last_access_ts_secs = int(clock.time())
        return super().move_after(root, clock)


# A linked list of all cache entries, allowing efficient time based eviction.
GLOBAL_ROOT = _ListNode[_CacheEntry]()


@wrap_as_background_process("LruCache._expire_old_entries")
async def _expire_old_entries(clock: Clock, expiry_seconds: int):
    """Walks the global cache list to find cache entries that haven't been
    accessed in the given number of seconds.
    """

    now = int(clock.time())
    node = GLOBAL_ROOT.prev_node
    assert node is not None

    i = 0
    orphaned_nodes = 0

    logger.debug("Searching for stale caches")

    while node is not GLOBAL_ROOT:
        # Only the root node isn't a `_TimedListNode`.
        assert isinstance(node, _TimedListNode)

        if node.last_access_ts_secs > now - expiry_seconds:
            break

        cache_entry = node.get_cache_entry()
        current_node = node
        node = node.prev_node
        if cache_entry:
            cache_entry.drop_from_cache()
        else:
            # The cache entry has been dropped without being cleared out of this
            # list. This can happen if the `LruCache` has been dropped without
            # being cleared up properly.
            orphaned_nodes += 1
            current_node.remove_from_list()

        assert node is not None

        # If we do lots of work at once we yield to allow other stuff to happen.
        if (i + 1) % 10000 == 0:
            logger.debug("Waiting during drop")
            await clock.sleep(0)
            logger.debug("Waking during drop")

        # If we've yielded then our current node may have been evicted, so we
        # need to check that its still valid.
        if node.prev_node is None:
            break

        i += 1

    logger.info("Dropped %d items from caches, (orphaned: %d)", i, orphaned_nodes)


def setup_expire_lru_cache_entries(hs: "HomeServer"):
    """Start a background job that expires all cache entries if they have not
    been accessed for the given number of seconds.
    """
    if not hs.config.caches.expiry_time_msec:
        return

    logger.info(
        "Expiring LRU caches after %d seconds", hs.config.caches.expiry_time_msec / 1000
    )

    clock = hs.get_clock()
    clock.looping_call(
        _expire_old_entries, 30 * 1000, clock, hs.config.caches.expiry_time_msec / 1000
    )


class _Node:
    __slots__ = [
        "list_node",
        "global_list_node",
        "cache",
        "key",
        "value",
        "callbacks",
        "memory",
        "__weakref__",
    ]

    def __init__(
        self,
        root: "_ListNode[_Node]",
        key,
        value,
        cache: "LruCache",
        clock: Clock,
        callbacks: Collection[Callable[[], None]] = (),
    ):
        self_ref = weakref.ref(self, lambda _: self.drop_from_lists())
        self.list_node = _ListNode.insert_after(self_ref, root, clock)
        self.global_list_node = _TimedListNode.insert_after(
            self_ref, GLOBAL_ROOT, clock
        )

        # We store a weak reference to the cache object so that this _Node can
        # remove itself from the cache. If the cache is dropped we ensure we
        # remove our entries in the lists.
        self.cache = weakref.ref(cache, lambda _: self.drop_from_lists())

        self.key = key
        self.value = value

        # Set of callbacks to run when the node gets deleted. We store as a list
        # rather than a set to keep memory usage down (and since we expect few
        # entries per node, the performance of checking for duplication in a
        # list vs using a set is negligible).
        #
        # Note that we store this as an optional list to keep the memory
        # footprint down. Storing `None` is free as its a singleton, while empty
        # lists are 56 bytes (and empty sets are 216 bytes, if we did the naive
        # thing and used sets).
        self.callbacks = None  # type: Optional[List[Callable[[], None]]]

        self.add_callbacks(callbacks)

        self.memory = 0
        if caches.TRACK_MEMORY_USAGE:
            self.memory = (
                _get_size_of(key)
                + _get_size_of(value)
                + _get_size_of(self.callbacks, recurse=False)
                + _get_size_of(self, recurse=False)
            )
            self.memory += _get_size_of(self.memory, recurse=False)

    def add_callbacks(self, callbacks: Collection[Callable[[], None]]) -> None:
        """Add to stored list of callbacks, removing duplicates."""

        if not callbacks:
            return

        if not self.callbacks:
            self.callbacks = []

        for callback in callbacks:
            if callback not in self.callbacks:
                self.callbacks.append(callback)

    def run_and_clear_callbacks(self) -> None:
        """Run all callbacks and clear the stored list of callbacks. Used when
        the node is being deleted.
        """

        if not self.callbacks:
            return

        for callback in self.callbacks:
            callback()

        self.callbacks = None

    def drop_from_cache(self) -> None:
        """Implements `_CacheEntry` protocol."""
        cache = self.cache()
        if not cache or not cache.pop(self.key, None):
            # `cache.pop` should call `drop_from_lists()`, unless this Node had
            # already been removed from the cache.
            self.drop_from_lists()

    def drop_from_lists(self) -> None:
        """Remove this node from the cache lists."""
        self.list_node.remove_from_list()
        self.global_list_node.remove_from_list()


class LruCache(Generic[KT, VT]):
    """
    Least-recently-used cache, supporting prometheus metrics and invalidation callbacks.

    If cache_type=TreeCache, all keys must be tuples.
    """

    def __init__(
        self,
        max_size: int,
        cache_name: Optional[str] = None,
        cache_type: Type[Union[dict, TreeCache]] = dict,
        size_callback: Optional[Callable] = None,
        metrics_collection_callback: Optional[Callable[[], None]] = None,
        apply_cache_factor_from_config: bool = True,
        clock: Optional[Clock] = None,
    ):
        """
        Args:
            max_size: The maximum amount of entries the cache can hold

            cache_name: The name of this cache, for the prometheus metrics. If unset,
                no metrics will be reported on this cache.

            cache_type (type):
                type of underlying cache to be used. Typically one of dict
                or TreeCache.

            size_callback (func(V) -> int | None):

            metrics_collection_callback:
                metrics collection callback. This is called early in the metrics
                collection process, before any of the metrics registered with the
                prometheus Registry are collected, so can be used to update any dynamic
                metrics.

                Ignored if cache_name is None.

            apply_cache_factor_from_config (bool): If true, `max_size` will be
                multiplied by a cache factor derived from the homeserver config
        """
        # Default `clock` to something sensible. Note that we rename it to
        # `real_clock` so that mypy doesn't think its still `Optional`.
        if clock is None:
            real_clock = Clock(reactor)
        else:
            real_clock = clock

        cache = cache_type()
        self.cache = cache  # Used for introspection.
        self.apply_cache_factor_from_config = apply_cache_factor_from_config

        # Save the original max size, and apply the default size factor.
        self._original_max_size = max_size
        # We previously didn't apply the cache factor here, and as such some caches were
        # not affected by the global cache factor. Add an option here to disable applying
        # the cache factor when a cache is created
        if apply_cache_factor_from_config:
            self.max_size = int(max_size * cache_config.properties.default_factor_size)
        else:
            self.max_size = int(max_size)

        # register_cache might call our "set_cache_factor" callback; there's nothing to
        # do yet when we get resized.
        self._on_resize = None  # type: Optional[Callable[[],None]]

        if cache_name is not None:
            metrics = register_cache(
                "lru_cache",
                cache_name,
                self,
                collect_callback=metrics_collection_callback,
            )  # type: Optional[CacheMetric]
        else:
            metrics = None

        # this is exposed for access from outside this class
        self.metrics = metrics

        list_root = _ListNode[_Node]()

        lock = threading.Lock()

        def evict():
            orphaned = 0
            while cache_len() > self.max_size:
                todelete = list_root.prev_node
                assert todelete is not None

                node = todelete.get_cache_entry()
                if not node:
                    todelete.remove_from_list()
                    orphaned += 1
                    continue

                evicted_len = delete_node(node)
                cache.pop(node.key, None)
                if metrics:
                    metrics.inc_evictions(evicted_len)

            if orphaned:
                logger.warning("Found %d orphaned nodes in cache %r", cache_name)

        def synchronized(f: FT) -> FT:
            @wraps(f)
            def inner(*args, **kwargs):
                with lock:
                    return f(*args, **kwargs)

            return cast(FT, inner)

        cached_cache_len = [0]
        if size_callback is not None:

            def cache_len():
                return cached_cache_len[0]

        else:

            def cache_len():
                return len(cache)

        self.len = synchronized(cache_len)

        def add_node(key, value, callbacks: Collection[Callable[[], None]] = ()):
            node = _Node(list_root, key, value, self, real_clock, callbacks)
            cache[key] = node

            if size_callback:
                cached_cache_len[0] += size_callback(node.value)

            if caches.TRACK_MEMORY_USAGE and metrics:
                metrics.inc_memory_usage(node.memory)

        def move_node_to_front(node: _Node):
            node.list_node.move_after(list_root, real_clock)
            node.global_list_node.move_after(GLOBAL_ROOT, real_clock)

        def delete_node(node: _Node) -> int:
            node.drop_from_lists()

            deleted_len = 1
            if size_callback:
                deleted_len = size_callback(node.value)
                cached_cache_len[0] -= deleted_len

            node.run_and_clear_callbacks()

            if caches.TRACK_MEMORY_USAGE and metrics:
                metrics.dec_memory_usage(node.memory)

            return deleted_len

        @overload
        def cache_get(
            key: KT,
            default: Literal[None] = None,
            callbacks: Collection[Callable[[], None]] = ...,
            update_metrics: bool = ...,
        ) -> Optional[VT]:
            ...

        @overload
        def cache_get(
            key: KT,
            default: T,
            callbacks: Collection[Callable[[], None]] = ...,
            update_metrics: bool = ...,
        ) -> Union[T, VT]:
            ...

        @synchronized
        def cache_get(
            key: KT,
            default: Optional[T] = None,
            callbacks: Collection[Callable[[], None]] = (),
            update_metrics: bool = True,
        ):
            node = cache.get(key, None)
            if node is not None:
                move_node_to_front(node)
                node.add_callbacks(callbacks)
                if update_metrics and metrics:
                    metrics.inc_hits()
                return node.value
            else:
                if update_metrics and metrics:
                    metrics.inc_misses()
                return default

        @synchronized
        def cache_set(key: KT, value: VT, callbacks: Iterable[Callable[[], None]] = ()):
            node = cache.get(key, None)
            if node is not None:
                # We sometimes store large objects, e.g. dicts, which cause
                # the inequality check to take a long time. So let's only do
                # the check if we have some callbacks to call.
                if value != node.value:
                    node.run_and_clear_callbacks()

                # We don't bother to protect this by value != node.value as
                # generally size_callback will be cheap compared with equality
                # checks. (For example, taking the size of two dicts is quicker
                # than comparing them for equality.)
                if size_callback:
                    cached_cache_len[0] -= size_callback(node.value)
                    cached_cache_len[0] += size_callback(value)

                node.add_callbacks(callbacks)

                move_node_to_front(node)
                node.value = value
            else:
                add_node(key, value, set(callbacks))

            evict()

        @synchronized
        def cache_set_default(key: KT, value: VT) -> VT:
            node = cache.get(key, None)
            if node is not None:
                return node.value
            else:
                add_node(key, value)
                evict()
                return value

        @overload
        def cache_pop(key: KT, default: Literal[None] = None) -> Optional[VT]:
            ...

        @overload
        def cache_pop(key: KT, default: T) -> Union[T, VT]:
            ...

        @synchronized
        def cache_pop(key: KT, default: Optional[T] = None):
            node = cache.get(key, None)
            if node:
                delete_node(node)
                cache.pop(node.key, None)
                return node.value
            else:
                return default

        @synchronized
        def cache_del_multi(key: KT) -> None:
            """Delete an entry, or tree of entries

            If the LruCache is backed by a regular dict, then "key" must be of
            the right type for this cache

            If the LruCache is backed by a TreeCache, then "key" must be a tuple, but
            may be of lower cardinality than the TreeCache - in which case the whole
            subtree is deleted.
            """
            popped = cache.pop(key, None)
            if popped is None:
                return
            # for each deleted node, we now need to remove it from the linked list
            # and run its callbacks.
            for leaf in iterate_tree_cache_entry(popped):
                delete_node(leaf)

        @synchronized
        def cache_clear() -> None:
            for node in cache.values():
                node.run_and_clear_callbacks()
                node.drop_from_lists()

            list_root.next_node = list_root
            list_root.prev_node = list_root

            cache.clear()
            if size_callback:
                cached_cache_len[0] = 0

            if caches.TRACK_MEMORY_USAGE and metrics:
                metrics.clear_memory_usage()

        @synchronized
        def cache_contains(key: KT) -> bool:
            return key in cache

        self.sentinel = object()

        # make sure that we clear out any excess entries after we get resized.
        self._on_resize = evict

        self.get = cache_get
        self.set = cache_set
        self.setdefault = cache_set_default
        self.pop = cache_pop
        self.del_multi = cache_del_multi
        # `invalidate` is exposed for consistency with DeferredCache, so that it can be
        # invalidated by the cache invalidation replication stream.
        self.invalidate = cache_del_multi
        self.len = synchronized(cache_len)
        self.contains = cache_contains
        self.clear = cache_clear

    def __getitem__(self, key):
        result = self.get(key, self.sentinel)
        if result is self.sentinel:
            raise KeyError()
        else:
            return result

    def __setitem__(self, key, value):
        self.set(key, value)

    def __delitem__(self, key, value):
        result = self.pop(key, self.sentinel)
        if result is self.sentinel:
            raise KeyError()

    def __len__(self):
        return self.len()

    def __contains__(self, key):
        return self.contains(key)

    def set_cache_factor(self, factor: float) -> bool:
        """
        Set the cache factor for this individual cache.

        This will trigger a resize if it changes, which may require evicting
        items from the cache.

        Returns:
            bool: Whether the cache changed size or not.
        """
        if not self.apply_cache_factor_from_config:
            return False

        new_size = int(self._original_max_size * factor)
        if new_size != self.max_size:
            self.max_size = new_size
            if self._on_resize:
                self._on_resize()
            return True
        return False
