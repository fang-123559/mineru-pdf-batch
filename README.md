# MinerU PDF Batch Skill

A project-scoped agent skill and Python CLI for splitting local PDFs, converting them through MinerU's batch API, and keeping verified result archives. The original PDFs remain unchanged.

## Use it

Place the skill folder at `.agents/skills/mineru-pdf-batch/` in your project. In Codex, invoke `$mineru-pdf-batch`. The CLI also works on its own with Python 3.11+.

From the skill folder, install the dependency into your chosen Python environment:

```sh
python -m pip install -r requirements.txt
```

Plan and prepare locally, with a job directory outside the skill checkout:

```sh
python -B scripts/mineru_pdf_batch.py plan --input document.pdf
python -B scripts/mineru_pdf_batch.py prepare --input document.pdf --job-root ../mineru-job
```

Set `MINERU_API_TOKEN` securely in your local process environment. When the PDFs may be sent to MinerU:

```sh
python -B scripts/mineru_pdf_batch.py run --job-root ../mineru-job --submit
python -B scripts/mineru_pdf_batch.py poll --job-root ../mineru-job
python -B scripts/mineru_pdf_batch.py validate --job-root ../mineru-job
```

See the [workflow reference](references/workflow.md) for quota accounting, recovery, and result formats. Keep job folders private: their manifests contain original paths and remote batch identifiers. The repository contains no real documents, job results, or credentials.

## Test

```sh
python -B -m unittest discover -s tests -v
```

The tests use synthetic PDFs and fake service responses. They do not upload documents or use a real token.

Licensed under [MIT](LICENSE).
