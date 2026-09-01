""" utility functions """

import logging
import os
from datetime import datetime, timezone
from time import time_ns
from typing import Optional

from dotenv import dotenv_values

logger = logging.getLogger("RL_Logger")

LILLISA_SERVER_ENV_DICT = {**dotenv_values("./env/lillisa_server.env")}
# load config params from override folder
if areofp := LILLISA_SERVER_ENV_DICT.get("LILLISA_SERVER_ENV_OVERRIDE_FILEPATH", None):
    # if the folder exists and contains a file called lillisa_server.env, load it using dotenv
    if os.path.isfile(areofp):
        LILLISA_SERVER_ENV_DICT = {**LILLISA_SERVER_ENV_DICT, **dotenv_values(areofp)}
    else:
        logger.debug("The env file at LILLISA_SERVER_ENV_OVERRIDE_FILEPATH is missing")
else:
    logger.debug("LILLISA_SERVER_ENV_OVERRIDE_FILEPATH env variable not found in lillisa_server.env")
LILLISA_SERVER_ENV_DICT = {
    **LILLISA_SERVER_ENV_DICT,
    **os.environ,  # override loaded values with environment variables
}


NO_ANSWER_MARKER = "[[NO_ANSWER]]"


def parse_leading_no_answer_marker(
    llm_response: str, marker: str = NO_ANSWER_MARKER
) -> tuple[bool, str]:
    """Treat as no-answer only if `marker` starts the response (after leading whitespace).

    The QA prompt requires the marker first, then the user-facing text. A later
    occurrence (quoted instructions, a fenced example, echoed retrieved text)
    is not a no-answer signal and must stay in the body.

    Returns (answer_found, text_for_user). When no-answer, the leading marker
    and following whitespace are stripped. Leading whitespace on a real answer
    is left unchanged.
    """
    stripped = llm_response.lstrip()
    if stripped.startswith(marker):
        return False, stripped[len(marker) :].lstrip()
    return True, llm_response


# Same-prompt retry after a leading [[NO_ANSWER]] when the top rerank score is
# above this value. The retry's answer is served; build_no_answer_retry_log_record
# captures each occurrence so pr42-enhancements.2 can check whether try-2 is
# actually grounded in that high-scoring chunk.
NO_ANSWER_RETRY_SCORE_THRESHOLD = 3.0


def build_no_answer_retry_log_record(
    *,
    product: str,
    original_query: str,
    generated_query: str,
    top_rerank_score: float,
    threshold: float,
    top_chunk_text: str,
    top_chunk_metadata: dict,
    first_response: str,
    retry_response: str,
    first_answer_found: bool,
    retry_answer_found: bool,
) -> dict:
    """Full retry payload for DEBUG (`NO_ANSWER_RETRY_DETAIL`).

    Queries, both raw completions, and the top chunk are PII-ish — do not log
    this dict at INFO. `changed_outcome` is true when try-1 was no-answer and
    try-2 is not. pr42-enhancements.2 should extract DEBUG detail lines (or
    enable DEBUG for a measurement window).
    """
    return {
        "event": "NO_ANSWER_RETRY",
        "product": product,
        "original_query": original_query,
        "generated_query": generated_query,
        "top_rerank_score": top_rerank_score,
        "threshold": threshold,
        "top_chunk_text": top_chunk_text,
        "top_chunk_metadata": top_chunk_metadata,
        "first_response": first_response,
        "retry_response": retry_response,
        "first_answer_found": first_answer_found,
        "retry_answer_found": retry_answer_found,
        "changed_outcome": (not first_answer_found) and retry_answer_found,
    }


def build_no_answer_retry_info_record(detail: dict) -> dict:
    """INFO subset: enough for ops to see a retry ran, no question/answer text."""
    return {
        "event": "NO_ANSWER_RETRY",
        "product": detail["product"],
        "top_rerank_score": detail["top_rerank_score"],
        "threshold": detail["threshold"],
        "first_answer_found": detail["first_answer_found"],
        "retry_answer_found": detail["retry_answer_found"],
        "changed_outcome": detail["changed_outcome"],
        "query_chars": len(detail.get("original_query") or ""),
        "retry_chars": len(detail.get("retry_response") or ""),
    }


def get_env_variable(var_name: str, default: Optional[str] = None) -> str:
    """
    Helper function to get the environment variable or raise exception.
    Used inside the container applications. DO NOT REMOVE!
    """
    try:
        return os.environ[var_name]
    except KeyError as exc:
        if default is not None:
            return default
        error_msg = f"The environment variable {var_name} was missing, abort..."
        logger.critical("%s", error_msg)
        raise EnvironmentError(error_msg) from exc


