"""Lazy offline EmbeddingGemma worker used by the installation service.

The installer must pass an already hash-verified, immutable model directory.
This module never acquires weights, accepts repositories, or opens a server.
Heavy dependencies and model data are loaded only in the isolated child, on an
explicit embedding request. Project authorization belongs to the caller.
"""

from __future__ import annotations

import concurrent.futures
import itertools
import json
import math
import multiprocessing
import os
from pathlib import Path
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, Literal, Sequence


MODEL_ID = "google/embeddinggemma-300m"
MODEL_REVISION = "57c266a740f537b4dc058e1b0cda161fd15afa75"
VECTOR_DIMENSIONS = 768
MAX_TOKENS = 2048
PREPROCESSING_VERSION = "embeddinggemma-retrieval-v1"


@dataclass(frozen=True)
class EmbeddingProfile:
    model_id: str = MODEL_ID
    revision: str = MODEL_REVISION
    dimensions: int = VECTOR_DIMENSIONS
    normalized: bool = True
    preprocessing: str = PREPROCESSING_VERSION
    precision: str = "float32"


PROFILE = EmbeddingProfile()


@dataclass(frozen=True)
class RuntimeLimits:
    cpu_threads: int = 2
    batch_size: int = 4
    pending_requests: int = 8
    request_timeout_seconds: float = 60.0
    max_input_bytes: int = 32768

    def __post_init__(self):
        for value, maximum in ((self.cpu_threads, 4), (self.batch_size, 16),
                               (self.pending_requests, 64), (self.max_input_bytes, 65536)):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("Invalid embedding runtime limit")
        if (isinstance(self.request_timeout_seconds, bool)
                or not math.isfinite(self.request_timeout_seconds)
                or not 0 < self.request_timeout_seconds <= 300):
            raise ValueError("Invalid embedding runtime timeout")


class EmbeddingRuntimeError(Exception):
    """Only stable codes cross the worker boundary; never upstream exceptions."""

    CODES = frozenset({"unavailable", "busy", "timeout", "closed", "invalid_input",
                       "input_too_long", "invalid_output"})

    def __init__(self, code: str):
        self.code = code if code in self.CODES else "unavailable"
        super().__init__(self.code)


def format_inputs(kind: Literal["query", "document"], texts: Sequence[str],
                  titles: Sequence[str] | None, limits: RuntimeLimits) -> tuple[str, ...]:
    if (kind not in ("query", "document") or isinstance(texts, (str, bytes))
            or not 1 <= len(texts) <= limits.batch_size):
        raise EmbeddingRuntimeError("invalid_input")
    if titles is not None and (kind != "document" or isinstance(titles, (str, bytes))
                               or len(titles) != len(texts)):
        raise EmbeddingRuntimeError("invalid_input")
    formatted = []
    for index, text in enumerate(texts):
        title = titles[index] if titles is not None else "none"
        if (not isinstance(text, str) or not text.strip() or not isinstance(title, str)
                or len(title) > 1024):
            raise EmbeddingRuntimeError("invalid_input")
        value = (f"task: search result | query: {text}" if kind == "query"
                 else f"title: {title or 'none'} | text: {text}")
        try:
            length = len(value.encode("utf-8"))
        except UnicodeError:
            raise EmbeddingRuntimeError("invalid_input") from None
        if length > limits.max_input_bytes:
            raise EmbeddingRuntimeError("input_too_long")
        formatted.append(value)
    return tuple(formatted)


def validate_vectors(value, count: int, *, dimensions: int = VECTOR_DIMENSIONS) -> list[list[float]]:
    if not isinstance(value, list) or len(value) != count:
        raise EmbeddingRuntimeError("invalid_output")
    result = []
    for row in value:
        if not isinstance(row, list) or len(row) != dimensions:
            raise EmbeddingRuntimeError("invalid_output")
        if any(type(item) not in (int, float) or not math.isfinite(item) for item in row):
            raise EmbeddingRuntimeError("invalid_output")
        if not math.isclose(math.sqrt(sum(item * item for item in row)), 1.0, abs_tol=0.002):
            raise EmbeddingRuntimeError("invalid_output")
        result.append([float(item) for item in row])
    return result


