# User Guide

This guide walks through starting the local Hy-MT translation server and
converting a complete EPUB using the one-command workflow.

## 1. Prepare the project

Open PowerShell and change to the folder containing the scripts:

```powershell
Set-Location "C:\path\to\books-translator"
```

The scripts use Python's standard library only. Check that Python is available:

```powershell
python --version
```

If you use the existing project virtual environment, the Python command in the
examples is `.\.venv\Scripts\python.exe`.

## 2. Install or locate `llama-server`

`llama-server` is part of [llama.cpp](https://github.com/ggml-org/llama.cpp).
Use a Windows build that includes `llama-server.exe`, and make sure the
executable is on your `PATH`. Check it with:

```powershell
llama-server --version
```

If PowerShell reports that `llama-server` is not recognized, install or extract
a Windows llama.cpp build that contains the server executable, then either add
its directory to `PATH` or call `llama-server.exe` by its full path.

## 3. Start Hy-MT locally

The project uses the GGUF Q4_K_M model from the
[`tencent/Hy-MT2-1.8B` Hugging Face repository](https://huggingface.co/tencent/Hy-MT2-1.8B).
Open a dedicated PowerShell window in the project directory and run:

```powershell
llama-server -hf tencent/Hy-MT2-1.8B-GGUF:Q4_K_M
```

Keep this window open while translating. The server downloads the model the
first time if needed. Its default port is 8080, which matches the translator's
default URL `http://localhost:8080/v1`.

You can check that the server is responding from another PowerShell window:

```powershell
Invoke-RestMethod -Uri "http://localhost:8080/health"
```

If you need a specific bind address or port, start the server with options such
as `--host 127.0.0.1 --port 8080` and pass the matching API base URL to the
pipeline with `--server-url`.

## 4. Translate an EPUB

Open a second PowerShell window in the project directory. Run the pipeline with
the source EPUB path:

```powershell
.\.venv\Scripts\python.exe .\translate_epub.py .\Cant-Hurt-Me.epub
```

Or, if using system Python:

```powershell
python .\translate_epub.py .\Cant-Hurt-Me.epub
```

The script runs each stage automatically:

1. Reads the EPUB package and extracts its spine documents to Markdown.
2. Translates the book title, contents, and chapter text to Arabic in bounded
   groups using `hy_translator.py`.
3. Applies the translated text to the original EPUB documents and sets Arabic
   right-to-left language and layout metadata.

The final output is `Cant-Hurt-Me.ar.epub` beside the source EPUB. Intermediate
files are retained in `.work\Cant-Hurt-Me\`:

- `Cant-Hurt-Me.md` — extracted source Markdown
- `Cant-Hurt-Me.arabic.md` — translated Markdown
- `translation-cache.json` — completed translations for resuming

## 5. Rerun or choose custom paths

If a run is interrupted, start the server again if necessary and rerun the same
command. The translation cache reuses completed text groups.

To change the output or intermediate folder:

```powershell
python .\translate_epub.py .\book.epub `
  --output .\output\book.ar.epub `
  --work-dir .\.work\book
```

To change the server URL:

```powershell
python .\translate_epub.py .\book.epub `
  --server-url http://127.0.0.1:8080/v1
```

To extract Markdown without translating, the server does not need to be
running:

```powershell
python .\translate_epub.py .\book.epub --extract-only
```

Run `python .\translate_epub.py --help` to see all supported options.

## Troubleshooting

### Could not reach the Hy-MT server

- Keep the PowerShell window running `llama-server` open.
- Confirm `http://localhost:8080/health` responds.
- Confirm `--server-url` points to the server's API base URL and ends in `/v1`.
- If using a different port, start the server with that port and use the same
  port in `--server-url`.

### `llama-server` is not recognized

Install or extract a Windows llama.cpp build containing `llama-server.exe`.
Add its folder to `PATH`, restart PowerShell, or invoke the executable by full
path.

### A translation or EPUB mapping error occurs

Intermediate Markdown and the translation cache remain in the work directory.
Review the translated Markdown and rerun after correcting any manual edits.
The EPUB builder requires the original and translated Markdown to correspond
to the same source book; do not remove its spine document marker comments.

## Pagination note

The builder uses the original EPUB structure, preserving its page-break markers
and image files in place. EPUB readers can reflow text based on device size,
font, and reading settings, so the visible page count may differ even when
explicit page breaks and original image placement are retained.
