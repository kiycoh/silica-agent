"""The format table: what each list read from it must stay, and that its rows name real readers.

The hosted list is served to the pickers in this order, the ingest labels are a
metric's allowlist and not a format set, and every list here was compared with
the literal it replaced when the table was written."""
from __future__ import annotations

from silica.kernel.base import formats, markdown
from silica.sources import convert


def test_every_row_names_a_reader_convert_has():
    for ext, fmt in formats.FORMATS.items():
        if fmt.reader and fmt.reader != formats.PDF_READER:
            assert callable(getattr(convert, fmt.reader)), ext


def test_the_hosted_list_keeps_the_order_the_pickers_are_served():
    assert formats.HOSTED_EXTENSIONS == (
        ".md", ".pdf", ".txt", ".docx", ".pptx", ".epub", ".fb2", ".odt", ".odp", ".ods", ".rtf",
        ".png", ".jpg", ".jpeg", ".webp", ".gif", ".csv",
        ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma",
        ".3gp", ".3g2", ".aiff", ".aif", ".aifc", ".amr", ".au", ".snd")


def test_the_ingest_line_names_what_it_named_and_no_more():
    """A hosted `.mp4` attempt logs `other`: the labels are not "every format"."""
    assert formats.INGEST_LABELS == {
        ".pdf", ".md", ".markdown", ".txt", ".docx", ".pptx", ".xlsx", ".odt", ".ods",
        ".epub", ".rtf", ".html", ".htm", ".csv", ".json", ".ipynb", ".py",
        ".png", ".jpg", ".jpeg", ".mp3", ".m4a", ".wav",
        ".fb2", ".odp", ".webp", ".gif", ".aac", ".flac", ".ogg", ".opus", ".wma",
        ".3gp", ".3g2", ".aiff", ".aif", ".aifc", ".amr", ".au", ".snd"}


def test_a_new_file_format_cannot_slip_past_the_document_lists():
    assert set(formats.CONVERTIBLE_DOC_EXTS) <= set(formats.DOC_EXTS)
    assert not set(formats.CONVERTIBLE_DOC_EXTS) & {*formats.IMG_EXTS, *formats.MEDIA_EXTS, ".ipynb"}


def test_the_lists_that_differ_differ_on_purpose():
    # converter, page and model each answer another question about an image
    assert ".tif" in formats.IMG_EXTS and ".tif" not in formats.SHOWN_IMAGE_EXTS
    assert ".svg" in formats.SHOWN_IMAGE_EXTS and ".svg" not in formats.IMAGE_MIME
    assert ".bmp" in formats.IMAGE_MIME and ".bmp" not in formats.HOSTED_EXTENSIONS
    assert set(formats.WEB_IMAGE_EXTS) == set(formats.IMAGE_MIME) - {".bmp"}
    assert set(formats.VISUAL_MEDIA_EXTS) < set(markdown.NON_MD_EXTENSIONS)
    assert set(formats.TABULAR_EXTS) <= set(markdown.NON_MD_EXTENSIONS)


def test_a_type_saves_under_the_first_extension_of_its_rows():
    assert formats.IMAGE_SUFFIX["image/jpeg"] == ".jpg"
    assert set(formats.IMAGE_SUFFIX) == set(formats.IMAGE_MIME.values())