def format_ns(time_in_ns):
    """convert nanoseconds to text format"""
    formatted_time_upto_seconds = datetime.fromtimestamp(time_in_ns / 1e9, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )

    fractional_sec = (time_in_ns // 10**9) * 10**9
    nanoseconds_string = f"{fractional_sec}"[:9]

    return f"{formatted_time_upto_seconds}.{nanoseconds_string}Z"


# For time in nanoseconds
# https://stackoverflow.com/questions/31328300/python-logging-module-logging-timestamp-to-include-microsecond
class LogRecordNs(logging.LogRecord):  # pylint: disable=too-few-public-methods
    """class that returns nanoseconds"""

    def __init__(self, *args, **kwargs):
        self.created_ns = time_ns()  # Fetch precise timestamp
        super().__init__(*args, **kwargs)


LOG_LEVEL = logging.DEBUG if __debug__ else logging.INFO
if log_level := LILLISA_SERVER_ENV_DICT["LOG_LEVEL"]:
    if log_level == "DEBUG":
        LOG_LEVEL = logging.DEBUG
    elif log_level == "INFO":
        LOG_LEVEL = logging.INFO
    elif log_level == "WARNING":
        LOG_LEVEL = logging.WARNING
    elif log_level == "ERROR":
        LOG_LEVEL = logging.ERROR
    elif log_level == "CRITICAL":
        LOG_LEVEL = logging.CRITICAL
    else:
        raise ValueError("LOG_LEVEL is not one of DEBUG, INFO, WARNING, ERROR, CRITICAL")
else:
    print("LOG_LEVEL env variable is not specified in env file or environment variable")
logging.basicConfig(level=LOG_LEVEL)


class FormatterNs(logging.Formatter):
    """nanosecond log formatter"""

    default_nsec_format = "%Y-%m-%dT%H:%M:%S.%09dZ"

    def formatTime(self, record, datefmt=None):
        if datefmt is not None:  # Do not handle custom formats here ...
            return super().formatTime(record, datefmt)  # ... leave to original implementation
        return format_ns(record.created_ns)


logging.setLogRecordFactory(LogRecordNs)

LOG_FORMAT = "%(asctime)s - %(levelname)s - %(filename)s-%(funcName)s - %(message)s"
log_formatter = FormatterNs(LOG_FORMAT)

# create logger
logger = logging.getLogger("RL_Logger")
logger.setLevel(LOG_LEVEL)

if LOG_LEVEL == "DEBUG":

    logger.propagate = False  # otherwise you will see duplicate log entries

    # # clear any existing handlers for our logger
    logger.handlers.clear()

    # create console handler and set level to debug
    ch = logging.StreamHandler()
    ch.setLevel(LOG_LEVEL)

    # create and add formatter to ch
    ch.setFormatter(log_formatter)

    # add ch to logger
    logger.addHandler(ch)


# create a separate logger for pytest_logger to log assertions
# with a slightly different format
pytest_assertion_logger = logging.getLogger("PyTest_Logger")
pytest_assertion_logger.setLevel(logging.DEBUG)
pytest_assertion_logger.propagate = False  # otherwise you will see duplicate log entries
pytest_assertion_logger.handlers.clear()
ch = logging.StreamHandler()
ch.setLevel(logging.DEBUG)
ch.setFormatter(FormatterNs("%(asctime)s - %(levelname)s - %(message)s"))
pytest_assertion_logger.addHandler(ch)

logging.getLogger("opentelemetry.exporter.otlp.proto.grpc.exporter").setLevel(logging.ERROR)
logging.getLogger("boto").setLevel(logging.WARNING)
logging.getLogger("botocore").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)
logging.getLogger("filelock").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("speedict").setLevel(logging.WARNING)
logging.getLogger("llamaindex").setLevel(logging.WARNING)
logging.getLogger("llama_index.core").setLevel(logging.WARNING)
logging.getLogger("llama_index.core.indices").setLevel(logging.WARNING)
logging.getLogger("llama_index.core.indices.utils").setLevel(logging.WARNING)
logging.getLogger("src.llama_index_lancedb_vector_store").setLevel(logging.WARNING)

# some testing code
if __name__ == "__main__":
    logger.debug("debug message")
    logger.info("info message")
    logger.warning("warn message")
    logger.error("error message")
    logger.critical("critical message")

# To disable __debug__ and set the log level to INFO, use the -O option as shown below
# python3 -O utils.py
