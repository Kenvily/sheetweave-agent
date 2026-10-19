"""Native Word numbering remains editable without changing confirmed report text."""

from dataclasses import replace
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pytest

from weekly_report_agent.docx_renderer import render_docx
from weekly_report_agent.models import ParagraphKind, ReportParagraph, ReportPlan
from weekly_report_agent.numbering import renumber, validate_numbering
from weekly_report_agent.validator import ReportValidationError, validate_report
from weekly_report_agent.word_lists import BODY_KINDS, NUMBERING_REL, qn, read_lists

TEMPLATE = Path("data/研发三部周报模板-v2.docx")


def plan():
    entries = [
        ("title", "数据科学产品部周报"),
        ("date", "20261019"),
        ("heading_1", "9. 大模型产品研发"),
        ("heading_2", "9.1. 合成项目"),
        ("heading_3", "9.1.1. 合成任务"),
        ("body_1", "1、一级甲："),
        ("body_2", "（1）二级甲："),
        ("body_3", "①三级甲，AUC 0.81。"),
        ("body_3", "②三级乙。"),
        ("body_2", "（2）二级乙："),
        ("body_3", "①三级丙。"),
        ("body_1", "2、一级乙："),
        ("body_2", "（1）二级丙："),
        ("body_3", "①三级丁。"),
        ("heading_3", "9.1.2. 其他任务"),
        ("body_1", "1、新章节。"),
    ]
    return ReportPlan(
        date(2026, 10, 19),
        "合成输入",
        9,
        2,
        tuple(ReportParagraph(text, ParagraphKind(role)) for role, text in entries),
    )


def parts(path):
    with ZipFile(path) as package:
        return {n: package.read(n) for n in package.namelist()}


def write_parts(path, content):
    with ZipFile(path, "w") as package:
        for name, value in content.items():
            package.writestr(name, value)


def test_native_lists_share_levels_restart_and_preserve_body_facts(tmp_path):
    output = tmp_path / "report.docx"
    expected = plan()
    render_docx(expected, TEMPLATE, output)
    validate_report(output, expected_plan=expected)
    content = parts(output)
    assert b"<Types xmlns=" in content["[Content_Types].xml"]
    assert b"<Relationships xmlns=" in content["word/_rels/document.xml.rels"]
    document = ET.fromstring(content["word/document.xml"])
    paragraphs = document.findall(f"{qn('body')}/{qn('p')}")
    body = [p for p in paragraphs if p.find(f"{qn('pPr')}/{qn('numPr')}") is not None]
    num_ids = [p.find(f"{qn('pPr')}/{qn('numPr')}/{qn('numId')}").get(qn("val")) for p in body]
    assert len(set(num_ids[:-1])) == 1
    assert num_ids[-1] != num_ids[0]  # A new heading starts at 1 again.
    assert [p.find(f"{qn('pPr')}/{qn('numPr')}/{qn('ilvl')}").get(qn("val")) for p in body] == [
        "0",
        "1",
        "2",
        "2",
        "1",
        "2",
        "0",
        "1",
        "2",
        "0",
    ]
    assert ["".join(n.text or "" for n in p.iter(qn("t"))) for p in body][:4] == [
        "一级甲：",
        "二级甲：",
        "三级甲，AUC 0.81。",
        "三级乙。",
    ]
    # Direct formatting must not pin a paragraph to its previous list level.
    assert all(p.find(f"{qn('pPr')}/{qn('ind')}") is None for p in body)
    assert all(p.find(f"{qn('r')}/{qn('rPr')}") is None for p in body)
    numbering = ET.fromstring(content["word/numbering.xml"])
    levels = numbering.find(qn("abstractNum")).findall(qn("lvl"))
    assert [n.find(qn("lvlText")).get(qn("val")) for n in levels] == ["%1、", "（%2）", "%3"]
    assert levels[2].find(qn("numFmt")).get(qn("val")) == "decimalEnclosedCircle"
    styles = ET.fromstring(content["word/styles.xml"])
    definitions = {n.get(qn("styleId")): n for n in styles.findall(qn("style"))}
    resolved = read_lists(paragraphs, numbering, definitions)
    assert [resolved[i][0] for i in resolved] == [
        p.text for p in expected.paragraphs if p.kind in BODY_KINDS
    ]