def _load_sentence_transformer(model_directory: str, limits: RuntimeLimits):
    # Never consult home-directory tokens or caches, even if a configuration file
    # references the Hub. Actual acquisition is a separate explicit operation.
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                      HF_HUB_DISABLE_TELEMETRY="1", TOKENIZERS_PARALLELISM="false",
                      OMP_NUM_THREADS=str(limits.cpu_threads),
                      MKL_NUM_THREADS=str(limits.cpu_threads))
    directory = Path(model_directory)
    if not directory.is_absolute() or not directory.is_dir():
        raise EmbeddingRuntimeError("unavailable")
    modules = json.loads((directory / "modules.json").read_text())
    expected = [("", "Transformer"), ("1_Pooling", "Pooling"), ("2_Dense", "Dense"),
                ("3_Dense", "Dense"), ("4_Normalize", "Normalize")]
    if (not isinstance(modules, list) or len(modules) != len(expected)
            or any(module.get("path") != path
                   or module.get("type") != f"sentence_transformers.models.{name}"
                   for module, (path, name) in zip(modules, expected))):
        raise EmbeddingRuntimeError("unavailable")
    import torch
    from sentence_transformers import SentenceTransformer

    torch.set_num_threads(limits.cpu_threads)
    torch.set_num_interop_threads(1)
    model = SentenceTransformer(
        str(directory), device="cpu", local_files_only=True, trust_remote_code=False,
        token=False, model_kwargs={"torch_dtype": torch.float32, "use_safetensors": True},
    )
    model.eval()
    model.max_seq_length = MAX_TOKENS
    if model.get_sentence_embedding_dimension() != VECTOR_DIMENSIONS:
        raise EmbeddingRuntimeError("unavailable")

    def encode(texts: tuple[str, ...]):
        # Check the complete formatted input before the library can truncate it.
        tokenized = model.tokenizer(list(texts), truncation=False, padding=False,
                                    add_special_tokens=True)
        if any(len(ids) > MAX_TOKENS for ids in tokenized["input_ids"]):
            raise EmbeddingRuntimeError("input_too_long")
        with torch.inference_mode():
            return model.encode(list(texts), batch_size=limits.batch_size, prompt="",
                                show_progress_bar=False, normalize_embeddings=True,
                                convert_to_numpy=True).tolist()

    return encode


def _worker(connection, model_directory: str, limits: RuntimeLimits, backend_factory):
    # Library progress/errors may include source text or paths. The child only
    # returns validated vectors and stable error codes through its private pipe.
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
    encode = None
    try:
        while True:
            texts = connection.recv()
            if texts is None:
                break
            try:
                if encode is None:
                    encode = backend_factory(model_directory, limits)
                result = validate_vectors(encode(texts), len(texts))
                connection.send(("ok", result))
            except EmbeddingRuntimeError as error:
                connection.send(("error", error.code))
            except Exception:
                connection.send(("error", "unavailable"))
    except (EOFError, BrokenPipeError, OSError):
        pass
    finally:
        connection.close()


