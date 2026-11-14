# Conversion workflow

This CLI uses the [MinerU hosted v4 API](https://mineru.net/apiManage/docs), not the lightweight Agent endpoint or a self-hosted API. Consult the service documentation for current limits and supported models.

| Command | Network | Purpose |
|---|---|---|
| `plan --input <PDF...>` | No | Print exact page ranges, counts and sizes; does not write |
| `prepare --input <PDF...> --job-root <dir>` | No | Make split PDFs and a local manifest |
| `run --job-root <dir> --submit` | Yes | Poll existing batches, retrieve results, and submit eligible parts |
| `poll --job-root <dir>` | Yes | Poll and retrieve, with no new submissions |
| `status --job-root <dir>` | No | Print page counts and state |
| `validate --job-root <dir>` | No | Verify sources, split coverage and completed archives; exit 2 if incomplete |

`plan` and `prepare` accept `--max-pages` (default 200), `--max-bytes` (default 190,000,000), and `--daily-page-budget` (default 1000; 1–1000). Splits are also capped at the job budget. A split over the byte limit is bisected; an oversized single page stops preparation. Exact duplicate sources are deduplicated by SHA-256. File page ranges use a one-based index, which need not match printed pagination. Keep source files at their original locations throughout the job.

`run` uses three files per batch by default (`--batch-size`), `vlm` (`--model pipeline` is also supported), Chinese (`--language en` for English), and OCR (`--no-ocr` to disable). A retry uses the original model, language and OCR settings. `run` and `poll` support `--max-wait` in seconds; in-flight network operations can finish after the wait window. Use `--max-wait 0` for one pass without sleeping.

## Budget and resuming

The budget is local to one job, counted by upload start date in UTC+08:00 (Shanghai). Each PUT reserves its pages in the manifest before sending the file. Uncertain uploads remain charged; an eligible retry is a new charged attempt. The budget is not an account balance. Coordinate other jobs or clients using the same account; this CLI does not provide a shared account ledger. MinerU documents 1000 pages as a high-priority allowance, not a universal hard page cap. A server quota rejection pauses new submissions for the local day.

The tool stores the batch and part mapping before uploading and locks the job against simultaneous changes. It polls previously allocated work before creating more batches. It never automatically resubmits a verified part. Definite transient parsing failures can be retried once, with the original attempt preserved. There is no automatic resplitting after a failure.

- `allocation_uncertain`: the create request may have reached the service, but the local job has no usable batch ID. Stop and reconcile through the MinerU account; `data_id` is a correlation identifier, not a documented idempotency key.
- `allocated`, `uploading`, `upload_uncertain`: poll for the existing batch. A crash can leave unuploaded parts waiting for files. Signed URLs are not persisted and this tool does not reissue them or silently abandon a batch. If the service cannot resolve the batch, inspect the account and explicitly decide how to proceed. Do not reset the manifest or assume uncertain pages are free.
- `download_pending`: poll again after transient download failure. If an existing ZIP or extracted directory is damaged, diagnose it and move only the damaged output aside before polling again. Keep the manifest so the same cloud result is retrieved.
- `failed`: inspect the code in the private manifest. A terminal failure is not counted as verified.
- Missing/expired credentials: restore the process-local environment and resume the same job without pasting a token into a command or chat.

## Results

Each part has `results/<part-id>/result.zip` and `extracted/`. The tool checks CRC, rejects unsafe ZIP members, parses JSON, checks result Markdown and supported inline image references, and verifies the layout pages and their indices. It recognizes legacy `pdf_info` layouts and `docvortex.middle` page lists. An unfamiliar layout is left unverified. External image URLs are not fetched. `validate` checks that verified extracted files still match the ZIP.

A verified archive is not a promise of accurate OCR. Check source pages for missing body text, reading order, quantities and tables. A table omitted from the exported content list can still be present in the layout JSON. Keep any reviewed corrections separate from the original result.
