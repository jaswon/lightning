import logging
import os
import signal
import tempfile
import traceback
import types
from abc import abstractmethod
from multiprocessing import Process, Queue
from queue import Empty
from shutil import copyfile, rmtree
from time import sleep, time
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar
from urllib import parse

import torch
from tqdm.auto import tqdm

from lightning import seed_everything
from lightning.data.streaming import Cache
from lightning.data.streaming.constants import (
    _BOTO3_AVAILABLE,
    _DEFAULT_FAST_DEV_RUN_ITEMS,
    _INDEX_FILENAME,
    _LIGHTNING_CLOUD_GREATER_EQUAL_0_5_42,
    _TORCH_GREATER_EQUAL_2_1_0,
)
from lightning.fabric.accelerators.cuda import is_cuda_available
from lightning.fabric.plugins.environments import LightningEnvironment
from lightning.fabric.utilities.distributed import (
    _distributed_is_initialized,
    _init_dist_connection,
)
from lightning.fabric.utilities.distributed import group as _group

if _TORCH_GREATER_EQUAL_2_1_0:
    from torch.utils._pytree import tree_flatten, tree_unflatten

if _LIGHTNING_CLOUD_GREATER_EQUAL_0_5_42:
    from lightning_cloud.resolver import _LightningSrcResolver, _LightningTargetResolver

if _BOTO3_AVAILABLE:
    import boto3
    import botocore

logger = logging.Logger(__name__)


def _get_cache_folder() -> str:
    """Returns the cache folder."""
    return os.getenv("DATA_OPTIMIZER_CACHE_FOLDER", "/cache")


def _get_num_nodes() -> int:
    """Returns the number of nodes."""
    return int(os.getenv("DATA_OPTIMIZER_NUM_NODES", 1))


def _get_node_rank() -> int:
    """Returns the current node rank of the instance."""
    return int(os.getenv("DATA_OPTIMIZER_NODE_RANK", 0))


def _get_fast_dev_run() -> int:
    """Returns whether fast dev mode is enabled."""
    return bool(int(os.getenv("DATA_OPTIMIZER_FAST_DEV_RUN", 1)))


def _get_home_folder() -> str:
    """Returns whether cache folder for the filepaths."""
    return os.getenv("DATA_OPTIMIZER_HOME_FOLDER", os.path.expanduser("~"))


def _get_cache_dir(name: Optional[str]) -> str:
    """Returns the cache directory used by the Cache to store the chunks."""
    if name is None:
        return _get_cache_folder()
    return os.path.join(_get_cache_folder(), name)


def _get_cache_data_dir(name: Optional[str]) -> str:
    """Returns the cache data directory used by the DataProcessor workers to download the files."""
    if name is None:
        return os.path.join(_get_cache_folder(), "data")
    return os.path.join(_get_cache_folder(), "data", name)


def _get_s3_client() -> Any:
    return boto3.client("s3", config=botocore.config.Config(retries={"max_attempts": 1000, "mode": "standard"}))


def _wait_for_file_to_exist(s3: Any, obj: parse.ParseResult, sleep_time: int = 2) -> Any:
    """This function check."""
    while True:
        try:
            return s3.head_object(Bucket=obj.netloc, Key=obj.path.lstrip("/"))
        except botocore.exceptions.ClientError as e:
            if "the HeadObject operation: Not Found" in str(e):
                sleep(sleep_time)
            else:
                raise e


def _download_data_target(
    input_dir: str, remote_input_dir: str, cache_dir: str, queue_in: Queue, queue_out: Queue
) -> None:
    """This function is used to download data from a remote directory to a cache directory to optimise reading."""
    s3 = _get_s3_client()

    while True:
        # 2. Fetch from the queue
        r: Optional[Tuple[int, List[str]]] = queue_in.get()

        # 3. Terminate the process if we received a termination signal
        if r is None:
            queue_out.put(None)
            return

        # 4. Unpack
        index, paths = r

        # 5. Check whether all the files are already downloaded
        if all(os.path.exists(p.replace(input_dir, cache_dir) if input_dir else p) for p in paths):
            queue_out.put(index)
            continue

        if remote_input_dir is not None:
            # 6. Download all the required paths to unblock the current index
            for path in paths:
                remote_path = path.replace(input_dir, remote_input_dir)
                obj = parse.urlparse(remote_path)
                local_path = path.replace(input_dir, cache_dir)

                if obj.scheme == "s3":
                    dirpath = os.path.dirname(local_path)

                    os.makedirs(dirpath, exist_ok=True)

                    with open(local_path, "wb") as f:
                        s3.download_fileobj(obj.netloc, obj.path.lstrip("/"), f)

                elif os.path.isfile(remote_path):
                    copyfile(remote_path, local_path)
                else:
                    raise ValueError(f"The provided {remote_input_dir} isn't supported.")

        # 7. Inform the worker the current files are available
        queue_out.put(index)