class LocalEmbeddingRuntime:
    """One lazy CPU worker, a bounded priority queue, and enforced deadlines.

    Query requests precede queued ingestion; already-running inference is not
    preempted. A timeout, cancellation or child crash discards that child and
    the next request gets a clean one. No model is loaded merely by constructing
    this object. Call close() during application shutdown/removal.
    """

    profile = PROFILE

    def __init__(self, model_directory: Path, *, limits: RuntimeLimits | None = None,
                 _backend_factory: Callable = _load_sentence_transformer):
        self._directory = str(model_directory.absolute())
        self._limits = limits or RuntimeLimits()
        self._backend_factory = _backend_factory
        self._queue = queue.PriorityQueue()
        self._slots = threading.BoundedSemaphore(self._limits.pending_requests)
        self._counter = itertools.count()
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._process = None
        self._connection = None

    @property
    def worker_pid(self) -> int | None:
        return self._process.pid if self._process is not None else None

    def submit(self, kind: Literal["query", "document"], texts: Sequence[str], *,
               titles: Sequence[str] | None = None) -> concurrent.futures.Future:
        formatted = format_inputs(kind, texts, titles, self._limits)
        with self._lock:
            if self._closed.is_set():
                raise EmbeddingRuntimeError("closed")
            if not self._slots.acquire(blocking=False):
                raise EmbeddingRuntimeError("busy")
            future = concurrent.futures.Future()
            deadline = time.monotonic() + self._limits.request_timeout_seconds
            self._queue.put((0 if kind == "query" else 1, next(self._counter),
                             deadline, formatted, future))
            if self._thread is None:
                self._thread = threading.Thread(target=self._dispatch, daemon=True,
                                                name="embedding-dispatch")
                self._thread.start()
            return future

    def embed_queries(self, texts: Sequence[str]) -> list[list[float]]:
        return self.submit("query", texts).result()

    def embed_documents(self, texts: Sequence[str], *,
                        titles: Sequence[str] | None = None) -> list[list[float]]:
        return self.submit("document", texts, titles=titles).result()

    def _stop_worker(self, *, graceful=False):
        if graceful and self._connection is not None and self._process is not None:
            try:
                self._connection.send(None)
                self._process.join(timeout=1)
            except (BrokenPipeError, EOFError, OSError):
                pass
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._process is not None:
            if self._process.is_alive():
                self._process.terminate()
                self._process.join(timeout=1)
            if self._process.is_alive():
                self._process.kill()
                self._process.join(timeout=1)
            self._process.close()
            self._process = None

    def _start_worker(self):
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(target=_worker, args=(child, self._directory,
                                  self._limits, self._backend_factory), daemon=True)
        process.start()
        child.close()
        self._connection, self._process = parent, process

    def _run(self, texts, deadline, future):
        if time.monotonic() >= deadline:
            raise EmbeddingRuntimeError("timeout")
        if self._process is None:
            self._start_worker()
        self._connection.send(texts)
        while not self._connection.poll(0.05):
            if future.cancelled() or self._closed.is_set():
                raise EmbeddingRuntimeError("closed")
            if time.monotonic() >= deadline:
                raise EmbeddingRuntimeError("timeout")
            if not self._process.is_alive():
                raise EmbeddingRuntimeError("unavailable")
        if time.monotonic() >= deadline:
            raise EmbeddingRuntimeError("timeout")
        status, value = self._connection.recv()
        if status != "ok":
            raise EmbeddingRuntimeError(value)
        return validate_vectors(value, len(texts))

    def _dispatch(self):
        try:
            while not self._closed.is_set():
                try:
                    _, _, deadline, texts, future = self._queue.get(timeout=0.05)
                except queue.Empty:
                    continue
                try:
                    if not future.cancelled():
                        result = self._run(texts, deadline, future)
                        if not future.done():
                            future.set_result(result)
                except Exception as error:
                    if not (isinstance(error, EmbeddingRuntimeError)
                            and error.code in {"invalid_input", "input_too_long"}):
                        self._stop_worker()
                    if not future.done():
                        safe = error if isinstance(error, EmbeddingRuntimeError) else EmbeddingRuntimeError("unavailable")
                        future.set_exception(safe)
                finally:
                    self._slots.release()
                    self._queue.task_done()
        finally:
            self._stop_worker(graceful=True)
            while True:
                try:
                    _, _, _, _, future = self._queue.get_nowait()
                except queue.Empty:
                    break
                if not future.done():
                    future.set_exception(EmbeddingRuntimeError("closed"))
                self._slots.release()
                self._queue.task_done()

    def close(self):
        with self._lock:
            self._closed.set()
        if self._thread is not None:
            self._thread.join(timeout=3)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
