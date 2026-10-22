import concurrent.futures
from pathlib import Path
import os
import time

import pytest

from feedback.ai_runtime import (
    EmbeddingRuntimeError, LocalEmbeddingRuntime, RuntimeLimits, VECTOR_DIMENSIONS,
    format_inputs, validate_vectors,
)


def fixture_backend(directory, limits):
    """Spawn-safe local protocol fixture. It is never a production backend."""
    root = Path(directory)
    (root / "loaded").write_text(str(os.getpid()))

    def encode(texts):
        if "BLOCK" in texts[0]:
            (root / "active").touch()
            while not (root / "release").exists():
                time.sleep(0.01)
        if "HANG" in texts[0]:
            time.sleep(10)
        if "CRASH" in texts[0]:
            os._exit(3)
        if "SECRET" in texts[0]:
            raise ValueError("PRIVATE_CONTENT_AND_TOKEN")
        if "TOKEN_LONG" in texts[0]:
            raise EmbeddingRuntimeError("input_too_long")
        if "BAD" in texts[0]:
            return [[float("nan")] * VECTOR_DIMENSIONS]
        with (root / "order").open("a") as stream:
            stream.write(texts[0] + "\n")
        return [[1.0] + [0.0] * (VECTOR_DIMENSIONS - 1) for _ in texts]

    return encode


def wait_for(path):
    deadline = time.monotonic() + 3
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists()


def make_runtime(tmp_path, **kwargs):
    return LocalEmbeddingRuntime(tmp_path, limits=RuntimeLimits(**kwargs),
                                 _backend_factory=fixture_backend)


def test_formats_queries_and_documents_without_changing_original_text():
    limits = RuntimeLimits()
    assert format_inputs("query", [" Reset? "], None, limits) == (
        "task: search result | query:  Reset? ",)
    assert format_inputs("document", ["Click Reset"], ["Accounts"], limits) == (
        "title: Accounts | text: Click Reset",)
    assert format_inputs("document", ["Click Reset"], None, limits) == (
        "title: none | text: Click Reset",)


@pytest.mark.parametrize("texts,titles", [([], None), ([""], None), ("text", None),
                                           (["text"], ["one", "two"]), ([1], None)])
def test_rejects_malformed_inputs(texts, titles):
    with pytest.raises(EmbeddingRuntimeError, match="invalid_input"):
        format_inputs("document", texts, titles, RuntimeLimits())


def test_input_budget_includes_prompt_and_uses_utf8_bytes():
    with pytest.raises(EmbeddingRuntimeError, match="input_too_long"):
        format_inputs("query", ["é" * 20], None, RuntimeLimits(max_input_bytes=50))


@pytest.mark.parametrize("vector", [[0.0] * 768, [1.0], [float("inf")] * 768,
                                    [True] + [0] * 767, [float("nan")] * 768])
def test_rejects_nonfinite_unnormalized_or_wrong_dimension_vectors(vector):
    with pytest.raises(EmbeddingRuntimeError, match="invalid_output"):
        validate_vectors([vector], 1)


def test_lazy_worker_loads_only_on_explicit_call_and_reuses_process(tmp_path):
    with make_runtime(tmp_path) as runtime:
        assert runtime.worker_pid is None
        assert not (tmp_path / "loaded").exists()
        assert len(runtime.embed_queries(["hello"])[0]) == 768
        pid = runtime.worker_pid
        runtime.embed_documents(["A document"])
        assert runtime.worker_pid == pid
        assert pid != os.getpid()


def test_query_priority_and_bounded_pending_work(tmp_path):
    with make_runtime(tmp_path, pending_requests=3) as runtime:
        first = runtime.submit("document", ["BLOCK"])
        wait_for(tmp_path / "active")
        document = runtime.submit("document", ["bulk"])
        query = runtime.submit("query", ["interactive"])
        with pytest.raises(EmbeddingRuntimeError, match="busy"):
            runtime.submit("query", ["overflow"])
        (tmp_path / "release").touch()
        for future in (first, query, document):
            assert len(future.result(timeout=3)[0]) == 768
    order = (tmp_path / "order").read_text().splitlines()
    assert "BLOCK" in order[0]
    assert "interactive" in order[1]
    assert "bulk" in order[2]


@pytest.mark.parametrize("bad,code", [("HANG", "timeout"), ("SECRET", "unavailable"),
                                     ("CRASH", "unavailable"), ("BAD", "invalid_output")])
def test_failure_is_safe_and_next_request_uses_clean_worker(tmp_path, bad, code):
    with make_runtime(tmp_path, request_timeout_seconds=1.5) as runtime:
        with pytest.raises(EmbeddingRuntimeError, match=code) as raised:
            runtime.embed_queries([bad])
        assert "PRIVATE_CONTENT" not in str(raised.value)
        assert len(runtime.embed_queries(["retry"])[0]) == 768


def test_cancelling_active_work_terminates_child(tmp_path):
    with make_runtime(tmp_path) as runtime:
        future = runtime.submit("query", ["BLOCK"])
        wait_for(tmp_path / "active")
        pid = runtime.worker_pid
        assert future.cancel()
        assert len(runtime.embed_queries(["next"])[0]) == 768
        assert runtime.worker_pid != pid
        with pytest.raises(concurrent.futures.CancelledError):
            future.result()


def test_token_limit_error_keeps_healthy_model_loaded(tmp_path):
    with make_runtime(tmp_path) as runtime:
        runtime.embed_queries(["hello"])
        pid = runtime.worker_pid
        with pytest.raises(EmbeddingRuntimeError, match="input_too_long"):
            runtime.embed_queries(["TOKEN_LONG"])
        runtime.embed_queries(["next"])
        assert runtime.worker_pid == pid


def test_close_finishes_queued_futures_and_rejects_new_work(tmp_path):
    runtime = make_runtime(tmp_path)
    first = runtime.submit("query", ["BLOCK"])
    wait_for(tmp_path / "active")
    second = runtime.submit("document", ["queued"])
    runtime.close()
    for future in (first, second):
        with pytest.raises(EmbeddingRuntimeError, match="closed"):
            future.result(timeout=1)
    assert runtime.worker_pid is None
    with pytest.raises(EmbeddingRuntimeError, match="closed"):
        runtime.submit("query", ["later"])
    runtime.close()


def test_missing_model_fails_locally_without_optional_dependencies(tmp_path):
    with LocalEmbeddingRuntime(tmp_path) as runtime:
        with pytest.raises(EmbeddingRuntimeError, match="unavailable"):
            runtime.embed_queries(["hello"])
