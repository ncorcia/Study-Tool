"""Text extraction from PDF files using pdfplumber."""

import pdfplumber


def extract_text(file_path: str) -> str:
    """Extract text from a PDF, page by page, including simple table text."""
    pages_text = []
    with pdfplumber.open(file_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            pages_text.append(text)
    return "\n\n".join(pages_text).strip()
