"""Split local PDFs, submit approved batches to MinerU, and verify complete archives."""
import argparse
import contextlib
import hashlib
import html
import http.client
import io
import json
import logging
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pypdf import PdfReader, PdfWriter

API = "https://mineru.net/api/v4"
SHANGHAI = timezone(timedelta(hours=8))
MAX_BYTES = 200_000_000
ZIP_LIMIT = 2 * 1024**3
ACTIVE = {"allocated", "uploading", "upload_uncertain", "uploaded", "download_pending"}
REMOTE_WAIT = {"waiting-file", "pending", "running", "converting"}
AUTH = {"401", "403", "A0202", "A0211"}
QUOTA = {"-60018", "-60019"}


class Problem(Exception):
    """A diagnostic safe to print; never construct it with a response or URL."""


class ServiceError(Problem):
    def __init__(self, code):
        value = str(code)
        self.code = value if re.fullmatch(r"-?\d{1,6}|A\d{4}", value) else "unknown"
        super().__init__(f"MinerU request failed ({self.code})")


def now():
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def today():
    return now()[:10]


def hash_file(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def safe_path(root, *pieces):
    """Resolve generated paths without allowing existing links to escape the job."""
    path = root.joinpath(*pieces)
    if not path.resolve().is_relative_to(root.resolve()):
        raise Problem("Path escapes the job directory")
    return path


def atomic_json(path, value):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".manifest-", delete=False) as file:
        temp = Path(file.name)
        try:
            json.dump(value, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        except BaseException:
            file.close()
            temp.unlink(missing_ok=True)
            raise
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


@contextlib.contextmanager
def job_lock(root):
    """An OS lock is released when the process exits, including after a crash."""
    with safe_path(root, ".lock").open("a+b") as file:
        if os.name == "nt":
            import msvcrt
            if not file.seek(0, os.SEEK_END):
                file.write(b"\0")
                file.flush()
            file.seek(0)
            try:
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise Problem("Another process owns this job") from None
        else:
            import fcntl
            try:
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Problem("Another process owns this job") from None
        yield


def pdf_parts(path, page_limit, byte_limit):
    with path.open("rb") as file:
        reader = PdfReader(file)
        if reader.is_encrypted and not reader.decrypt(""):
            raise Problem("PDF requires a password")
        count = len(reader.pages)
        if not count:
            raise Problem("PDF has no pages")

        def encode(lo, hi):
            writer = PdfWriter()
            for n in range(lo, hi):
                writer.add_page(reader.pages[n])
            out = io.BytesIO()
            writer.write(out)
            data = out.getvalue()
            if len(data) > byte_limit:
                if hi - lo == 1:
                    raise Problem(f"Source page {lo + 1} alone exceeds the byte limit")
                mid = (lo + hi) // 2
                del data, out, writer
                yield from encode(lo, mid)
                yield from encode(mid, hi)
            else:
                if len(PdfReader(io.BytesIO(data)).pages) != hi - lo:
                    raise Problem("Split PDF page count did not verify")
                yield lo + 1, hi, data

        for lo in range(0, count, page_limit):
            yield from encode(lo, min(lo + page_limit, count))


def prepare(inputs, page_limit, byte_limit, budget, dest=None):
    state = {"version": 1, "job": uuid.uuid4().hex, "budget": budget,
             "sources": [], "parts": [], "quota_stop": None}
    seen = set()
    for path in inputs:
        path = path.resolve(strict=True)
        if not path.is_file() or path.suffix.lower() != ".pdf":
            raise Problem("Each input must be a local PDF")
        digest = hash_file(path)
        if digest in seen:
            continue
        seen.add(digest)
        source = {"id": f"s{len(seen):03}", "sha256": digest, "pages": 0}
        if dest is not None:
            source["path"] = str(path)
        state["sources"].append(source)
        for start, end, data in pdf_parts(path, page_limit, byte_limit):
            ident = f"{source['id']}_p{start:05}-{end:05}"
            part = {"id": ident, "source": source["id"], "start": start, "end": end,
                    "pages": end - start + 1, "bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "state": "prepared", "attempts": []}
            if dest is not None:
                name = f"{ident}.pdf"
                final = safe_path(dest, "inputs", name)
                temp = safe_path(dest, "inputs", f".{name}.{uuid.uuid4().hex}.partial")
                try:
                    with temp.open("xb") as out:
                        out.write(data)
                        out.flush()
                        os.fsync(out.fileno())
                    if final.exists():
                        if hash_file(final) != part["sha256"]:
                            raise Problem("Existing split differs; use another job directory")
                    else:
                        os.replace(temp, final)
                finally:
                    temp.unlink(missing_ok=True)
            source["pages"] = end
            state["parts"].append(part)
        if hash_file(path) != digest:
            raise Problem("Source PDF changed during preparation")
    return state


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def signed_target(url):
    if not isinstance(url, str):
        raise Problem("Service returned an invalid HTTPS address")
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.fragment or parsed.port not in (None, 443)):
        raise Problem("Service returned an invalid HTTPS address")
    return parsed


class MinerU:
    def __init__(self):
        self.token = os.environ.get("MINERU_API_TOKEN", "").strip()
        if not self.token:
            raise Problem("Set MINERU_API_TOKEN in the current process environment")
        self.opener = urllib.request.build_opener(NoRedirect())

    def api(self, endpoint, payload=None):
        request = urllib.request.Request(
            API + endpoint,
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
        )
        try:
            with self.opener.open(request, timeout=45) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 429):
                raise ServiceError(exc.code) from None
            try:
                code = json.load(exc).get("code", exc.code)
            except (ValueError, AttributeError, OSError):
                code = exc.code
            raise ServiceError(code) from None
        except (OSError, ValueError, http.client.HTTPException):
            raise Problem("API request outcome is unknown; see the local job status") from None
        if not isinstance(result, dict) or result.get("code") != 0:
            raise ServiceError(result.get("code") if isinstance(result, dict) else None)
        if not isinstance(result.get("data"), dict):
            raise Problem("Unrecognized API result")
        return result["data"]

    def allocate(self, payload):
        return self.api("/file-urls/batch", payload)

    def query(self, batch):
        if not isinstance(batch, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", batch):
            raise Problem("Invalid batch identifier")
        return self.api("/extract-results/batch/" + batch)

    def put(self, url, path):
        endpoint = signed_target(url)
        route = endpoint.path or "/"
        if endpoint.query:
            route += "?" + endpoint.query
        conn = http.client.HTTPSConnection(endpoint.hostname, timeout=120)
        try:
            with path.open("rb") as data:
                conn.request("PUT", route, body=data,
                             headers={"Content-Length": str(os.fstat(data.fileno()).st_size)})
                with conn.getresponse() as response:
                    if response.status not in (200, 201, 204):
                        raise Problem(f"Upload was not confirmed (HTTP {response.status})")
        finally:
            conn.close()

    def download(self, url, dest):
        signed_target(url)
        with self.opener.open(url, timeout=120) as source, dest.open("xb") as out:
            size = 0
            while data := source.read(1024 * 1024):
                size += len(data)
                if size > ZIP_LIMIT:
                    raise Problem("Result ZIP is larger than the download limit")
                out.write(data)


def zip_members(archive):
    entries = archive.infolist()
    if len(entries) > 100_000 or sum(item.file_size for item in entries) > ZIP_LIMIT:
        raise Problem("Result ZIP exceeds the extraction limit")
    seen = set()
    for entry in entries:
        parts = entry.filename.replace("\\", "/").removesuffix("/").split("/")
        if (any(not p or p in {".", ".."} or p.endswith((" ", ".")) or
                re.search(r'[:<>"|?*\x00-\x1f]', p) or
                re.fullmatch(r"(?i:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[0-9¹²³])",
                             p.split(".")[0].rstrip(" ")) for p in parts)
                or stat.S_ISLNK(entry.external_attr >> 16)):
            raise Problem("Result ZIP contains an unsafe member")
        key = "/".join(parts).casefold()
        if key in seen:
            raise Problem("Result ZIP contains duplicate member paths")
        seen.add(key)
        yield entry, parts


def qa_tree(tree, page_count):
    markdown = [path for path in tree.rglob("*.md") if path.name in ("full.md", "markdown.md")]
    if len(markdown) != 1:
        raise Problem("Expected exactly one result Markdown")
    md = markdown[0]
    text = md.read_text(encoding="utf-8-sig")
    if not text.strip():
        raise Problem("Result Markdown is empty")
    references = re.findall(r"!\[[^\]]*\]\(\s*(<[^>]+>|[^\s)]+)", text)
    references += re.findall(r"""<img\b[^>]*\bsrc\s*=\s*["']([^"']+)""", text, re.I)
    for ref in references:
        url = urllib.parse.urlsplit(html.unescape(ref.strip("<>")))
        if url.scheme in ("http", "https", "data") and not re.match(r"^[A-Za-z]:", ref):
            continue
        if url.scheme or url.netloc:
            raise Problem("Unsupported image reference")
        target = (md.parent / urllib.parse.unquote(url.path)).resolve()
        if not target.is_relative_to(tree.resolve()) or not target.is_file():
            raise Problem("A Markdown image reference is missing or escapes the archive")
    supported = []
    for path in tree.rglob("*.json"):
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            continue
        if path.name == "layout.json" or path.name.endswith("_middle.json"):
            if isinstance(value.get("pdf_info"), list):
                supported.append(value["pdf_info"])
        if value.get("schema") == "docvortex.middle" and isinstance(value.get("pages"), list):
            supported.append(value["pages"])
    if not supported or any(
        len(pages) != page_count or
        [x.get("page_idx") if isinstance(x, dict) else None for x in pages] != list(range(page_count))
        for pages in supported
    ):
        raise Problem("Unsupported layout schema or unexpected result page indices")
    return {"markdown": md.relative_to(tree).as_posix(), "pages": page_count,
            "image_references": len(references)}


class Job:
    def __init__(self, root, state, client=None):
        self.root = root
        self.state = state
        self.client = client

    def save(self):
        atomic_json(safe_path(self.root, "manifest.json"), self.state)

    def parts(self):
        return self.state["parts"]

    def input(self, part):
        return safe_path(self.root, "inputs", part["id"] + ".pdf")

    def results(self, part):
        return safe_path(self.root, "results", part["id"])

    def used(self, date):
        return sum(p["pages"] for p in self.parts()
                   for attempt in p["attempts"] if attempt.get("charged_on") == date)

    def check_inputs(self):
        for src in self.state["sources"]:
            if hash_file(Path(src["path"])) != src["sha256"]:
                raise Problem("A source PDF changed")
            cursor = 1
            for part in sorted((p for p in self.parts() if p["source"] == src["id"]),
                               key=lambda p: p["start"]):
                if part["start"] != cursor or part["pages"] != part["end"] - cursor + 1:
                    raise Problem("Split ranges are not continuous")
                path = self.input(part)
                if hash_file(path) != part["sha256"] or len(PdfReader(path).pages) != part["pages"]:
                    raise Problem("A split PDF changed")
                cursor = part["end"] + 1
            if cursor - 1 != src["pages"]:
                raise Problem("Split ranges do not cover the source")

    def quota_error(self, error):
        if error.code in QUOTA:
            self.state["quota_stop"] = today()
            self.save()

    def submit(self, batch_size, model, language, ocr):
        date = today()
        if self.state["quota_stop"] == date or any(
            p["state"] in ACTIVE | {"allocation_uncertain"} for p in self.parts()
        ):
            return False
        available = self.state["budget"] - self.used(date)
        retries = [p for p in self.parts() if p["state"] == "failed"
                   and len(p["attempts"]) == 1 and p["attempts"][-1].get("retryable")]
        chosen = []
        settings = {"model_version": model, "language": language, "is_ocr": ocr}
        for part in retries + [p for p in self.parts() if p["state"] == "prepared"]:
            if part["pages"] > available:
                continue
            if hash_file(self.input(part)) != part["sha256"]:
                raise Problem("A split PDF changed before allocation")
            if part["attempts"] and part["attempts"][-1]["settings"] != settings:
                raise Problem("Use the original model, language and OCR settings when retrying")
            chosen.append(part)
            available -= part["pages"]
            if len(chosen) == batch_size:
                break
        if not chosen:
            return False
        files = []
        previous = [part["state"] for part in chosen]
        for part in chosen:
            attempt = {"id": f"{self.state['job']}_{part['id']}_{len(part['attempts']) + 1}",
                       "batch": None, "charged_on": None, "created": now(), "settings": settings}
            part["attempts"].append(attempt)
            part["state"] = "allocation_uncertain"
            files.append({"name": self.input(part).name, "data_id": attempt["id"], "is_ocr": ocr})
        self.save()
        try:
            answer = self.client.allocate({"files": files, "model_version": model,
                                           "language": language, "enable_table": True,
                                           "enable_formula": True})
        except ServiceError as exc:
            # Only documented definitive rejections can undo the local allocation intent.
            if exc.code in AUTH | QUOTA | {"429", "-500", "-10002", "-60009"}:
                for part, original in zip(chosen, previous):
                    part["attempts"].pop()
                    part["state"] = original
                if exc.code in QUOTA:
                    self.state["quota_stop"] = today()
                self.save()
            raise
        batch, urls = answer.get("batch_id"), answer.get("file_urls")
        if not isinstance(batch, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", batch):
            raise Problem("Allocation response has no usable batch identifier")
        for part in chosen:
            part["attempts"][-1]["batch"] = batch
            part["state"] = "allocated"
        self.save()
        if not isinstance(urls, list) or len(urls) != len(chosen):
            raise Problem("Allocation URL count does not match the request")
        for url in urls:
            signed_target(url)
        for part, url in zip(chosen, urls):
            date = today()
            if self.state["quota_stop"] == date or self.used(date) + part["pages"] > self.state["budget"]:
                raise Problem("No upload budget remains for the allocated batch")
            if hash_file(self.input(part)) != part["sha256"]:
                raise Problem("A split PDF changed before upload")
            attempt = part["attempts"][-1]
            attempt["charged_on"] = date
            part["state"] = "uploading"
            self.save()
            try:
                self.client.put(url, self.input(part))
            except (Problem, OSError, ValueError, http.client.HTTPException):
                part["state"] = "upload_uncertain"
                self.save()
                raise Problem("Upload was not confirmed; poll the existing batch before any retry") from None
            part["state"] = "uploaded"
            self.save()
        return True

    def archive(self, part, url):
        result = self.results(part)
        result.mkdir(parents=True, exist_ok=True)
        package = safe_path(result, "result.zip")
        extracted = safe_path(result, "extracted")
        if not package.exists():
            partial = safe_path(result, f".{uuid.uuid4().hex}.partial")
            try:
                self.client.download(url, partial)
                with zipfile.ZipFile(partial) as archive:
                    list(zip_members(archive))
                    if archive.testzip() is not None:
                        raise Problem("Result ZIP failed CRC validation")
                os.replace(partial, package)
            finally:
                partial.unlink(missing_ok=True)
        with zipfile.ZipFile(package) as archive:
            entries = list(zip_members(archive))
            if archive.testzip() is not None:
                raise Problem("Stored result ZIP failed CRC validation")
            if extracted.exists():
                # A crash may occur after moving the tree, but before saving the manifest.
                qa = qa_tree(extracted, part["pages"])
                if not archive_matches(extracted, archive, entries):
                    raise Problem("Existing extraction differs from its ZIP")
            else:
                with tempfile.TemporaryDirectory(prefix=".extract-", dir=result) as temp:
                    tree = Path(temp) / "tree"
                    tree.mkdir()
                    for entry, pieces in entries:
                        dest = safe_path(tree, *pieces)
                        if entry.is_dir():
                            dest.mkdir(parents=True, exist_ok=True)
                        else:
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            with archive.open(entry) as source, dest.open("xb") as out:
                                shutil.copyfileobj(source, out)
                    qa = qa_tree(tree, part["pages"])
                    os.replace(tree, extracted)
        return {**qa, "zip_sha256": hash_file(package)}

    def poll(self):
        groups = {}
        for part in self.parts():
            if part["state"] in ACTIVE:
                groups.setdefault(part["attempts"][-1]["batch"], []).append(part)
        for batch, parts in groups.items():
            try:
                data = self.client.query(batch)
            except ServiceError as exc:
                self.quota_error(exc)
                raise
            tasks = data.get("extract_result")
            if not isinstance(tasks, list) or not all(isinstance(t, dict) for t in tasks):
                raise Problem("Unrecognized batch result")
            for part in parts:
                attempt = part["attempts"][-1]
                matches = [t for t in tasks if t.get("data_id") == attempt["id"]]
                if not matches:
                    matches = [t for t in tasks if not t.get("data_id")
                               and t.get("file_name") == self.input(part).name]
                if len(matches) != 1:
                    continue
                task = matches[0]
                remote = task.get("state")
                attempt["remote"] = remote if isinstance(remote, str) and remote in (
                    REMOTE_WAIT | {"done", "failed"}) else "unknown"
                if remote == "done":
                    part["state"] = "download_pending"
                    self.save()
                    part["qa"] = self.archive(part, task.get("full_zip_url"))
                    part["state"] = "verified"
                elif remote == "failed":
                    error = ServiceError(task.get("err_code"))
                    self.quota_error(error)
                    attempt["error_code"] = error.code
                    attempt["retryable"] = bool(attempt["charged_on"]) and (
                        error.code == "-60007" or str(task.get("err_msg", "")).strip().lower()
                        == "parsing failed, please try again later"
                    ) and error.code not in QUOTA | AUTH
                    part["state"] = "failed"
                elif isinstance(remote, str) and remote in REMOTE_WAIT - {"waiting-file"}:
                    part["state"] = "uploaded"
                self.save()

    def status(self):
        parts = self.parts()
        counts = {}
        for part in parts:
            name = part["state"]
            counts[name] = counts.get(name, 0) + 1
        return {"date": today(), "budget": self.state["budget"], "charged_today": self.used(today()),
                "total_pages": sum(p["pages"] for p in parts),
                "verified_pages": sum(p["pages"] for p in parts if p["state"] == "verified"),
                "states": counts, "quota_stop": self.state["quota_stop"]}

    def validate(self):
        self.check_inputs()
        for part in self.parts():
            if part["state"] != "verified":
                continue
            result = self.results(part)
            package = safe_path(result, "result.zip")
            tree = safe_path(result, "extracted")
            if hash_file(package) != part["qa"]["zip_sha256"]:
                raise Problem("A verified ZIP changed")
            with zipfile.ZipFile(package) as archive:
                entries = list(zip_members(archive))
                if archive.testzip() is not None or not archive_matches(tree, archive, entries):
                    raise Problem("A verified archive or extraction changed")
            qa_tree(tree, part["pages"])
        return all(p["state"] == "verified" for p in self.parts())


def archive_matches(tree, archive, entries):
    expected = {"/".join(pieces) for entry, pieces in entries if not entry.is_dir()}
    if expected != {p.relative_to(tree).as_posix() for p in tree.rglob("*") if p.is_file()}:
        return False
    for entry, pieces in entries:
        if not entry.is_dir():
            with archive.open(entry) as packed, safe_path(tree, *pieces).open("rb") as saved:
                if hashlib.file_digest(packed, "sha256").digest() != hashlib.file_digest(saved, "sha256").digest():
                    return False
    return True


def arguments():
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="action", required=True)
    for name in ("plan", "prepare"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--input", nargs="+", required=True, type=Path)
        cmd.add_argument("--max-pages", type=int, default=200)
        cmd.add_argument("--max-bytes", type=int, default=190_000_000)
        cmd.add_argument("--daily-page-budget", type=int, default=1000)
        if name == "prepare":
            cmd.add_argument("--job-root", type=Path, required=True)
    for name in ("status", "validate", "poll", "run"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--job-root", type=Path, required=True)
        if name in ("poll", "run"):
            cmd.add_argument("--max-wait", type=int, default=1200,
                             help="Seconds between operations; in-flight requests can finish later")
        if name == "run":
            cmd.add_argument("--submit", required=True, action="store_true",
                             help="Explicitly allow uploads to MinerU")
            cmd.add_argument("--batch-size", type=int, choices=range(1, 16), default=3)
            cmd.add_argument("--model", choices=("vlm", "pipeline"), default="vlm")
            cmd.add_argument("--language", choices=("ch", "en"), default="ch")
            cmd.add_argument("--no-ocr", action="store_true")
    return cli.parse_args()


def main():
    args = arguments()
    if args.action in ("plan", "prepare") and not (
        1 <= args.max_pages <= 200 and 1 <= args.max_bytes <= MAX_BYTES
        and 1 <= args.daily_page_budget <= 1000
    ):
        raise Problem("Split or daily budget is outside the supported range")
    if args.action == "plan":
        state = prepare(args.input, min(args.max_pages, args.daily_page_budget),
                        args.max_bytes, args.daily_page_budget)
        print(json.dumps({"sources": state["sources"], "parts": state["parts"]}, indent=2))
        return 0
    root = args.job_root.resolve()
    if args.action == "prepare":
        root.mkdir(parents=True, exist_ok=True)
    if not root.is_dir():
        raise Problem("Job directory does not exist")
    with job_lock(root):
        manifest = safe_path(root, "manifest.json")
        if args.action == "prepare":
            if manifest.exists():
                raise Problem("This job already exists; use status, poll or run to resume")
            safe_path(root, "inputs").mkdir(exist_ok=True)
            state = prepare(args.input, min(args.max_pages, args.daily_page_budget),
                            args.max_bytes, args.daily_page_budget, root)
            job = Job(root, state)
            job.check_inputs()
            job.save()
        else:
            if not manifest.is_file():
                raise Problem("Missing job manifest; run prepare first")
            state = json.loads(manifest.read_text(encoding="utf-8"))
            if state.get("version") != 1:
                raise Problem("Unsupported manifest version")
            job = Job(root, state)
        if args.action == "validate":
            complete = job.validate()
            print(json.dumps({**job.status(), "complete": complete}))
            return 0 if complete else 2
        if args.action in ("poll", "run"):
            job.client = MinerU()
            if args.action == "run":
                job.check_inputs()
            deadline = time.monotonic() + max(args.max_wait, 0)
            while True:
                job.poll()
                active = any(p["state"] in ACTIVE for p in job.parts())
                if not active:
                    if args.action != "run" or not job.submit(
                        args.batch_size, args.model, args.language, not args.no_ocr
                    ):
                        break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(20, remaining))
                if time.monotonic() >= deadline:
                    break
        print(json.dumps(job.status()))
    return 0


if __name__ == "__main__":
    logging.getLogger("pypdf").disabled = True
    try:
        sys.exit(main())
    except Problem as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("Interrupted; resume with the existing job directory", file=sys.stderr)
        sys.exit(130)
    except Exception as exc:
        # File paths, server bodies and signed URLs can appear in exception text.
        print(f"Job stopped ({type(exc).__name__}); inspect local state", file=sys.stderr)
        sys.exit(2)