def _remove_target(input_dir: str, cache_dir: str, queue_in: Queue) -> None:
    """This function is used to delete files from the cache directory to minimise disk space."""
    while True:
        # 1. Collect paths
        paths = queue_in.get()

        # 2. Terminate the process if we received a termination signal
        if paths is None:
            return

        # 3. Iterate through the paths and delete them sequentially.
        for path in paths:
            if input_dir:
                cached_filepath = path.replace(input_dir, cache_dir)

                if os.path.exists(cached_filepath):
                    os.remove(cached_filepath)


def _upload_fn(upload_queue: Queue, remove_queue: Queue, cache_dir: str, remote_output_dir: str) -> None:
    """This function is used to upload optimised chunks from a local to remote dataset directory."""
    obj = parse.urlparse(remote_output_dir)

    if obj.scheme == "s3":
        s3 = _get_s3_client()

    while True:
        local_filepath: Optional[str] = upload_queue.get()

        # Terminate the process if we received a termination signal
        if local_filepath is None:
            return

        # Upload the file to the target cloud storage
        if not local_filepath.startswith(cache_dir):
            local_filepath = os.path.join(cache_dir, local_filepath)

        if obj.scheme == "s3":
            s3.upload_file(
                local_filepath, obj.netloc, os.path.join(obj.path.lstrip("/"), os.path.basename(local_filepath))
            )
        elif os.path.isdir(remote_output_dir):
            copyfile(local_filepath, os.path.join(remote_output_dir, os.path.basename(local_filepath)))
        else:
            raise ValueError(f"The provided {remote_output_dir} isn't supported.")

        # Inform the remover to delete the file
        if remove_queue:
            remove_queue.put([local_filepath])


def _associated_items_to_workers(num_workers: int, user_items: List[Any]) -> Tuple[List[int], List[List[Any]]]:
    # Associate the items to the workers based on number of nodes and node rank.
    num_nodes = _get_num_nodes()
    current_node_rank = _get_node_rank()
    node_size = len(user_items) // num_nodes
    workers_user_items = []
    begins = []
    for node_rank in range(num_nodes):
        if node_rank != current_node_rank:
            continue
        is_last_node = node_rank == num_nodes - 1
        start_node = node_rank * node_size
        end_node = len(user_items) if is_last_node else (node_rank + 1) * node_size
        node_user_items = user_items[start_node:end_node]
        worker_size = len(node_user_items) // num_workers
        for worker_idx in range(num_workers):
            is_last = worker_idx == num_workers - 1
            begin = worker_idx * worker_size
            end = len(node_user_items) if is_last else (worker_idx + 1) * worker_size
            workers_user_items.append(node_user_items[begin:end])
            begins.append(begin)
        return begins, workers_user_items
    raise RuntimeError(f"The current_node_rank {current_node_rank} doesn't exist in {num_nodes}.")


