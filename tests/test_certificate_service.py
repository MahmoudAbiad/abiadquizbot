from services.certificate_service import build_certificate


def test_build_certificate_returns_png_and_pdf():
    png, pdf, filename = build_certificate(
        name="طالب تجريبي",
        rank=1,
        score=8,
        total_questions=10,
        quiz_title="اختبار تجريبي",
    )

    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert pdf.startswith(b"%PDF")
    assert filename
