"""Read embedded XLSX pictures without loading drawings through an optional image library."""

from __future__ import annotations

import base64
import hashlib
import posixpath
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from .models import SourceImage

S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
X = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 24 * 1024 * 1024


def _read(package, name, limit):
    info = package.getinfo(name)
    if info.file_size > limit:
        raise ValueError("Excel 图片或绘图部件过大，请压缩后重试。")
    return package.read(info)


def _relationships(package, part):
    name = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
    if name not in package.namelist():
        return {}
    result = {}
    for item in ET.fromstring(_read(package, name, 2 * 1024 * 1024)):
        if item.get("TargetMode") == "External":
            continue
        target = item.get("Target", "")
        path = posixpath.normpath(posixpath.join(posixpath.dirname(part), target))
        if target.startswith("/"):
            path = target.lstrip("/")
        if path.startswith("../") or "\\" in path:
            raise ValueError("Excel 图片关系路径无效。")
        result[item.get("Id")] = path
    return result


def _dimensions(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return "png", int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data.startswith(b"\xff\xd8"):
        offset = 2
        while offset + 4 <= len(data):
            if data[offset] != 255:
                break
            marker = data[offset + 1]
            offset += 2
            if marker == 255:
                offset -= 1
                continue
            if marker in {0xD8, 0xD9, 0x01}:
                continue
            length = int.from_bytes(data[offset : offset + 2], "big")
            if length < 2:
                break
            if marker in {0xC0, 0xC1, 0xC2} and offset + 7 <= len(data):
                return (
                    "jpg",
                    int.from_bytes(data[offset + 5 : offset + 7], "big"),
                    int.from_bytes(data[offset + 3 : offset + 5], "big"),
                )
            offset += length
    raise ValueError("暂仅支持有效的 PNG/JPEG 内嵌图片。")


def read_excel_images(
    path: Path, sheet: str
) -> tuple[dict[int, tuple[SourceImage, ...]], tuple[str, ...]]:
    """Associate each picture with its top-left anchor; never fetch external resources."""
    result, warnings, total = {}, [], 0
    try:
        with zipfile.ZipFile(path) as package:
            workbook = ET.fromstring(_read(package, "xl/workbook.xml", 2 * 1024 * 1024))
            links = _relationships(package, "xl/workbook.xml")
            node = next(
                n for n in workbook.findall(f"{{{S}}}sheets/{{{S}}}sheet") if n.get("name") == sheet
            )
            part = links[node.get(f"{{{R}}}id")]
            sheet_xml = ET.fromstring(_read(package, part, 32 * 1024 * 1024))
            sheet_links = _relationships(package, part)
            if "xl/cellimages.xml" in package.namelist() or any(
                n.startswith("xl/richData/") for n in package.namelist()
            ):
                warnings.append("检测到特殊单元格图片，请改为插入普通内嵌 PNG/JPEG 图片后生成。")
            for drawing in sheet_xml.findall(f"{{{S}}}drawing"):
                drawing_part = sheet_links.get(drawing.get(f"{{{R}}}id"))
                if not drawing_part:
                    warnings.append("外部链接图片未读取，请将图片嵌入 Excel。")
                    continue
                drawing_xml = ET.fromstring(_read(package, drawing_part, 2 * 1024 * 1024))
                image_links = _relationships(package, drawing_part)
                for index, anchor in enumerate(drawing_xml):
                    pictures = anchor.findall(f"{{{X}}}pic")
                    if not pictures:
                        continue
                    origin = anchor.find(f"{{{X}}}from")
                    if origin is None:
                        warnings.append("无单元格锚点的图片未导入，请将图片放置在对应数据行。")
                        continue
                    row = int(origin.findtext(f"{{{X}}}row")) + 1
                    column = int(origin.findtext(f"{{{X}}}col"))
                    for picture in pictures:
                        blip = picture.find(f".//{{{A}}}blip")
                        target = (
                            image_links.get(blip.get(f"{{{R}}}embed")) if blip is not None else None
                        )
                        if not target:
                            warnings.append(f"第 {row} 行外部链接图片未读取，请使用内嵌图片。")
                            continue
                        data = _read(package, target, MAX_IMAGE_BYTES)
                        total += len(data)
                        if total > MAX_TOTAL_BYTES or sum(map(len, result.values())) >= 100:
                            raise ValueError("Excel 图片总量过大，请减少图片后重试。")
                        try:
                            extension, width, height = _dimensions(data)
                        except ValueError:
                            warnings.append(f"第 {row} 行包含不支持或损坏的图片，仅支持 PNG/JPEG。")
                            continue
                        if not 0 < width <= 50000 or not 0 < height <= 50000:
                            raise ValueError("Excel 图片尺寸无效。")
                        extent = anchor.find(f"{{{X}}}ext")
                        if extent is None:
                            extent = picture.find(f".//{{{A}}}xfrm/{{{A}}}ext")
                        if extent is not None:
                            width = max(1, int(extent.get("cx", "0")) // 9525)
                            height = max(1, int(extent.get("cy", "0")) // 9525)
                        identity = hashlib.sha256(
                            f"{sheet}:{row}:{column}:{index}:".encode() + data
                        ).hexdigest()[:24]
                        image = SourceImage(
                            identity,
                            base64.b64encode(data).decode("ascii"),
                            extension,
                            width,
                            height,
                            column,
                        )
                        result.setdefault(row, []).append(image)
    except (KeyError, ET.ParseError, StopIteration, TypeError) as exc:
        raise ValueError("Excel 图片结构无效，请重新保存文件后重试。") from exc
    return {row: tuple(images) for row, images in result.items()}, tuple(dict.fromkeys(warnings))
