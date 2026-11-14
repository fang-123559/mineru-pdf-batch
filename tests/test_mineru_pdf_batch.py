import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from pypdf import PdfReader, PdfWriter

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("mineru_pdf_batch",
                                             ROOT / "scripts" / "mineru_pdf_batch.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def write_pdf(path, pages):
    writer = PdfWriter()
    for index in range(pages):
        writer.add_blank_page(500 + index, 800)
    with path.open("xb") as file:
        writer.write(file)


def make_zip(pages, broken_image=False, extra=None, schema="legacy"):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("full.md", "# Example\n\n![diagram](images/example.svg)\n")
        if not broken_image:
            archive.writestr("images/example.svg", "<svg/>")
        rows = [{"page_idx": i} for i in range(pages)]
        layout = {"pdf_info": rows} if schema == "legacy" else {
            "schema": schema, "pages": rows}
        archive.writestr("layout.json", json.dumps(layout))
        archive.writestr("content_list.json", "[]")
        if extra:
            archive.writestr(extra, "synthetic")
    return out.getvalue()


class FakeService:
    def __init__(self):
        self.allocation_count = 0
        self.queries = []
        self.uploads = []
        self.downloads = []
        self.answer = {"extract_result": []}
        self.allocate_error = None
        self.half_upload = False
        self.download_error = False
        self.package = make_zip(2)

    def allocate(self, payload):
        self.allocation_count += 1
        self.last_payload = payload
        if self.allocate_error:
            raise self.allocate_error
        return {"batch_id": f"batch-{self.allocation_count}",
                "file_urls": ["https://upload.example.invalid/object"] * len(payload["files"])}

    def put(self, url, path):
        self.uploads.append(path.name)
        if self.half_upload:
            raise TimeoutError("synthetic timeout")

    def query(self, batch):
        self.queries.append(batch)
        return self.answer

    def download(self, url, path):
        self.downloads.append(url)
        if self.download_error:
            path.write_bytes(b"partial")
            raise TimeoutError("synthetic timeout")
        path.write_bytes(self.package)


class TestJob(unittest.TestCase):
    def setUp(self):
        workspace = m.safe_path(ROOT, ".test-work")
        workspace.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=workspace)
        self.root = Path(self.temp.name).resolve()
        self.assertTrue(self.root.is_relative_to(workspace.resolve()))
        self.addCleanup(self.temp.cleanup)
        self.pdf = self.root / "sample.pdf"
        self.work = self.root / "job"
        (self.work / "inputs").mkdir(parents=True)
        self.client = FakeService()

    def job(self, pages=2, limit=200, budget=1000, path=None):
        write_pdf(self.pdf, pages)
        state = m.prepare([self.pdf] if path is None else [self.pdf, path],
                          limit, 190_000_000, budget, self.work)
        job = m.Job(self.work, state, self.client)
        job.save()
        return job

    def reopen(self):
        return m.Job(self.work, json.loads((self.work / "manifest.json").read_text("utf-8")),
                     self.client)

    def finish(self, job, *, fallback_name=False):
        part = job.parts()[0]
        key = ({"file_name": job.input(part).name} if fallback_name
               else {"data_id": part["attempts"][-1]["id"]})
        self.client.answer = {"extract_result": [
            {**key, "state": "done", "full_zip_url": "https://download.example.invalid/result"}]}

    def test_plan_splits_405_pages_and_preserves_source_order(self):
        write_pdf(self.pdf, 405)
        before = m.hash_file(self.pdf)
        ranges = []
        widths = []
        for start, end, data in m.pdf_parts(self.pdf, 200, 190_000_000):
            ranges.append((start, end))
            widths.extend(int(page.mediabox.width) for page in PdfReader(io.BytesIO(data)).pages)
        self.assertEqual(ranges, [(1, 200), (201, 400), (401, 405)])
        self.assertEqual(widths, list(range(500, 905)))
        self.assertEqual(m.hash_file(self.pdf), before)

    def test_byte_limit_bisects_and_rejects_an_oversized_single_page(self):
        write_pdf(self.pdf, 8)
        cap = max(len(data) for _, _, data in m.pdf_parts(self.pdf, 1, 1_000_000)) + 100
        parts = list(m.pdf_parts(self.pdf, 200, cap))
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(data) <= cap for _, _, data in parts))
        self.assertEqual([i for start, end, _ in parts for i in range(start, end + 1)],
                         list(range(1, 9)))
        with self.assertRaisesRegex(m.Problem, "alone exceeds"):
            list(m.pdf_parts(self.pdf, 200, 1))

    def test_source_and_split_hashes_are_enforced(self):
        job = self.job()
        path = job.input(job.parts()[0])
        path.write_bytes(path.read_bytes() + b"\n")
        with self.assertRaisesRegex(m.Problem, "split PDF changed"):
            job.check_inputs()
        with self.assertRaises(m.Problem):
            job.submit(3, "vlm", "ch", True)
        self.assertEqual(self.client.allocation_count, 0)

    def test_budget_does_not_submit_a_part_that_cannot_fit(self):
        job = self.job(pages=201, limit=100, budget=100)
        self.assertTrue(job.submit(3, "vlm", "ch", True))
        self.assertEqual(self.client.uploads, ["s001_p00001-00100.pdf"])
        self.assertEqual(job.used(m.today()), 100)
        self.client.package = make_zip(100)
        self.finish(job)
        job.poll()
        self.assertFalse(job.submit(3, "vlm", "ch", True))
        self.assertEqual(self.client.allocation_count, 1)

    def test_lost_post_response_blocks_resubmission(self):
        job = self.job()
        self.client.allocate_error = m.Problem("synthetic unknown allocation")
        with self.assertRaises(m.Problem):
            job.submit(3, "vlm", "ch", True)
        job = self.reopen()
        self.assertEqual(job.parts()[0]["state"], "allocation_uncertain")
        self.assertFalse(job.submit(3, "vlm", "ch", True))
        self.assertEqual(job.used(m.today()), 0)
        self.assertEqual(self.client.allocation_count, 1)

    def test_uncertain_put_charged_and_resumes_existing_batch(self):
        job = self.job()
        self.client.half_upload = True
        with self.assertRaises(m.Problem):
            job.submit(3, "vlm", "ch", True)
        job = self.reopen()
        self.assertEqual(job.parts()[0]["state"], "upload_uncertain")
        self.assertEqual(job.used(m.today()), 2)
        self.assertFalse(job.submit(3, "vlm", "ch", True))
        self.finish(job, fallback_name=True)
        job.poll()
        self.assertEqual(self.client.queries, ["batch-1"])
        self.assertTrue(job.validate())
        self.assertEqual(self.client.allocation_count, 1)

    def test_retry_keeps_history_and_is_charged_for_new_attempt(self):
        job = self.job()
        for _ in range(2):
            self.assertTrue(job.submit(3, "vlm", "ch", True))
            attempt = job.parts()[0]["attempts"][-1]
            self.client.answer = {"extract_result": [
                {"data_id": attempt["id"], "state": "failed", "err_code": -60007}]}
            job.poll()
        self.assertFalse(job.submit(3, "vlm", "ch", True))
        self.assertEqual([a["batch"] for a in job.parts()[0]["attempts"]], ["batch-1", "batch-2"])
        self.assertEqual(job.used(m.today()), 4)

    def test_quota_rejection_stops_new_work_for_today(self):
        job = self.job()
        self.client.allocate_error = m.ServiceError(-60018)
        with self.assertRaises(m.ServiceError):
            job.submit(3, "vlm", "ch", True)
        job = self.reopen()
        self.assertEqual(job.state["quota_stop"], m.today())
        self.assertEqual(job.used(m.today()), 0)
        self.assertFalse(job.submit(3, "vlm", "ch", True))

    def test_download_resume_does_not_submit_again(self):
        job = self.job()
        job.submit(3, "vlm", "ch", True)
        self.finish(job)
        self.client.download_error = True
        with self.assertRaises(TimeoutError):
            job.poll()
        job = self.reopen()
        self.assertEqual(job.parts()[0]["state"], "download_pending")
        self.client.download_error = False
        job.poll()
        self.assertTrue(job.validate())
        self.assertEqual(self.client.allocation_count, 1)
        self.assertEqual(len(self.client.downloads), 2)
        self.assertFalse(job.submit(3, "vlm", "ch", True))

    def test_archive_rejects_traversal_invalid_crc_missing_assets_and_schema(self):
        job = self.job()
        part = job.parts()[0]
        crc_bad = make_zip(2).replace(b"<svg/>", b"<bad/>")
        for package in (make_zip(2, extra="../../escape.txt"), make_zip(2, extra="CON.txt"),
                        crc_bad, make_zip(2, broken_image=True), make_zip(2, schema="unknown"),
                        make_zip(1)):
            with self.subTest(package_length=len(package)):
                self.client.package = package
                with self.assertRaises((m.Problem, zipfile.BadZipFile)):
                    job.archive(part, "https://download.example.invalid/result")
                result = job.results(part)
                self.assertFalse((result / "extracted").exists())
                (result / "result.zip").unlink(missing_ok=True)
        self.assertFalse((self.work / "escape.txt").exists())

    def test_validate_detects_edited_extraction(self):
        job = self.job()
        job.submit(3, "vlm", "ch", True)
        self.finish(job)
        job.poll()
        edited = job.results(job.parts()[0]) / "extracted" / "full.md"
        edited.write_text("# Edited", encoding="utf-8")
        with self.assertRaises(m.Problem):
            job.validate()

    def test_plan_without_credentials_is_read_only(self):
        write_pdf(self.pdf, 2)
        before = set(self.root.rglob("*"))
        with mock.patch.dict(m.os.environ, {}, clear=True), mock.patch.object(m.sys, "argv", [
            "mineru_pdf_batch.py", "plan", "--input", str(self.pdf)
        ]), mock.patch.object(m, "MinerU", side_effect=AssertionError("network client created")):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(m.main(), 0)
        self.assertNotIn(str(self.pdf), out.getvalue())
        self.assertEqual(set(self.root.rglob("*")), before)

    def test_lock_prevents_simultaneous_resume(self):
        with m.job_lock(self.work):
            with self.assertRaises(m.Problem), m.job_lock(self.work):
                self.fail("lock was unexpectedly acquired")
        with m.job_lock(self.work):
            pass

    def test_service_code_diagnostics_never_echo_untrusted_text(self):
        marker = "SYNTHETIC-PRIVATE-VALUE"
        self.assertNotIn(marker, str(m.ServiceError(marker)))
        self.assertEqual(m.ServiceError(-60018).code, "-60018")
        with self.assertRaises(m.Problem):
            m.signed_target("https://demo@localhost/result")

    def test_bearer_is_sent_only_to_api_not_storage(self):
        marker = "synthetic-auth-marker"
        reply = io.BytesIO(b'{"code": 0, "data": {"batch_id": "batch-1"}}')
        with mock.patch.dict(m.os.environ, {"MINERU_API_TOKEN": marker}):
            client = m.MinerU()
        with mock.patch.object(client.opener, "open", return_value=reply) as send:
            self.assertEqual(client.allocate({"files": []})["batch_id"], "batch-1")
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, "https://mineru.net/api/v4/file-urls/batch")
        self.assertEqual(request.get_header("Authorization"), "Bearer " + marker)
        with mock.patch.object(client.opener, "open", return_value=io.BytesIO(b"result")) as send:
            client.download("https://download.example.invalid/object?synthetic=1",
                            self.root / "result.zip")
        self.assertIsInstance(send.call_args.args[0], str)
        self.assertNotIn(marker, repr(send.call_args))
        job = self.job()
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        with mock.patch.object(m.http.client, "HTTPSConnection") as connect:
            connect.return_value.getresponse.return_value = response
            client.put("https://upload.example.invalid/object?synthetic=1",
                       job.input(job.parts()[0]))
        sent = connect.return_value.request.call_args
        self.assertEqual(sent.args[:2], ("PUT", "/object?synthetic=1"))
        self.assertEqual(set(sent.kwargs["headers"]), {"Content-Length"})


if __name__ == "__main__":
    unittest.main()
