# evidence-tool

A local Flask web app for building an evidence inventory from many folders/files, detecting duplicate files by hash, exporting results to CSV, and using a local Ollama instance to categorize and briefly describe unique files.

This is intended to be run locally on the same machine that can access the evidence folders. It does not upload your evidence to any hosted service. Phase 2 sends extracted text, file metadata, optional OCR text, and optionally image payloads to whatever endpoint you configure in `OLLAMA_ENDPOINTS`, so keep that endpoint local or otherwise trusted.

## Features

### Phase 1: Inventory scan

The scanner accepts multiple absolute folder paths and/or individual file paths, one per line. Directories are scanned recursively. The inventory table and CSV export use this column order:

1. File name
2. Type of file
3. Folder location
4. Last modified date
5. Size (MB)
6. Hash

The default hash is `blake2b` with a 256-bit digest because it is fast and modern. You can switch to `md5` or `sha256` in `.env`.

### Phase 2: Ollama categorization

After a scan, the app submits only one representative copy of each unique hash to Ollama. Duplicate files are not sent repeatedly.

The screen asks for:

- categories to use, with optional category definitions;
- an optional category-definition CSV where column 1 is the category name and column 2 is the definition/description;
- free-text investigation/project context;
- model override, if you do not want to use `OLLAMA_MODEL` from `.env`;
- maximum extracted characters per file;
- optional maximum number of unique files to process in that run.

Phase 2 categorization jobs are persisted in SQLite and run through a single FIFO worker, so a second request waits until the prior categorization job finishes or is cancelled. The web page includes a **Categorization jobs** section with historical and current jobs, progress, error count, model, timestamps, and a cancel button. Cancelling a running job stops it after the current Ollama request returns.

The app asks Ollama for compact JSON output and parses several common model formats, including JSON, partial JSON, simple label formats, and tag-like responses. The prompt tells the model to use your project context as background only and to describe the file's actual contents rather than repeating generic context for every file.

The current default model in `.env.example` is:

```env
OLLAMA_MODEL=gemma4:12b
```

### Structured evidence fields

The AI export and on-screen preview include these AI fields:

- `AI primary category`
- `AI secondary tags`
- `AI confidence`
- `AI description`
- `AI evidence basis`
- `AI key people`
- `AI key organizations`
- `AI date or event`
- `AI why useful as evidence`
- `AI needs human review`
- `AI status`

The app validates `AI primary category` against the allowed category list. If the model invents or modifies a category, the app retries once with a stricter correction prompt. If the retry still does not produce a valid category, the app sets the primary category to `Unrelated or insufficient evidence` and marks the row for human review. The raw invalid category is still kept internally for debugging, but it is no longer shown in the preview table or exported CSV.

Category input supports either one category per line or category-definition lines like:

```text
Category Name: Category definition or description
```

You can also use the optional category-definition CSV selector in Phase 2. The first column should contain the category name and the second column should contain its definition/description.

Default primary categories:

```text
Network assembly / convening materials
Participant / attendee lists
People / organization directory
Network strategy / governance / regeneration
Work group / priority-area planning
Policy framework / policy agenda
Community schools
Shared Story / narrative / communications strategy
Education resourcing / school funding
Teacher / educator workforce
Federal funding / ARP / COVID response
Place-based / Key Places strategy
Research / evaluation / findings report
Public education defense / voucher-privatization response
Administrative logistics / internal run-of-show
General network announcement / member update
Unrelated or insufficient evidence
```

Allowed evidence-basis values:

```text
extracted text
visible image text
metadata only
filename only
mixed
```

### Text and image handling

For common text-bearing formats, the app extracts local text before calling Ollama.

Supported text extraction:

- `.txt`, `.md`, `.log`, `.json`, `.xml`, `.html`, `.csv`, `.tsv`
- `.pdf`
- `.docx` with a zipped-XML fallback when normal DOCX extraction fails
- `.xlsx`, `.xlsm`
- `.pptx`
- `.eml`