def test_reusing_export_as_template_preserves_old_numbering(tmp_path):
    first, second = tmp_path / "first.docx", tmp_path / "second.docx"
    expected = plan()
    render_docx(expected, TEMPLATE, first)
    render_docx(expected, first, second)
    old = ET.fromstring(parts(first)["word/numbering.xml"])
    new = ET.fromstring(parts(second)["word/numbering.xml"])
    for tag, key in (("abstractNum", "abstractNumId"), ("num", "numId")):
        previous = {n.get(qn(key)): ET.tostring(n) for n in old.findall(qn(tag))}
        current = {n.get(qn(key)): ET.tostring(n) for n in new.findall(qn(tag))}
        assert len(current) > len(previous)
        assert all(current[k] == value for k, value in previous.items())
    validate_report(second, expected_plan=expected)


def test_directories_do_not_share_list_definitions_or_style_numbering(tmp_path):
    output = tmp_path / "isolated.docx"
    render_docx(plan(), TEMPLATE, output)
    content = parts(output)
    root = ET.fromstring(content["word/document.xml"])
    numbers = ET.fromstring(content["word/numbering.xml"])
    definitions = ET.fromstring(content["word/styles.xml"])
    styles = {n.get(qn("styleId")): n for n in definitions.findall(qn("style"))}
    instances = {n.get(qn("numId")): n for n in numbers.findall(qn("num"))}
    abstracts = {n.get(qn("abstractNumId")): n for n in numbers.findall(qn("abstractNum"))}
    families = {}
    for paragraph in root.findall(f"{qn('body')}/{qn('p')}"):
        pp = paragraph.find(qn("pPr"))
        np = pp.find(qn("numPr"))
        if np is None:
            continue
        num_id = np.find(qn("numId")).get(qn("val"))
        sid = pp.find(qn("pStyle")).get(qn("val"))
        # Reapplying a style in Word must not rebind this directory to the first one.
        assert styles[sid].find(f"{qn('pPr')}/{qn('numPr')}/{qn('numId')}").get(qn("val")) == num_id
        abstract_id = instances[num_id].find(qn("abstractNumId")).get(qn("val"))
        families.setdefault(num_id, (abstract_id, set()))[1].add(sid)
    assert len(families) == 2
    first, second = list(families.values())
    assert first[0] != second[0]
    assert first[1].isdisjoint(second[1])
    assert len({n.find(qn("nsid")).get(qn("val")) for n in abstracts.values()}) == 2
    other_definition = ET.tostring(abstracts[second[0]])
    other_styles = {sid: ET.tostring(styles[sid]) for sid in second[1]}
    third = abstracts[first[0]].findall(qn("lvl"))[2]
    third.find(qn("suff")).set(qn("val"), "space")
    assert ET.tostring(abstracts[second[0]]) == other_definition
    assert {sid: ET.tostring(styles[sid]) for sid in second[1]} == other_styles


def test_report_without_body_keeps_usable_body_list_styles(tmp_path):
    output = tmp_path / "headings.docx"
    expected = replace(
        plan(), paragraphs=tuple(p for p in plan().paragraphs if p.kind not in BODY_KINDS)
    )
    render_docx(expected, TEMPLATE, output)
    validate_report(output, expected_plan=expected)
    content = parts(output)
    definitions = ET.fromstring(content["word/styles.xml"])
    numbers = ET.fromstring(content["word/numbering.xml"])
    ids = {n.get(qn("numId")) for n in numbers.findall(qn("num"))}
    styles = [n for n in definitions if n.get(qn("styleId"), "").startswith("DocAgentBody")]
    assert len(styles) == 3
    assert all(
        n.find(f"{qn('pPr')}/{qn('numPr')}/{qn('numId')}").get(qn("val")) in ids for n in styles
    )


@pytest.mark.parametrize("choice", [0, 1, 2])
def test_native_lists_honor_all_scheme_body_number_formats(tmp_path, choice):
    from weekly_report_agent.numbering import CHOICES

    numbering = validate_numbering({kind.value: CHOICES[kind.value][choice] for kind in BODY_KINDS})
    expected = renumber(plan(), numbering)
    output = tmp_path / "custom.docx"
    render_docx(expected, TEMPLATE, output, numbering=numbering)
    validate_report(output, numbering=numbering, expected_plan=expected)


