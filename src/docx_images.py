"""Wire local picture parts and inline DrawingML into the existing OOXML renderer."""

from __future__ import annotations

import base64
import hashlib
from xml.etree import ElementTree as ET

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
P = "http://schemas.openxmlformats.org/package/2006/relationships"
C = "http://schemas.openxmlformats.org/package/2006/content-types"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
D = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
PIC = "http://schemas.openxmlformats.org/drawingml/2006/picture"
IMAGE_REL = R + "/image"


def image_parts(plan, relationships, content_types, names):
    replacements, mapping = {}, {}
    ids = {n.get("Id") for n in relationships}
    for paragraph in plan.paragraphs:
        for image in paragraph.images:
            if image.image_id in mapping:
                continue
            data = base64.b64decode(image.data, validate=True)
            stem = "sheetweave-" + hashlib.sha256(data).hexdigest()[:24]
            part = f"word/media/{stem}.{image.extension}"
            while part in names or part in replacements:
                stem += "-copy"
                part = f"word/media/{stem}.{image.extension}"
            rel_id = "rIdSheetWeaveImage" + str(len(mapping) + 1)
            while rel_id in ids:
                rel_id += "x"
            ids.add(rel_id)
            ET.SubElement(
                relationships,
                f"{{{P}}}Relationship",
                {"Id": rel_id, "Type": IMAGE_REL, "Target": part.removeprefix("word/")},
            )
            ET.SubElement(
                content_types,
                f"{{{C}}}Override",
                {"PartName": "/" + part, "ContentType": image.mime_type},
            )
            replacements[part] = data
            mapping[image.image_id] = rel_id
    return replacements, mapping


def append_picture(paragraph, image, relationship, unique_id, max_width, max_height):
    """Append a line-broken inline picture; no text/list numbering is introduced."""
    scale = min(9525, max_width / image.width, max_height / image.height)
    cx, cy = max(1, int(image.width * scale)), max(1, int(image.height * scale))

    def child(parent, ns, tag, attrs=None):
        return ET.SubElement(parent, f"{{{ns}}}{tag}", attrs or {})

    run = child(paragraph, W, "r")
    child(run, W, "br")
    drawing = child(run, W, "drawing")
    inline = child(drawing, D, "inline", {"distT": "0", "distB": "0", "distL": "0", "distR": "0"})
    child(inline, D, "extent", {"cx": str(cx), "cy": str(cy)})
    child(
        inline,
        D,
        "docPr",
        {"id": str(unique_id), "name": "Source picture " + str(unique_id), "descr": "来源行附图"},
    )
    locks = child(inline, D, "cNvGraphicFramePr")
    child(locks, A, "graphicFrameLocks", {"noChangeAspect": "1"})
    graphic = child(inline, A, "graphic")
    graphic_data = child(graphic, A, "graphicData", {"uri": PIC})
    pic = child(graphic_data, PIC, "pic")
    nonvisual = child(pic, PIC, "nvPicPr")
    child(nonvisual, PIC, "cNvPr", {"id": "0", "name": image.image_id})
    child(nonvisual, PIC, "cNvPicPr")
    fill = child(pic, PIC, "blipFill")
    child(fill, A, "blip", {f"{{{R}}}embed": relationship})
    child(child(fill, A, "stretch"), A, "fillRect")
    shape = child(pic, PIC, "spPr")
    transform = child(shape, A, "xfrm")
    child(transform, A, "off", {"x": "0", "y": "0"})
    child(transform, A, "ext", {"cx": str(cx), "cy": str(cy)})
    child(child(shape, A, "prstGeom", {"prst": "rect"}), A, "avLst")


def validate_images(package, root, relationships, expected_plan):
    """Verify each expected picture is still embedded in its planned paragraph."""
    import posixpath

    links = {n.get("Id"): n for n in relationships} if relationships is not None else {}
    paragraphs = root.findall(f"{{{W}}}body/{{{W}}}p")
    for index, paragraph in enumerate(paragraphs):
        blips = paragraph.findall(f".//{{{A}}}blip")
        expected = (
            expected_plan.paragraphs[index].images
            if expected_plan and index < len(expected_plan.paragraphs)
            else ()
        )
        if expected_plan and len(blips) != len(expected):
            raise ValueError("导出图片数量与已确认报告计划不一致。")
        for offset, blip in enumerate(blips):
            link = links.get(blip.get(f"{{{R}}}embed"))
            if (
                link is None
                or link.get("Type") != IMAGE_REL
                or link.get("TargetMode") == "External"
            ):
                raise ValueError("DOCX 图片关系无效。")
            part = posixpath.normpath(posixpath.join("word", link.get("Target", "")))
            data = package.read(part)
            if expected_plan and data != base64.b64decode(expected[offset].data, validate=True):
                raise ValueError("导出图片内容与来源不一致。")
