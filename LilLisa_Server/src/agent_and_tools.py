"""
ReAct agent that handles a query in an intelligent manner
"""



import os
import re
import time
import traceback
from difflib import get_close_matches
from enum import Enum
import logging
import json


from concurrent.futures import ThreadPoolExecutor
import threading

from litellm import completion
from llama_index.core import Settings, VectorStoreIndex
from llama_index.core.postprocessor import SentenceTransformerRerank
from llama_index.embeddings.openai import OpenAIEmbedding
from openai import OpenAI
import torch

import lancedb

from src import utils
from src.llama_index_lancedb_vector_store import LanceDBVectorStore

OPENAI_API_KEY = None
IDDM_RETRIEVER = None
IDA_RETRIEVER = None
IDDM_QA_PAIRS_RETRIEVER = None
IDA_QA_PAIRS_RETRIEVER = None
IDO_QA_PAIRS_RETRIEVER = None
TECHSUPPORT_QA_PAIRS_RETRIEVER = None
if torch.cuda.is_available():
    _reranker_device = "cuda"
    logging.info("Reranker using GPU (CUDA): %s", torch.cuda.get_device_name(0))
else:
    _reranker_device = "cpu"
    logging.info("Reranker using CPU -- no CUDA GPU detected")

RERANKER = SentenceTransformerRerank(
    top_n=20,
    model="cross-encoder/ms-marco-MiniLM-L-12-v2",
    device=_reranker_device,
)
CLIENT = None
OPENAI_CLIENT = None

# Slack section/context blocks are capped at ~3000 characters, so the "potentially
# relevant" match displays must stay well under that even when several matches clear
# the threshold. Cap the number of matches shown and truncate each answer individually.
# (Used only for golden QA pairs' "potentially relevant" display -- techsupport content
# is merged into normal document retrieval with no separate display, see
# answer_from_document_retrieval below.)
MAX_DISPLAYED_MATCHES = 3
MATCH_ANSWER_DISPLAY_LENGTH = 500

QA_SYSTEM_PROMPT = None
QA_USER_PROMPT = None

IDDM_PRODUCT_VERSIONS = None
IDA_PRODUCT_VERSIONS = None
IDO_PRODUCT_VERSIONS = None

lillisa_server_env = utils.LILLISA_SERVER_ENV_DICT

if not (LLM_MODEL := lillisa_server_env.get("LLM_MODEL")):
    traceback.print_exc()
    utils.logger.critical("LLM_MODEL not found in lillisa_server.env")
    raise ValueError("LLM_MODEL not found in lillisa_server.env")

if fp := lillisa_server_env["SPEEDICT_FOLDERPATH"]:
    speedict_folderpath = str(fp)
else:
    traceback.print_exc()
    utils.logger.critical("SPEEDICT_FOLDERPATH not found in lillisa_server.env")
    raise ValueError("SPEEDICT_FOLDERPATH not found in lillisa_server.env")


if fp := lillisa_server_env["OPENAI_API_KEY_FILEPATH"]:
    openai_api_key_filepath = str(fp)
else:
    traceback.print_exc()
    utils.logger.critical("OPENAI_API_KEY_FILEPATH not found in lillisa_server.env")
    raise ValueError("OPENAI_API_KEY_FILEPATH not found in lillisa_server.env")

with open(openai_api_key_filepath, "r", encoding="utf-8") as file:
    OPENAI_API_KEY = file.read()

os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY
OPENAI_CLIENT = OpenAI(api_key=OPENAI_API_KEY)


if fp := lillisa_server_env["QA_SYSTEM_PROMPT_FILEPATH"]:
    qa_system_prompt_filepath = str(fp)
else:
    traceback.print_exc()
    utils.logger.critical("QA_SYSTEM_PROMPT_FILEPATH not found in lillisa_server.env")
    raise ValueError("QA_SYSTEM_PROMPT_FILEPATH not found in lillisa_server.env")

with open(qa_system_prompt_filepath, "r", encoding="utf-8") as file:
    QA_SYSTEM_PROMPT = file.read()


if fp := lillisa_server_env["QA_USER_PROMPT_FILEPATH"]:
    qa_user_prompt_filepath = str(fp)
else:
    traceback.print_exc()
    utils.logger.critical("QA_USER_PROMPT_FILEPATH not found in lillisa_server.env")
    raise ValueError("QA_USER_PROMPT_FILEPATH not found in lillisa_server.env")

with open(qa_user_prompt_filepath, "r", encoding="utf-8") as file:
    QA_USER_PROMPT = file.read()


if fp := lillisa_server_env["AWS_ACCESS_KEY_ID_FILEPATH"]:
    aws_access_key_id_filepath = str(fp)
else:
    traceback.print_exc()
    utils.logger.critical("AWS_ACCESS_KEY_ID not found in lillisa_server.env")
    raise ValueError("AWS_ACCESS_KEY_ID not found in lillisa_server.env")

