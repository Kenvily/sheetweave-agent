"""Validated paragraph format contracts shared by rendering and validation."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace

from .models import ParagraphKind


@dataclass(frozen=True, slots=True)
class ParagraphStyle:
    font: str
    size_pt: float
    bold: bool | None
    line_spacing: float = 1.5
    before_pt: float = 0
    after_pt: float = 0
    first_line_chars: float = 0

    def attributes(self) -> dict[str, str]:
        """Return OOXML paragraph spacing in twips and character hundredths."""
        return {
            "line": str(round(self.line_spacing * 240)),
            "lineRule": "auto",
            "before": str(round(self.before_pt * 20)),
            "after": str(round(self.after_pt * 20)),
        }

    def indent_attributes(self) -> dict[str, str]:
        """Provide character indentation plus an equivalent twip fallback for DOCX readers."""
        return {
            "firstLineChars": str(round(self.first_line_chars * 100)),
            "firstLine": str(round(self.first_line_chars * self.size_pt * 20)),
        }


def default_styles() -> dict[str, dict]:
    """Return independent JSON-compatible copies of the original report contract."""
    result = {}
    for kind in ParagraphKind:
        heading = kind.value.startswith("heading")
        body = kind.value.startswith("body")
        size = {"title": 16, "date": 21, "heading_1": 16, "heading_2": 15, "heading_3": 14}.get(
            kind.value, 10.5
        )
        result[kind.value] = asdict(
            ParagraphStyle(
                "Microsoft YaHei" if heading or kind is ParagraphKind.TITLE else "SimSun",
                size,
                not body,
                before_pt=6 if heading else 0,
                after_pt=6 if heading else 0,
                first_line_chars=4 if kind is ParagraphKind.BODY_3 else 2 if body else 0,
            )
        )
    return result


def validate_styles(payload: object = None) -> dict[str, dict]:
    """Reject unsupported roles/properties rather than silently ignoring them."""
    result = default_styles()
    if payload is None:
        return result
    if not isinstance(payload, dict) or set(payload) - set(result):
        raise ValueError("document.styles 包含不支持的段落角色。")
    limits = {
        "size_pt": (5, 72),
        "line_spacing": (1, 3),
        "before_pt": (0, 100),
        "after_pt": (0, 100),
        "first_line_chars": (0, 10),
    }
    for role, values in payload.items():
        if not isinstance(values, dict) or set(values) - set(result[role]):
            raise ValueError(f"段落 {role} 包含不支持的格式属性。")
        style = result[role] | values
        if not isinstance(style["font"], str) or not 1 <= len(style["font"].strip()) <= 100:
            raise ValueError(f"段落 {role} 必须明确字体。")
        if style["bold"] is not None and type(style["bold"]) is not bool:
            raise ValueError("bold 只能是 true、false 或 null（不约束）。")
        for key, (low, high) in limits.items():
            if key == "first_line_chars" and role == "body_3":
                high = 12  # A second-level indent up to 10 plus two characters.
            value = style[key]
            if type(value) not in (int, float) or not low <= value <= high:
                raise ValueError(f"段落 {role} 的 {key} 超出支持范围 {low}～{high}。")
            scale = {
                "size_pt": 2,
                "line_spacing": 240,
                "before_pt": 20,
                "after_pt": 20,
                "first_line_chars": 100,
            }[key]
            if abs(value * scale - round(value * scale)) > 1e-6:
                raise ValueError(f"段落 {role} 的 {key} 无法精确表示为 OOXML 单位。")
        result[role] = style
    result["body_3"]["first_line_chars"] = max(
        result["body_3"]["first_line_chars"], result["body_2"]["first_line_chars"] + 2
    )
    return result


def paragraph_style(kind: ParagraphKind, styles: dict | None = None) -> ParagraphStyle:
    """Resolve a fully validated paragraph role."""
    values = styles or default_styles()
    style = ParagraphStyle(**values[kind.value])
    if kind is ParagraphKind.BODY_3:
        # Existing profiles may have identical body indents: apply the hierarchy at rendering time.
        minimum = values[ParagraphKind.BODY_2.value]["first_line_chars"] + 2
        style = replace(style, first_line_chars=max(style.first_line_chars, minimum))
    return style
