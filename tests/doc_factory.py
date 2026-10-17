"""Hand-written test documents: PDF (text pages, outline, metadata, image pages,
unmapped fonts), DOCX, EPUB, FB2, PPTX.

The suite used to build its fixtures with pymupdf, which left an AGPL package in
the dev environment after the runtime dropped it (2026-08-31). A text-only PDF
is ~40 lines of the 1.4 syntax: one Helvetica font, one content stream per
page, an /Outlines tree when asked, an /Info dictionary when asked. Standard
14 fonts need no embedding and WinAnsi covers the Latin-1 the tests use, so
any reader recovers the text verbatim.
"""
from __future__ import annotations


def _pdf_string(text: str) -> bytes:
    """A PDF literal string: Latin-1 bytes with the three escapes the syntax needs."""
    raw = text.encode("latin-1", "replace")
    return b"(" + raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


def pdf_bytes(
    pages: list[str],
    toc: list[tuple[int, str, int]] | None = None,
    creation_date: str | None = None,
    title: str | None = None,
    author: str | None = None,
    font_sizes: list[list[float]] | None = None,
    images: dict[int, tuple[int, int, bytes]] | None = None,
    garbled: set[int] | None = None,
    math: set[int] | None = None,
) -> bytes:
    """A real one-column PDF, one page per string, lines separated by "\\n".

    `toc` entries are (level, title, 1-based page), flattened: every entry
    hangs off the document outline (level is recorded for readers that expose
    it, nesting is not built). `creation_date` is the PDF form, e.g.
    "D:20240402093000+02'00'". `font_sizes`, when given, holds one size per
    line of each page (default 11 pt), so a test can stage a heading heuristic.

    `images` maps a 0-based page to (width, height, 8-bit gray pixels), drawn
    468 pt wide at the top of that page as an image XObject: with "" as the
    page's text, a page that is only a picture, as a scan is (ADR-0087).
    Pixels shorter than width times height are taken as the stream already
    Flate-compressed, which builds a 1 GiB picture in 1 MB.
    `garbled` pages draw their text in a font whose ToUnicode map sends every
    code into the private use area, which is what a subset font with no usable
    map extracts as: the glyphs are on the page, the letters are not.
    `math` pages draw every line after the first in a font named CambriaMath,
    as PowerPoint's equation editor draws a formula under a slide's title; its
    WinAnsi encoding still maps, so the text extracts.
    """
    import zlib

    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    n_pages = len(pages)
    # Object numbers are assigned up front so that pages can point at their
    # parent and the parent at its kids without a second pass.
    catalog_id, pages_id, font_id = 1, 2, 3
    first_page_id = 4
    content_ids = [first_page_id + n_pages + i for i in range(n_pages)]
    next_id = first_page_id + 2 * n_pages
    outlines_id = next_id if toc else None
    if toc:
        next_id += 1 + len(toc)
    info_id = next_id if (creation_date or title or author) else None
    if info_id:
        next_id += 1
    image_ids: dict[int, int] = {}
    for i in sorted(images or {}):
        image_ids[i] = next_id
        next_id += 1
    garbled_font_id = next_id if garbled else None
    if garbled:
        next_id += 2  # the font and its ToUnicode stream
    math_font_id = next_id if math else None

    add(b"<< /Type /Catalog /Pages 2 0 R"
        + (f" /Outlines {outlines_id} 0 R /PageMode /UseOutlines".encode() if toc else b"")
        + b" >>")
    kids = " ".join(f"{first_page_id + i} 0 R" for i in range(n_pages))
    add(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    for i in range(n_pages):
        fonts = (f"/F1 {font_id} 0 R" + (f" /F2 {garbled_font_id} 0 R" if garbled_font_id else "")
                 + (f" /F3 {math_font_id} 0 R" if math_font_id else ""))
        xobject = f" /XObject << /Im0 {image_ids[i]} 0 R >>" if i in image_ids else ""
        add(f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << {fonts} >>{xobject} >> "
            f"/Contents {content_ids[i]} 0 R >>".encode())
    for i, text in enumerate(pages):
        lines = text.split("\n")
        sizes = (font_sizes[i] if font_sizes else None) or [11.0] * len(lines)
        font = "/F2" if garbled and i in garbled else "/F1"
        stream = b""
        if images and i in images:
            w, h, _ = images[i]
            drawn = 468 * h / w
            stream += f"q 468 0 0 {drawn:g} 72 {720 - drawn:g} cm /Im0 Do Q\n".encode()
        stream += b"BT\n"
        y = 720.0
        for k, (line, size) in enumerate(zip(lines, sizes)):
            if math and i in math and k:
                font = "/F3"
            stream += f"{font} {size:g} Tf 1 0 0 1 72 {y:g} Tm ".encode() + _pdf_string(line) + b" Tj\n"
            y -= size * 1.6
        stream += b"ET"
        add(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    if toc:
        first, last = outlines_id + 1, outlines_id + len(toc)
        add(f"<< /Type /Outlines /First {first} 0 R /Last {last} 0 R /Count {len(toc)} >>".encode())
        for k, (level, heading, page) in enumerate(toc):
            oid = outlines_id + 1 + k
            entry = ("<< /Title ".encode() + _pdf_string(heading)
                     + f" /Parent {outlines_id} 0 R "
                       f"/Dest [{first_page_id + page - 1} 0 R /XYZ 0 792 0]".encode())
            if k > 0:
                entry += f" /Prev {oid - 1} 0 R".encode()
            if k < len(toc) - 1:
                entry += f" /Next {oid + 1} 0 R".encode()
            # /Level is not standard; readers derive nesting from /First. Kept in
            # a private key so a test can still see what it asked for.
            entry += f" /SilicaLevel {level} >>".encode()
            add(entry)
    if info_id:
        info = b"<<"
        if title:
            info += b" /Title " + _pdf_string(title)
        if author:
            info += b" /Author " + _pdf_string(author)
        if creation_date:
            info += b" /CreationDate " + _pdf_string(creation_date)
        add(info + b" >>")
    for i in sorted(images or {}):
        w, h, pixels = images[i]
        data = zlib.compress(pixels) if len(pixels) == w * h else pixels
        add(f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace /DeviceGray "
            f"/BitsPerComponent 8 /Filter /FlateDecode /Length {len(data)} >>\nstream\n".encode()
            + data + b"\nendstream")
    if garbled_font_id:
        add(f"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding "
            f"/ToUnicode {garbled_font_id + 1} 0 R >>".encode())
        add(b"<< /Length " + str(len(_PUA_CMAP)).encode() + b" >>\nstream\n" + _PUA_CMAP + b"\nendstream")
    if math_font_id:
        add(b"<< /Type /Font /Subtype /TrueType /BaseFont /CambriaMath /Encoding /WinAnsiEncoding >>")

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    trailer = f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R"
    if info_id:
        trailer += f" /Info {info_id} 0 R"
    out += (trailer + f" >>\nstartxref\n{xref}\n%%EOF\n").encode()
    return bytes(out)


# Every printable ASCII code to U+E020..U+E07E: letters drawn, private-use
# characters extracted.
_PUA_CMAP = b"""/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /Adobe-Identity-UCS def
/CMapType 2 def
1 begincodespacerange
<00> <FF>
endcodespacerange
1 beginbfrange
<20> <7E> <E020>
endbfrange
endcmap
CMapName currentdict /CMap defineresource pop
end
end"""


def gray_lines(width: int = 300, height: int = 200) -> tuple[int, int, bytes]:
    """A white picture crossed by dark bars, as lines of type look from afar:
    something on the page to render, and no text layer under it."""
    rows = []
    for y in range(height):
        ink = (y // 6) % 3 == 0 and 10 < y < height - 10
        rows.append(bytes([30 if ink and 20 < x < width - 20 else 255 for x in range(width)]))
    return width, height, b"".join(rows)


# --- DOCX ---------------------------------------------------------------------

_CT = ('<?xml version="1.0" encoding="UTF-8"?>'
       '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
       '<Default Extension="xml" ContentType="application/xml"/>'
       '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
       '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
       '<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>'
       '</Types>')
# A bullet definition: mammoth emits <ul><li> only for paragraphs whose numPr
# resolves to a numbering instance, so a list needs this part and its relationship.
_DOC_RELS = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
             '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>'
             '</Relationships>')
_NUMBERING = ('<?xml version="1.0" encoding="UTF-8"?>'
              '<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
              '<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/><w:lvlText w:val="\u2022"/></w:lvl></w:abstractNum>'
              '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
              '</w:numbering>')
_RELS = ('<?xml version="1.0" encoding="UTF-8"?>'
         '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
         '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
         '</Relationships>')
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def docx_bytes(
    blocks: list[tuple[str, str]],
    created: str | None = None,
    title: str | None = None,
    creator: str | None = None,
) -> bytes:
    """A minimal WordprocessingML package.

    `blocks` are (kind, text) with kind "h1", "h2", "p" or "li"; the heading
    kinds map to the built-in Heading1/Heading2 style ids mammoth recognises
    without a styles part. `created`/`title`/`creator` land in docProps/core.xml.
    """
    import io
    import zipfile
    from xml.sax.saxutils import escape

    body = []
    for kind, text in blocks:
        style = {"h1": "Heading1", "h2": "Heading2", "li": "ListParagraph"}.get(kind)
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        if kind == "li":
            ppr = '<w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
        body.append(f'<w:p>{ppr}<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>')
    document = (f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{_W}"><w:body>'
                + "".join(body) + "</w:body></w:document>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/document.xml", document)
        z.writestr("word/_rels/document.xml.rels", _DOC_RELS)
        z.writestr("word/numbering.xml", _NUMBERING)
        if created or title or creator:
            z.writestr("docProps/core.xml", _core_xml(created, title, creator))
    return buf.getvalue()


def _core_xml(created: str | None, title: str | None, creator: str | None) -> str:
    from xml.sax.saxutils import escape
    parts = ['<?xml version="1.0" encoding="UTF-8"?>'
             '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
             'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
             'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">']
    if title:
        parts.append(f"<dc:title>{escape(title)}</dc:title>")
    if creator:
        parts.append(f"<dc:creator>{escape(creator)}</dc:creator>")
    if created:
        parts.append(f'<dcterms:created xsi:type="dcterms:W3CDTF">{created}</dcterms:created>')
    parts.append("</cp:coreProperties>")
    return "".join(parts)


# --- EPUB ---------------------------------------------------------------------

def epub_bytes(
    chapters: list[tuple[str, str, list[str]]],
    title: str | None = None,
    creator: str | None = None,
    date: str | None = None,
    spine_order: list[int] | None = None,
) -> bytes:
    """An EPUB 2/3 container: mimetype, META-INF/container.xml, one OPF, one
    XHTML file per chapter. `chapters` are (id, heading, paragraphs). The spine
    lists chapters in `spine_order` (indices) when given, so a test can check
    that reading order follows the spine and not the file names.
    """
    import io
    import zipfile
    from xml.sax.saxutils import escape

    order = spine_order if spine_order is not None else list(range(len(chapters)))
    manifest = "".join(
        f'<item id="{cid}" href="{cid}.xhtml" media-type="application/xhtml+xml"/>'
        for cid, _, _ in chapters)
    spine = "".join(f'<itemref idref="{chapters[i][0]}"/>' for i in order)
    meta = ""
    if title:
        meta += f"<dc:title>{escape(title)}</dc:title>"
    if creator:
        meta += f"<dc:creator>{escape(creator)}</dc:creator>"
    if date:
        meta += f"<dc:date>{date}</dc:date>"
    opf = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">'
           f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="uid">x</dc:identifier>{meta}</metadata>'
           f"<manifest>{manifest}</manifest><spine>{spine}</spine></package>")
    container = ('<?xml version="1.0" encoding="UTF-8"?>'
                 '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                 '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                 "</rootfiles></container>")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        for cid, heading, paragraphs in chapters:
            ps = "".join(f"<p>{escape(p)}</p>" for p in paragraphs)
            z.writestr(f"OEBPS/{cid}.xhtml",
                       '<?xml version="1.0" encoding="UTF-8"?><html xmlns="http://www.w3.org/1999/xhtml">'
                       f"<head><title>{escape(heading)}</title><style>p{{margin:0}}</style></head>"
                       f"<body><h1>{escape(heading)}</h1>{ps}</body></html>")
    return buf.getvalue()


# --- FB2 ----------------------------------------------------------------------

def fb2_bytes(sections: list[tuple[str, list[str]]], title: str | None = None) -> bytes:
    """A FictionBook 2 document: `sections` are (title, paragraphs)."""
    from xml.sax.saxutils import escape

    body = "".join(
        f"<section><title><p>{escape(t)}</p></title>" + "".join(f"<p>{escape(p)}</p>" for p in ps) + "</section>"
        for t, ps in sections)
    ti = f"<title-info><book-title>{escape(title)}</book-title></title-info>" if title else ""
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">'
            f"<description>{ti}</description><body>{body}</body></FictionBook>").encode("utf-8")