class BaseWorker:
    def __init__(
        self,
        worker_index: int,
        num_workers: int,
        start_index: int,
        dataset_name: str,
        node_rank: int,
        data_recipe: "DataRecipe",
        input_dir: str,
        remote_input_dir: str,
        remote_output_dir: Optional[str],
        items: List[Any],
        progress_queue: Queue,
        error_queue: Queue,
        stop_queue: Queue,
        num_downloaders: int,
        remove: bool,
    ) -> None:
        """The BaseWorker is responsible to process the user data."""
        self.worker_index = worker_index
        self.num_workers = num_workers
        self.start_index = start_index
        self.dataset_name = dataset_name
        self.node_rank = node_rank
        self.data_recipe = data_recipe
        self.input_dir = input_dir
        self.remote_input_dir = remote_input_dir
        self.remote_output_dir = remote_output_dir
        self.items = items
        self.num_items = len(self.items)
        self.num_downloaders = num_downloaders
        self.remove = remove
        self.paths: List[List[str]] = []
        self.remover: Optional[Process] = None
        self.downloaders: List[Process] = []
        self.to_download_queues: List[Queue] = []
        self.stop_queue = stop_queue
        self.ready_to_process_queue: Queue = Queue()
        self.remove_queue: Queue = Queue()
        self.upload_queue: Queue = Queue()
        self.progress_queue: Queue = progress_queue
        self.error_queue: Queue = error_queue
        self.uploader: Optional[Process] = None
        self._collected_items = 0
        self._counter = 0
        self._last_time = time()
        self._index_counter = 0
        self._current_item: Any = None

    def run(self) -> None:
        try:
            self._setup()
            self._loop()
        except Exception:
            traceback_format = traceback.format_exc()
            print(traceback_format)
            self.error_queue.put(traceback_format)
        print(f"Worker {self.worker_index} is done.")

    def _setup(self) -> None:
        self._set_environ_variables()
        self._create_cache()
        self._collect_paths()
        self._start_downloaders()
        self._start_uploader()
        self._start_remover()

    def _loop(self) -> None:
        num_downloader_finished = 0

        while True:
            index = self.ready_to_process_queue.get()

            if index is None:
                num_downloader_finished += 1
                if num_downloader_finished == self.num_downloaders:
                    if isinstance(self.data_recipe, DataChunkRecipe):
                        self._handle_data_chunk_recipe_end()

                    if self.remote_output_dir:
                        assert self.uploader
                        self.upload_queue.put(None)
                        self.uploader.join()

                    if self.remove:
                        assert self.remover
                        self.remove_queue.put(None)
                        self.remover.join()

                    if self.progress_queue:
                        self.progress_queue.put((self.worker_index, self._counter))
                    return
                continue

            if isinstance(self.data_recipe, DataChunkRecipe):
                self._handle_data_chunk_recipe(index)
            else:
                self._handle_data_transform_recipe(index)

            self._counter += 1

            # Don't send the last progress update, so the main thread awaits for the uploader and remover
            if self.progress_queue and (time() - self._last_time) > 1 and self._counter < (self.num_items - 2):
                self.progress_queue.put((self.worker_index, self._counter))
                self._last_time = time()

            if self.remove:
                self.remove_queue.put(self.paths[index])

            try:
                self.stop_queue.get(timeout=0.0001)
                return
            except Empty:
                pass

    def _set_environ_variables(self) -> None:
        # set the optimizer global rank and world_size
        os.environ["DATA_OPTIMIZER_GLOBAL_RANK"] = str(_get_node_rank() * self.num_workers + self.worker_index)
        os.environ["DATA_OPTIMIZER_NUM_WORKERS"] = str(self.num_workers)

    def _create_cache(self) -> None:
        self.cache_data_dir = _get_cache_data_dir(self.dataset_name)
        os.makedirs(self.cache_data_dir, exist_ok=True)

        self.cache_chunks_dir = _get_cache_dir(self.dataset_name)
        os.makedirs(self.cache_chunks_dir, exist_ok=True)

        if isinstance(self.data_recipe, DataTransformRecipe):
            return

        self.cache = Cache(
            self.cache_chunks_dir,
            chunk_bytes=self.data_recipe.chunk_bytes,
            chunk_size=self.data_recipe.chunk_size,
            compression=self.data_recipe.compression,
        )
        self.cache._reader._rank = _get_node_rank() * self.num_workers + self.worker_index

    def _try_upload(self, filepath: Optional[str]) -> None:
        if not filepath or self.remote_output_dir is None:
            return

        assert os.path.exists(filepath), filepath
        self.upload_queue.put(filepath)

    def _collect_paths(self) -> None:
        items = []
        for item in self.items:
            flattened_item, spec = tree_flatten(item)

            # For speed reasons, we assume starting with `self.input_dir` is enough to be a real file.
            # Other alternative would be too slow.
            # TODO: Try using dictionary for higher accurary.
            indexed_paths = {
                index: element
                for index, element in enumerate(flattened_item)
                if isinstance(element, str)
                and (
                    element.startswith(self.input_dir) if self.input_dir is not None else os.path.exists(element)
                )  # For speed reasons
            }

            if len(indexed_paths) == 0:
                raise ValueError(f"The provided item {item} didn't contain any filepaths. {flattened_item}")

            paths = []
            for index, path in indexed_paths.items():
                paths.append(path)
                if self.input_dir:
                    path = path.replace(self.input_dir, self.cache_data_dir)
                flattened_item[index] = path

            self.paths.append(paths)

            items.append(tree_unflatten(flattened_item, spec))
            self._collected_items += 1

        self.items = items

    def _start_downloaders(self) -> None:
        for _ in range(self.num_downloaders):
            to_download_queue: Queue = Queue()
            p = Process(
                target=_download_data_target,
                args=(
                    self.input_dir,
                    self.remote_input_dir,
                    self.cache_data_dir,
                    to_download_queue,
                    self.ready_to_process_queue,
                ),
            )
            p.start()
            self.downloaders.append(p)
            self.to_download_queues.append(to_download_queue)

        for index, paths in enumerate(self.paths):
            self.to_download_queues[index % self.num_downloaders].put((index, paths))

        for downloader_index in range(self.num_downloaders):
            self.to_download_queues[downloader_index].put(None)

    def _start_remover(self) -> None:
        if not self.remove:
            return
        self.remover = Process(
            target=_remove_target,
            args=(
                self.input_dir,
                self.cache_data_dir,
                self.remove_queue,
            ),
        )
        self.remover.start()

    def _start_uploader(self) -> None:
        if self.remote_output_dir is None:
            return
        self.uploader = Process(
            target=_upload_fn,
            args=(
                self.upload_queue,
                self.remove_queue,
                self.cache_chunks_dir,
                self.remote_output_dir,
            ),
        )
        self.uploader.start()

    def _handle_data_chunk_recipe(self, index: int) -> None:
        try:
            self._current_item = self.items[index]
            item_data_or_generator = self.data_recipe.prepare_item(self._current_item)
            if isinstance(item_data_or_generator, types.GeneratorType):
                for item_data in item_data_or_generator:
                    if item_data is not None:
                        chunk_filepath = self.cache._add_item(self._index_counter, item_data)
                        self._try_upload(chunk_filepath)
                        self._index_counter += 1
            elif item_data_or_generator is not None:
                chunk_filepath = self.cache._add_item(self._index_counter, item_data_or_generator)
                self._try_upload(chunk_filepath)
                self._index_counter += 1
        except Exception as e:
            raise RuntimeError(f"Failed processing {self._current_item}") from e

    def _handle_data_chunk_recipe_end(self) -> None:
        chunks_filepaths = self.cache.done()

        if chunks_filepaths:
            for chunk_filepath in chunks_filepaths:
                if isinstance(chunk_filepath, str) and os.path.exists(chunk_filepath):
                    self.upload_queue.put(chunk_filepath)

    def _handle_data_transform_recipe(self, index: int) -> None:
        # Don't use a context manager to avoid deleting files that are being uploaded.
        output_dir = tempfile.mkdtemp()
        item_data = self.data_recipe.prepare_item(str(output_dir), self.items[index])
        if item_data is not None:
            raise ValueError(
                "When using a `DataTransformRecipe`, the `prepare_item` shouldn't return anything."
                " Simply store your files under the output_dir."
            )
        filepaths = []
        for directory, _, filenames in os.walk(output_dir):
            for filename in filenames:
                filepaths.append(os.path.join(directory, filename))

        if len(filepaths) == 0:
            raise RuntimeError("You haven't saved any files under the `output_dir`.")

        for filepath in filepaths:
            self._try_upload(filepath)


