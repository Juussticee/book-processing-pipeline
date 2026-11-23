"""Local vision-language model service for book cover recovery.

Provides a local alternative to GeminiRecoveryService. Uses Ollama's
HTTP API to run a vision-language model (VLM) locally — no external API
key, no daily quota, no network dependency beyond localhost.

The service is designed as a DROP-IN replacement for the Gemini recovery
extraction layer. It produces the same output shape ({title, author,
confidence}) so the existing MetadataService.identifybook() pipeline
remains the decision-maker.

Architecture:
    START PROCESS
        ↓
    Load model once (Ollama handles this automatically)
        ↓
    Process images sequentially (one at a time)
        ↓
    Unload/close when done

Resource safety:
    - No multiprocessing or concurrent calls
    - Max 2 retries per image with exponential backoff
    - Memory safety check before each inference
    - Model persists for the process lifetime (Ollama server manages it)
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from typing import Optional

from core.logger import setup_logger

logger = setup_logger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
DEFAULT_MODEL = os.getenv("LOCAL_VLM_MODEL", "moondream:latest")
DEFAULT_TIMEOUT = 120  # seconds — VLMs need time for image reasoning
DEFAULT_MAX_RETRIES = 2
MIN_FREE_RAM_GB = 0.5  # moondream:latest is 1B params — works with < 1GB free

# Recovery triggers only for books with confidence below this threshold
RECOVERY_CONFIDENCE_THRESHOLD: float = 0.70

# VLM prompt (optimized for book cover analysis)
# Note: moondream does NOT respect Ollama's format=json parameter reliably,
# so we include explicit JSON structure guidance in the prompt itself.
VVM_PROMPT = (
    "Return ONLY valid JSON. No markdown, no extra text. \n"
    "{\n"
    '  "title": "the book title text", \n'
    '  "author": "the primary author name", \n'
    '  "confidence": 0.9 \n'
    "}\n"
    "\n"
    "This is a book cover image. Extract visible title and author. \n"
    "If text unreadable, use empty strings and 0.0 confidence. \n"
)


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------


class LocalVLMError(Exception):
    """Base exception for local VLM failures."""


class LocalVLMNotAvailableError(LocalVLMError):
    """Raised when Ollama is not installed or not running."""


class LocalVLMModelNotFoundError(LocalVLMError):
    """Raised when the specified model is not downloaded in Ollama."""


class LocalVLMTimeoutError(LocalVLMError):
    """Raised when inference exceeds the timeout."""


class LocalVLMJSONParseError(LocalVLMError):
    """Raised when the model response is not valid JSON."""


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class VLMRecoveryResult:
    """Result of a local VLM extraction attempt.

    Attributes:
        vlm_called: Whether the VLM was actually invoked.
            False if skipped due to memory safety, errors, or high confidence.
        title: Extracted book title, or None if not extracted.
        author: Extracted author name, or None if not extracted.
        confidence: VLM's confidence estimate (0.0-1.0).
        reasoning: Short factual explanation from the VLM.
        raw_response: The raw model output text.
        error: Error message if extraction failed.
        retry_count: Number of retries consumed.
        latency: Inference time in seconds.
        ram_used_mb: Estimated RAM used during inference.
    """

    vlm_called: bool = False
    title: Optional[str] = None
    author: Optional[str] = None
    confidence: float = 0.0
    reasoning: Optional[str] = None
    raw_response: Optional[str] = None
    error: Optional[str] = None
    retry_count: int = 0
    latency: float = 0.0
    ram_used_mb: Optional[float] = None

    def to_dict(self) -> dict:
        """Convert to dict for the orchestrator."""
        return {
            "vlm_called": self.vlm_called,
            "title": self.title,
            "author": self.author,
            "confidence": self.confidence,
            "reasoning": self.reasoning,
            "raw_response": self.raw_response,
            "error": self.error,
            "retry_count": self.retry_count,
            "latency": self.latency,
            "ram_used_mb": self.ram_used_mb,
        }


# ---------------------------------------------------------------------------
# Memory utilities
# ---------------------------------------------------------------------------


def get_available_ram_gb() -> float:
    """Return available system RAM in GB.

    Uses psutil if available, falls back to platform-specific methods.
    """
    try:
        import psutil

        return psutil.virtual_memory().available / (1024 ** 3)
    except ImportError:
        pass
    # Fallback for Unix-like systems
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return float(line.split()[1]) / (1024 ** 2)  # KB -> GB
    except (FileNotFoundError, IndexError):
        pass
    # Fallback for Windows (via wmic)
    try:
        import subprocess

        result = subprocess.run(
            ["wmic", "OS", "get", "FreePhysicalMemory"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        lines = result.stdout.strip().split("\n")
        if len(lines) >= 2:
            return float(lines[1].strip()) / (1024 ** 2)  # KB -> GB
    except Exception:
        pass
    return -1.0  # Unable to determine


def check_memory_safety() -> Optional[str]:
    """Check if there's enough free RAM for safe VLM inference.

    Returns None if safe, or an error message string if unsafe.
    """
    available_gb = get_available_ram_gb()
    if available_gb < 0:
        return "WARNING: Could not determine available RAM - proceeding anyway"
    if available_gb < MIN_FREE_RAM_GB:
        return (
            f"LOCAL VVM MEMORY SAFETY LIMIT REACHED: "
            f"only {available_gb:.1f} GB RAM available "
            f"(minimum {MIN_FREE_RAM_GB} GB required)"
        )
    return None


# ---------------------------------------------------------------------------
# Main service
# ---------------------------------------------------------------------------


class LocalVisionService:
    """Local VLM service via Ollama HTTP API.

    Drop-in replacement for GeminiRecoveryService.extract_from_image().
    Communicates with localhost Ollama server (default port 11434).

    Args:
        model: Ollama model name (e.g. "moondream:latest").
        host: Ollama server URL.
        timeout: Per-request timeout in seconds.
        max_retries: Maximum retry attempts per image.
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        host: str = OLLAMA_HOST,
        timeout: int = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._model = model
        self._host = host
        self._timeout = timeout
        self._max_retries = max_retries
        self._initialized = False

    # -------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------

    def extract_from_image(
        self,
        image_path: str,
        current_confidence: float = 0.0,
        recovery_threshold: float = RECOVERY_CONFIDENCE_THRESHOLD,
    ) -> VLMRecoveryResult:
        """Extract title/author from a book cover image using local VLM.

        Mirrors GeminiRecoveryService.extract_from_image() interface:
        - Skips when current_confidence >= recovery_threshold (fast path)
        - Returns VLMRecoveryResult with same field semantics

        Args:
            image_path: Path to the book cover image.
            current_confidence: Existing pipeline confidence (0.0-1.0).
            recovery_threshold: Skip if confidence >= this (default 0.70).

        Returns:
            VLMRecoveryResult. If current_confidence >= recovery_threshold,
            vlm_called=False.
        """
        # Fast path: skip VLM for high-confidence books
        if current_confidence >= recovery_threshold:
            return VLMRecoveryResult(
                vlm_called=False,
                error="skipped_high_confidence",
            )

        # Memory safety check
        mem_warning = check_memory_safety()
        if mem_warning and "LIMIT REACHED" in mem_warning:
            logger.error(mem_warning)
            return VLMRecoveryResult(
                vlm_called=True,
                error=mem_warning,
            )

        # Ensure Ollama is available
        if not self._check_ollama_available():
            return VLMRecoveryResult(
                vlm_called=True,
                error=f"Ollama server not reachable at {self._host}",
            )

        # Ensure model is pulled
        if not self._ensure_model():
            return VLMRecoveryResult(
                vlm_called=True,
                error=f"Model '{self._model}' not available in Ollama",
            )

        # Measure RAM before inference
        ram_before = get_available_ram_gb()

        # Process with retries
        retry_count = 0
        last_error: Optional[str] = None
        start_time = time.time()

        for attempt in range(self._max_retries + 1):
            try:
                result = self._call_vlm(image_path)
                result.retry_count = retry_count
                result.latency = time.time() - start_time
                ram_after = get_available_ram_gb()
                result.ram_used_mb = (
                    (ram_before - ram_after) * 1024
                    if ram_before >= 0 and ram_after >= 0
                    else None
                )
                return result

            except LocalVLMTimeoutError as exc:
                last_error = f"Timeout: {exc}"
                if attempt < self._max_retries:
                    retry_count += 1
                    self._backoff(retry_count)
                    continue
                break

            except LocalVLMJSONParseError as exc:
                # Parse errors are deterministic -- don't retry
                logger.warning(
                    "VLM JSON parse error for %s: %s",
                    image_path,
                    exc,
                )
                return VLMRecoveryResult(
                    vlm_called=True,
                    error=str(exc),
                    retry_count=retry_count,
                    latency=time.time() - start_time,
                )

            except LocalVLMError as exc:
                last_error = str(exc)
                if attempt < self._max_retries:
                    retry_count += 1
                    self._backoff(retry_count)
                    continue
                break

            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self._max_retries:
                    retry_count += 1
                    self._backoff(retry_count)
                    continue
                break

        total_latency = time.time() - start_time
        return VLMRecoveryResult(
            vlm_called=True,
            error=last_error or "all retries exhausted",
            retry_count=retry_count,
            latency=total_latency,
        )

    # -------------------------------------------------------------------
    # Internal: Ollama communication
    # -------------------------------------------------------------------

    def _check_ollama_available(self) -> bool:
        """Check if the model is loaded in Ollama."""
        try:
            import httpx

            response = httpx.get(
                f"{self._host}/api/tags",
                timeout=5,
            )
            return response.status_code == 200
        except Exception as exc:
            logger.error("Ollama check failed: %s", exc)
            return False

    def _ensure_model(self) -> bool:
        """Ensure the model is downloaded in Ollama.

        Returns True if model exists or was pulled successfully.
        """
        try:
            import httpx

            # Check if model exists
            response = httpx.get(
                f"{self._host}/api/tags",
                timeout=10,
            )
            if response.status_code == 200:
                models = response.json().get("models", [])
                model_names = [m.get("name", "") for m in models]
                if self._model in model_names:
                    self._initialized = True
                    return True

            # Model not found -- try to pull it
            logger.info("Pulling model '%s' from Ollama...", self._model)
            pull_response = httpx.post(
                f"{self._host}/api/pull",
                json={"name": self._model},
                timeout=600,  # Pull can take a while
            )
            if pull_response.status_code == 200:
                self._initialized = True
                return True
            else:
                logger.error(
                    "Failed to pull model '%s': HTTP %d",
                    self._model,
                    pull_response.status_code,
                )
                return False
        except Exception as exc:
            logger.error("Model check/pull failed: %s", exc)
            return False

    def _call_vlm(self, image_path: str) -> VLMRecoveryResult:
        """Send image to Ollama VLM and parse JSON response.

        Raises:
            LocalVLMTimeoutError: If the request times out.
            LocalVLMJSONParseError: If response is not valid JSON.
            LocalVLMError: For other VLM failures.
        """
        # Encode image as base64
        try:
            with open(image_path, "rb") as f:
                image_data = base64.b64encode(f.read()).decode("utf-8")
        except Exception as exc:
            raise LocalVLMError(f"Failed to read image: {exc}")

        # Call Ollama chat API with vision
        import httpx

        payload = {
            "model": self._model,
            "messages": [
                {
                    "role": "user",
                    "content": VVM_PROMPT,
                    "images": [image_data],
                }
            ],
            "stream": False,
        }

        try:
            response = httpx.post(
                f"{self._host}/api/chat",
                json=payload,
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise LocalVLMTimeoutError(
                f"VLM inference timed out after {self._timeout}s"
            )

        if response.status_code != 200:
            raise LocalVLMError(
                f"Ollama API returned HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )

        try:
            result = response.json()
        except json.JSONDecodeError as exc:
            raise LocalVLMJSONParseError(
                f"Invalid JSON from Ollama: {exc}"
            )

        # Extract the response content
        message = result.get("message", {})
        content = message.get("content", "")

        # Parse JSON from the response (handles moondream's quirks)
        parsed = self._parse_vlm_response(content)

        # Extract fields with safe defaults
        title = str(parsed.get("title", "")).strip() or None
        author = str(parsed.get("author", "")).strip() or None
        try:
            confidence = float(parsed.get("confidence", 0.0) or 0.0)
        except (ValueError, TypeError):
            confidence = 0.0
        # Clamp confidence to [0, 1]
        confidence = max(0.0, min(1.0, confidence))
        reasoning = str(parsed.get("reasoning", "")).strip() or None

        return VLMRecoveryResult(
            vlm_called=True,
            title=title,
            author=author,
            confidence=confidence,
            reasoning=reasoning,
            raw_response=content,
        )

    def _parse_vlm_response(self, content: str) -> dict:
        """Parse VLM response, handling moondream's repeated/echoed output.

        moondream:latest on Ollama has these behaviours:
        1. Wraps output in a JSON array [...], possibly with repeated objects
        2. Sometimes includes extra fields (age, text) that should be ignored
        3. Sometimes has duplicate keys (first wins in Python's json.loads)
        4. Ignores format=json and includes prompt template text in values

        This parser:
        1. Strips markdown fences
        2. If the content is a JSON array, extracts the object with the
           highest confidence (prefers non-zero confidence)
        3. If not parseable as JSON, extracts the first valid {...} block
        4. Filters to only title, author, confidence keys
        """
        cleaned = content.strip()

        # Remove markdown code fences if present
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        # Try direct parse as a plain dict first
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict):
                return self._filter_fields(parsed)
            elif isinstance(parsed, list):
                # Find the dict with the highest confidence
                best = self._select_best_from_array(parsed)
                if best is not None:
                    return best
        except json.JSONDecodeError:
            pass

        # moondream repeats JSON objects — find the first complete {…} block
        # and try to parse it
        brace_depth = 0
        json_start = -1
        candidates = []
        for i, ch in enumerate(cleaned):
            if ch == "{":
                if brace_depth == 0:
                    json_start = i
                brace_depth += 1
            elif ch == "}":
                brace_depth -= 1
                if brace_depth == 0 and json_start >= 0:
                    candidate = cleaned[json_start : i + 1]
                    try:
                        parsed = json.loads(candidate)
                        if isinstance(parsed, dict):
                            candidates.append(parsed)
                    except json.JSONDecodeError:
                        pass
                    json_start = -1

        # If no complete JSON object found, the response may be truncated
        # (moondream produces very long output that Ollama truncates).
        # Try to salvage by closing any unclosed braces/strings.
        if not candidates and json_start >= 0:
            # Try closing the object by appending closing chars
            truncated = cleaned[json_start:]
            # Find the last key-value pair we have
            # Try appending "} to close
            for suffix in ['"}', '"}}', '"\n  }', '"\n}}']:
                attempt = truncated.rstrip() + suffix
                try:
                    parsed = json.loads(attempt)
                    if isinstance(parsed, dict):
                        candidates.append(parsed)
                        break
                except json.JSONDecodeError:
                    continue
            # Also try regex-based extraction for specific patterns
            if not candidates:
                candidates = self._salvage_truncated_json(cleaned, json_start)

        if candidates:
            # Prefer the candidate with highest confidence
            best = self._select_best_from_array(candidates)
            if best is not None:
                return best

        # If still no valid JSON found, try finding JSON after the separator
        if "---" in cleaned:
            parts = cleaned.split("---", 1)
            if len(parts) > 1:
                cleaned = parts[1].strip()
                try:
                    parsed = json.loads(cleaned)
                    if isinstance(parsed, dict):
                        return self._filter_fields(parsed)
                    elif isinstance(parsed, list):
                        best = self._select_best_from_array(parsed)
                        if best is not None:
                            return best
                except json.JSONDecodeError:
                    pass

        raise LocalVLMJSONParseError(
            f"Could not parse VLM response as JSON. "
            f"Raw (first 500 chars): {content[:500]}"
        )

    def _salvage_truncated_json(self, cleaned: str, start: int) -> list:
        """Try to salvage a truncated JSON object by extracting fields.

        When moondream output is truncated mid-string, we use regex to
        extract known fields (title, author, confidence) from the partial JSON.
        """
        import re
        obj_text = cleaned[start:]
        result = {}

        # Extract title (may be truncated — capture what we have)
        title_match = re.search(r'"title"\s*:\s*"((?:[^"\\]|\\.)*)', obj_text)
        if title_match:
            result["title"] = title_match.group(1)

        # Extract author
        author_match = re.search(r'"author"\s*:\s*"((?:[^"\\]|\\.)*)"', obj_text)
        if author_match:
            result["author"] = author_match.group(1)

        # Extract confidence
        conf_match = re.search(r'"confidence"\s*:\s*([0-9.]+)', obj_text)
        if conf_match:
            try:
                result["confidence"] = float(conf_match.group(1))
            except ValueError:
                pass

        if result:
            return [result]
        return []

    def _filter_fields(self, parsed: dict) -> dict:
        """Keep only title, author, confidence fields from parsed JSON."""
        return {
            "title": parsed.get("title", ""),
            "author": parsed.get("author", ""),
            "confidence": parsed.get("confidence", 0.0),
        }

    def _select_best_from_array(self, arr) -> Optional[dict]:
        """From a list of JSON objects, pick the one with highest confidence.

        If all have confidence 0.0 or missing, pick the first one that has
        non-empty title or author.
        """
        if not arr:
            return None

        # Filter: extract only valid dicts with our fields
        filtered = []
        for item in arr:
            if isinstance(item, dict):
                filtered.append(self._filter_fields(item))

        if not filtered:
            return None

        # Prefer items with non-zero confidence
        with_conf = [f for f in filtered if f.get("confidence", 0) > 0]
        if with_conf:
            return max(with_conf, key=lambda x: x.get("confidence", 0))

        # If all confidence is 0, prefer items with non-empty title/author
        with_text = [f for f in filtered if f.get("title") or f.get("author")]
        if with_text:
            return with_text[0]

        return filtered[0]

    def _backoff(self, attempt: int) -> None:
        """Exponential backoff with jitter between retries."""
        delay = min(2 ** attempt, 10)  # max 10s
        jitter = delay * 0.25
        delay += (time.time() % 1) * jitter  # simple jitter
        logger.warning(
            "VLM retry %d after %.1fs",
            attempt,
            delay,
        )
        time.sleep(delay)

    # -------------------------------------------------------------------
    # Cleanup
    # -------------------------------------------------------------------

    def unload_model(self) -> None:
        """Unload the model from Ollama to free VRAM/RAM.

        Sends a DELETE request to the Ollama model management API.
        """
        try:
            import httpx

            response = httpx.delete(
                f"{self._host}/api/models/{self._model}",
                timeout=30,
            )
            if response.status_code == 200:
                logger.info("Model '%s' unloaded from Ollama", self._model)
            else:
                logger.warning(
                    "Failed to unload model '%s': HTTP %d",
                    self._model,
                    response.status_code,
                )
        except Exception as exc:
            logger.warning("Could not unload model: %s", exc)
