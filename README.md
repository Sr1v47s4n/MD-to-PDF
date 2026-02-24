# MD → PDF Converter

A **self-hosted, zero-storage** Markdown to PDF converter built with Flask and ReportLab. Convert Markdown documents to beautifully formatted PDFs with full control over styling—all processing happens in-memory with no server-side storage.

## Features

- **Rich Code Editor** — CodeMirror with syntax highlighting, formatting toolbar, and keyboard shortcuts
- **Live Preview** — Real-time rendering as you type
- **File Upload** — Drag & drop `.md` or `.txt` files, or paste raw Markdown
- **Customizable Styling** — Choose fonts, sizes, line height, accent colors, and page padding
- **Page Options** — Select paper size (A4/Letter/A3/Legal), orientation, and margins
- **Color Schemes** — Classic, Warm, Night, and Ocean themes
- **Sample Templates** — Report, README, Letter, and Resume templates
- **Fullscreen Mode** — Distraction-free editing with ESC to exit
- **Zero Storage** — All conversion is in-memory; no data is persisted on the server

## Quick Start

### Local Installation

```bash
# Install dependencies
pip install -r requirements.txt

# Run the app
python app.py

# Open in browser
open http://localhost:5000
```

### Docker

```bash
# Using Docker Compose
docker compose up --build

# Or with standalone Docker
docker build -t mdpdf .
docker run -p 5000:5000 --read-only --tmpfs /tmp mdpdf
```

## Configuration

| Variable | Default | Description        |
|----------|---------|-------------------|
| `PORT`   | `5000`  | HTTP server port  |
| `DEBUG`  | `false` | Flask debug mode  |

## How It Works

1. **Submit** — Write or upload Markdown and configure PDF settings
2. **Preview** — See changes in real-time as you edit
3. **Export** — Download the PDF with your chosen styling
4. **No Storage** — All processing happens in-memory; nothing is saved on the server

### Privacy & Security

- All conversion is **in-memory only**
- No files are written to disk
- No data is logged or persisted
- Docker container runs with `--read-only` for maximum security

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Backend | Flask 3 |
| PDF Rendering | ReportLab (pure Python) |
| Markdown Parser | Python-Markdown |
| Editor | CodeMirror 5 |
| Fonts | Google Fonts (Playfair Display, JetBrains Mono, Lora) |

## Supported Markdown

- **Headings** — H1 to H4
- **Text Formatting** — bold, italic, strikethrough, inline code
- **Code Blocks** — fenced with syntax highlighting
- **Lists** — unordered and ordered
- **Tables** — with automatic alignment
- **Blockquotes** — with custom styling
- **Links** — clickable URLs
- **Horizontal Rules** — for section breaks

## Requirements

- Python 3.8+
- Flask 3
- ReportLab
- Python-Markdown

See `requirements.txt` for full dependencies.

## License

MIT