class DataWorkerProcess(BaseWorker, Process):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """The DataWorkerProcess is responsible to process the user data inside processes."""
        BaseWorker.__init__(self, *args, **kwargs)
        Process.__init__(self)


T = TypeVar("T")


class DataRecipe:
    @abstractmethod
    def prepare_structure(self, input_dir: Optional[str]) -> List[T]:
        pass

    @abstractmethod
    def prepare_item(self, *args: Any) -> Any:
        pass

    def listdir(self, path: str) -> List[str]:
        home = _get_home_folder()
        filepath = os.path.join(home, ".cache", f"{self._name}/filepaths.txt")

        if os.path.exists(filepath):
            lines = []
            with open(filepath) as f:
                for line in f.readlines():
                    lines.append(line.replace("\n", ""))
            return lines

        filepaths = []
        for dirpath, _, filenames in os.walk(path):
            for filename in filenames:
                filepaths.append(os.path.join(dirpath, filename))

        os.makedirs(os.path.dirname(filepath), exist_ok=True)

        with open(filepath, "w") as f:
            for filepath in filepaths:
                f.write(f"{filepath}\n")

        return filepaths

    def __init__(self) -> None:
        self._name: Optional[str] = None

    def _setup(self, name: Optional[str]) -> None:
        self._name = name

    def _done(self, delete_cached_files: bool, remote_output_dir: str) -> None:
        pass


