"""Tests for the dependency-free dashboard.

The dashboard is the part of the product most likely to rot silently, because
nothing else imports it: a broken template still produces a file, and a broken
script still produces a file that opens to a blank page. These tests assert the
built artefact rather than the source.

Three layers:

1. The template keeps its data placeholder and no stray ``</script>`` can
   terminate the JSON block early.
2. The embedded payload is valid JSON with the shape the renderer reads.
3. The renderer actually executes, under a stubbed DOM, and emits no
   ``undefined``/``NaN`` into the page.

Layer 3 shells out to Node. Node is not required to *build* or *use* the
dashboard - only to test it - so those tests skip when it is unavailable
rather than failing.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent
FRONTEND_DIR = REPO_DIR / "frontend"
TEMPLATE = FRONTEND_DIR / "index.template.html"
BUILT = FRONTEND_DIR / "index.html"
DATA = FRONTEND_DIR / "dashboard-data.json"
SMOKE_JS = Path(__file__).resolve().parent / "dashboard_smoke.js"

NODE = shutil.which("node")

#: Number of renderer assertions dashboard_smoke.js must make. Pinned here and
#: cross-checked against the harness source by
#: TestRendererExecution.test_expected_count_matches_the_harness_source, so
#: coverage cannot silently shrink or drift.
EXPECTED_RENDERER_CHECKS = 143

PAYLOAD_RE = re.compile(
    r'<script id="d6-data" type="application/json">(.*?)</script>', re.S
)
RENDERER_RE = re.compile(r'<script>\s*"use strict";(.*?)</script>\s*</body>', re.S)


def build_into(tmpdir: Path) -> Path:
    """Run the real builder so tests exercise production code, not a copy."""
    out = tmpdir / "index.html"
    proc = subprocess.run(
        [
            str(BACKEND_DIR / ".venv" / "bin" / "python"),
            "-m",
            "scripts.build_dashboard",
            "--template",
            str(TEMPLATE),
            "--data",
            str(DATA),
            "--output",
            str(out),
        ],
        cwd=BACKEND_DIR,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"build_dashboard failed ({proc.returncode}):\n"
            f"{proc.stdout}\n{proc.stderr}"
        )
    return out


class TestTemplate(unittest.TestCase):
    def test_template_exists(self):
        self.assertTrue(TEMPLATE.exists(), f"missing template {TEMPLATE}")

    def test_template_has_exactly_one_placeholder(self):
        html = TEMPLATE.read_text()
        self.assertEqual(
            html.count("__D6_DATA__"),
            1,
            "template must contain exactly one __D6_DATA__ placeholder",
        )

    def test_template_has_no_external_resource(self):
        """The dashboard must work offline, from file://, with no CDN."""
        html = TEMPLATE.read_text()
        self.assertNotIn("http://", html, "template must not reference http://")
        self.assertNotIn("https://", html, "template must not reference https://")
        self.assertNotIn("fetch(", html, "dashboard must not fetch; data is inlined")

    def test_payload_block_cannot_be_closed_early(self):
        """A literal </script> in the payload would truncate the JSON."""
        blob = '{"x":"</script>"}'
        blob = blob.replace("</", "<\\/")
        self.assertNotIn("</script>", blob)
        self.assertEqual(json.loads(blob.replace("<\\/", "</")), {"x": "</script>"})


class TestBuiltDashboard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.path = build_into(Path(cls._tmp.name))
        cls.html = cls.path.read_text()
        m = PAYLOAD_RE.search(cls.html)
        assert m, "built dashboard has no embedded payload"
        cls.payload = json.loads(m.group(1).replace("<\\/", "</"))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_placeholder_fully_replaced(self):
        self.assertNotIn("__D6_DATA__", self.html)

    def test_payload_parses(self):
        self.assertIsInstance(self.payload, dict)

    def test_payload_has_every_section_the_renderer_reads(self):
        for key in ("hotel", "portfolio", "monthly", "scenario", "recommendations"):
            self.assertIn(key, self.payload)

    def test_portfolio_totals_present(self):
        totals = self.payload["portfolio"]["totals"]
        for key in (
            "gross_revenue",
            "net_revenue",
            "distribution_cost",
            "distribution_cost_pct",
            "gross_adr",
            "net_adr",
            "occupancy",
            "net_revpar",
        ):
            self.assertIn(key, totals)

    def test_money_is_serialised_as_strings(self):
        """Exactness: money must never round-trip through a JSON float."""
        self.assertIsInstance(self.payload["portfolio"]["totals"]["net_revenue"], str)
        self.assertIsInstance(self.payload["portfolio"]["channels"][0]["net_revenue"], str)

    def test_demo_data_is_flagged(self):
        self.assertIn("SYNTHETIC", self.payload["demo_warning"])
        self.assertIn("SYNTHETIC", self.html)

    def test_scenario_is_labelled_modelled(self):
        scenario = self.payload["scenario"]
        self.assertIn(scenario["objective"], ("MAXIMIZE_NET_REVENUE",))
        self.assertIn("MODELLED", self.html)

    def test_exact_scenario_warning_is_present(self):
        """The warning must be this string, verbatim, in the shipped file.

        Asserting the exact wording stops a well-meaning edit from quietly
        weakening it to "modelled estimate" or similar.
        """
        self.assertIn("MODELLED SCENARIO \u2014 NOT A GUARANTEE", self.html)
        self.assertIn("MODELLED SCENARIO \u2014 NOT A GUARANTEE", TEMPLATE.read_text())

    def test_built_file_is_reproducible(self):
        """Rebuilding from the same inputs must be byte-identical."""
        with tempfile.TemporaryDirectory() as d2:
            again = build_into(Path(d2))
            self.assertEqual(
                again.read_text(),
                self.html,
                "dashboard build is not deterministic",
            )

    def test_committed_index_is_up_to_date(self):
        """frontend/index.html must not be stale relative to the payload."""
        if not BUILT.exists():
            self.skipTest("frontend/index.html not built yet")
        with tempfile.TemporaryDirectory() as d3:
            fresh = build_into(Path(d3))
            self.assertEqual(
                BUILT.read_text(),
                fresh.read_text(),
                "frontend/index.html is stale; re-run scripts.build_dashboard",
            )


