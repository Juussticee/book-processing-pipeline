"""PaddleOCR-VL-1.6 Vision Extraction Service.

Isolated service that sends book-cover images to the Baidu AI Studio
PaddleOCR-VL-1.6 API and returns structured layout data.

Does NOT touch OCR fallback, book ranking, or production pipeline.
Configured via environment variables:

    PADDLEOCR_API_TOKEN   AI Studio bearer token (required)
    PADDLEOCR_BASE_URL    API base URL (default: https://paddleocr.aistudio-app.com)
    PADDLEOCR_TIMEOUT     Per-request timeout in seconds (default: 30)
    PADDLEOCR_MAX_RETRIES Max retries on 429 (default: 3)
    PADDLEOCR_JOB_TIMEOUT Polling timeout per job, seconds (default: 60)
    PADDLEOCR_JOB_POLL_INTERVAL  Poll interval, seconds (default: 2)

API Flow:
    POST /api/v2/ocr/jobs   →  jobId
    GET  /api/v2/ocr/jobs/{jobId}  →  pending|running|done|failed
    Download and parse the JSON result
    Parse layout blocks
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import requests

from core.logger import setup_logger

logger = setup_logger(__name__)


@dataclass
class LayoutBlock:
    """A single layout block from PaddleOCR-VL."""

    text: str
    bbox: List[int]  # [x1, y1, x2, y2]
    label: str  # "title", "author", "paragraph", "table", etc.
    score: float = 0.0
    order: int = 0  # Reading order
    polygon: Optional[List[List[float]]] = None
    text_lines: List[Dict[str, Any]] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    def area(self) -> int:
        if len(self.bbox) >= 4:
            return (self.bbox[2] - self.bbox[0]) * (self.bbox[3] - self.bbox[1])
        return 0

    def height(self) -> int:
        if len(self.bbox) >= 4:
            return self.bbox[3] - self.bbox[1]
        return 0

    def width(self) -> int:
        if len(self.bbox) >= 4:
            return self.bbox[2] - self.bbox[0]
        return 0


@dataclass
class PaddleOCRVlResult:
    """Structured result from PaddleOCR-VL extraction."""

    drive_id: str
    image_path: str
    orientation: int = 0  # 0, 90, 180, 270
    layout_blocks: List[LayoutBlock] = field(default_factory=list)
    raw_markdown: str = ""
    raw_response: Dict[str, Any] = field(default_factory=dict)
    api_latency_s: float = 0.0
    total_processing_time: float = 0.0
    request_count: int = 0
    error: Optional[str] = None
    http_status: int = 200


class PaddleOCRVlService:
    """Service that calls the Baidu AI Studio PaddleOCR-VL-1.6 API.

    Handles the full API flow: submit job → poll → download → parse.
    Returns structured data for the title/author extraction layer.
    """

    def __init__(
        self,
        token: Optional[str] = None,
        base_url: str = "https://paddleocr.aistudio-app.com",
        timeout: int = 30,
        max_retries: int = 3,
        job_timeout: int = 60,
        job_poll_interval: float = 2.0,
        model: str = "PaddleOCR-VL-1.6",
    ) -> None:
        self._token = token or os.environ.get("PADDLEOCR_API_TOKEN", "")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_retries = max_retries
        self._job_timeout = job_timeout
        self._job_poll_interval = job_poll_interval
        self._model = model

        if not self._token:
            import logging

            logging.getLogger(__name__).warning(
                "PADDLEOCR_API_TOKEN not set — service will reject requests"
            )

    @property
    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def is_configured(self) -> bool:
        return bool(self._token)

    def submit_job(self, image_path: str) -> Dict[str, Any]:
        """Submit an image for PaddleOCR-VL processing.

        Uses multipart upload for local image files.
        """
        if not self._token:
            raise ValueError("PADDLEOCR_API_TOKEN is not set")

        url = urljoin(self._base_url + "/", "/api/v2/ocr/jobs")
        headers = {
            "Authorization": f"Bearer {self._token}",
        }
        optional_payload = {
            "useDocOrientationClassify": True,
            "useDocUnwarping": False,
            "useChartRecognition": False,
        }

        # Build multipart form: file upload + model parameter
        # The API expects the image field to be named "file" and requires
        # a "model" parameter specifying the OCR model version.
        fh = open(image_path, "rb")
        files = {
            "file": (Path(image_path).name, fh, "image/jpeg"),
        }
        data = {
            "model": self._model,
            "useDocOrientationClassify": "true",
            "useDocUnwarping": "false",
            "useChartRecognition": "false",
        }

        t0 = time.time()
        try:
            resp = requests.post(
                url,
                headers=headers,
                files=files,
                data=data,
                timeout=self._timeout,
            )
        finally:
            fh.close()

        latency = time.time() - t0

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else 5.0
            raise _RateLimitError(f"429 Too Many Requests, retry after {wait}s", wait)

        if resp.status_code >= 400:
            raise _APIError(
                f"HTTP {resp.status_code}: {resp.text[:500]}",
                status_code=resp.status_code,
            )

        try:
            result = resp.json()
        except json.JSONDecodeError as e:
            raise _APIError(
                f"Malformed JSON response: {e}", status_code=resp.status_code
            )

        job_id = (
            result.get("data", {}).get("jobId")
            or result.get("jobId")
            or result.get("job_id")
        )
        if not job_id:
            raise _APIError("No jobId in response", status_code=resp.status_code)

        return {"job_id": job_id, "raw": result, "latency": latency}

    def poll_job(self, job_id: str) -> Dict[str, Any]:
        """Poll a job until it's done or failed. Returns the result dict."""
        url = urljoin(self._base_url + "/", f"/api/v2/ocr/jobs/{job_id}")
        deadline = time.time() + self._job_timeout

        while time.time() < deadline:
            try:
                resp = requests.get(url, headers=self._headers, timeout=self._timeout)
            except requests.Timeout:
                # Transient: the poll loop retries until the deadline. Log at
                # debug so a persistently timing-out API is still diagnosable.
                logger.debug("PaddleOCR poll timed out for job %s (continuing)", job_id)
            else:
                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after else 5.0
                    time.sleep(min(wait, 30))
                    continue

                if resp.status_code >= 400:
                    raise _APIError(
                        f"HTTP {resp.status_code}: {resp.text[:500]}",
                        status_code=resp.status_code,
                    )

                try:
                    result = resp.json()
                except json.JSONDecodeError as e:
                    raise _APIError(
                        f"Malformed JSON: {e}", status_code=resp.status_code
                    )

                data = result.get("data", result)
                state = data.get("state") or data.get("status", "pending")
                if state in ("done", "completed", "succeeded"):
                    return data
                if state in ("failed", "error"):
                    err = data.get("error", "Job failed")
                    raise _APIError(f"Job {job_id} failed: {err}", status_code=500)

                # pending / running — keep polling
            time.sleep(self._job_poll_interval)

        raise _TimeoutError(f"Job {job_id} timed out after {self._job_timeout}s")

    def download_result(self, result_url: str) -> List[Dict[str, Any]]:
        """Download and parse the JSON result from the job result URL.

        The API returns a JSON object with structure:
            {"logId": "...", "result": {"layoutParsingResults": [...]}}
        Falls back to JSONL parsing if the response is not a single JSON object.
        """
        # Download WITHOUT Authorization header — the URL contains auth
        # credentials in the query string. Sending a Bearer token causes
        # BCE (Baidu Cloud) storage to reject with 400 SignatureDoesNotMatch.
        resp = requests.get(result_url, timeout=self._timeout)
        if resp.status_code >= 400:
            raise _APIError(
                f"HTTP {resp.status_code} downloading result: {resp.text[:300]}",
                status_code=resp.status_code,
            )

        try:
            full = resp.json()
            return [full]
        except json.JSONDecodeError:
            # Try JSONL format as fallback
            lines = resp.text.strip().split("\n")
            records = []
            for line in lines:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            return records

    def extract(self, image_path: str, drive_id: str = "") -> PaddleOCRVlResult:
        """Full extraction: submit → poll → download → parse.

        Returns a PaddleOCRVlResult with layout blocks and metadata.
        """
        from core.logger import setup_logger

        logger = setup_logger(__name__)

        start_time = time.time()
        result = PaddleOCRVlResult(
            drive_id=drive_id or Path(image_path).stem,
            image_path=image_path,
        )

        try:
            # Step 1: Submit job
            job_data = self.submit_job(image_path)
            result.request_count += 1
            job_id = job_data["job_id"]
            result.raw_response["submit"] = job_data["raw"]
            logger.info("Submitted PaddleOCR-VL job %s for %s", job_id, image_path)

            # Step 2: Poll until done
            job_result = self.poll_job(job_id)
            result.raw_response["poll"] = job_result

            # Step 3: Download result
            # result_url may be a dict {"jsonUrl": "..."} or a string
            result_url = job_result.get("resultUrl") or job_result.get("jsonUrl")
            if isinstance(result_url, dict):
                result_url = result_url.get("jsonUrl") or result_url.get("url")
            if not result_url:
                # Look for resultUrl/jsonUrl in nested structures
                for v in job_result.values():
                    if isinstance(v, dict):
                        if "resultUrl" in v:
                            url_val = v["resultUrl"]
                            if isinstance(url_val, dict):
                                result_url = url_val.get("jsonUrl") or url_val.get(
                                    "url"
                                )
                            else:
                                result_url = url_val
                        elif "jsonUrl" in v:
                            result_url = v["jsonUrl"]
                        elif "url" in v:
                            result_url = v["url"]
                        if result_url:
                            break
                if not result_url:
                    # Try inside data wrapper (poll_job may return full response)
                    data_wrapper = job_result.get("data", job_result)
                    if isinstance(data_wrapper, dict):
                        ru = data_wrapper.get("resultUrl") or data_wrapper.get(
                            "jsonUrl"
                        )
                        if isinstance(ru, dict):
                            result_url = ru.get("jsonUrl") or ru.get("url")
                        elif isinstance(ru, str):
                            result_url = ru

            if result_url:
                records = self.download_result(result_url)
                result.request_count += 1
                result.raw_response["records"] = records
            else:
                records = [job_result]

            # Step 4: Parse layout blocks
            if records:
                parsed = self._parse_layout(records[0], job_result)
                result.layout_blocks = parsed["layout_blocks"]
                result.raw_markdown = parsed.get("markdown", "")
                result.orientation = parsed.get("orientation", 0)
                result.raw_response["parsed"] = parsed

            result.api_latency_s = time.time() - start_time
            result.total_processing_time = result.api_latency_s
            logger.info(
                "PaddleOCR-VL complete: %d blocks, orient=%d, %.1fs",
                len(result.layout_blocks),
                result.orientation,
                result.total_processing_time,
            )

        except _RateLimitError as e:
            result.error = f"Rate limit: {e}"
            result.total_processing_time = time.time() - start_time
        except _APIError as e:
            result.error = f"API error: {e}"
            result.http_status = e.status_code
            result.total_processing_time = time.time() - start_time
        except _TimeoutError as e:
            result.error = f"Timeout: {e}"
            result.total_processing_time = time.time() - start_time
        except Exception as e:
            result.error = f"Unexpected error: {e}"
            result.total_processing_time = time.time() - start_time

        return result

    def _parse_layout(
        self, record: Dict[str, Any], raw: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Parse the PaddleOCR-VL result record into structured layout blocks.

        Handles the actual API response structure:
            {"result": {"layoutParsingResults": [{"prunedResult": {"parsing_res_list": [...]}}]}}
        """
        blocks: List[LayoutBlock] = []
        markdown = ""

        # Navigate the API response structure
        # Top-level may have "result" wrapper containing layoutParsingResults
        result_data = record.get("result", record)

        # layoutParsingResults is a list; first element has prunedResult
        lpr_list = result_data.get("layoutParsingResults", [])
        if isinstance(lpr_list, list) and lpr_list:
            first = lpr_list[0]
        elif isinstance(lpr_list, dict):
            first = lpr_list
        else:
            first = {}

        pruned = first.get("prunedResult", {})

        # Extract markdown
        md_data = first.get("markdown", {})
        if isinstance(md_data, dict):
            markdown = md_data.get("text", "")
        elif isinstance(md_data, str):
            markdown = md_data
        if not markdown:
            markdown = result_data.get("markdown", "")

        # Extract orientation from doc_preprocessor_res
        orientation = 0
        if "doc_preprocessor_res" in pruned:
            orientation = pruned.get("doc_preprocessor_res", {}).get("angle", 0)
        elif "doc_orientation" in record:
            orient_info = record.get("doc_orientation", {})
            if isinstance(orient_info, dict):
                orientation = orient_info.get("angle", 0)
        elif "doc_orientation" in first:
            orient_info = first.get("doc_orientation", {})
            if isinstance(orient_info, dict):
                orientation = orient_info.get("angle", 0)
        if isinstance(orientation, str):
            try:
                orientation = int(orientation)
            except ValueError:
                orientation = 0

        # Extract layout blocks from parsing_res_list
        blocks_raw = pruned.get("parsing_res_list", [])

        # Fallback: try layout_det_res.boxes
        if not blocks_raw:
            layout_det = pruned.get("layout_det_res", {})
            blocks_raw = layout_det.get("boxes", [])

        # Fallback: try direct layout_blocks
        if not blocks_raw:
            layout_blocks = first.get("layout_blocks", []) or record.get(
                "layout_blocks", []
            )
            blocks_raw = layout_blocks

        for i, block in enumerate(blocks_raw):
            if not isinstance(block, dict):
                continue

            # Handle both prunedResult format and layout_det_res format
            text = block.get("block_content") or block.get("text", "")
            bbox = (
                block.get("block_bbox")
                or block.get("coordinate")
                or block.get("bbox", [])
            )
            label = block.get("block_label") or block.get("label", "text")
            score = block.get("score", 1.0) or 1.0
            order = block.get("block_order") or block.get("order", i)
            polygon = (
                block.get("block_polygon_points")
                or block.get("polygon_points")
                or block.get("polygon")
            )

            if not text:
                continue

            # Normalize bbox
            if bbox is None:
                bbox = []
            elif isinstance(bbox, dict):
                bbox = [
                    bbox.get("x1", 0),
                    bbox.get("y1", 0),
                    bbox.get("x2", 0),
                    bbox.get("y2", 0),
                ]
            elif len(bbox) == 4 and isinstance(bbox[0], (list, tuple)):
                flat = [c for pt in bbox for c in pt]
                if flat:
                    bbox = [
                        min(flat[0::2]),
                        min(flat[1::2]),
                        max(flat[0::2]),
                        max(flat[1::2]),
                    ]

            blocks.append(
                LayoutBlock(
                    text=text,
                    bbox=list(bbox) if bbox else [],
                    label=label,
                    score=float(score) if isinstance(score, (int, float)) else 1.0,
                    order=order,
                    polygon=polygon,
                    text_lines=[],
                    raw=block,
                )
            )

        # Sort by reading order (top-to-bottom, then left-to-right)
        blocks.sort(
            key=lambda b: (
                min(b.bbox[1::2]) if len(b.bbox) >= 4 else 0,
                min(b.bbox[::2]) if len(b.bbox) >= 4 else 0,
            )
        )
        for i, b in enumerate(blocks):
            b.order = i

        # Fallback: parse from markdown if no blocks found
        if not blocks and markdown:
            lines = markdown.split("\n")
            for i, line in enumerate(lines):
                line = line.strip()
                if line and not line.startswith("#"):
                    if line.upper() == line and len(line) < 30:
                        label = "title_if_important"
                    blocks.append(
                        LayoutBlock(
                            text=line,
                            bbox=[0, i * 20, 100, (i + 1) * 20],
                            label="text",
                            score=0.0,
                            order=i,
                        )
                    )

        return {
            "layout_blocks": blocks,
            "markdown": markdown,
            "orientation": orientation,
        }


# --- Error types ---


class _PaddleOCRVLError(Exception):
    """Base error for PaddleOCR-VL service."""


class _APIError(_PaddleOCRVLError):
    def __init__(self, message: str, status_code: int = 500):
        super().__init__(message)
        self.status_code = status_code


class _RateLimitError(_PaddleOCRVLError):
    def __init__(self, message: str, retry_after: float = 5.0):
        super().__init__(message)
        self.retry_after = retry_after


class _TimeoutError(_PaddleOCRVLError):
    """Job polling timed out."""
