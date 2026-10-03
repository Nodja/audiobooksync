"""Fetch the exported Parakeet-CTC graph from the Hugging Face Hub.

The graph is downloaded on first use through `huggingface_hub`, which caches it.  The tokenizer
comes from the same repository, so vocabulary and weights share a revision.

`onnx-community/parakeet-ctc-0.6b-ONNX` publishes `nvidia/parakeet-ctc-0.6b` in several
precisions.  `q4` is the default: 3.8x smaller than fp32, with the same word placement on the test
book (see `docs/parakeet-alignment-findings.md`).  It needs onnxruntime 1.22 or newer
(`MatMulNBits`).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

#: Hub repository publishing the exported graphs, tokeniser and config.
DEFAULT_REPO = "onnx-community/parakeet-ctc-0.6b-ONNX"

#: Variant name -> graph inside the repository.  Weights sit beside it as ``<graph>_data``.
VARIANTS: dict[str, str] = {
    "q4": "onnx/model_q4.onnx",
    "q4f16": "onnx/model_q4f16.onnx",
    "int8": "onnx/model_int8.onnx",
    "uint8": "onnx/model_uint8.onnx",
    "quantized": "onnx/model_quantized.onnx",
    "bnb4": "onnx/model_bnb4.onnx",
    "fp16": "onnx/model_fp16.onnx",
    "fp32": "onnx/model.onnx",
}

DEFAULT_VARIANT = "q4"


class ModelError(RuntimeError):
    """The graph could not be fetched, or the repository does not have the variant asked for."""


@dataclass(frozen=True)
class ModelFiles:
    """Where a variant lives once the Hub cache has it."""

    repo: str
    variant: str
    graph: Path
    weights: Path

    @property
    def total_mb(self) -> float:
        return (self.graph.stat().st_size + self.weights.stat().st_size) / 1024 / 1024


def fetch(
    repo: str = DEFAULT_REPO,
    variant: str = DEFAULT_VARIANT,
    progress=lambda _message: None,
) -> ModelFiles:
    """Download the graph and its external weights, or reuse the cached copies."""
    from huggingface_hub import hf_hub_download
    from huggingface_hub.errors import EntryNotFoundError, RepositoryNotFoundError

    if variant not in VARIANTS:
        raise ModelError(f"unknown variant {variant!r}; expected one of {', '.join(VARIANTS)}")

    graph_name = VARIANTS[variant]
    try:
        graph = Path(hf_hub_download(repo, graph_name))
        # The weights (613 MB) are a separate file and must be requested explicitly.
        weights = Path(hf_hub_download(repo, f"{graph_name}_data"))
    except (RepositoryNotFoundError, EntryNotFoundError) as exc:
        raise ModelError(
            f"{repo} does not publish {graph_name} ({type(exc).__name__}).\n"
            f"       available variants: {', '.join(VARIANTS)}"
        ) from exc

    files = ModelFiles(repo=repo, variant=variant, graph=graph, weights=weights)
    progress(f"model: {variant} {files.total_mb:.0f} MB -> {graph}")
    return files


def available_variants() -> list[str]:
    """Variant names, in the order the CLI lists them."""
    return list(VARIANTS)


__all__ = [
    "DEFAULT_REPO",
    "DEFAULT_VARIANT",
    "VARIANTS",
    "ModelError",
    "ModelFiles",
    "available_variants",
    "fetch",
]
