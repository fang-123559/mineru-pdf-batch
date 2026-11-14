---
name: mineru-pdf-batch
description: Split local PDFs by page count and file size, convert them with the MinerU cloud API, resume batches, and verify complete result archives. Use for long PDFs or scanned books to convert into Markdown with JSON and images.
---

# MinerU PDF Batch

Use [scripts/mineru_pdf_batch.py](scripts/mineru_pdf_batch.py) with Python 3.11+ and the dependencies in [requirements.txt](requirements.txt). See [workflow](references/workflow.md) for commands, state recovery, and validation limits.

1. Confirm the PDF inputs, job directory, and scope of cloud upload. Use `plan` to preview splits without writing files or contacting MinerU.
2. Run `prepare` to write split PDFs and a manifest inside the job directory. Keep the source PDFs unchanged.
3. After the upload is authorized, run `run --submit`. Reuse the same job directory on later runs. Use `poll` to retrieve existing results without submitting new batches.
4. Before reporting completion, run `validate`. Keep the complete ZIP, extracted Markdown, JSON, and images. Report archive validation separately from OCR accuracy.

Read credentials only from the local process environment variable `MINERU_API_TOKEN`. Never copy tokens, signed URLs, job state, inputs, or results into the skill repository. If an upload or allocation was not confirmed, check the existing job and avoid blindly creating another batch. Reuse the existing local budget across resumes; coordinate separate jobs that share the same account.
