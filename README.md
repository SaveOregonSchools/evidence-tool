# evidence-tool

A local Flask web app for building an evidence inventory from many folders/files, detecting duplicate files by hash, exporting results to CSV, and using a local Ollama instance to categorize and briefly describe unique files.

This is intended to be run locally on the same machine that can access the evidence folders. It does not upload your evidence to any hosted service. Phase 2 sends extracted text and file metadata to whatever endpoint you configure in `OLLAMA_ENDPOINTS`, so keep that endpoint local or otherwise trusted.

## Features

### Phase 1: Inventory scan

The scanner accepts multiple absolute folder paths and/or individual file paths, one per line. Directories are scanned recursively. The inventory table and CSV export use this column order:

1. File name
2. Type of file
3. Folder location
4. Last modified date
5. Creation date
6. Size (MB)
7. Hash

The default hash is `blake2b` with a 256-bit digest because it is fast and modern. You can switch to `md5` or `sha256` in `.env`.

### Phase 2: Ollama categorization

After a scan, the app submits only one representative copy of each unique hash to Ollama. Duplicate files are not sent repeatedly.

The screen asks for:

- categories to use;
- free-text investigation/project context;
- model override, if you do not want to use `OLLAMA_MODEL` from `.env`;
- maximum extracted characters per file;
- optional maximum number of unique files to process in that run.

Phase 2 categorization jobs are persisted in SQLite and run through a single FIFO worker, so a second request waits until the prior categorization job finishes or is cancelled. The web page includes a **Categorization jobs** section with historical and current jobs, progress, error count, model, timestamps, and a cancel button. Cancelling a running job stops it after the current Ollama request returns.

The app now asks Ollama for compact JSON output and parses several common model formats, including JSON, partial JSON, simple `Category:` / `Description:` labels, and tag-like responses. The prompt tells the model to use your project context as background only and to describe the file's actual contents rather than repeating generic context for every file.

For common text-bearing formats, the app extracts local text before calling Ollama. Unsupported formats such as video/audio/images/archives are categorized from metadata only.

Supported text extraction:

- `.txt`, `.md`, `.log`, `.json`, `.xml`, `.html`, `.csv`, `.tsv`
- `.pdf`
- `.docx` with a zipped-XML fallback when normal DOCX extraction fails
- `.xlsx`, `.xlsm`
- `.pptx`
- `.eml`

Legacy binary Office files like `.doc`, `.xls`, and `.ppt` are inventoried but categorized from metadata only unless a future extractor is added.

## Setup

```bash
git clone <your-repo-url> evidence-tool
cd evidence-tool
python -m venv .venv
```

On Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

On macOS/Linux:

```bash
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set your Ollama model, for example:

```env
OLLAMA_ENDPOINTS=http://localhost:11434/api/chat
OLLAMA_MODEL=llama3.1:8b
```

Then run:

```bash
python app.py
```

Open the local URL Flask prints, usually:

```text
http://127.0.0.1:5000
```

## Ollama notes

Make sure Ollama is running and the model is pulled:

```bash
ollama pull llama3.1:8b
ollama serve
```

`OLLAMA_ENDPOINTS` supports a comma-separated list. The app tries them in order, matching the pattern used in the IRS 990 tool.

The defaults include per-file retries and a consecutive-error cutoff:

```env
OLLAMA_RETRIES=2
OLLAMA_RETRY_DELAY=3
EVIDENCE_AI_MAX_CONSECUTIVE_ERRORS=3
```

This helps when a Tailscale/VPN or local Ollama connection is temporarily unavailable. If the app sees repeated Ollama/API failures, it stops the categorization job instead of marking every remaining file as an error. After fixing the endpoint or model, submit another categorization run; by default, errored files are retried and successfully categorized files are skipped.

## Scanning folders

Paste one path per line, for example:

```text
C:\Users\Jeff\Documents\Evidence Folder
D:\Cases\CaseA\source-file.pdf
```

The Flask app must have permission to read those paths. If you run the app on another machine, those paths must exist on the server machine, not your browser machine.

## Upload option

The upload form copies selected files/folders into `uploads/` and scans that staging location. This is useful if the browser and Flask server are not the same environment, but for very large evidence stores, path-based scanning is better because it avoids copying files.

## CSV exports

The app has two export buttons:

- **Export inventory CSV**: exactly the seven Phase 1 columns.
- **Export inventory + AI CSV**: the seven inventory columns plus `AI category`, `AI description`, and `AI status`.

The on-screen inventory preview always shows the AI columns.

## Duplicate handling

Duplicates are identified by identical file hash within a scan. All file copies remain in the inventory table. Phase 2 sends only one file per unique hash to Ollama, then joins that result back to all duplicates in the AI export.

## Local database

Scan metadata, categorization jobs, and AI results are stored in SQLite at:

```text
db/evidence.db
```

Change this with `EVIDENCE_DB_PATH` in `.env`.

## Practical limits and future improvements

For tens of thousands of files, Phase 1 should work as a long-running local scan. Phase 2 can take a long time because files are processed sequentially through Ollama. The app therefore lets you limit a categorization run to a smaller number of unique files for testing.

Possible next improvements:

- add case/project workspaces;
- add search and filtering in the inventory table;
- add richer duplicate-group reporting;
- add OCR for scanned PDFs;
- add media metadata extraction for video/audio;
- add resumable queued jobs across app restarts.
