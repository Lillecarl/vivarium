import pytest

from vivarium.junit_in import Case, parse

# The shape pytest's --junitxml writes, trimmed.
PYTEST = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="4">
  <testcase classname="tests.unit.test_a" name="test_ok" time="0.010"/>
  <testcase classname="tests.unit.test_a" name="test_bad" time="0.200">
    <failure message="assert 1 == 2">def test_bad():
&gt;       assert 1 == 2</failure>
  </testcase>
  <testcase classname="tests.unit.test_a" name="test_broken" time="0.001">
    <error message="fixture failed">boom</error>
  </testcase>
  <testcase classname="tests.unit.test_a" name="test_later" time="0">
    <skipped type="pytest.skip" message="not today"/>
  </testcase>
</testsuite></testsuites>"""


class TestParse:
    def test_every_outcome(self):
        cases = parse(PYTEST)
        assert [(c.name.split("::")[-1], c.outcome) for c in cases] == [
            ("test_ok", "passed"),
            ("test_bad", "failed"),
            ("test_broken", "error"),
            ("test_later", "skipped"),
        ]

    def test_the_name_carries_the_class(self):
        assert parse(PYTEST)[0].name == "tests.unit.test_a::test_ok"

    def test_a_failure_keeps_message_and_body(self):
        bad = parse(PYTEST)[1]
        assert bad == Case(
            "tests.unit.test_a::test_bad",
            "failed",
            0.2,
            message="assert 1 == 2",
            error="def test_bad():\n>       assert 1 == 2",
        )

    def test_a_skip_keeps_its_reason(self):
        assert parse(PYTEST)[3].reason == "not today"

    def test_a_bare_testsuite_root(self):
        """go-junit-report writes <testsuites>; some runners write one
        <testsuite> alone."""
        text = '<testsuite><testcase name="TestX" time="1.5"/></testsuite>'
        assert parse(text) == [Case("TestX", "passed", 1.5)]

    def test_not_xml_says_so(self):
        with pytest.raises(ValueError, match="not JUnit XML"):
            parse("<testsuite><testcase")
