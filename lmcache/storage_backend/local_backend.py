import os
import queue
import threading
import time
from collections import OrderedDict
from concurrent.futures import Future, ProcessPoolExecutor
from typing import Dict, Optional, Tuple, Union

import torch
from safetensors import safe_open
from safetensors.torch import save_file

from lmcache.config import LMCacheEngineConfig, LMCacheMemPoolMetadata
from lmcache.logging import init_logger
from lmcache.storage_backend.abstract_backend import LMCBackendInterface
from lmcache.storage_backend.evictor import DummyEvictor
from lmcache.storage_backend.evictor.base_evictor import PutStatus
from lmcache.storage_backend.mem_pool import (KVObj, LocalCPUBufferPool,
                                              LocalCPUPool, LocalGPUPool,
                                              LocalPool)
from lmcache.utils import (CacheEngineKey, DiskCacheMetadata, KVCache,
                           _lmcache_nvtx_annotate)

logger = init_logger(__name__)


class LocalBackendEndSignal:
    pass


class LMCLocalBackend(LMCBackendInterface):
    """
    Cache engine for storing the KV cache of the tokens in the local cpu/gpu
    memory.
    """

    def __init__(self, config: LMCacheEngineConfig,
                 metadata: LMCacheMemPoolMetadata):
        """
        Throws:
            RuntimeError if the loaded configuration does not match the current
                configuration
        """
        super().__init__()

        self.chunk_size = config.chunk_size
        self.config = config
        self.dict: OrderedDict[CacheEngineKey, KVObj] = OrderedDict()
        self.device = config.local_device

        self.put_queue: queue.Queue[
            Union[Tuple[CacheEngineKey, torch.Tensor],
                  LocalBackendEndSignal]] = queue.Queue()
        self.put_thread = threading.Thread(target=self.put_worker, args=())
        self.put_thread.start()
        self.update_lock = threading.Lock()

        # FIXME(Jiayi): `use_pin_memory` and `dst_device` should be configged
        # dynamically
        self.dst_device = "cuda"

        # TODO(Jiayi): The storage size and caching policy for both
        # evictor and mpool need to be configured dynamically
        self.evictor = DummyEvictor()
        self.mpool: LocalPool
        if self.device == "cpu":
            self.mpool = LocalCPUPool(metadata)
        elif self.device == "cuda":
            self.mpool = LocalGPUPool(metadata)

        # TODO(Jiayi): A gpu buffer could speed up `get`
        # self.fix_sized_dst_buffer = torch.tensor()

    def contains(
        self,
        key: CacheEngineKey,
    ) -> bool:
        """
        Check if the cache engine contains the key.

        Input:
            key: the key of the token chunk, including prefix hash and format

        Returns:
            True if the cache engine contains the key, False otherwise
        """
        return key in self.dict

    def remove(
        self,
        key: CacheEngineKey,
    ) -> None:
        """
        Remove the KV cache chunk by the given key

        Input:
            key: the key of the token chunk, including prefix hash and format

        """
        kv_obj = self.dict.pop(key)
        self.mpool.free(kv_obj)

    @_lmcache_nvtx_annotate
    def put_worker(self, ):
        while True:
            # TODO: dirty fix to downgrade the priority of the put worker
            # time.sleep(0.01)
            item = self.put_queue.get()
            if isinstance(item, LocalBackendEndSignal):
                break
            key, value = item
            self.put_nonblocking(key, value)

    @_lmcache_nvtx_annotate
    @torch.inference_mode()
    def put_nonblocking(self, key, kv_chunk):
        # Obtain keys to evict
        self.update_lock.acquire()
        evict_keys, put_status = self.evictor.update_on_put(
            self.dict, kv_chunk)
        if put_status == PutStatus.ILLEGAL:
            self.update_lock.release()
            return

        # evict caches
        for evict_key in evict_keys:
            self.remove(evict_key)

        # free old block to avoid mem leak
        if key in self.dict:
            self.remove(key)

        # Allocate the kv chunk
        kv_obj = self.mpool.allocate(kv_chunk)
        self.update_lock.release()

        if kv_obj is None:
            return

        put_stream = torch.cuda.Stream()
        if kv_chunk.device != torch.cpu:
            # wait operation in main stream to finish
            # e.g., view operations on kv_chunk
            put_stream.wait_stream(torch.cuda.default_stream(kv_chunk.device))

        with torch.cuda.stream(put_stream):
            kv_obj.data.copy_(kv_chunk, non_blocking=True)
            kv_chunk.record_stream(put_stream)
        put_stream.synchronize()

        # Store new chunk
        self.update_lock.acquire()
        self.dict[key] = kv_obj
        self.update_lock.release()

    @torch.inference_mode()
    def put_blocking(self, key, kv_chunk):

        # Obtain keys to evict
        evict_keys, put_status = self.evictor.update_on_put(
            self.dict, kv_chunk)

        # Abort put if cache too big
        if put_status == PutStatus.ILLEGAL:
            return

        kv_obj = self.mpool.allocate(kv_chunk)

        if kv_obj is None:
            return

        kv_obj.data.copy_(kv_chunk, non_blocking=False)

        # free old block to avoid mem leak
        if key in self.dict:
            self.remove(key)

        # Evict caches
        for evict_key in evict_keys:
            self.remove(evict_key)

        # Store new chunk
        self.dict[key] = kv_obj

    def put(
        self,
        key: CacheEngineKey,
        kv_chunk: torch.Tensor,
        blocking: bool = True,
    ) -> None:
        """
        Store the KV cache of the tokens into the cache engine.

        Input:
            key: the key of the token chunk, including prefix hash and format
            kv_chunk: the kv cache of the token chunk, in the format of nested 
            tuples

        Returns:
            None

        Note:
            The KV cache should NOT have the "batch" dimension.
        """
        if blocking:
            self.put_blocking(key, kv_chunk)
        else:
            self.put_queue.put((key, kv_chunk))

    @_lmcache_nvtx_annotate
    def get(
        self,
        key: CacheEngineKey,
    ) -> Optional[torch.Tensor]:
        """
        Retrieve the KV cache chunk by the given key

        Input:
            key: the key of the token chunk, including prefix hash and format
        Output:
            the kv cache of the token chunk, in the format of nested tuples
            None if the key is not found
        """
        kv_chunk = None

        self.update_lock.acquire()
        kv_obj = self.dict.get(key, None)

        # Update cache recency
        if kv_obj is not None:
            self.evictor.update_on_get(key, self.dict)
            kv_chunk = kv_obj.data.to(self.dst_device)

        self.update_lock.release()

        return kv_chunk

    def close(self):
        if self.put_thread is not None and self.put_thread.is_alive():
            self.put_queue.put(LocalBackendEndSignal())
            self.put_thread.join()
            logger.info("Closed the put worker in local backend")

    def __del__(self):
        self.close()