# --- PPTX ---------------------------------------------------------------------

_P = "http://schemas.openxmlformats.org/presentationml/2006/main"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
# 1x1 PNG, a valid file for a reader that sniffs it
PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c63000100000500010d0a2db40000000049454e44ae426082")


def pptx_bytes(slides: list[dict], swap_order: bool = False) -> bytes:
    """A minimal PresentationML package, slide size 10 x 7.5 in (EMU).

    Each slide dict may carry `title`, `bullets` (list of (level, text)),
    `text` (a free text box's paragraphs), `table` (rows of cells), `notes`,
    `picture` (fraction of the slide's area, 0..1), `logo` (a picture whose
    media file every slide with the key shares), `big` (a one-line text box at
    36 pt, the other text boxes then at 18) and `footer` (a sldNum
    placeholder's text, which the reader drops). `swap_order` lists the slides
    in presentation.xml in reverse, to pin that order comes from there.
    """
    import io
    import zipfile
    from xml.sax.saxutils import escape

    cx, cy = 9144000, 6858000

    def para(text: str, lvl: int = 0, sz: int = 0) -> str:
        ppr = f'<a:pPr lvl="{lvl}"/>' if lvl else ""
        rpr = f'<a:rPr sz="{sz}"/>' if sz else ""
        return f"<a:p>{ppr}<a:r>{rpr}<a:t>{escape(text)}</a:t></a:r></a:p>"

    def sp(ph: str | None, paras: str, idx: int = 1) -> str:
        nvpr = (f'<p:nvPr><p:ph type="{ph}"/></p:nvPr>' if ph not in (None, "")
                else '<p:nvPr><p:ph idx="1"/></p:nvPr>' if ph == "" else "<p:nvPr/>")
        return (f'<p:sp><p:nvSpPr><p:cNvPr id="{idx}" name="s"/><p:cNvSpPr/>{nvpr}</p:nvSpPr>'
                f"<p:spPr/><p:txBody><a:bodyPr/>{paras}</p:txBody></p:sp>")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        ids = []
        for n, s in enumerate(slides, 1):
            shapes, rels = [], []
            if "title" in s:
                shapes.append(sp("title", para(s["title"]), 2))
            if "bullets" in s:
                shapes.append(sp("", "".join(para(t, lvl) for lvl, t in s["bullets"]), 3))
            if "big" in s:  # a title drawn as a free text box, set larger than the rest
                shapes.append(sp(None, para(s["big"], sz=3600), 8))
            if "text" in s:
                shapes.append(sp(None, "".join(para(t, sz=1800 if "big" in s else 0) for t in s["text"]), 4))
            if "footer" in s:
                shapes.append(sp("sldNum", para(s["footer"]), 5))
            if "table" in s:
                rows = "".join("<a:tr>" + "".join(f"<a:tc><a:txBody>{para(c)}</a:txBody></a:tc>" for c in row)
                               + "</a:tr>" for row in s["table"])
                shapes.append('<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="6" name="t"/></p:nvGraphicFramePr>'
                              f"<a:graphic><a:graphicData><a:tbl>{rows}</a:tbl></a:graphicData></a:graphic>"
                              "</p:graphicFrame>")
            if "logo" in s:  # one media file every such slide points at
                shapes.append('<p:pic><p:nvPicPr><p:cNvPr id="9" name="l"/></p:nvPicPr>'
                              '<p:blipFill><a:blip r:embed="rIdLogo"/></p:blipFill><p:spPr><a:xfrm>'
                              f'<a:off x="0" y="0"/><a:ext cx="{cx // 2}" cy="{cy // 2}"/></a:xfrm></p:spPr></p:pic>')
                rels.append(f'<Relationship Id="rIdLogo" Type="{_R}/image" Target="../media/logo.png"/>')
                if "ppt/media/logo.png" not in z.namelist():
                    z.writestr("ppt/media/logo.png", PNG_1X1)
            if "picture" in s:
                side = s["picture"] ** 0.5
                shapes.append('<p:pic><p:nvPicPr><p:cNvPr id="7" name="p"/></p:nvPicPr>'
                              f'<p:blipFill><a:blip r:embed="rIdImg"/></p:blipFill><p:spPr><a:xfrm>'
                              f'<a:off x="0" y="0"/><a:ext cx="{int(cx * side)}" cy="{int(cy * side)}"/>'
                              "</a:xfrm></p:spPr></p:pic>")
                rels.append(f'<Relationship Id="rIdImg" Type="{_R}/image" Target="../media/image{n}.png"/>')
                z.writestr(f"ppt/media/image{n}.png", PNG_1X1)
            if "notes" in s:
                rels.append(f'<Relationship Id="rIdN" Type="{_R}/notesSlide" '
                            f'Target="../notesSlides/notesSlide{n}.xml"/>')
                z.writestr(f"ppt/notesSlides/notesSlide{n}.xml",
                           f'<p:notes xmlns:p="{_P}" xmlns:a="{_A}"><p:cSld><p:spTree>'
                           + sp("sldImg", "", 2) + sp("body", para(s["notes"]), 3)
                           + "</p:spTree></p:cSld></p:notes>")
            z.writestr(f"ppt/slides/slide{n}.xml",
                       f'<p:sld xmlns:p="{_P}" xmlns:a="{_A}" xmlns:r="{_R}"><p:cSld><p:spTree>'
                       + "".join(shapes) + "</p:spTree></p:cSld></p:sld>")
            z.writestr(f"ppt/slides/_rels/slide{n}.xml.rels",
                       f'<Relationships xmlns="{_PKG}">' + "".join(rels) + "</Relationships>")
            ids.append(n)
        order = list(reversed(ids)) if swap_order else ids
        z.writestr("ppt/presentation.xml",
                   f'<p:presentation xmlns:p="{_P}" xmlns:r="{_R}"><p:sldIdLst>'
                   + "".join(f'<p:sldId id="{255 + n}" r:id="rId{n}"/>' for n in order)
                   + f'</p:sldIdLst><p:sldSz cx="{cx}" cy="{cy}"/></p:presentation>')
        z.writestr("ppt/_rels/presentation.xml.rels",
                   f'<Relationships xmlns="{_PKG}">'
                   + "".join(f'<Relationship Id="rId{n}" Type="{_R}/slide" Target="slides/slide{n}.xml"/>'
                             for n in ids) + "</Relationships>")
    return buf.getvalue()
