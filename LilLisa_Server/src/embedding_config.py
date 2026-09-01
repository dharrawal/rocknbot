"""
embedding_config.py
====================================
Standalone embedding configuration, extracted out of src/main.py so it can be
imported without dragging in the whole FastAPI app (routes, lifespan startup,
GitHub cloning, etc.). Anything that needs to embed text the same way the
server's CONTEXTUAL chunking strategy does -- main.py itself, or standalone
scripts like lil-lisa-cron-scripts/techsupport_qa_ingest.py -- should import from here
instead of redefining this class.

VoyageEmbedding wraps Voyage AI's voyage-context-3 model. It requires the
VOYAGE_API_KEY environment variable to be set (main.py sets this from
VOYAGE_API_KEY_FILEPATH at startup; standalone scripts must set it themselves
before constructing this class).
"""

from typing import List

import voyageai
from llama_index.core.embeddings import BaseEmbedding

VOYAGE_EMBEDDING_DIMENSION = 2048  # Embedding dimension for Voyage AI model


class VoyageEmbedding(BaseEmbedding):
    """Voyage AI embedding implementation."""

    model_name: str = "voyage-context-3"
    output_dimension: int = VOYAGE_EMBEDDING_DIMENSION
    client: voyageai.Client = None

    def __init__(self, model: str = "voyage-context-3", output_dimension: int = VOYAGE_EMBEDDING_DIMENSION, **kwargs):
        super().__init__(**kwargs)
        self.model_name = model
        self.output_dimension = output_dimension
        # Direct client initialization - no lazy loading needed
        self.client = voyageai.Client()

    def _get_query_embedding(self, query: str) -> List[float]:
        """Get embedding for a single query - direct API call."""
        result = self.client.contextualized_embed(
            inputs=[[query]],
            model=self.model_name,
            input_type="query",
            output_dimension=self.output_dimension
        )
        return result.results[0].embeddings[0]

    def _get_text_embedding(self, text: str) -> List[float]:
        """Get embedding for a single text (used for queries)."""
        return self._get_query_embedding(text)

    def _get_text_embeddings(self, texts: List[str]) -> List[List[float]]:
        """Get embeddings for multiple texts - direct batch API call."""
        inputs = [[text] for text in texts]
        result = self.client.contextualized_embed(
            inputs=inputs,
            model=self.model_name,
            input_type="query",
            output_dimension=self.output_dimension
        )
        return [res.embeddings[0] for res in result.results]

    # Required async methods for BaseEmbedding compatibility
    async def _aget_query_embedding(self, query: str) -> List[float]:
        """Async version - just calls sync method since Voyage client is sync."""
        return self._get_query_embedding(query)

    async def _aget_text_embedding(self, text: str) -> List[float]:
        """Async version - just calls sync method since Voyage client is sync."""
        return self._get_text_embedding(text)

    async def _aget_text_embeddings(self, texts: List[str]) -> List[List[float]]:
        """Async version - just calls sync method since Voyage client is sync."""
        return self._get_text_embeddings(texts)

    @classmethod
    def get_contextualized_embeddings(cls, documents_chunks: List[List[str]], model: str = "voyage-context-3", output_dimension: int = VOYAGE_EMBEDDING_DIMENSION) -> List[List[List[float]]]:
        """
        Get contextualized embeddings for document chunks.

        Args:
            documents_chunks: List of documents, where each document is a list of chunks
            model: Voyage model name
            output_dimension: Output dimension for embeddings

        Returns:
            List of embeddings for each document, where each document contains embeddings for its chunks
        """
        client = voyageai.Client()
        result = client.contextualized_embed(
            inputs=documents_chunks,
            model=model,
            input_type="document",
            output_dimension=output_dimension
        )
        return [res.embeddings for res in result.results]