with open(aws_access_key_id_filepath, "r", encoding="utf-8") as file:
    aws_access_key_id = file.read()


if fp := lillisa_server_env["AWS_SECRET_ACCESS_KEY_FILEPATH"]:
    aws_secret_access_key_filepath = str(fp)
else:
    traceback.print_exc()
    utils.logger.critical("AWS_SECRET_ACCESS_KEY not found in lillisa_server.env")
    raise ValueError("AWS_SECRET_ACCESS_KEY not found in lillisa_server.env")

with open(aws_secret_access_key_filepath, "r", encoding="utf-8") as file:
    aws_secret_access_key = file.read()

if not os.path.exists(speedict_folderpath):
    os.makedirs(speedict_folderpath)

if fp := lillisa_server_env["LANCEDB_FOLDERPATH"]:
    lancedb_folderpath = str(fp)
else:
    traceback.print_exc()
    utils.logger.critical("LANCEDB_FOLDERPATH not found in lillisa_server.env")
    raise ValueError("LANCEDB_FOLDERPATH not found in lillisa_server.env")

if iddm_product_versions := lillisa_server_env["IDDM_PRODUCT_VERSIONS"]:
    IDDM_PRODUCT_VERSIONS = str(iddm_product_versions).split(", ")
else:
    traceback.print_exc()
    utils.logger.critical("IDDM_PRODUCT_VERSIONS not found in lillisa_server.env")
    raise ValueError("IDDM_PRODUCT_VERSIONS not found in lillisa_server.env")

if ida_product_versions := lillisa_server_env["IDA_PRODUCT_VERSIONS"]:
    IDA_PRODUCT_VERSIONS = str(ida_product_versions).split(", ")
else:
    traceback.print_exc()
    utils.logger.critical("IDA_PRODUCT_VERSIONS not found in lillisa_server.env")
    raise ValueError("IDA_PRODUCT_VERSIONS not found in lillisa_server.env")

if ido_product_versions := lillisa_server_env.get("IDO_PRODUCT_VERSIONS"):
    IDO_PRODUCT_VERSIONS = str(ido_product_versions).split(", ")
else:
    utils.logger.warning("IDO_PRODUCT_VERSIONS not found in lillisa_server.env — IDO functionality will be disabled")

IDDM_INDEX = None
IDDM_QA_PAIRS_INDEX = None
IDA_INDEX = None
IDA_QA_PAIRS_INDEX = None
IDO_INDEX = None
IDO_QA_PAIRS_INDEX = None
IDO_QA_PAIRS_RETRIEVER = None
IDO_RETRIEVER = None
IDDM_RETRIEVER = None
IDA_RETRIEVER = None
IDDM_QA_PAIRS_RETRIEVER = None
IDA_QA_PAIRS_RETRIEVER = None
TECHSUPPORT_QA_PAIRS_INDEX = None
TECHSUPPORT_QA_PAIRS_RETRIEVER = None
def create_docdbs_lancedb_retrievers_and_indices(lancedb_folderpath: str) -> None:
    """Create indices and retrievers from lancedb tables, attempting to create indices if they don't exist."""
    global IDDM_RETRIEVER, IDA_RETRIEVER, IDO_RETRIEVER
    global IDDM_INDEX, IDA_INDEX, IDO_INDEX

    lance_db = lancedb.connect(lancedb_folderpath)
    iddm_table = lance_db.open_table("IDDM")
    ida_table = lance_db.open_table("IDA")
    iddm_vector_store = LanceDBVectorStore.from_table(iddm_table)
    ida_vector_store = LanceDBVectorStore.from_table(ida_table)
    IDDM_INDEX = VectorStoreIndex.from_vector_store(vector_store=iddm_vector_store)
    IDA_INDEX = VectorStoreIndex.from_vector_store(vector_store=ida_vector_store)
    IDDM_RETRIEVER = IDDM_INDEX.as_retriever(similarity_top_k=50)
    IDA_RETRIEVER = IDA_INDEX.as_retriever(similarity_top_k=50)

    # IDO is optional — only initialize if the table exists
    try:
        ido_table = lance_db.open_table("IDO")
        ido_vector_store = LanceDBVectorStore.from_table(ido_table)
        IDO_INDEX = VectorStoreIndex.from_vector_store(vector_store=ido_vector_store)
        IDO_RETRIEVER = IDO_INDEX.as_retriever(similarity_top_k=50)
    except Exception:
        utils.logger.warning("IDO LanceDB table not found — IDO document retrieval will be disabled")

