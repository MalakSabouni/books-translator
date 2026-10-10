# EPUB Book Translator

Translate an EPUB into an Arabic, right-to-left EPUB with one command. The
pipeline extracts the EPUB's reading-order text, translates it with the local
Hy-MT model through `hy_translator.py`, then puts the translated text back into
the original EPUB structure. Original spine documents, image assets, and
explicit page-break markers are retained.

## Requirements

- Windows PowerShell
- Python 3.10 or newer
- `llama-server` from [llama.cpp](https://github.com/ggml-org/llama.cpp)
- The Hy-MT model: [`tencent/Hy-MT2-1.8B`](https://huggingface.co/tencent/Hy-MT2-1.8B)

The Python workflow uses only the standard library; no pip packages are needed.
See [GUIDE.md](./GUIDE.md) for setup, server startup, and troubleshooting.

## Quick start

Open one PowerShell window in this project directory and start the model server:

```powershell
llama-server -hf tencent/Hy-MT2-1.8B-GGUF:Q4_K_M
```

Leave that window open. In a second PowerShell window, run the full conversion
with the source EPUB as the only required argument:

```powershell
.\.venv\Scripts\python.exe .\translate_epub.py .\Book-To-Translate-to-Arabic.epub
```

By default, the script creates:

- `Book-To-Translate-to-Arabic.ar.epub` — final Arabic RTL book beside the original
- `.work\Book-To-Translate-to-Arabic\Book-To-Translate-to-Arabic.md` — extracted source Markdown
- `.work\Book-To-Translate-to-Arabic\Book-To-Translate-to-Arabic.arabic.md` — translated Markdown
- `.work\Book-To-Translate-to-Arabic\translation-cache.json` — resumable translation cache

Use a system Python executable instead if you do not have the project venv:

```powershell
python .\translate_epub.py .\Book-To-Translate-to-Arabic.epub
```

## Common options

```powershell
python .\translate_epub.py .\book.epub `
  --output .\translated\book-arabic.epub `
  --work-dir .\.work\book `
  --server-url http://localhost:8080/v1 `
  --target-language Arabic `
  --max-chars 6000
```

Run `python .\translate_epub.py --help` for all options. To inspect extraction
without starting the model server:

```powershell
python .\translate_epub.py .\book.epub --extract-only
```

## Component scripts

The one-command entry point orchestrates these individual utilities:

- `epub_to_markdown.py` — extract EPUB spine text and navigation to Markdown
- `translate_markdown.py` — translate Markdown sections using
  `hy_translator.py`
- `hy_translator.py` — call the local llama-server and split long text
- `markdown_to_epub.py` — rebuild an RTL EPUB using the original EPUB layout
- `translate_epub.py` — complete pipeline; use this for normal translation

Intermediate Markdown and the translation cache are intentionally retained.
If a translation is interrupted, rerun the same command: cached completed
groups are reused. The original EPUB is never overwritten by the default
output path.

## Layout and pagination

The original EPUB is the layout source. Its reading order, XHTML documents,
images, and explicit page-break elements are kept while translated text is
inserted. EPUB readers may still reflow text according to their display and
font settings, so a reflowable book may not have the same screen page count.

## Model and endpoint

The documented server command uses
[`tencent/Hy-MT2-1.8B`](https://huggingface.co/tencent/Hy-MT2-1.8B) in GGUF
Q4_K_M format. `llama-server` downloads the model on its first run if it is not
already cached. The default API base URL is `http://localhost:8080/v1`; override
it with `--server-url` or the `HY_MT_URL` environment variable.

## Privacy

With the documented local server, book text is sent to the llama-server running
on your machine. The Hugging Face repository is used to download the model if
it is not already available locally. Review the model repository's license and
terms before use.
