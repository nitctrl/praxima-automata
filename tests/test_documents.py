"""Document ingestion, tagging and retrieval; unreviewed text must never reach a caller."""

import io
import zipfile
from uuid import UUID, uuid4

import pytest
from docx import Document as DocxDocument

from clinic.documents import DocumentIndex, DocumentRejected, excerpt, extract, tag_doctors
from clinic.snapshot import DocumentSection

DOCTOR = UUID(int=2)
OTHER = UUID(int=3)


def docx_bytes(build):
    document = DocxDocument()
    build(document)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def section(text, heading="About us", doctor=None, topic="about"):
    return DocumentSection(
        id=uuid4(),
        document_id=UUID(int=9),
        document_title="Clinic story",
        document_version=1,
        topic=topic,
        heading=heading,
        text=text,
        doctor_id=doctor,
        keywords=(),
    )


def test_markdown_sections_keep_order_and_headings():
    source = (
        b"# Our clinic\n\nFounded by a family of physicians.\n\n"
        b"## Vision\n\nWe want calm, unhurried care.\n\n"
        b"```\nignored = code\n```\n\n"
        b"![logo](logo.png) See [our site](https://example.test) for more.\n"
    )
    result = extract("story.md", source)
    assert result.title == "Our clinic"
    assert result.mime_type == "text/markdown"
    assert [s.heading for s in result.sections] == ["Our clinic", "Vision"]
    assert "unhurried" in result.sections[1].text
    assert "ignored" not in result.sections[1].text
    assert "our site" in result.sections[1].text and "example.test" not in result.sections[1].text


def test_markdown_accepts_hindi_and_rejects_other_encodings():
    result = extract("hindi.md", "# परिचय\n\nहमारा क्लिनिक शांत देखभाल देता है।\n".encode())
    assert result.sections[0].heading == "परिचय"
    with pytest.raises(DocumentRejected):
        extract("hindi.md", "# परिचय\n".encode("utf-16"))


def test_docx_reads_headings_paragraphs_and_tables_in_order():
    def build(document):
        document.add_heading("Dr Anaya Sharma", level=1)
        document.add_paragraph("Twenty years of general practice in the city.")
        table = document.add_table(rows=1, cols=2)
        table.rows[0].cells[0].text = "Training"
        table.rows[0].cells[1].text = "Fictional Medical College"

    result = extract("bio.docx", docx_bytes(build))
    assert result.title == "Dr Anaya Sharma"
    assert result.sections[0].heading == "Dr Anaya Sharma"
    assert "Twenty years" in result.sections[0].text
    assert "Fictional Medical College" in result.sections[0].text


def test_unsupported_and_hostile_uploads_are_rejected():
    for filename, payload in [
        ("story.md", b""),
        ("story.pdf", b"%PDF-1.4"),
        ("story.docx", b"not a zip"),
        ("story.md", b"\n\n   \n"),
    ]:
        with pytest.raises(DocumentRejected):
            extract(filename, payload)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", b'<!DOCTYPE x [<!ENTITY a "boom">]><w:document/>')
    with pytest.raises(DocumentRejected):
        extract("evil.docx", buffer.getvalue())
    with pytest.raises(DocumentRejected):
        extract("big.md", b"x" * (5 * 1024 * 1024 + 1))


def test_zip_bomb_and_macro_packages_are_rejected():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"<w:document/>")
        archive.writestr("word/media/bomb.bin", b"0" * (21 * 1024 * 1024))
    with pytest.raises(DocumentRejected):
        extract("bomb.docx", buffer.getvalue())


def test_sections_are_tagged_with_a_single_named_doctor():
    sections = extract(
        "bios.md",
        (
            b"# Dr Anaya Sharma\n\nShe trained in paediatrics.\n\n"
            b"# Reception\n\nOur front desk helps with forms.\n"
        ),
    ).sections
    tagged = tag_doctors(sections, [(DOCTOR, ["Dr Anaya Sharma"]), (OTHER, ["Dr Dev Sharma"])])
    assert tagged[0].doctor_id == DOCTOR
    assert tagged[1].doctor_id is None


def test_search_filters_by_doctor_and_ignores_unrelated_questions():
    index = DocumentIndex(
        [
            section("Dr Anaya trained in paediatrics and leads vaccination drives.", doctor=DOCTOR),
            section("Dr Dev studied sports medicine abroad.", doctor=OTHER),
            section("The clinic started in a two room building.", heading="Our story"),
        ]
    )
    anaya = index.search("what is her training", doctor_id=DOCTOR)
    assert anaya and all(s.doctor_id in (None, DOCTOR) for _, s in anaya)
    assert not any("sports medicine" in s.text for _, s in anaya)
    assert index.search("how much is the consultation fee") == []
    assert index.search("") == []
    story = index.search("how did the clinic begin")
    assert story and "two room" in story[0][1].text


def test_empty_index_and_bounded_excerpt():
    assert DocumentIndex([]).search("anything") == []
    long_text = "Background. " * 200 + "The award was given in 2019."
    trimmed = excerpt(long_text, ["award"])
    assert len(trimmed) <= 640 and "award" in trimmed


def test_rrf_fuses_lexical_and_semantic_rankings():
    lexical = section("The clinic began in a two room building.", heading="Our story")
    semantic = section("Families value our calm, unhurried consultations.", heading="Vision")
    both = section("Our founding vision was calm family care.", heading="Our story")
    index = DocumentIndex([lexical, semantic, both])
    result = index.search("clinic founding vision", semantic=[semantic.id, both.id], limit=3)
    assert result[0][1].id == both.id
    assert {row.id for _, row in result} == {lexical.id, semantic.id, both.id}


def test_named_doctor_must_exist_even_when_semantic_search_returns_another_doctor():
    mahto = section("Dr Suresh Kumar Mahto has an MBBS qualification.")
    index = DocumentIndex([mahto])
    assert index.search("Tell me about Dr Sharma", semantic=[mahto.id]) == []
    assert index.search("What qualification does Dr Suresh have?", semantic=[mahto.id])