def create_qa_pairs_lancedb_retrievers_and_indices(lancedb_folderpath: str) -> None:
    """Create indices and retrievers from lancedb tables, attempting to create indices if they don't exist."""
    global IDDM_QA_PAIRS_RETRIEVER, IDA_QA_PAIRS_RETRIEVER, IDO_QA_PAIRS_RETRIEVER
    global IDDM_QA_PAIRS_INDEX, IDA_QA_PAIRS_INDEX, IDO_QA_PAIRS_INDEX
    global TECHSUPPORT_QA_PAIRS_INDEX, TECHSUPPORT_QA_PAIRS_RETRIEVER

    lance_db = lancedb.connect(lancedb_folderpath)
    iddm_qa_pairs_table = lance_db.open_table("IDDM_QA_PAIRS")
    ida_qa_pairs_table = lance_db.open_table("IDA_QA_PAIRS")
    iddm_qa_pairs_vector_store = LanceDBVectorStore.from_table(iddm_qa_pairs_table, "vector")
    ida_qa_pairs_vector_store = LanceDBVectorStore.from_table(ida_qa_pairs_table, "vector")
    IDDM_QA_PAIRS_INDEX = VectorStoreIndex.from_vector_store(vector_store=iddm_qa_pairs_vector_store)
    IDA_QA_PAIRS_INDEX = VectorStoreIndex.from_vector_store(vector_store=ida_qa_pairs_vector_store)
    IDDM_QA_PAIRS_RETRIEVER = IDDM_QA_PAIRS_INDEX.as_retriever(similarity_top_k=8)
    IDA_QA_PAIRS_RETRIEVER = IDA_QA_PAIRS_INDEX.as_retriever(similarity_top_k=8)

    # IDO QA pairs are optional — only initialize if the table exists
    try:
        ido_qa_pairs_table = lance_db.open_table("IDO_QA_PAIRS")
        ido_qa_pairs_vector_store = LanceDBVectorStore.from_table(ido_qa_pairs_table, "vector")
        IDO_QA_PAIRS_INDEX = VectorStoreIndex.from_vector_store(vector_store=ido_qa_pairs_vector_store)
        IDO_QA_PAIRS_RETRIEVER = IDO_QA_PAIRS_INDEX.as_retriever(similarity_top_k=8)
    except Exception:
        utils.logger.warning("IDO_QA_PAIRS LanceDB table not found — IDO QA pairs retrieval will be disabled")

    # Shared techsupport QA pairs table (product-agnostic, used by IDA/IDDM/IDO
    # alike -- see techsupport_qa_ingest.py) — optional, only initialize if it exists
    try:
        techsupport_qa_pairs_table = lance_db.open_table("TECHSUPPORT_QA_PAIRS")
        techsupport_qa_pairs_vector_store = LanceDBVectorStore.from_table(techsupport_qa_pairs_table, "vector")
        TECHSUPPORT_QA_PAIRS_INDEX = VectorStoreIndex.from_vector_store(vector_store=techsupport_qa_pairs_vector_store)
        TECHSUPPORT_QA_PAIRS_RETRIEVER = TECHSUPPORT_QA_PAIRS_INDEX.as_retriever(similarity_top_k=8)
    except Exception:
        utils.logger.warning("TECHSUPPORT_QA_PAIRS LanceDB table not found — shared techsupport QA pairs retrieval will be disabled")

def create_lancedb_retrievers_and_indices(lancedb_folderpath: str) -> None:
    """Create indices and retrievers from lancedb tables, attempting to create indices if they don't exist."""
    create_docdbs_lancedb_retrievers_and_indices(lancedb_folderpath)
    create_qa_pairs_lancedb_retrievers_and_indices(lancedb_folderpath)

class PRODUCT(str, Enum):
    """Product"""

    IDA = "IDA"
    IDDM = "IDDM"
    IDO = "IDO"

    @staticmethod
    def get_product(product: str) -> "PRODUCT":
        """get product"""
        if product in (product.value for product in PRODUCT):
            return PRODUCT(product)
        raise ValueError(f"{product} does not exist")


# def update_retrievers(retriever_name, new_retriever):
#     """
#     Updates the reference to the appropriate retriever after "rebuild_docs" or "update_golden_qa_pairs" is called.
#     """
#     global IDDM_RETRIEVER, IDA_RETRIEVER, IDDM_QA_PAIRS_RETRIEVER, IDA_QA_PAIRS_RETRIEVER
#     if retriever_name == "IDDM":
#         IDDM_RETRIEVER = new_retriever
#     elif retriever_name == "IDA":
#         IDA_RETRIEVER = new_retriever
#     elif retriever_name == "IDDM_QA_PAIRS":
#         IDDM_QA_PAIRS_RETRIEVER = new_retriever
#     elif retriever_name == "IDA_QA_PAIRS":
#         IDA_QA_PAIRS_RETRIEVER = new_retriever
#     else:
#         raise ValueError(f"{retriever_name} does not exist")


# def update_indices(retriever_name, new_index):
#     """
#     Updates the reference to the appropriate indices after "rebuild_docs" or "update_golden_qa_pairs" is called.
#     """
#     global IDDM_INDEX, IDA_INDEX, IDDM_QA_PAIRS_INDEX, IDA_QA_PAIRS_INDEX
#     if retriever_name == "IDDM":
#         IDDM_INDEX = new_index
#     elif retriever_name == "IDA":
#         IDA_INDEX = new_index
#     elif retriever_name == "IDDM_QA_PAIRS":
#         IDDM_QA_PAIRS_INDEX = new_index
#     elif retriever_name == "IDA_QA_PAIRS":
#         IDA_QA_PAIRS_INDEX = new_index
#     else:
#         raise ValueError(f"{retriever_name} does not exist")


