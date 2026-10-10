"""Validated rule presets for input mapping, content organization, and documents."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Any

PRESET_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
SUPPORTED_LAYOUTS = {"weekly_matrix", "records"}
SUPPORTED_GROUPINGS = {"row", "project"}


class RulePresetError(ValueError):
    """Raised when a rule preset is missing or invalid."""


@dataclass(frozen=True, slots=True)
class InputRule:
    """Describe how source columns are resolved."""

    layout: str
    header_rows: int
    required_fields: tuple[str, ...]
    column_aliases: dict[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class SectionRule:
    """Describe one content section and its source field."""

    id: str
    title: str
    source_field: str
    group_by: str


@dataclass(frozen=True, slots=True)
class DocumentRule:
    """Describe report-level document settings."""

    report_title: str
    template: str
    heading_start_default: int


@dataclass(frozen=True, slots=True)
class RulePreset:
    """A validated deterministic weekly-report rule preset."""

    id: str
    name: str
    schema_version: int
    input: InputRule
    sections: tuple[SectionRule, ...]
    document: DocumentRule


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RulePresetError(f"规则字段“{label}”必须是对象。")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RulePresetError(f"规则字段“{label}”必须是非空字符串。")
    return value.strip()


def _string_list(value: Any, label: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (not value and not allow_empty):
        raise RulePresetError(f"规则字段“{label}”必须是字符串数组。")
    result = tuple(_string(item, label) for item in value)
    if len(set(result)) != len(result):
        raise RulePresetError(f"规则字段“{label}”不能包含重复值。")
    return result


def validate_rule_preset(payload: dict[str, Any]) -> RulePreset:
    """Validate a JSON-compatible mapping and return an immutable preset."""
    preset_id = _string(payload.get("id"), "id")
    if not PRESET_ID_PATTERN.fullmatch(preset_id):
        raise RulePresetError("规则 id 只能使用小写字母、数字、下划线或短横线。")
    name = _string(payload.get("name"), "name")
    if payload.get("schema_version") != 1:
        raise RulePresetError("仅支持 schema_version=1 的规则。")

    input_data = _object(payload.get("input"), "input")
    layout = _string(input_data.get("layout"), "input.layout")
    if layout not in SUPPORTED_LAYOUTS:
        raise RulePresetError(f"不支持的输入布局：{layout}。")
    header_rows = input_data.get("header_rows")
    if not isinstance(header_rows, int) or not 1 <= header_rows <= 10:
        raise RulePresetError("input.header_rows 必须是 1～10 的整数。")
    aliases_data = _object(input_data.get("column_aliases"), "input.column_aliases")
    aliases = {
        _string(field, "input.column_aliases 字段名"): _string_list(
            values, f"input.column_aliases.{field}"
        )
        for field, values in aliases_data.items()
    }
    required_fields = _string_list(input_data.get("required_fields"), "input.required_fields")
    missing_aliases = [field for field in required_fields if field not in aliases]
    if missing_aliases:
        raise RulePresetError(f"必填字段缺少列别名：{'、'.join(missing_aliases)}。")

    raw_sections = payload.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections:
        raise RulePresetError("规则至少需要一个 sections 项。")
    sections: list[SectionRule] = []
    for index, raw_section in enumerate(raw_sections, start=1):
        section = _object(raw_section, f"sections[{index}]")
        section_id = _string(section.get("id"), f"sections[{index}].id")
        if not PRESET_ID_PATTERN.fullmatch(section_id):
            raise RulePresetError(f"章节 id 格式无效：{section_id}。")
        source_field = _string(section.get("source_field"), f"sections[{index}].source_field")
        if source_field not in aliases and source_field != "progress":
            raise RulePresetError(f"章节 {section_id} 引用了未知字段：{source_field}。")
        group_by = _string(section.get("group_by", "row"), f"sections[{index}].group_by")
        if group_by not in SUPPORTED_GROUPINGS:
            raise RulePresetError(f"章节 {section_id} 的 group_by 无效：{group_by}。")
        sections.append(
            SectionRule(
                id=section_id,
                title=_string(section.get("title"), f"sections[{index}].title"),
                source_field=source_field,
                group_by=group_by,
            )
        )
    section_ids = [section.id for section in sections]
    if len(set(section_ids)) != len(section_ids):
        raise RulePresetError("章节 id 不能重复。")

    document_data = _object(payload.get("document"), "document")
    template = _string(document_data.get("template"), "document.template")
    template_path = PurePosixPath(template)
    unsafe_template = (
        "\\" in template
        or template_path.is_absolute()
        or ".." in template_path.parts
        or template_path.suffix.lower() != ".docx"
    )
    if unsafe_template:
        raise RulePresetError("document.template 必须是受控目录内的相对 .docx 路径。")
    heading_start = document_data.get("heading_start_default")
    if not isinstance(heading_start, int) or not 1 <= heading_start <= 999:
        raise RulePresetError("document.heading_start_default 必须在 1～999 之间。")
    if heading_start + len(sections) - 1 > 999:
        raise RulePresetError("默认起始章节号与章节数量超出 999。")
    if any(section.group_by == "project" for section in sections) and "project" not in aliases:
        raise RulePresetError("按项目分组的规则必须定义 project 字段别名。")

    return RulePreset(
        id=preset_id,
        name=name,
        schema_version=1,
        input=InputRule(layout, header_rows, required_fields, aliases),
        sections=tuple(sections),
        document=DocumentRule(
            report_title=_string(document_data.get("report_title"), "document.report_title"),
            template=template,
            heading_start_default=heading_start,
        ),
    )


def load_rule_preset(preset_id: str = "rd3", rules_dir: Path | None = None) -> RulePreset:
    """Load and validate a named preset from a controlled directory."""
    if not PRESET_ID_PATTERN.fullmatch(preset_id):
        raise RulePresetError("规则 id 格式无效。")
    try:
        if rules_dir is None:
            text = files(__package__).joinpath(f"{preset_id}.json").read_text(encoding="utf-8")
        else:
            root = rules_dir.resolve()
            path = (root / f"{preset_id}.json").resolve()
            if path.parent != root:
                raise RulePresetError("规则路径超出受控目录。")
            text = path.read_text(encoding="utf-8")
        payload = json.loads(text)
    except FileNotFoundError as exc:
        raise RulePresetError(f"找不到规则预设：{preset_id}。") from exc
    except json.JSONDecodeError as exc:
        raise RulePresetError(f"规则 JSON 无效：第 {exc.lineno} 行第 {exc.colno} 列。") from exc
    if not isinstance(payload, dict):
        raise RulePresetError("规则 JSON 根节点必须是对象。")
    preset = validate_rule_preset(payload)
    if preset.id != preset_id:
        raise RulePresetError(f"规则文件名与内部 id 不一致：{preset_id} != {preset.id}。")
    return preset


def list_rule_presets(rules_dir: Path | None = None) -> tuple[RulePreset, ...]:
    """Return all valid built-in or configured presets in stable name order."""
    if rules_dir is None:
        names = sorted(
            item.name for item in files(__package__).iterdir() if item.name.endswith(".json")
        )
        return tuple(load_rule_preset(Path(name).stem) for name in names)
    root = rules_dir.resolve()
    return tuple(load_rule_preset(path.stem, root) for path in sorted(root.glob("*.json")))