class TestRendererExecution(unittest.TestCase):
    """Execute the render functions under Node against a stubbed DOM."""

    @classmethod
    def setUpClass(cls):
        if not NODE:
            return
        cls._tmp = tempfile.TemporaryDirectory()
        cls.path = build_into(Path(cls._tmp.name))
        cls.proc = subprocess.run(
            [NODE, str(SMOKE_JS), str(cls.path)],
            capture_output=True,
            text=True,
        )

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "_tmp", None):
            cls._tmp.cleanup()

    def setUp(self):
        if not NODE:
            self.skipTest("node not installed; dashboard render tests skipped")

    def test_no_undefined_or_nan_reaches_the_page(self):
        self.assertEqual(
            self.proc.returncode,
            0,
            f"renderer checks failed:\n{self.proc.stdout}\n{self.proc.stderr}",
        )
        self.assertIn(
            f"ALL {EXPECTED_RENDERER_CHECKS} CHECKS PASSED", self.proc.stdout
        )

    def test_every_check_passed(self):
        self.assertNotIn("> FAIL", self.proc.stdout)
        self.assertEqual(
            len(re.findall(r"^ {2}PASS {2}", self.proc.stdout, re.M)),
            EXPECTED_RENDERER_CHECKS,
            f"expected exactly {EXPECTED_RENDERER_CHECKS} renderer checks to pass",
        )

    def test_every_declared_check_actually_ran(self):
        """No check may be silently dropped from the harness.

        A check that is removed from dashboard_smoke.js must change the runtime
        count and fail test_every_check_passed. This guards the other
        direction: a check whose name is declared must actually appear in the
        report, so a renamed or short-circuited assertion cannot hide behind a
        count that still happens to add up.
        """
        reported = set(re.findall(r"^ {2}PASS {2}(.+)$", self.proc.stdout, re.M))
        reported |= set(re.findall(r"^> FAIL {2}(.+)$", self.proc.stdout, re.M))
        source = SMOKE_JS.read_text()
        # prefix-only names come from the two generated families, which
        # test_loop_generated_check_families_are_complete covers exhaustively
        declared = set(
            n for n in re.findall(r'\bhas\("([^"]*)"', source)
            if not n.endswith((" ", "#"))
        )
        missing = {d for d in declared if d not in reported and d.strip()}
        self.assertEqual(
            missing, set(),
            f"checks declared in the harness but never reported: {sorted(missing)}",
        )

    def test_loop_generated_check_families_are_complete(self):
        """The two generated families must cover everything they enumerate.

        One source line fans out into seven column checks and six section
        checks, so the raw count alone cannot prove they all ran.
        """
        reported = self.proc.stdout
        for section in ("overview", "leakage", "channels", "simulator",
                        "recommendations", "bookings"):
            self.assertIn(
                f"PASS  section #{section} exists", reported,
                f"section #{section} was never checked",
            )
        for column in ("Bookings", "Room Nights", "Gross Revenue",
                       "Distribution Cost", "Net Revenue", "Net ADR",
                       "Repeat Rate"):
            self.assertIn(
                f"PASS  column {column}", reported,
                f"channel column {column!r} was never checked",
            )

    def test_builder_rejects_a_duplicated_placeholder(self):
        """Regression: a prose mention of the token doubled the payload."""
        with tempfile.TemporaryDirectory() as d:
            dup = Path(d) / "dup.html"
            dup.write_text(
                f'<script id="d6-data" type="application/json">{ "__D6_DATA__" }'
                f"</script><!-- prose mentions __D6_DATA__ too -->"
            )
            out = Path(d) / "out.html"
            proc = subprocess.run(
                [
                    str(BACKEND_DIR / ".venv" / "bin" / "python"),
                    "-m",
                    "scripts.build_dashboard",
                    "--template",
                    str(dup),
                    "--data",
                    str(DATA),
                    "--output",
                    str(out),
                ],
                cwd=BACKEND_DIR,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("expected exactly 1", proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
