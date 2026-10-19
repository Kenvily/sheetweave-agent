"""Validated rule presets for input mapping, content organization, and documents."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import Any

from ..document_style import validate_styles
from ..numbering import validate_numbering

PRESET_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
SUPPORTED_LAYOUTS = {"weekly_matrix", "records"}
SUPPORTED_GROUPINGS = {"row", "project", "section"}


class RulePresetError(ValueError):
    """Raised when a rule preset is missing or invalid."""


@dataclass(frozen=True, slots=True)
class HistoricalCompletionRule:
    """Configure status-based marking for work completed in an earlier week."""

    enabled: bool
    completed_statuses: tuple[str, ...]
    output_text: str


@dataclass(frozen=True, slots=True)
class InputRule:
    """Describe how source columns are resolved."""

    layout: str
    header_rows: int
    required_fields: tuple[str, ...]
    column_aliases: dict[str, tuple[str, ...]]
    historical_completion: HistoricalCompletionRule | None = None
    status_policy: dict = field(default_factory=dict)
    expected_headers: tuple[str, ...] = ()
    sheet_default: str | None = None


@dataclass(frozen=True, slots=True)
class SectionRule:
    """Describe one content section and its source field."""

    id: str
    title: str
    source_field: str
    group_by: str
    llm_editable: bool
    source_fields: tuple[str, ...] = ()
    heading_path: tuple[str, ...] = ()
    heading_fields: tuple[str, ...] = ()
    match: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DocumentRule:
    """Describe report-level document settings."""

    report_title: str
    template: str
    heading_start_default: int
    styles: dict = field(default_factory=validate_styles)
    numbering: dict = field(default_factory=validate_numbering)


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
    allowed = {"id", "name", "schema_version", "input", "sections", "document"}
    if set(payload) - allowed:
        raise RulePresetError("规则包含不支持的顶层配置。")
    preset_id = _string(payload.get("id"), "id")
    if not PRESET_ID_PATTERN.fullmatch(preset_id):
        raise RulePresetError("规则 id 只能使用小写字母、数字、下划线或短横线。")
    name = _string(payload.get("name"), "name")
    if payload.get("schema_version") != 1:
        raise RulePresetError("仅支持 schema_version=1 的规则。")

    input_data = _object(payload.get("input"), "input")
    if set(input_data) - {
        "layout",
        "header_rows",
        "required_fields",
        "column_aliases",
        "historical_completion",
        "status_policy",
        "expected_headers",
        "sheet_default",
    }:
        raise RulePresetError("输入规则包含不支持的配置。")
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

    historical_completion = None
    raw_history_rule = input_data.get("historical_completion")
    if raw_history_rule is not None:
        history_rule = _object(raw_history_rule, "input.historical_completion")
        enabled = history_rule.get("enabled", False)
        if not isinstance(enabled, bool):
            raise RulePresetError("input.historical_completion.enabled 必须是布尔值。")
        completed_statuses = _string_list(
            history_rule.get("completed_statuses", []),
            "input.historical_completion.completed_statuses",
            allow_empty=True,
        )
        output_text = _string(
            history_rule.get("output_text", "已完成"),
            "input.historical_completion.output_text",
        )
        if enabled and not completed_statuses:
            raise RulePresetError("启用历史完成标记时必须配置完成状态。")
        if enabled and layout != "weekly_matrix":
            raise RulePresetError("历史完成标记仅支持包含周次列的 weekly_matrix 输入布局。")
        if enabled and "status" not in aliases:
            raise RulePresetError("启用历史完成标记时必须配置 status 列别名。")
        historical_completion = HistoricalCompletionRule(
            enabled=enabled,
            completed_statuses=completed_statuses,
            output_text=output_text,
        )

    required_canonical = {"project", "content"} if layout == "records" else {"project"}
    if not required_canonical <= set(required_fields):
        raise RulePresetError("当前布局须声明 project/content 等规范必填字段。")

    raw_sections = payload.get("sections")
    if not isinstance(raw_sections, list) or not raw_sections:
        raise RulePresetError("规则至少需要一个 sections 项。")
    sections: list[SectionRule] = []
    for index, raw_section in enumerate(raw_sections, start=1):
        section = _object(raw_section, f"sections[{index}]")
        if set(section) - {
            "id",
            "title",
            "source_field",
            "source_fields",
            "heading_path",
            "heading_fields",
            "match",
            "group_by",
            "llm_editable",
        }:
            raise RulePresetError("内容组包含不支持的配置。")
        section_id = _string(section.get("id"), f"sections[{index}].id")
        if not PRESET_ID_PATTERN.fullmatch(section_id):
            raise RulePresetError(f"章节 id 格式无效：{section_id}。")
        source_field = _string(section.get("source_field"), f"sections[{index}].source_field")
        if source_field not in aliases and source_field != "progress":
            raise RulePresetError(f"章节 {section_id} 引用了未知字段：{source_field}。")
        group_by = _string(section.get("group_by", "row"), f"sections[{index}].group_by")
        if group_by not in SUPPORTED_GROUPINGS:
            raise RulePresetError(f"章节 {section_id} 的 group_by 无效：{group_by}。")
        llm_editable = section.get("llm_editable", source_field in {"progress", "content"})
        if not isinstance(llm_editable, bool):
            raise RulePresetError(f"章节 {section_id} 的 llm_editable 必须是布尔值。")
        source_fields = _string_list(
            section.get("source_fields", []), "source_fields", allow_empty=True
        )
        source_fields = source_fields or (source_field,)
        if any(value not in aliases and value != "progress" for value in source_fields):
            raise RulePresetError("内容组引用了未知字段。")
        if source_fields[0] != source_field:
            raise RulePresetError("source_field 必须是 source_fields 的第一项。")
        raw_path = section.get("heading_path", [])
        if not isinstance(raw_path, list):
            raise RulePresetError("heading_path 必须是目录数组。")
        path = tuple(_string(title, "heading_path") for title in raw_path)
        if path and not 1 <= len(path) <= 3:
            raise RulePresetError("报告目录支持1～3级标题。")
        heading_fields = section.get("heading_fields", [])
        if (
            not isinstance(heading_fields, list)
            or (heading_fields and len(heading_fields) != len(path))
            or any(
                not isinstance(key, str) or (key and key not in aliases) for key in heading_fields
            )
        ):
            raise RulePresetError("标题来源须与目录层级对应，且只能引用已映射字段。")
        match = section.get("match", {})
        if not isinstance(match, dict) or set(match) - set(aliases):
            raise RulePresetError("来源匹配包含未知字段。")
        for values in match.values():
            _string_list(values, "来源匹配值")
        sections.append(
            SectionRule(
                id=section_id,
                title=_string(section.get("title"), f"sections[{index}].title"),
                source_field=source_field,
                group_by=group_by,
                llm_editable=llm_editable,
                source_fields=source_fields,
                heading_path=path,
                heading_fields=tuple(heading_fields),
                match=match,
            )
        )
    if any(item.heading_path for item in sections) and not all(
        item.heading_path for item in sections
    ):
        raise RulePresetError("自定义目录不能与旧章节规则混用。")
    section_ids = [section.id for section in sections]
    if len(set(section_ids)) != len(section_ids):
        raise RulePresetError("章节 id 不能重复。")

    document_data = _object(payload.get("document"), "document")
    if set(document_data) - {
        "report_title",
        "template",
        "heading_start_default",
        "styles",
        "numbering",
    }:
        raise RulePresetError("文档规则包含不支持的格式/编号配置。")
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

    policy = input_data.get("status_policy", {})
    if not isinstance(policy, dict) or set(policy) - {"include", "known", "unknown"}:
        raise RulePresetError("status_policy 只支持 include、known、unknown。")
    if policy:
        for key in ("include", "known"):
            _string_list(policy.get(key, []), f"status_policy.{key}", allow_empty=True)
        if policy.get("unknown", "keep") != "keep":
            raise RulePresetError("未知状态必须保留（unknown=keep），不可静默删除。")
        if not set(policy.get("include", [])) <= set(policy.get("known", [])):
            raise RulePresetError("保留状态必须包含在 known 中。")
        if "status" not in aliases:
            raise RulePresetError("状态筛选必须映射 status 字段。")
    expected_headers = _string_list(
        input_data.get("expected_headers", []), "input.expected_headers", allow_empty=True
    )
    try:
        styles = validate_styles(document_data.get("styles"))
    except ValueError as exc:
        raise RulePresetError(str(exc)) from exc
    sheet_default = input_data.get("sheet_default")
    if sheet_default is not None:
        sheet_default = _string(sheet_default, "input.sheet_default")
    return RulePreset(
        id=preset_id,
        name=name,
        schema_version=1,
        input=InputRule(
            layout=layout,
            header_rows=header_rows,
            required_fields=required_fields,
            column_aliases=aliases,
            historical_completion=historical_completion,
            status_policy=policy,
            expected_headers=expected_headers,
            sheet_default=sheet_default,
        ),
        sections=tuple(sections),
        document=DocumentRule(
            report_title=_string(document_data.get("report_title"), "document.report_title"),
            template=template,
            heading_start_default=heading_start,
            styles=styles,
            numbering=validate_numbering(document_data.get("numbering")),
        ),
    )


def load_rule_preset(preset_id: str = "rd3", rules_dir: Path | None = None) -> RulePreset:
    """Load and validate a named preset from a controlled directory."""
    if not PRESET_ID_PATTERN.fullmatch(preset_id):
        raise RulePresetError("规则 id 格式无效。")
    if rules_dir is None and preset_id.startswith("p_"):
        from ..profiles import ProfileStore

        return ProfileStore.from_environment().load(preset_id)[0]
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


def preset_payload(preset: RulePreset) -> dict:
    """Freeze an independent, validated JSON-compatible rule payload."""
    return json.loads(json.dumps(asdict(preset), ensure_ascii=False))
