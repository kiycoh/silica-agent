# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Alessandro Carosia

"""What a file format is: one row per extension, and the lists read from it.

A new format is a row here, and a reader in `sources/convert.py` if it is read.
Data only, so the kernel can ask it: a reader is named, not imported, because
the readers sit above this layer.

The lists at the bottom are not one list spelled several ways. A set of formats
is named for the question it answers, and two that look alike can differ on
purpose: `IMG_EXTS` is what the converter opens, `SHOWN_IMAGE_EXTS` what a page
can draw, `IMAGE_MIME` what the chat model is sent.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from functools import partial
from typing import NamedTuple

# `Format.reader` for the one format whose reader is a setting, the PDF provider
# seam (ADR-0011): `convert.PDF_PROVIDERS` names the function at call time.
PDF_READER = "pdf"

_NO_SPEECH = ("no speech transcribed from {name} — the audio track carries no recognisable "
              "speech (music, silence, or a language the model was not given)")
# mineru already ran (it is the only backend for these), so pointing at OCR
# would be advice the user has already taken.
_NO_OCR_TEXT = ("no readable text in {name} — the OCR pass found none "
                "(a photo with no writing in it, or an empty document)")
# No OCR anywhere in this path, so advice about OCR would be a red herring: the
# file really does carry no text.
_NO_TEXT = ("no text in {name} — the document is empty, or its content is entirely images "
            "(which this path does not read; re-save it as PDF to run those through OCR)")


class Format(NamedTuple):
    family: str
    # The function in `sources/convert.py` that reads it. Empty: no converter
    # does, and the row is here for another column.
    reader: str = ""
    # ADR-0087 point 5, the owner's answer of 2026-10-12: the hosted pilot reads
    # PDF, markdown and plain text, and nothing else is installed to read more.
    # Audio since the amendment of 2026-10-14: ffmpeg is on the host for the
    # transcriber, and its demuxers are held to convert._FFMPEG_FORMATS. Not
    # video, which the notice does not name and which is mostly bytes the
    # transcriber never hears under the edge's 100 MB.
    # ADR-0120 (2026-10-17) adds what NotebookLM takes and the base install
    # reads with nothing installed on the host: documents read in process under
    # the 16 MB part ceiling (DOCX in pdf_extract's child), images read by the
    # chat model, and a CSV's profile.
    hosted: bool = False
    # What the first bytes of an upload must be for its extension: a renamed
    # file reached a reader as whatever it really was (ADR-0098 H4). Empty for a
    # format with no fixed opening (markdown, CSV, audio, which admission
    # probes), and for a PDF, whose header `pdf_extract.require_header` reads.
    magic: tuple[bytes, ...] = ()
    # The sentence for a read that came back empty, `{name}` the file's. Empty:
    # the file is a PDF or ends as one, and the sentence depends on the provider
    # that ran (`convert._no_text_sentence`).
    empty_text_message: str = ""
    # Whether the ingest line may name the extension. A suffix is part of the
    # file's name, which is the student's text, so anything else is "other".
    ingest_label: bool = False
    # What the chat model is told it is sent. Raster formats a vision projector
    # takes: SVG is a document and remote URLs are not the vault's, so both stay
    # unread.
    image_mime: str = ""


_ZIP = (b"PK\x03\x04",)
_JPEG = (b"\xff\xd8\xff",)

# Text documents read in process with no PDF renderer in the path: DOCX through
# mammoth, EPUB (a ZIP of XHTML), FB2 (XML) and PPTX (a ZIP of slide XML)
# through the standard library. `.txt`/`.md` are absent on purpose:
# ProseAdapter already claims them.
_text = partial(Format, "text", empty_text_message=_NO_TEXT)

# ODF is a ZIP with a `content.xml` inside, so reading it needs no office suite
# and no dependency at all — `zipfile` plus `xml.etree`. RTF and legacy `.xls`
# need one small pure-python parser each (striprtf 15 KB BSD-3, xlrd 192 KB
# BSD). Together that is ~200 KB against the ~240 MB minimum a headless
# LibreOffice install costs on Debian (measured: libreoffice-core 155 MB +
# -common 47 MB + one app), for five of the seven formats that used to demand it.
#
# ponytail: text only. The old `soffice → pdf → MuPDF` path carried embedded
# figures through; these do not. Re-save as PDF when the images are the point.
_office = partial(Format, "office", empty_text_message=_NO_TEXT)

# Image formats mineru opens (its own `image_suffixes`). It wraps the image into
# a one-page PDF internally, so a screenshot or a scan takes the same OCR
# pipeline a scanned PDF takes. Both `.tif` and `.tiff` land because mineru
# sniffs content rather than trusting the extension (measured: `.tif` -> "tiff",
# `.jpg` -> "jpeg"). `.heic` is absent: mineru does not list it, and reading it
# at all needs pillow-heif. Without mineru only `IMAGE_MIME`'s formats are read,
# by the chat model (`convert._via_image_model`).
_image = partial(Format, "image", "_pdf_via_mineru", empty_text_message=_NO_OCR_TEXT)

# Media: transcribed, not parsed. Both families take the same path (ffmpeg
# demuxes any container to 16 kHz mono wav, which is the one input every ASR
# accepts), so video costs nothing beyond `-vn`. The list is what ffmpeg reads
# in a default build, not everything it can be built for. The rows from `.3gp`
# on are ADR-0120's, what a phone or a Mac recorder writes: 3GP/3G2 (old Android
# recorders, the `mov` demuxer), AIFF (macOS), AMR (voice memos) and Sun AU.
# `.3gp` can hold video, whose audio is the part transcribed, as for `.mp4`.
# Not NotebookLM's `.ram` (a playlist, the class `_FFMPEG_FORMATS` keeps out),
# `.cda` (a 44-byte CD track stub with no audio in it) or `.mid` (MIDI, no speech).
_audio = partial(Format, "audio", "_via_asr", True, empty_text_message=_NO_SPEECH, ingest_label=True)
_video = partial(Format, "video", "_via_asr", empty_text_message=_NO_SPEECH)

# Data files: never prose-converted row by row. A .csv in the inbox becomes a
# PROFILE note — schema, per-column stats, a 5-row sample — that makes the file
# discoverable by recall while the rows stay on disk for `silica_query_table`
# to aggregate in place. json/ndjson stay out: their shape is unknowable up
# front (config? API dump? records?), so profiling them would guess.
_tabular = partial(Format, "tabular", "_convert_tabular")

# Row order is part of the data: `HOSTED_EXTENSIONS` is served to the pickers in
# this order, and `IMAGE_SUFFIX` takes the first row of a type.
FORMATS: dict[str, Format] = {
    ".md": Format("prose", hosted=True, ingest_label=True),
    ".pdf": Format("pdf", PDF_READER, hosted=True, ingest_label=True),
    ".txt": Format("prose", hosted=True, ingest_label=True),
    ".docx": _text("_via_docx", hosted=True, magic=_ZIP, ingest_label=True),
    ".pptx": _text("_via_pptx", hosted=True, magic=_ZIP, ingest_label=True),
    ".epub": _text("_via_epub", hosted=True, magic=_ZIP, ingest_label=True),
    ".fb2": _text("_via_fb2", hosted=True, magic=(b"<?xml", b"\xef\xbb\xbf<?xml", b"<FictionBook"),
                  ingest_label=True),
    ".odt": _office("_via_odf", hosted=True, magic=_ZIP, ingest_label=True),
    ".odp": _office("_via_odf", hosted=True, magic=_ZIP, ingest_label=True),
    ".ods": _office("_via_odf", hosted=True, magic=_ZIP, ingest_label=True),
    ".rtf": _office("_via_rtf", hosted=True, magic=(b"{\\rtf",), ingest_label=True),
    ".xls": _office("_via_xls"),
    # OOXML mineru parses NATIVELY, via openpyxl, with no LibreOffice anywhere in
    # the path. Output lands in `<stem>/office/<stem>.md` instead of `auto/`,
    # which the provider's recursive glob already finds. mineru is the only
    # backend that opens it, so the PDF seam does not apply.
    #
    # `.docx` is deliberately NOT this family: mammoth reads it in the base
    # install, so routing it through mineru would demand an OCR install for a
    # format that already works. Nor is `.pptx` since 2026-10-17 (ADR-0120):
    # mineru 3.2.1's pptx path is python-pptx with no OCR at all (`grep -i ocr`
    # on its model/pptx/ and backend/office/ is empty), so it read nothing the
    # deck's own XML does not, and `_via_pptx` reads that XML with no install.
    # The pre-2007 binaries are not here either: neither mineru nor MuPDF opens
    # them (see `convert._SOFFICE_QUIET`).
    ".xlsx": Format("sheet", "_pdf_via_mineru", empty_text_message=_NO_OCR_TEXT, ingest_label=True),
    # The two pre-2007 binaries, which end as a PDF through LibreOffice.
    ".ppt": Format("legacy", "_via_legacy_office"),
    ".doc": Format("legacy", "_via_legacy_office"),
    ".png": _image(hosted=True, magic=(b"\x89PNG\r\n\x1a\n",), ingest_label=True, image_mime="image/png"),
    ".jpg": _image(hosted=True, magic=_JPEG, ingest_label=True, image_mime="image/jpeg"),
    ".jpeg": _image(hosted=True, magic=_JPEG, ingest_label=True, image_mime="image/jpeg"),
    ".jp2": _image(),
    # `RIFF` opens more than WebP: `vault_import` also reads the form type.
    ".webp": _image(hosted=True, magic=(b"RIFF",), ingest_label=True, image_mime="image/webp"),
    ".gif": _image(hosted=True, magic=(b"GIF87a", b"GIF89a"), ingest_label=True, image_mime="image/gif"),
    # Not hosted: no OpenRouter provider lists it.
    ".bmp": _image(image_mime="image/bmp"),
    ".tif": _image(),
    ".tiff": _image(),
    ".csv": _tabular(hosted=True, ingest_label=True),
    ".tsv": _tabular(),
    ".parquet": _tabular(),
    **{ext: _audio() for ext in (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma",
                                 ".3gp", ".3g2", ".aiff", ".aif", ".aifc", ".amr", ".au", ".snd")},
    **{ext: _video() for ext in (".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".mpg", ".mpeg")},
    # Reaches convert() only where the code lane does not claim it
    # (`NotebookAdapter.matches`: no git checkout around the vault).
    ".ipynb": Format("notebook", "_via_ipynb", ingest_label=True),
    # No reader here: a lane elsewhere takes these or none does, and an upload
    # of one still counts by type.
    **{ext: Format("other", ingest_label=True) for ext in (".markdown", ".html", ".htm", ".json", ".py")},
}


def exts_of(*families: str) -> tuple[str, ...]:
    return tuple(e for e, f in FORMATS.items() if f.family in families)


def ext_alternation(exts: Iterable[str]) -> str:
    """Extensions as a regex group, without their dots."""
    return "(?:" + "|".join(re.escape(e[1:]) for e in exts) + ")"


IMG_EXTS = exts_of("image")
OFFICE_EXTS = exts_of("sheet")
LEGACY_OFFICE_EXTS = exts_of("legacy")
AUDIO_EXTS = exts_of("audio")
VIDEO_EXTS = exts_of("video")
MEDIA_EXTS = (*AUDIO_EXTS, *VIDEO_EXTS)
TABULAR_EXTS = exts_of("tabular")
PROSE_EXTS = exts_of("prose")

# What `convert()` takes.
DOC_EXTS = tuple(e for e, f in FORMATS.items() if f.reader)

# The subset that is a *document* someone meant to keep, as opposed to an
# attachment that happens to be convertible. Onboarding offers to convert what
# it finds sitting in a vault, and by that measure an image or a video is almost
# always an attachment: every Obsidian vault carries pasted screenshots, and
# `_copy_images` puts Silica's OWN extracted figures in `<inbox>/Images`. Offering
# to convert those would mean offering to re-ingest our own output.
CONVERTIBLE_DOC_EXTS = exts_of("pdf", "text", "office", "sheet", "legacy", "tabular")

HOSTED_EXTENSIONS = tuple(e for e, f in FORMATS.items() if f.hosted)
INGEST_LABELS = frozenset(e for e, f in FORMATS.items() if f.ingest_label)

# Extension to the type the chat model is sent, and a type to the extension a
# saved copy takes.
IMAGE_MIME = {e: f.image_mime for e, f in FORMATS.items() if f.image_mime}
IMAGE_SUFFIX = {mime: e for e, mime in reversed(IMAGE_MIME.items())}

# What the vault shows and `convert._rewrite_image_links` embeds: EMF and WMF,
# which decks carry for vector art, nothing here renders. The hosted images,
# which is IMAGE_MIME without .bmp.
WEB_IMAGE_EXTS = tuple(e for e in IMG_EXTS if FORMATS[e].hosted)

# What a page can draw as a picture of the vault's. SVG too, which no model reads.
SHOWN_IMAGE_EXTS = (*IMAGE_MIME, ".svg")

# What an embed shows instead of saying, so prose goes without it (kernel/text/media).
# Visual media only: a .zip or .ipynb link must stay in the text.
VISUAL_MEDIA_EXTS = (*SHOWN_IMAGE_EXTS, ".tif", ".tiff", ".avif", ".mp4", ".mov", ".avi", ".mkv", ".pdf")

# A wikilink in the person's own words that names an attachment and not a note.
# Not `markdown.NON_MD_EXTENSIONS`, which grew from links the index found in
# notes: this one grew from what a chat names, so it holds audio, office files
# and Obsidian's `.canvas`/`.base`, and leaves out `.tif`, `.mkv` and `.ipynb`.
# What it holds decides which notes a turn reads aloud to the model, so the two
# stay apart until someone measures merging them.
ATTACHMENT_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".avif", ".pdf",
                   ".mp3", ".mp4", ".m4a", ".wav", ".ogg", ".webm", ".mov", ".canvas", ".base",
                   ".csv", ".xls", ".xlsx", ".doc", ".docx", ".ppt", ".pptx", ".zip", ".epub")