class DataChunkRecipe(DataRecipe):
    def __init__(
        self, chunk_size: Optional[int] = None, chunk_bytes: Optional[int] = None, compression: Optional[str] = None
    ):
        super().__init__()
        if chunk_size is not None and chunk_bytes is not None:
            raise ValueError("Either one of the `chunk_size` or the `chunk_bytes` need to be provided.")

        self.chunk_size = chunk_size
        self.chunk_bytes = 1 << 26 if chunk_size is None else chunk_bytes
        self.compression = compression

    @abstractmethod
    def prepare_structure(self, input_dir: Optional[str]) -> List[T]:
        """Return the structure of your data.

        Each element should contain at least a filepath.

        """

    @abstractmethod
    def prepare_item(self, item_metadata: T) -> Any:  # type: ignore
        """The return of this `prepare_item` method is persisted in chunked binary files."""

    def _done(self, delete_cached_files: bool, remote_output_dir: str) -> None:
        num_nodes = _get_num_nodes()
        assert self._name
        cache_dir = _get_cache_dir(self._name)

        chunks = [file for file in os.listdir(cache_dir) if file.endswith(".bin")]
        if chunks and delete_cached_files and remote_output_dir:
            raise RuntimeError(f"All the chunks should have been deleted. Found {chunks}")

        merge_cache = Cache(cache_dir, chunk_bytes=1)
        node_rank = _get_node_rank()
        merge_cache._merge_no_wait(node_rank if num_nodes > 1 else None)
        self._upload_index(remote_output_dir, cache_dir, num_nodes, node_rank)

    def _upload_index(self, remote_output_dir: str, cache_dir: str, num_nodes: int, node_rank: Optional[int]) -> None:
        """This method upload the index file to the remote cloud directory."""
        if not remote_output_dir:
            return

        obj = parse.urlparse(remote_output_dir)
        if num_nodes > 1:
            local_filepath = os.path.join(cache_dir, f"{node_rank}-{_INDEX_FILENAME}")
        else:
            local_filepath = os.path.join(cache_dir, _INDEX_FILENAME)

        if obj.scheme == "s3":
            s3 = _get_s3_client()
            s3.upload_file(
                local_filepath, obj.netloc, os.path.join(obj.path.lstrip("/"), os.path.basename(local_filepath))
            )
        elif os.path.isdir(remote_output_dir):
            copyfile(local_filepath, os.path.join(remote_output_dir, os.path.basename(local_filepath)))

        if num_nodes == 1 or node_rank is None:
            return

        # Merge the index files generated by each node.
        # Note: When using the Data Optimizer, they should be a single process on each node executing this section
        # So no risk to get race conditon.
        if num_nodes == node_rank + 1:
            # Get the index file locally
            for node_rank in range(num_nodes - 1):
                remote_filepath = os.path.join(remote_output_dir, f"{node_rank}-{_INDEX_FILENAME}")
                node_index_filepath = os.path.join(cache_dir, os.path.basename(remote_filepath))
                if obj.scheme == "s3":
                    obj = parse.urlparse(remote_filepath)
                    _wait_for_file_to_exist(s3, obj)
                    with open(node_index_filepath, "wb") as f:
                        s3.download_fileobj(obj.netloc, obj.path.lstrip("/"), f)
                elif os.path.isdir(remote_output_dir):
                    copyfile(remote_filepath, node_index_filepath)

            merge_cache = Cache(cache_dir, chunk_bytes=1)
            merge_cache._merge_no_wait()
            self._upload_index(remote_output_dir, cache_dir, 1, None)


class DataTransformRecipe(DataRecipe):
    @abstractmethod
    def prepare_structure(self, input_dir: Optional[str]) -> List[T]:
        """Return the structure of your data.

        Each element should contain at least a filepath.

        """

    @abstractmethod
    def prepare_item(self, output_dir: str, item_metadata: T) -> None:  # type: ignore
        """Use your item metadata to process your files and save the file outputs into `output_dir`."""