class CachedQueryEmbedding:
    """Wraps an embedding model to cache query embeddings, avoiding duplicate API calls.

    When two retrievers need to embed the same query (e.g., QA pairs + documents),
    the first call computes the embedding and the second returns the cached result.
    Thread-safe for use with concurrent retrieval.
    """

    def __init__(self, base_embed_model):
        self._base = base_embed_model
        self._cache = {}
        self._lock = threading.Lock()

    def get_query_embedding(self, query_str):
        with self._lock:
            if query_str in self._cache:
                utils.logger.debug("PERF | query_embedding_cache_hit")
                return list(self._cache[query_str])  # Return copy to prevent mutation
        t0 = time.perf_counter()
        embedding = self._base.get_query_embedding(query_str)
        elapsed = time.perf_counter() - t0
        utils.logger.debug("PERF | query_embedding | %.3fs", elapsed)
        with self._lock:
            self._cache[query_str] = embedding
        return embedding

    def __getattr__(self, name):
        """Delegate all other attribute access to the base embedding model."""
        return getattr(self._base, name)


def handle_user_answer(answer: str) -> str:
    """
    Tool should be called when a user enters an answer to a previous question of theirs. Thank them and merely mimic their answer.
    """
    return answer

def improve_query(query: str, conversation_history: str) -> str:
    """
    Clears up vagueness from query with the help of the conversation history and returns a new query revealing the user's true intention, without distorting the meaning behind the original query. If needed, this should be the first tool called; else, should not be called at all.
    * query should be the original query that the user prompted the agent with, needing some clarification
    * conversation_history is the conversation history the user prompted the agent with
    """
    user_prompt = f"""
    ###CONVERSATION HISTORY###
    {conversation_history}

    ###QUERY###
    {query}

    Based on the conversation history and query, generate a new query that links the two, maximizing semantic understanding.
    """
    
    response = ""
    t0 = time.perf_counter()
    for chunk in completion(
        model=LLM_MODEL, 
        messages=[
            {"role": "user", "content": user_prompt}
        ],
        stream=True,  # Enable streaming
    ):
        # Process each chunk as it arrives
        if chunk.choices and len(chunk.choices) > 0 and chunk.choices[0].delta.content:
            content = chunk.choices[0].delta.content
            response += content
    elapsed = time.perf_counter() - t0
    utils.logger.debug("PERF | improve_query_llm | %.3fs", elapsed)

    return response


def refine_escalation_query(conversation_history: str) -> str:
    """
    Combines every message the user sent in a thread into a single, faithful question, for use when
    escalating a thread to tech support. Unlike improve_query(), this must NOT reinterpret, broaden,
    narrow, or otherwise change the scope of what the user asked - it only merges what the user actually
    said (e.g. an initial question plus a follow-up rephrasing or clarification) into one coherent question.
    * conversation_history is the full conversation history (all User/Assistant turns) for the thread being escalated
    """
    user_prompt = f"""
    ###CONVERSATION HISTORY###
    {conversation_history}

    Above is a conversation thread that is being escalated to human tech support because the assistant could
    not answer it. Find every message sent by "User" in that history, and combine them into a single,
    self-contained question suitable for a tech support agent who has not seen the thread.

    Rules:
    - Preserve the scope and meaning of the user's messages exactly. Do NOT broaden, narrow, reinterpret, add
      assumptions, or infer intent beyond what the user actually wrote.
    - If later user messages rephrase, clarify, or add detail to an earlier question, merge them into one
      coherent question rather than listing them separately or picking only one.
    - Do not answer the question. Do not mention the assistant's prior replies except as needed for the
      combined question to stand on its own.
    - Output ONLY the final combined question text - no preamble, labels, or explanation.
    """

    response = ""
    t0 = time.perf_counter()
    for chunk in completion(
        model=LLM_MODEL,
        messages=[
            {"role": "user", "content": user_prompt}
        ],
        stream=True,  # Enable streaming
    ):
        # Process each chunk as it arrives
        if chunk.choices and len(chunk.choices) > 0 and chunk.choices[0].delta.content:
            content = chunk.choices[0].delta.content
            response += content
    elapsed = time.perf_counter() - t0
    utils.logger.debug("PERF | refine_escalation_query_llm | %.3fs", elapsed)

    return response.strip()