Image handling supports:

- `.jpg`, `.jpeg`, `.png`, `.webp`, `.tif`, `.tiff`, `.bmp`

For images, the app attempts optional local OCR with `pytesseract` if it is installed. If `EVIDENCE_AI_SEND_IMAGES=true`, the app also sends the image to Ollama as a base64 image payload when the file is under `EVIDENCE_IMAGE_MAX_BYTES`. This is most useful with a multimodal model such as `gemma4:12b`.

Image prompts now tell the model to classify campaign graphics, partner-logo images, screenshots, and social cards by their visible topic/purpose rather than automatically treating them as directories. The app also applies conservative confidence caps for image-only, metadata-only, filename-only, ambiguous, or human-review rows so confidence is more useful for triage.

If OCR is not available and the model cannot use the image payload, the app falls back to filename/metadata and marks the result for human review.

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

Edit `.env` and set your Ollama endpoint/model, for example:

```env
OLLAMA_ENDPOINTS=http://localhost:11434/api/chat
OLLAMA_MODEL=gemma4:12b
OLLAMA_THINK=false
OLLAMA_NUM_CTX=32768
OLLAMA_NUM_PREDICT=4096
OLLAMA_TIMEOUT=300
EVIDENCE_EXTRACT_MAX_CHARS=40000
EVIDENCE_AI_SEND_IMAGES=true
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
ollama pull gemma4:12b
ollama serve
```

`OLLAMA_ENDPOINTS` supports a comma-separated list. The app tries them in order, matching the pattern used in the IRS 990 tool. Endpoint values can include or omit `/api/chat`.

For high-throughput categorization, these settings have worked well as a starting point:

```env
OLLAMA_MODEL=gemma4:12b
OLLAMA_THINK=false
OLLAMA_NUM_CTX=32768
OLLAMA_NUM_PREDICT=4096
OLLAMA_TIMEOUT=300
OLLAMA_RETRIES=0
EVIDENCE_EXTRACT_MAX_CHARS=40000
EVIDENCE_AI_MAX_CONSECUTIVE_ERRORS=3
```

If the app sees repeated Ollama/API failures, it stops the categorization job instead of marking every remaining file as an error. After fixing the endpoint or model, submit another categorization run; by default, errored files are retried and successfully categorized files are skipped.

## Optional OCR setup

Local OCR is optional. The app will still run without OCR libraries.

For Python packages:

```bash
pip install pillow pytesseract
```

You also need the native Tesseract OCR engine installed on the machine running Flask. If Tesseract is not installed or not on PATH, the app records OCR as unavailable and continues with metadata, filename, extracted text, and/or Ollama vision input where available.

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

- **Export inventory CSV**: exactly the six Phase 1 columns.
- **Export inventory + AI CSV**: the six inventory columns plus the structured AI fields listed above.

The on-screen inventory preview always shows the AI columns.

## Duplicate handling

Duplicates are identified by identical file hash within a scan. All file copies remain in the inventory table. Phase 2 sends only one file per unique hash to Ollama, then joins that result back to all duplicates in the AI export.

## Local database

Scan metadata, categorization jobs, categorization errors, and AI results are stored in SQLite at:

```text
db/evidence.db
```

Change this with `EVIDENCE_DB_PATH` in `.env`. Database migrations run automatically on startup and add any missing columns/tables.

## Practical limits and future improvements

For tens of thousands of files, Phase 1 should work as a long-running local scan. Phase 2 can take a long time because files are processed sequentially through Ollama. The app therefore lets you limit a categorization run to a smaller number of unique files for testing.

Possible next improvements:

- add case/project workspaces;
- add search and filtering in the inventory table;
- add richer duplicate-group reporting;
- add OCR for scanned PDFs;
- add media metadata extraction for video/audio;
- add resumable queued jobs across app restarts.