# TODO(Jiayi): need to optimize disk saving/loading
# current impl. with "safetensors" might not be efficient
# but it is better than "torch.save/load"

# TODO(Jiayi): need to support prefetch for disk


class LMCLocalDiskBackend(LMCBackendInterface):
    """
    Cache engine for storing the KV cache of the tokens in the local disk.
    """

    def __init__(self, config: LMCacheEngineConfig,
                 metadata: LMCacheMemPoolMetadata):
        """
        Throws:
            RuntimeError if the loaded configuration does not match the current
                configuration
        """
        super().__init__()

        self.chunk_size = config.chunk_size
        self.config = config
        self.dict: OrderedDict[CacheEngineKey,
                               DiskCacheMetadata] = OrderedDict()
        self.path = config.local_device

        assert self.path is not None, ("Need to specify local path if when "
                                       "using LMCLocalDiskBackend")

        if not os.path.exists(self.path):
            os.makedirs(self.path)

        # TODO(Jiayi): the following async put code is repeated in all backends
        # Please consider use a parent class that can be inherited by all
        # (local) backends
        # This should be also be helpful for more flexible hierarchical backends
        # For async put
        self.put_queue: queue.Queue[
            Union[Tuple[CacheEngineKey, torch.Tensor],
                  LocalBackendEndSignal]] = queue.Queue()
        self.put_thread = threading.Thread(target=self.put_worker, args=())
        self.put_thread.start()
        self.update_lock = threading.Lock()

        # TODO (Jiayi): please remove this hard code
        self.dst_device = "cuda"

        # TODO(Jiayi): The storage size and caching policy for both
        # evictor and mpool need to be configured dynamically
        self.evictor = DummyEvictor()
        # NOTE(Jiayi): This mbufferpool should be smaller than the actual
        # cpu backend but big enough to avoid stalls in save
        # TODO(Jiayi): share the buffer if both cpu and disk backend are enabled
        self.cpu_mbufferpool = LocalCPUBufferPool(metadata)

        self.future_pool: Dict[CacheEngineKey, Future] = {}

        self.proc_pool_executor = ProcessPoolExecutor(max_workers=4)

    def contains(
        self,
        key: CacheEngineKey,
    ) -> bool:
        """
        Check if the cache engine contains the key.

        Input:
            key: the key of the token chunk, including prefix hash and format

        Returns:
            True if the cache engine contains the key, False otherwise
        """
        return key in self.dict

    def _key_to_path(
        self,
        key: CacheEngineKey,
    ) -> str:
        """
        Convert key to path_name

        Input:
            key: the key of the token chunk, including prefix hash and format

        Returns:
            returns the path name
        """
        return self.path + key.to_string().replace("/", "-") + ".pt"

    def remove(
        self,
        key: CacheEngineKey,
    ) -> None:
        """
        Remove the KV cache chunk by the given key

        Input:
            key: the key of the token chunk, including prefix hash and format

        """

        self.update_lock.acquire()
        path = self.dict[key].path
        self.dict.pop(key)
        self.update_lock.release()

        os.remove(path)

    @_lmcache_nvtx_annotate
    def put_worker(self, ):
        while True:
            item = self.put_queue.get()
            if isinstance(item, LocalBackendEndSignal):
                break
            key, value = item
            self.put_nonblocking(key, value)

    @_lmcache_nvtx_annotate
    @torch.inference_mode()
    def save_disk(
        self,
        path: str,
        kv_chunk: torch.Tensor,
    ):
        save_file({"kv_chunk": kv_chunk.contiguous()}, path)

    @_lmcache_nvtx_annotate
    @torch.inference_mode()
    def put_nonblocking(
        self,
        key: CacheEngineKey,
        kv_chunk: torch.Tensor,
    ) -> None:
        path = self._key_to_path(key)
        logger.debug(f"Saving cache to {path}")

        self.update_lock.acquire()

        # Obtain keys to evict
        evict_keys, put_status = self.evictor.update_on_put(
            self.dict, kv_chunk)

        # Abort put if cache too big
        if put_status == PutStatus.ILLEGAL:
            self.update_lock.release()
            return

        # evict caches
        for evict_key in evict_keys:
            self.remove(evict_key)
        self.update_lock.release()

        kv_obj = None

        # Allocate the kv chunk
        while kv_obj is None:
            self.update_lock.acquire()
            kv_obj = self.cpu_mbufferpool.allocate(kv_chunk)
            self.update_lock.release()
            if kv_obj is None:
                # TODO(Jiayi): Please tune the sleep time for better performance
                time.sleep(0.01)

        put_stream = torch.cuda.Stream()
        put_stream.wait_stream(torch.cuda.default_stream(kv_chunk.device))
        with torch.cuda.stream(put_stream):
            kv_obj.data.copy_(kv_chunk, non_blocking=True)
            kv_chunk.record_stream(put_stream)
        put_stream.synchronize()

        future = self.proc_pool_executor.submit(self.save_disk, path,
                                                kv_obj.data)

        self.update_lock.acquire()
        self.future_pool[key] = future
        self.dict[key] = DiskCacheMetadata(path,
                                           self.evictor.get_size(kv_obj.data))
        self.cpu_mbufferpool.free(kv_obj)
        self.update_lock.release()

    @_lmcache_nvtx_annotate
    @torch.inference_mode()
    def put_blocking(
        self,
        key: CacheEngineKey,
        kv_chunk: torch.Tensor,
    ) -> None:
        path = self._key_to_path(key)
        logger.debug(f"Saving cache to {path}")

        self.update_lock.acquire()
        # Obtain keys to evict
        evict_keys, put_status = self.evictor.update_on_put(
            self.dict, kv_chunk)

        # Abort put if cache too big
        if put_status == PutStatus.ILLEGAL:
            self.update_lock.release()
            return

        # evict caches
        for evict_key in evict_keys:
            self.remove(evict_key)
        self.update_lock.release()

        # The following order matters of `save_file` and `update dictionary`
        # matters
        save_file({"kv_chunk": kv_chunk}, path)

        self.update_lock.acquire()
        self.dict[key] = DiskCacheMetadata(path,
                                           self.evictor.get_size(kv_chunk))
        self.update_lock.release()

    def put(
        self,
        key: CacheEngineKey,
        kv_chunk: torch.Tensor,
        blocking: bool = True,
    ) -> None:
        """
        Store the KV cache of the tokens into the cache engine.

        Input:
            key: the key of the token chunk, including prefix hash and format
            kv_chunk: the kv cache of the token chunk, in the format of nested 
            tuples

        Returns:
            None

        Note:
            The KV cache should NOT have the "batch" dimension.
        """
        if blocking:
            self.put_blocking(key, kv_chunk)
        else:
            self.put_queue.put((key, kv_chunk))

    @_lmcache_nvtx_annotate
    def get(
        self,
        key: CacheEngineKey,
    ) -> Optional[KVCache]:
        """
        Retrieve the KV cache chunk by the given key

        Input:
            key: the key of the token chunk, including prefix hash and format
        Output:
            the kv cache of the token chunk, in the format of nested tuples
            None if the key is not found
        """
        self.update_lock.acquire()
        if key not in self.dict:
            self.update_lock.release()
            return None

        if key in self.future_pool:
            future = self.future_pool[key]
            if not future.done():
                return None
            del self.future_pool[key]

        path = self.dict[key].path
        self.evictor.update_on_get(key, self.dict)

        with safe_open(path, framework="pt",
                       device=self.dst_device) as f:  # type: ignore
            kv_chunk = f.get_tensor("kv_chunk")
        self.update_lock.release()
        return kv_chunk

    def close(self):
        if self.put_thread is not None and self.put_thread.is_alive():
            self.put_queue.put(LocalBackendEndSignal())
            self.put_thread.join()
            logger.info("Closed the put worker in local disk backend")
        self.proc_pool_executor.shutdown()

    def __del__(self):
        self.close()