def format_tables_in_chunks(chunks: str) -> str:
    """Detect and format tables in retrieved chunks at query time.

    This function processes the input string to identify Markdown tables,
    preserving their original format and adding a key-value representation.

    Args:
        chunks (str): Concatenated text from retrieved document nodes.

    Returns:
        str: Formatted text with tables in both Markdown and key-value formats.
    """
    lines = chunks.splitlines()
    result_lines = []
    i = 0

    while i < len(lines):
        line = lines[i].strip()

        # Check if the line might start a table (contains multiple pipes)
        if line.count("|") >= 2:
            table_start = i
            table_lines = [line]
            i += 1

            # Collect lines that form the table (pipes or separator lines)
            while i < len(lines) and (lines[i].count("|") >= 2 or set(lines[i].strip()) <= {"|", "-", " "}):
                table_lines.append(lines[i])
                i += 1

            # Verify table structure (needs at least header and separator)
            if len(table_lines) >= 2:
                # Preserve the original Markdown table
                result_lines.extend(table_lines)
                result_lines.append("")  # Separator

                # Generate key-value format
                try:
                    headers = [col.strip() for col in table_lines[0].strip("|").split("|")]
                    data_lines = [ln for ln in table_lines[2:] if "|" in ln]

                    if headers and data_lines:
                        result_lines.append("**Same table in key-value format:**")
                        for idx, row in enumerate(data_lines, start=1):
                            cols = [col.strip() for col in row.strip("|").split("|")]
                            result_lines.append(f"{idx}.")
                            for header, value in zip(headers, cols):
                                if header and value:
                                    result_lines.append(f"{idx}.{header}={value}")
                            result_lines.append("")  # Blank line between rows
                except Exception as e:
                    logging.error("Error processing table formatting: %s", e, exc_info=True)
                    result_lines.append("An error occurred while formatting the table.")

            else:
                # Not a valid table, treat as regular text
                result_lines.extend(table_lines)
        else:
            # Non-table line
            result_lines.append(line)
            i += 1

    return "\n".join(result_lines)


def _truncate_match_answer(answer: str, max_length: int = MATCH_ANSWER_DISPLAY_LENGTH) -> str:
    """Truncate a single QA/techsupport match's answer so no one match can blow out the display.

    A hard slice at max_length can land inside a ``` fenced code block, leaving it unclosed
    and breaking Slack's mrkdwn rendering. If that happens, cut before the code block instead
    of mid-block -- simpler and more robust than trying to re-close the fence, since it never
    risks displaying a syntactically confusing code fragment.
    """
    if len(answer) <= max_length:
        return answer
    truncated = answer[:max_length]
    if truncated.count("```") % 2 != 0:
        fence_index = truncated.rfind("```")
        before_fence = truncated[:fence_index].rstrip()
        if before_fence:
            return before_fence + "..."
        # The truncation point falls inside the very first code block with no preceding
        # text -- cutting it out entirely would leave nothing to show, so close the fence
        # instead of dropping it.
        return truncated + "\n```..."
    return truncated + "..."


