"""OCR parsing and input events, without a guest."""

import pytest

from vivarium_runner import display, ocr

# tesseract 4.1's TSV for two lines, trimmed to the rows that matter.
TSV = "\t".join(
    "level page_num block_num par_num line_num word_num left top width height conf text".split()
) + "\n" + "\n".join(
    "\t".join(str(cell) for cell in row)
    for row in [
        (1, 1, 0, 0, 0, 0, 0, 0, 1024, 768, -1, ""),
        (5, 1, 1, 1, 1, 1, 2, 129, 109, 11, 91.0, "[root@desk:~]#"),
        (5, 1, 1, 1, 1, 2, 120, 129, 30, 11, 90.0, "echo"),
        (5, 1, 1, 1, 1, 3, 160, 129, 100, 11, 85.0, "VIVARIUM-42"),
        (5, 1, 2, 1, 1, 1, 0, 150, 100, 11, 95.0, "VIVARIUM-42"),
        (5, 1, 2, 1, 1, 2, 0, 150, 0, 0, -1, " "),
    ]
)


def test_lines_join_words_in_order():
    screen = ocr.parse_tsv(TSV)
    assert screen.text == "[root@desk:~]# echo VIVARIUM-42\nVIVARIUM-42"


def test_find_boxes_the_matched_words():
    screen = ocr.parse_tsv(TSV)
    found = screen.find(r"echo VIVARIUM-\d+")
    assert found == [ocr.Text("echo VIVARIUM-42", 120, 129, 140, 11)]
    assert found[0].center == (190, 134)


def test_anchors_are_per_line():
    # The negative: an anchored pattern matches only the line that is
    # nothing else, not the command that contains it.
    found = ocr.parse_tsv(TSV).find(r"^VIVARIUM-42$")
    assert [(t.left, t.top) for t in found] == [(0, 150)]


def test_scale_maps_a_resample_back():
    screen = ocr.parse_tsv(TSV, scale=0.5)
    assert screen.find("echo")[0] == ocr.Text("echo", 60, 64, 15, 6)


def test_not_tsv_is_an_error():
    with pytest.raises(ocr.OcrError):
        ocr.parse_tsv("Tesseract Open Source OCR Engine")


def test_ppm_size():
    assert ocr.ppm_size(b"P6\n1024 768\n255\n\x00") == (1024, 768)
    with pytest.raises(ocr.OcrError):
        ocr.ppm_size(b"\x89PNG\r\n\x1a\n")


def test_keys_follow_nixos_test():
    assert [display.key_for(c) for c in "aZ9 $\n"] == ["a", "shift-z", "9", "spc", "shift-0x05", "ret"]
    with pytest.raises(ValueError):
        display.key_for("é")


def test_corners_are_the_axis_ends():
    assert display.move_events(0, 0, 1024, 768) == [
        {"type": "abs", "data": {"axis": "x", "value": 0}},
        {"type": "abs", "data": {"axis": "y", "value": 0}},
    ]
    assert [e["data"]["value"] for e in display.move_events(1023, 767, 1024, 768)] == [0x7FFF] * 2
    with pytest.raises(ValueError):
        display.move_events(1024, 0, 1024, 768)