def test_inserting_an_unprefixed_paragraph_advances_native_numbers(tmp_path):
    output = tmp_path / "editable.docx"
    expected = plan()
    render_docx(expected, TEMPLATE, output)
    content = parts(output)
    root = ET.fromstring(content["word/document.xml"])
    body = root.find(qn("body"))
    inserted = ET.fromstring(ET.tostring(body[8]))
    inserted.find(f"{qn('r')}/{qn('t')}").text = "插入的明细。"
    body.insert(9, inserted)
    content["word/document.xml"] = ET.tostring(root)
    write_parts(output, content)
    paragraphs = list(expected.paragraphs)
    paragraphs.insert(9, ReportParagraph("③插入的明细。", ParagraphKind.BODY_3))
    validate_report(output, expected_plan=replace(expected, paragraphs=tuple(paragraphs)))


@pytest.mark.parametrize(
    "fault",
    [
        "missing_part",
        "missing_relationship",
        "wrong_content_type",
        "missing_numpr",
        "missing_style",
        "wrong_level",
        "wrong_numid",
        "wrong_format",
        "wrong_restart",
        "wrong_start",
        "duplicate_prefix",
        "wrong_style_level",
        "wrong_style_numid",
        "wrong_indent",
    ],
)
def test_validation_rejects_broken_native_numbering(tmp_path, fault):
    output = tmp_path / "corrupt.docx"
    expected = plan()
    render_docx(expected, TEMPLATE, output)
    content = parts(output)
    doc = ET.fromstring(content["word/document.xml"])
    pp = doc.find(qn("body"))[7].find(qn("pPr"))
    numbers = ET.fromstring(content["word/numbering.xml"])
    lvl = numbers.find(qn("abstractNum")).findall(qn("lvl"))[2]
    if fault == "missing_part":
        del content["word/numbering.xml"]
    elif fault == "missing_relationship":
        rels = ET.fromstring(content["word/_rels/document.xml.rels"])
        rels.remove(next(n for n in rels if n.get("Type") == NUMBERING_REL))
        content["word/_rels/document.xml.rels"] = ET.tostring(rels)
    elif fault == "wrong_content_type":
        types = ET.fromstring(content["[Content_Types].xml"])
        next(n for n in types if n.get("PartName") == "/word/numbering.xml").set(
            "ContentType", "wrong"
        )
        content["[Content_Types].xml"] = ET.tostring(types)
    elif fault == "missing_numpr":
        pp.remove(pp.find(qn("numPr")))
    elif fault == "missing_style":
        pp.remove(pp.find(qn("pStyle")))
    elif fault == "wrong_level":
        pp.find(f"{qn('numPr')}/{qn('ilvl')}").set(qn("val"), "1")
    elif fault == "wrong_numid":
        pp.find(f"{qn('numPr')}/{qn('numId')}").set(qn("val"), "999999")
    elif fault == "wrong_format":
        lvl.find(qn("numFmt")).set(qn("val"), "decimal")
    elif fault == "wrong_restart":
        lvl.find(qn("lvlRestart")).set(qn("val"), "0")
    elif fault == "wrong_start":
        numbers.find(f"{qn('num')}/{qn('lvlOverride')}/{qn('startOverride')}").set(qn("val"), "5")
    elif fault == "duplicate_prefix":
        doc.find(qn("body"))[7].find(f"{qn('r')}/{qn('t')}").text = "①三级甲，AUC 0.81。"
    elif fault in {"wrong_style_level", "wrong_style_numid"}:
        definitions = ET.fromstring(content["word/styles.xml"])
        sid = pp.find(qn("pStyle")).get(qn("val"))
        style = next(n for n in definitions if n.get(qn("styleId")) == sid)
        np = style.find(f"{qn('pPr')}/{qn('numPr')}")
        if fault == "wrong_style_level":
            np.find(qn("ilvl")).set(qn("val"), "0")
        else:
            np.find(qn("numId")).set(qn("val"), numbers.findall(qn("num"))[-1].get(qn("numId")))
        content["word/styles.xml"] = ET.tostring(definitions)
    elif fault == "wrong_indent":
        lvl.find(f"{qn('pPr')}/{qn('ind')}").set(qn("firstLineChars"), "200")
    content["word/document.xml"] = ET.tostring(doc)
    if fault != "missing_part":
        content["word/numbering.xml"] = ET.tostring(numbers)
    write_parts(output, content)
    with pytest.raises(ReportValidationError):
        validate_report(output, expected_plan=expected)