def answer_from_document_retrieval(
    product: str, original_query: str, generated_query: str, conversation_history: str
) -> str:
    """
    RAG Search. Searches through a database of 10,000 documents, and based on a query, returns the top-10 relevant documents and synthesizes an answer.
    Return a JSON string with response and top 10 reranked nodes.
    """
    t0_total = time.perf_counter()
    response = ""
    qa_system_prompt = QA_SYSTEM_PROMPT
    query = generated_query or original_query

    product_enum = PRODUCT.get_product(product)
    if product_enum == PRODUCT.IDDM:
        if IDDM_INDEX is None or IDDM_QA_PAIRS_INDEX is None:
            elapsed = time.perf_counter() - t0_total
            utils.logger.debug("PERF | answer_from_document_retrieval_total | %.3fs", elapsed)
            return json.dumps({"response": "IDDM indices are not initialized. The server may have encountered an error during startup. Please contact an administrator.", "reranked_nodes": [], "answer_found": True})
        product_versions = IDDM_PRODUCT_VERSIONS
        version_pattern = re.compile(r"v?\d+\.\d+", re.IGNORECASE)
        document_index = IDDM_INDEX
        qa_pairs_index = IDDM_QA_PAIRS_INDEX
        default_document_retriever = IDDM_RETRIEVER
        default_qa_pairs_retriever = IDDM_QA_PAIRS_RETRIEVER
    elif product_enum == PRODUCT.IDO:
        # IDO is optional — check if it was initialized
        if IDO_INDEX is None or IDO_QA_PAIRS_INDEX is None:
            elapsed = time.perf_counter() - t0_total
            utils.logger.debug("PERF | answer_from_document_retrieval_total | %.3fs", elapsed)
            return json.dumps({"response": "IDO product is not configured on this server. Please contact an administrator.", "reranked_nodes": [], "answer_found": True})
        product_versions = IDO_PRODUCT_VERSIONS
        version_pattern = re.compile(r"\b(?:dev/)?v?\d+\.\d+\b", re.IGNORECASE)
        document_index = IDO_INDEX
        qa_pairs_index = IDO_QA_PAIRS_INDEX
        default_document_retriever = IDO_RETRIEVER
        default_qa_pairs_retriever = IDO_QA_PAIRS_RETRIEVER
    else:
        if IDA_INDEX is None or IDA_QA_PAIRS_INDEX is None:
            elapsed = time.perf_counter() - t0_total
            utils.logger.debug("PERF | answer_from_document_retrieval_total | %.3fs", elapsed)
            return json.dumps({"response": "IDA indices are not initialized. The server may have encountered an error during startup. Please contact an administrator.", "reranked_nodes": [], "answer_found": True})
        product_versions = IDA_PRODUCT_VERSIONS
        version_pattern = re.compile(r"\b(?:IAP[- ]\d+\.\d+|version[- ]\d+\.\d+|descartes(?:-dev)?)\b", re.IGNORECASE)
        document_index = IDA_INDEX
        qa_pairs_index = IDA_QA_PAIRS_INDEX
        default_document_retriever = IDA_RETRIEVER
        default_qa_pairs_retriever = IDA_QA_PAIRS_RETRIEVER

    if matched_versions := get_matching_versions(
        original_query, product_versions, version_pattern
    ):
        qa_system_prompt += f"\n10. Mention the product version(s) you used to craft your response were '{' and '.join(matched_versions)}'"
        lance_filter_documents = " OR ".join(f"(metadata.version = '{version}')" for version in matched_versions)
        lance_filter_qa_pairs = (
            f"(metadata.version = 'none') OR {lance_filter_documents}"
        )
        document_retriever = document_index.as_retriever(
            vector_store_kwargs={"where": lance_filter_documents}, similarity_top_k=50
        )
        qa_pairs_retriever = qa_pairs_index.as_retriever(
            vector_store_kwargs={"where": lance_filter_qa_pairs}, similarity_top_k=8
        )
    else:
        qa_system_prompt += "\n10. Mention that because a specific product version was not specified, information from all available versions was used. If your response begins with the \"[[NO_ANSWER]]\" marker per rule 9, that marker must still come first, before this mention."
        # Create fresh retrievers (not global defaults) so we can set cached embed model
        document_retriever = document_index.as_retriever(similarity_top_k=50)
        qa_pairs_retriever = qa_pairs_index.as_retriever(similarity_top_k=8)

    # Shared techsupport QA pairs table -- product-agnostic, so no version filtering.
    # Optional: gracefully skip if the table hasn't been created yet (fresh install),
    # same pattern as the optional IDO retrievers above.
    techsupport_qa_pairs_retriever = None
    if TECHSUPPORT_QA_PAIRS_INDEX is not None:
        techsupport_qa_pairs_retriever = TECHSUPPORT_QA_PAIRS_INDEX.as_retriever(similarity_top_k=8)

    # Set up embedding cache to avoid duplicate API calls during parallel retrieval
    cached_embed = CachedQueryEmbedding(Settings.embed_model)
    t0_embed = time.perf_counter()
    cached_embed.get_query_embedding(query)  # Pre-compute and cache the embedding
    utils.logger.debug("PERF | pre_compute_embedding | %.3fs", time.perf_counter() - t0_embed)
    document_retriever._embed_model = cached_embed
    qa_pairs_retriever._embed_model = cached_embed
    if techsupport_qa_pairs_retriever is not None:
        techsupport_qa_pairs_retriever._embed_model = cached_embed

    # Run QA pairs and document retrieval in parallel
    def _retrieve_qa():
        t0 = time.perf_counter()
        result = qa_pairs_retriever.retrieve(query)
        utils.logger.debug("PERF | retrieve_qa_pairs | %.3fs", time.perf_counter() - t0)
        return result

    def _retrieve_techsupport_qa():
        if techsupport_qa_pairs_retriever is None:
            return []
        t0 = time.perf_counter()
        try:
            result = techsupport_qa_pairs_retriever.retrieve(query)
            utils.logger.debug("PERF | retrieve_techsupport_qa_pairs | %.3fs", time.perf_counter() - t0)
            return result
        except Warning:
            utils.logger.debug("PERF | retrieve_techsupport_qa_pairs | %.3fs", time.perf_counter() - t0)
            return []
        except Exception:
            # Fail closed (no techsupport results) rather than crashing the whole
            # answer if the shared techsupport table is unavailable or misconfigured.
            utils.logger.exception("Shared techsupport QA pairs retrieval failed")
            utils.logger.debug("PERF | retrieve_techsupport_qa_pairs | %.3fs", time.perf_counter() - t0)
            return []

    def _retrieve_docs():
        t0 = time.perf_counter()
        try:
            result = document_retriever.retrieve(query)
            utils.logger.debug("PERF | retrieve_documents | %.3fs", time.perf_counter() - t0)
            return result
        except Warning:
            utils.logger.debug("PERF | retrieve_documents | %.3fs", time.perf_counter() - t0)
            return []

    t0_parallel = time.perf_counter()
    with ThreadPoolExecutor(max_workers=3) as executor:
        future_qa = executor.submit(_retrieve_qa)
        future_techsupport_qa = executor.submit(_retrieve_techsupport_qa)
        future_docs = executor.submit(_retrieve_docs)
        qa_nodes = list(future_qa.result())
        techsupport_qa_nodes = list(future_techsupport_qa.result())
        nodes = future_docs.result()
    utils.logger.debug("PERF | parallel_retrieval_total | %.3fs", time.perf_counter() - t0_parallel)

    if not nodes and not qa_nodes and not techsupport_qa_nodes:
        elapsed = time.perf_counter() - t0_total
        utils.logger.debug("PERF | answer_from_document_retrieval_total | %.3fs", elapsed)
        return json.dumps({"response": "No relevant documents were found for this query.", "reranked_nodes": [], "answer_found": False})

    relevant_qa_nodes = []
    potentially_relevant_qa_nodes = []

    for node in qa_nodes:
        if 0.85 <= node.score <= 1.0:
            relevant_qa_nodes.append(node)
        elif 0.7 <= node.score < 0.85:
            potentially_relevant_qa_nodes.append(node)

    # Techsupport nodes are merged wholesale into the same pool documentation
    # nodes go into, with no score threshold -- exactly like `nodes` above. The
    # reranker's cross-encoder + top-10 slice below does the relevance
    # filtering, same as for docs; there is no separate techsupport display.
    combined_nodes = nodes + relevant_qa_nodes + techsupport_qa_nodes
    t0_rerank = time.perf_counter()
    reranked_nodes = RERANKER.postprocess_nodes(nodes=combined_nodes, query_str=query)[:10]
    utils.logger.debug("PERF | reranking | %.3fs", time.perf_counter() - t0_rerank)
    # Documentation nodes carry webportal_url (plus their own source-repo github_url
    # and a front-matter title), techsupport nodes carry ONLY github_url (see
    # techsupport_qa_ingest.py) -- both are just "a link to this chunk's full
    # source," collected identically and blended into the same list/section below
    # rather than given separate treatment. Techsupport URLs end in a long
    # slugified anchor (unlike webportal_url's short page-name path segments), so
    # their display text uses the title stashed in metadata by
    # GenerateTechsupportTitle (techsupport_qa_ingest.py) instead of being derived
    # from the URL -- see the useful_links loop below. The absence of webportal_url
    # is what identifies a techsupport-only node; doc nodes always have one.
    useful_links = []
    seen_urls = set()
    for node in reranked_nodes:
        webportal_url = node.metadata.get("webportal_url")
        github_url = node.metadata.get("github_url")
        if url := webportal_url or github_url:
            if url in seen_urls:
                continue
            seen_urls.add(url)
            is_techsupport_link = webportal_url is None and github_url is not None
            title = node.metadata.get("title") if is_techsupport_link else None
            useful_links.append((url, title))
    useful_links = useful_links[:3]  # Take top 3 links

    chunks = []
    for node in reranked_nodes:
        # Golden QA pairs carry an 'answer' metadata field; techsupport nodes
        # (now prose summaries, see techsupport_qa_ingest.py) and documentation
        # nodes don't, so both fall into the plain node.text branch below.
        if 'answer' in node.metadata:
            answer = node.metadata.get('answer', 'No answer found')
            chunks.append(f"EXPERT VERIFIED ANSWER: {answer}")
        else:
            # Regular document node (or a techsupport prose summary)
            chunks.append(node.text)

    raw_chunks = "\n\n".join(chunks)
    t0_format = time.perf_counter()
    formatted_chunks = format_tables_in_chunks(raw_chunks)
    utils.logger.debug("PERF | format_tables | %.3fs", time.perf_counter() - t0_format)

    user_prompt = QA_USER_PROMPT.replace("<CONTEXT>", formatted_chunks)
    user_prompt = user_prompt.replace("<CONVERSATION_HISTORY>", conversation_history)
    user_prompt = user_prompt.replace("<QUESTION>", original_query)

    llm_messages = [
        {"role": "system", "content": qa_system_prompt},
        {"role": "user", "content": user_prompt}
    ]

    t0_llm = time.perf_counter()
    llm_response = completion(
        model=LLM_MODEL,
        messages=llm_messages,
        temperature=0.0,
    ).choices[0].message.content
    utils.logger.debug("PERF | answer_generation_llm | %.3fs", time.perf_counter() - t0_llm)

    no_answer_marker = "[[NO_ANSWER]]"
    utils.logger.info("DEBUG_NO_ANSWER | raw llm_response before stripping: %r", llm_response)

    # Mistral's hosted inference layer has been observed to non-deterministically emit
    # [[NO_ANSWER]] on identical input even at temperature=0 (confirmed: the same prompt
    # and context produced a correct answer in some runs and [[NO_ANSWER]] in others).
    # When the reranker was highly confident in the top match, a NO_ANSWER is more likely
    # a serving-layer fluke than a genuine "not in context" -- retry the identical call
    # once before giving up. Threshold is well below the 6.0069 top-score seen in a
    # confirmed-good case, with margin, since cross-encoder scores for genuinely
    # off-topic top matches were observed well under 1.
    NO_ANSWER_RETRY_SCORE_THRESHOLD = 3.0
    if (
        llm_response.find(no_answer_marker) != -1
        and reranked_nodes
        and reranked_nodes[0].score > NO_ANSWER_RETRY_SCORE_THRESHOLD
    ):
        utils.logger.info(
            "NO_ANSWER_RETRY | retrying | top_reranked_score=%.4f (threshold=%.1f)",
            reranked_nodes[0].score, NO_ANSWER_RETRY_SCORE_THRESHOLD,
        )
        t0_retry = time.perf_counter()
        retry_response = completion(
            model=LLM_MODEL,
            messages=llm_messages,
            temperature=0.0,
        ).choices[0].message.content
        utils.logger.debug("PERF | answer_generation_llm_retry | %.3fs", time.perf_counter() - t0_retry)
        retry_changed_outcome = retry_response.find(no_answer_marker) == -1
        utils.logger.info(
            "NO_ANSWER_RETRY | retry complete | changed_outcome=%s | retry_response: %r",
            retry_changed_outcome, retry_response,
        )
        llm_response = retry_response

    marker_index = llm_response.find(no_answer_marker)
    if marker_index != -1:
        answer_found = False
        llm_response = llm_response[marker_index + len(no_answer_marker):].lstrip()
    else:
        answer_found = True
    utils.logger.info(
        "DEBUG_NO_ANSWER | marker_stripped=%s answer_found=%s final llm_response: %r",
        not answer_found,
        answer_found,
        llm_response,
    )

    response += llm_response

    if potentially_relevant_qa_nodes and not relevant_qa_nodes:
        response += "\n\n\n"
        response += "In addition, here are some potentially relevant QA pairs that have been verified by an expert!\n"
        displayed_qa_nodes = sorted(potentially_relevant_qa_nodes, key=lambda n: n.score, reverse=True)[:MAX_DISPLAYED_MATCHES]
        for idx, node in enumerate(displayed_qa_nodes, start=1):
            answer = _truncate_match_answer(node.metadata['answer'])
            response += f"\nMatch {idx}:\nQuestion: {node.text}\nAnswer: {answer}\n"

    # Kept out of `response` and returned as its own field so slack.py can place the links
    # block after the techsupport/QA match sections instead of immediately after the answer.
    links_text = ""
    if useful_links:
        # Slack's mrkdwn -- what this text is rendered as, see slack.py -- uses
        # <url|text> for links, not standard Markdown's [text](url). The latter
        # renders as literal bracketed text with the raw URL auto-linkified
        # separately next to it.
        links_text = "Here are some potentially helpful documentation links:"
        for link, title in useful_links:
            if title:
                link_text = title
            else:
                # Extract the last part of the URL to use as link text
                path_parts = link.split('/')
                link_text = path_parts[-1]
            links_text += f"\n- <{link}|{link_text}>"

    nodes_info = []
    for node in reranked_nodes:
        if 'answer' in node.metadata:
            # Golden QA pair -- include both question and answer in the text field
            nodes_info.append({
                "text": f"EXPERT VERIFIED QA PAIR:\nQuestion: {node.text}\nAnswer: {node.metadata['answer']}",
                "metadata": node.metadata
            })
        else:
            # Regular document node (or a techsupport prose summary)
            nodes_info.append({"text": node.text, "metadata": node.metadata})

    # If the #1 reranked match is itself a techsupport entry (same detection as the
    # useful_links loop above), tag its title so the escalation flow can later merge
    # new insight into that entry instead of creating a duplicate -- see slack.py's
    # build_escalation_button_value and nightly_pipeline.py's enrich_verified_entry.
    primary_techsupport_match_title = None
    if reranked_nodes:
        top_node = reranked_nodes[0]
        top_webportal_url = top_node.metadata.get("webportal_url")
        top_github_url = top_node.metadata.get("github_url")
        if top_webportal_url is None and top_github_url is not None:
            primary_techsupport_match_title = top_node.metadata.get("title")

    response_dict = {
        "response": response,
        "links_text": links_text,
        "reranked_nodes": nodes_info,
        "answer_found": answer_found,
        "primary_techsupport_match_title": primary_techsupport_match_title,
    }
    elapsed = time.perf_counter() - t0_total
    utils.logger.debug("PERF | answer_from_document_retrieval_total | %.3fs", elapsed)
    return json.dumps(response_dict)


def get_matching_versions(query, product_versions, version_pattern):
    """
    Not a tool for the ReAct agent but instead a helper function for "answer_from_document_retrieval.
    Helps extract a version from a query.
    """
    extracted_versions = version_pattern.findall(query)
    matched_versions = []
    for extracted_version in extracted_versions:
        if closest_match := get_close_matches(
            extracted_version, product_versions, n=1, cutoff=0.4
        ):
            matched_versions.append(closest_match[0])
    return matched_versions