class DataProcessor:
    def __init__(
        self,
        name: Optional[str] = None,
        input_dir: Optional[str] = None,
        num_workers: Optional[int] = None,
        num_downloaders: Optional[int] = None,
        delete_cached_files: bool = True,
        src_resolver: Optional[Callable[[str], Optional[str]]] = None,
        fast_dev_run: Optional[bool] = None,
        remote_input_dir: Optional[str] = None,
        remote_output_dir: Optional[str] = None,
        random_seed: Optional[int] = 42,
    ):
        """The `DatasetOptimiser` provides an efficient way to process data across multiple machine into chunks to make
        training faster.

        Arguments:
            name: The name of your dataset.
            input_dir: The path to where the data are stored.
            num_workers: The number of worker threads to use.
            num_downloaders: The number of file downloaders to use.
            delete_cached_files: Whether to delete the cached files.
            fast_dev_run: Whether to run a quick dev run.
            remote_input_dir: The remote folder where the data are.
            remote_output_dir: The remote folder where the optimised data will be stored.
            random_seed: The random seed to be set before shuffling the data.

        """
        self.name = name
        self.input_dir = str(input_dir) if input_dir else None
        self.num_workers = num_workers or (1 if fast_dev_run else (os.cpu_count() or 1) * 4)
        self.num_downloaders = num_downloaders or 1
        self.delete_cached_files = delete_cached_files
        self.fast_dev_run = _get_fast_dev_run() if fast_dev_run is None else fast_dev_run
        self.workers: Any = []
        self.src_resolver = src_resolver or _LightningSrcResolver()
        self.dst_resolver = _LightningTargetResolver()
        self.workers_tracker: Dict[int, int] = {}
        self.progress_queue: Optional[Queue] = None
        self.error_queue: Queue = Queue()
        self.stop_queues: List[Queue] = []
        self.remote_input_dir = (
            str(remote_input_dir)
            if remote_input_dir is not None
            else ((self.src_resolver(input_dir) if input_dir else None) if self.src_resolver else None)
        )
        self.remote_output_dir = (
            remote_output_dir
            if remote_output_dir is not None
            else (self.dst_resolver(name) if self.dst_resolver else None)
        )
        if self.remote_output_dir:
            self.name = self._broadcast_object(self.name)
            # Ensure the remote src dir is the same across all ranks
            self.remote_output_dir = self._broadcast_object(self.remote_output_dir)
            print(f"Storing the files under {self.remote_output_dir}")

        self.random_seed = random_seed

    def run(self, data_recipe: DataRecipe) -> None:
        """The `DataProcessor.run(...)` method triggers the data recipe processing over your dataset."""
        if not isinstance(data_recipe, DataRecipe):
            raise ValueError("The provided value should be a data recipe.")

        t0 = time()
        print(f"Setup started for `{self.name}` with fast_dev_run={self.fast_dev_run}.")

        # Force random seed to be fixed
        seed_everything(self.random_seed)

        # Attach the name to the data recipe
        data_recipe._setup(self.name)

        # Call the setup method of the user
        user_items: List[Any] = data_recipe.prepare_structure(self.input_dir)

        if not isinstance(user_items, list):
            raise ValueError("The setup_fn should return a list of item metadata.")

        # Associate the items to the workers based on num_nodes and node_rank
        begins, workers_user_items = _associated_items_to_workers(self.num_workers, user_items)
        print(f"Setup finished in {round(time() - t0, 3)} seconds. Found {len(user_items)} items to process.")

        if self.fast_dev_run:
            items_to_keep = self.fast_dev_run if isinstance(self.fast_dev_run, int) else _DEFAULT_FAST_DEV_RUN_ITEMS
            workers_user_items = [w[:items_to_keep] for w in workers_user_items]
            print(f"Fast dev run is enabled. Limiting to {items_to_keep} items per process.")

        num_items = sum([len(items) for items in workers_user_items])

        self._cleanup_cache()

        print(f"Starting {self.num_workers} workers")

        if self.remote_input_dir is None and self.src_resolver is not None and self.input_dir:
            self.remote_input_dir = self.src_resolver(self.input_dir)
            print(f"The remote_dir is `{self.remote_input_dir}`.")

        signal.signal(signal.SIGINT, self._signal_handler)

        self._create_process_workers(data_recipe, begins, workers_user_items)

        print("Workers are ready ! Starting data processing...")

        current_total = 0
        with tqdm(total=num_items, smoothing=0, position=-1, mininterval=1) as pbar:
            while True:
                try:
                    error = self.error_queue.get(timeout=0.001)
                    self._exit_on_error(error)
                except Empty:
                    assert self.progress_queue
                    try:
                        index, counter = self.progress_queue.get(timeout=0.001)
                    except Empty:
                        continue
                    self.workers_tracker[index] = counter
                    new_total = sum(self.workers_tracker.values())
                pbar.update(new_total - current_total)
                current_total = new_total
                if current_total == num_items:
                    break

        num_nodes = _get_num_nodes()

        # TODO: Understand why it hangs.
        if num_nodes == 1:
            for w in self.workers:
                w.join(0)

        print("Workers are finished.")
        data_recipe._done(self.delete_cached_files, self.remote_output_dir)
        print("Finished data processing!")

    def _exit_on_error(self, error: str) -> None:
        for w in self.workers:
            w.join(0)
        raise RuntimeError(f"We found the following error {error}.")

    def _create_process_workers(
        self, data_recipe: DataRecipe, begins: List[int], workers_user_items: List[List[Any]]
    ) -> None:
        self.progress_queue = Queue()
        workers: List[DataWorkerProcess] = []
        stop_queues: List[Queue] = []
        for worker_idx, worker_user_items in enumerate(workers_user_items):
            stop_queues.append(Queue())
            worker = DataWorkerProcess(
                worker_idx,
                self.num_workers,
                begins[worker_idx],
                self.name,
                _get_node_rank(),
                data_recipe,
                self.input_dir,
                self.remote_input_dir,
                self.remote_output_dir,
                worker_user_items,
                self.progress_queue,
                self.error_queue,
                stop_queues[-1],
                self.num_downloaders,
                self.delete_cached_files,
            )
            worker.start()
            workers.append(worker)

        # Note: Don't store within the loop as weakref aren't serializable
        self.workers = workers
        self.stop_queues = stop_queues

    def _associated_items_to_workers(self, user_items: List[Any]) -> Tuple[List[int], List[List[Any]]]:
        # Associate the items to the workers based on world_size and node_rank
        num_nodes = _get_num_nodes()
        current_node_rank = _get_node_rank()
        node_size = len(user_items) // num_nodes
        workers_user_items = []
        begins = []
        for node_rank in range(num_nodes):
            if node_rank != current_node_rank:
                continue
            is_last_node = node_rank == num_nodes - 1
            start_node = node_rank * node_size
            end_node = len(user_items) if is_last_node else (node_rank + 1) * node_size
            node_user_items = user_items[start_node:end_node]
            worker_size = len(node_user_items) // self.num_workers
            for worker_idx in range(self.num_workers):
                is_last = worker_idx == self.num_workers - 1
                begin = worker_idx * worker_size
                end = len(node_user_items) if is_last else (worker_idx + 1) * worker_size
                workers_user_items.append(user_items[begin:end])
                begins.append(begin)
            return begins, workers_user_items
        raise RuntimeError(f"The current_node_rank {current_node_rank} doesn't exist in {num_nodes}.")

    def _signal_handler(self, signal: Any, frame: Any) -> None:
        """On temrination, we stop all the processes to avoid leaking RAM."""
        for stop_queue in self.stop_queues:
            stop_queue.put(None)
        for w in self.workers:
            w.join(0)
        os._exit(0)

    def _cleanup_cache(self) -> None:
        cache_dir = _get_cache_dir(self.name)

        # Cleanup the cache dir folder to avoid corrupted files from previous run to be there.
        if os.path.exists(cache_dir):
            rmtree(cache_dir)

        os.makedirs(cache_dir, exist_ok=True)

        cache_data_dir = _get_cache_data_dir(self.name)

        # Cleanup the cache data folder to avoid corrupted files from previous run to be there.
        if os.path.exists(cache_data_dir):
            rmtree(cache_data_dir)

        os.makedirs(cache_data_dir, exist_ok=True)

    def _broadcast_object(self, obj: Any) -> Any:
        """Enable to synchronize an object across machines using torch.distributed.collectives."""
        num_nodes = _get_num_nodes()
        if num_nodes == 1:
            return obj

        if not _distributed_is_initialized():
            process_group_backend = "nccl" if is_cuda_available() else "gloo"
            _init_dist_connection(LightningEnvironment(), process_group_backend, _get_node_rank(), num_nodes)

        obj = [obj]
        torch.distributed.broadcast_object_list(obj, 0, group=_group.WORLD)
        return obj[0]
