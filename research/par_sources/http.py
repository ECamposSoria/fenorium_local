from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

import requests


CACHE_MANIFEST = "_manifest.json"
# How far a host's pace may be slowed by its own refusals, and the longest wait a server may ask
# for and be believed. Both are ceilings on what a misbehaving limiter can cost a nightly run.
MAX_HOST_DELAY_SECONDS = 8.0
MAX_RETRY_AFTER_SECONDS = 60.0


def cache_fetched_at(path: Path) -> datetime:
    """Return the immutable fetch time recorded beside a mutable ``latest`` alias.

    Git does not preserve file modification times.  A clone would therefore make old evidence
    look freshly fetched if cache provenance relied on ``st_mtime`` alone.  New pulls and the
    checked-in snapshot both record an explicit timestamp in ``_manifest.json``; legacy caches
    retain the mtime fallback so this change does not invalidate local research archives.
    """
    manifest_path = path.parent / CACHE_MANIFEST
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entry = manifest.get("files", {}).get(path.name)
        value = entry.get("fetched_at") if isinstance(entry, dict) else entry
        fetched_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return fetched_at.replace(tzinfo=UTC) if fetched_at.tzinfo is None else fetched_at.astimezone(UTC)
    except (AttributeError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return datetime.fromtimestamp(path.stat().st_mtime, UTC)


def cache_age_minutes(path: Path, *, now: datetime | None = None) -> float:
    reference = now or datetime.now(UTC)
    return max(0.0, (reference - cache_fetched_at(path)).total_seconds() / 60.0)


def record_cache_manifest(
    path: Path,
    *,
    fetched_at: datetime,
    source: str,
    name: str,
    endpoint: str,
    outcome: str,
    remove: Path | None = None,
) -> None:
    """Atomically record provenance for a file under a ``latest`` cache directory."""
    manifest_path = path.parent / CACHE_MANIFEST
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = {"version": 1, "files": {}}
    if not isinstance(manifest, dict):
        manifest = {"version": 1, "files": {}}
    files = manifest.setdefault("files", {})
    if not isinstance(files, dict):
        files = {}
        manifest["files"] = files
    if remove is not None:
        files.pop(remove.name, None)
    files[path.name] = {
        "fetched_at": fetched_at.astimezone(UTC).isoformat(),
        "source": source,
        "name": name,
        "endpoint": endpoint,
        "outcome": outcome,
    }
    manifest["version"] = 1
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{manifest_path.name}.", suffix=".tmp", dir=manifest_path.parent,
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        # mkstemp creates 0600; the jobs sharing this cache run as different users and all read it.
        os.chmod(temporary, 0o644)
        temporary.replace(manifest_path)
    finally:
        temporary.unlink(missing_ok=True)


def replace_with_copy(source: Path, target: Path) -> None:
    """Copy ``source`` over ``target`` by renaming a fresh copy into place.

    Copying onto the existing file needs write permission on that file. The cache is shared by jobs
    that run as different users (the nightly snapshot as `par`, the refresh and trials as root), and
    an in-place copy refused every update to a file the other user had written last -- the nightly
    snapshot silently lost most of its rows that way. A rename needs only the directory.
    """
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


class SourceError(RuntimeError):
    """A source could not return or expose the required evidence."""


class CacheMissError(SourceError):
    """No usable cached response exists."""


@dataclass(frozen=True)
class PullRecord:
    source: str
    name: str
    endpoint: str
    fetched_at: str
    cache_path: str
    from_cache: bool
    fallback_reason: str | None = None


class HttpClient:
    def __init__(
        self,
        data_dir: Path,
        *,
        offline: bool = False,
        max_age_minutes: float | None = None,
        timeout_seconds: float = 20.0,
        delay_seconds: float = 0.25,
        retries: int = 3,
        logger: logging.Logger | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.latest_dir = data_dir / "latest"
        self.latest_dir.mkdir(parents=True, exist_ok=True)
        self.offline = offline
        self.max_age_minutes = max_age_minutes
        self.timeout_seconds = timeout_seconds
        self.delay_seconds = delay_seconds
        self.retries = retries
        self.log = logger or logging.getLogger("par")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "par-research/1.0", "Accept": "application/json"})
        self.records: list[PullRecord] = []
        # Rate limits belong to a host, so pacing belongs to a host too: one slow source should
        # not make every other source wait its turn, and a refusal from one should not slow the
        # rest of the run. Both are kept per host for that reason.
        self._host_last: dict[str, float] = {}
        self._host_delay: dict[str, float] = {}

    @staticmethod
    def utc_now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def _safe(value: str) -> str:
        return "".join(char if char.isalnum() or char in "-_" else "-" for char in value)

    def _latest_path(self, source: str, name: str) -> Path:
        return self.latest_dir / f"{self._safe(source)}_{self._safe(name)}.json"

    def _latest_error_path(self, source: str, name: str) -> Path:
        return self.latest_dir / f"{self._safe(source)}_{self._safe(name)}.error.json"

    def _record_latest_manifest(
        self,
        path: Path,
        *,
        source: str,
        name: str,
        endpoint: str,
        fetched_at: datetime,
        outcome: str,
        remove: Path | None = None,
    ) -> None:
        record_cache_manifest(
            path, source=source, name=name, endpoint=endpoint,
            fetched_at=fetched_at, outcome=outcome, remove=remove,
        )

    def _read_cache(self, source: str, name: str, endpoint: str, reason: str | None = None) -> Any:
        path = self._latest_path(source, name)
        if not path.exists():
            error_path = self._latest_error_path(source, name)
            if error_path.exists():
                try:
                    error_payload = json.loads(error_path.read_text(encoding="utf-8"))
                    archived_reason = error_payload.get("reason", "cached request failure")
                    fetched_at = str(error_payload.get("fetchedAt", cache_fetched_at(error_path).isoformat()))
                except (OSError, json.JSONDecodeError):
                    archived_reason = "cached request failure metadata is unreadable"
                    fetched_at = cache_fetched_at(error_path).isoformat()
                self.records.append(PullRecord(source, name, endpoint, fetched_at, str(error_path), True, archived_reason))
                raise CacheMissError(f"{endpoint}: last live attempt failed: {archived_reason}")
            raise CacheMissError(f"{endpoint}: no cached response at {path}")
        age_minutes = cache_age_minutes(path)
        if self.max_age_minutes is not None and age_minutes > self.max_age_minutes:
            raise CacheMissError(
                f"{endpoint}: cached response is {age_minutes:.1f} minutes old "
                f"(limit {self.max_age_minutes:g})"
            )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CacheMissError(f"{endpoint}: unreadable cache {path}: {exc}") from exc
        fetched_at = cache_fetched_at(path).isoformat()
        self.records.append(PullRecord(source, name, endpoint, fetched_at, str(path), True, reason))
        return payload

    @staticmethod
    def _host_of(url: str) -> str:
        return urlsplit(url).netloc.lower() or url

    def _pace(self, host: str) -> None:
        delay = self._host_delay.get(host, self.delay_seconds)
        remaining = delay - (time.monotonic() - self._host_last.get(host, float("-inf")))
        if remaining > 0:
            time.sleep(remaining)

    def _slow_down(self, host: str) -> None:
        """A limiter that refused at this rate will refuse at it again, so keep what it taught.

        The alternative is what the nightly collector was doing: pacing every host at four
        requests a second, being refused by the two that cannot take it, and writing the refusal
        into the cache as an error artifact -- a hundred of them a night, in exactly the evidence
        fields the credit gate then reports as missing.

        A caller that set no delay has opted out of pacing, and opting out is not overridden.
        """
        if self.delay_seconds <= 0:
            return
        current = self._host_delay.get(host, self.delay_seconds)
        self._host_delay[host] = min(MAX_HOST_DELAY_SECONDS, current * 2)

    @staticmethod
    def _retry_after_seconds(response: Any) -> float | None:
        """Seconds the server asked us to wait, where it said so in a form we can trust."""
        headers = getattr(response, "headers", None)
        raw = headers.get("Retry-After") if isinstance(headers, Mapping) else None
        try:
            seconds = float(str(raw).strip())
        except (TypeError, ValueError):
            return None
        return seconds if 0.0 <= seconds <= MAX_RETRY_AFTER_SECONDS else None

    def _archive(self, source: str, name: str, payload: Any, endpoint: str) -> Any:
        now = self.utc_now()
        stamp = now.strftime("%Y-%m-%dT%H-%M-%S.%fZ")
        archive = self.data_dir / f"{self._safe(source)}_{self._safe(name)}_{stamp}.json"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        latest = self._latest_path(source, name)
        replace_with_copy(archive, latest)
        error_path = self._latest_error_path(source, name)
        error_path.unlink(missing_ok=True)
        self._record_latest_manifest(
            latest, source=source, name=name, endpoint=endpoint,
            fetched_at=now, outcome="success", remove=error_path,
        )
        self.records.append(PullRecord(source, name, endpoint, now.isoformat(), str(archive), False))
        return payload

    def _archive_failure(self, source: str, name: str, endpoint: str, reason: str) -> None:
        now = self.utc_now()
        stamp = now.strftime("%Y-%m-%dT%H-%M-%S.%fZ")
        payload = {"endpoint": endpoint, "reason": reason, "fetchedAt": now.isoformat()}
        archive = self.data_dir / f"{self._safe(source)}_{self._safe(name)}_error_{stamp}.json"
        archive.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        error_path = self._latest_error_path(source, name)
        replace_with_copy(archive, error_path)
        self._record_latest_manifest(
            error_path, source=source, name=name, endpoint=endpoint,
            fetched_at=now, outcome="error",
        )

    def request_json(
        self,
        source: str,
        name: str,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Mapping[str, Any] | None = None,
        expected_statuses: tuple[int, ...] = (200,),
        headers: Mapping[str, str] | None = None,
        validate: Callable[[Any], bool] | None = None,
        retry_on_429: bool = True,
        prefer_cache_minutes: float | None = None,
        prefer_cached_errors: bool = True,
    ) -> Any:
        """`validate` rejects a 2xx body that parsed but carries no usable evidence.

        Sources degrade by returning HTTP 200 with an empty payload rather than an error —
        DefiLlama does this for sparse tokens. Without this hook such a response is archived as
        good and overwrites a working cache, which is exactly the failure the cache exists to
        prevent. A rejected body falls back to cache like any other failure.
        """
        endpoint = f"{method.upper()} {url}"
        if self.offline:
            return self._read_cache(source, name, endpoint)
        if prefer_cache_minutes is not None:
            # Preferring the cache is an optimisation, never a new way to fail. A caller with a
            # stricter freshness limit than this window must still get its live request rather
            # than the refusal `_read_cache` raises for evidence it considers too old.
            path = self._latest_path(source, name)
            if path.exists() and cache_age_minutes(path) <= prefer_cache_minutes:
                try:
                    return self._read_cache(source, name, endpoint)
                except CacheMissError:
                    pass
            # A cached *failure* is not evidence we hold, and replaying one to save a request
            # turns a single refusal into hours of them. It also silently ended the peg-history
            # sweep: the widths share one cache identity, so the first width's failure was
            # replayed for the other two and the wider searches were never made.
            error_path = self._latest_error_path(source, name)
            if prefer_cached_errors and error_path.exists() \
                    and cache_age_minutes(error_path) <= prefer_cache_minutes:
                # The refusal this raises is the point of the branch, so it is not swallowed the
                # way the success read above is: it carries why the last live attempt failed.
                return self._read_cache(source, name, endpoint)

        error: str | None = None
        host = self._host_of(url)
        asked_to_wait: float | None = None
        for attempt in range(self.retries + 1):
            self._pace(host)
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=dict(headers) if headers else None,
                    timeout=self.timeout_seconds,
                )
                self._host_last[host] = time.monotonic()
            except requests.RequestException as exc:
                error = f"network error: {exc}"
            else:
                if response.status_code in expected_statuses:
                    try:
                        payload = response.json()
                    except requests.JSONDecodeError as exc:
                        error = f"HTTP {response.status_code} returned non-JSON: {exc}"
                        break
                    if validate is not None and not validate(payload):
                        error = f"HTTP {response.status_code} carried no usable evidence"
                        break
                    return self._archive(source, name, payload, endpoint)
                body = response.text[:300].replace("\n", " ")
                error = f"HTTP {response.status_code}: {body}"
                if response.status_code == 429:
                    # The refusal is information about this host whether or not we try again.
                    self._slow_down(host)
                    asked_to_wait = self._retry_after_seconds(response)
                if response.status_code == 429 and not retry_on_429:
                    break
                if response.status_code != 429 and response.status_code < 500:
                    break
            if attempt < self.retries:
                wait = min(8.0, 0.75 * (2**attempt))
                # A limiter that names its own cooldown is believed over our guess. Guessing
                # shorter is how a retry becomes another refusal, three times over.
                if asked_to_wait is not None:
                    wait = max(wait, asked_to_wait)
                self.log.warning("%s failed (%s); retrying in %.2fs", endpoint, error, wait)
                time.sleep(wait)

        reason = error or "unknown request failure"
        self._archive_failure(source, name, endpoint, reason)
        try:
            self.log.warning("%s; using cache", reason)
            return self._read_cache(source, name, endpoint, reason)
        except CacheMissError as cache_exc:
            raise SourceError(f"{endpoint}: {reason}; {cache_exc}") from cache_exc


def require_mapping(payload: Any, endpoint: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SourceError(f"{endpoint}: expected JSON object, got {type(payload).__name__}")
    return payload


def require_list(payload: Any, endpoint: str) -> list[Any]:
    if not isinstance(payload, list):
        raise SourceError(f"{endpoint}: expected JSON list, got {type(payload).__name__}")
    return payload


def require_key(mapping: Mapping[str, Any], key: str, endpoint: str) -> Any:
    if key not in mapping:
        raise SourceError(f"{endpoint}: response lost required key `{key}`")
    return mapping[key]
