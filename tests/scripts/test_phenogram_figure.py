"""Unit tests for scripts/phenogram_figure.py.

The rules pinned here are the ones tests/phenogram_layout_test.ts pins for
lib/phenogram.ts: the island and the print figure implement one rule over one
encoding file and one cytoband table, and these two suites keep them in step.
Every test holds for any dataset -- the committed rows are only ever looped
over, and the constructed cases build their rows from the encoding. The
cSVD karyogram's own numbers are pinned in tests/scripts/csvd/.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest
from scripts.phenogram_figure import (
    DEFAULT_CYTOBANDS,
    DEFAULT_GENES,
    block_height,
    compute_layout,
    draw,
    glyph_lift,
    glyph_marker,
    glyph_scale,
    glyphs_for,
    load_cytobands,
    load_encoding,
    load_genes,
    main,
    normalize_gene,
    pills_for,
    place_gene,
    resolve_collisions,
)


@pytest.fixture(scope="module")
def genes() -> list[dict]:
    return load_genes(DEFAULT_GENES)


@pytest.fixture(scope="module")
def cytobands() -> dict:
    return load_cytobands(DEFAULT_CYTOBANDS)


@pytest.fixture(scope="module")
def encoding() -> dict:
    return load_encoding()


@pytest.fixture(scope="module")
def layout(genes, encoding, cytobands):
    return compute_layout(genes, encoding, cytobands)


def _gene(encoding: dict, **overrides: Any) -> dict[str, Any]:
    """A gene row every rule accepts, on a band the hg38 table carries."""
    row = {
        "gene": "GENE1",
        "protein": "Protein one",
        "chromosomalLocation": "7q31.1",
        "gwasTrait": [encoding["traits"][0]["key"]],
        "mendelianRandomization": "No",
        "evidenceFromOtherOmicsStudies": ["(none found)"],
        "linkToMonogenicDisease": ["(none found)"],
        "brainCellTypes": "(unknown)",
        "affectedPathway": "(unknown)",
        "references": ["10000001"],
        "sourceQuote": "GENE1 was associated with the phenotype.",
        "confidence": 0.9,
    }
    row.update(overrides)
    return normalize_gene(row)


class TestPlacement:
    def test_place_gene_is_exact(self, cytobands) -> None:
        hit = place_gene("7q31.1", cytobands)
        assert hit is not None
        assert (hit.chromosome, hit.band, hit.start, hit.end) == (
            "7",
            "q31.1",
            107800000,
            115000000,
        )
        assert hit.midpoint == 111400000
        assert place_gene("1p3", cytobands) is None
        assert place_gene("23q11", cytobands) is None
        assert place_gene("(unknown)", cytobands) is None

    def test_place_gene_spans_a_band_range(self, cytobands) -> None:
        """NCBI's range for a gene across a band edge, as lib/cytobands.ts."""
        hit = place_gene("10q24.33-q25.1", cytobands)
        assert hit is not None
        assert (hit.chromosome, hit.band, hit.start, hit.end) == (
            "10",
            "q24.33-q25.1",
            103100000,
            110100000,
        )
        assert hit.midpoint == 106600000
        reversed_hit = place_gene("10q25.1-q24.33", cytobands)
        assert reversed_hit is not None
        assert (reversed_hit.start, reversed_hit.end) == (103100000, 110100000)
        assert place_gene("10q24.33-q25", cytobands) is None
        assert place_gene("10q24.33-", cytobands) is None
        assert place_gene("10q24.33-11q25.1", cytobands) is None
        assert place_gene("10q24.33-q25.1-q25.2", cytobands) is None

    def test_only_chromosomes_with_a_gene_are_drawn(self, layout, genes) -> None:
        drawn = [c.name for c in layout.chromosomes]
        assert len(drawn) == len(set(drawn))
        assert set(drawn) == {b.chromosome for b in layout.blocks}
        assert len(layout.blocks) + len(layout.unplaced) == len(genes)
        assert [c.name for row in layout.rows for c in row] == drawn

    def test_a_constructed_table_places_its_genes_and_reports_the_unplaceable(
        self, encoding, cytobands
    ) -> None:
        rows = [
            _gene(encoding, gene="GENE1", chromosomalLocation="7q31.1"),
            _gene(encoding, gene="GENE2", chromosomalLocation="Xq22.1"),
            _gene(encoding, gene="GENE3", chromosomalLocation="(unknown)"),
        ]
        placed = compute_layout(rows, encoding, cytobands)
        assert [b.symbol for b in placed.blocks] == ["GENE1", "GENE2"]
        assert [c.name for c in placed.chromosomes] == ["7", "X"]
        assert [g["gene"] for g in placed.unplaced] == ["GENE3"]
        assert placed.canvas == tuple(encoding["layout"]["viewBox"][:2])

    def test_an_empty_table_draws_no_chromosome(self, encoding, cytobands) -> None:
        empty = compute_layout([], encoding, cytobands)
        assert empty.blocks == ()
        assert empty.chromosomes == ()
        assert empty.unplaced == ()

    def test_chromosomes_are_to_scale_and_bands_tile_them(
        self, encoding, cytobands
    ) -> None:
        rows = [
            _gene(encoding, gene="GENE1", chromosomalLocation="1p36.33"),
            _gene(encoding, gene="GENE2", chromosomalLocation="13q34"),
        ]
        placed = compute_layout(rows, encoding, cytobands)
        one = next(c for c in placed.chromosomes if c.name == "1")
        assert one.height == pytest.approx(encoding["layout"]["rowHeight"])
        thirteen = next(c for c in placed.chromosomes if c.name == "13")
        assert thirteen.height == pytest.approx(
            114364328 / 248956422 * encoding["layout"]["rowHeight"]
        )
        for chromosome in placed.chromosomes:
            cursor = chromosome.y
            for band in chromosome.bands:
                assert band.y == pytest.approx(cursor)
                assert band.fill == encoding["stains"][band.stain]
                cursor += band.height
            assert cursor == pytest.approx(chromosome.y + chromosome.height)

    def test_blocks_stay_in_their_row_and_never_overlap(self, layout, encoding) -> None:
        gap = encoding["layout"]["blockGap"]
        row_height = encoding["layout"]["rowHeight"]
        for chromosome in layout.chromosomes:
            blocks = [b for b in layout.blocks if b.chromosome == chromosome.name]
            for previous, block in zip(blocks, blocks[1:], strict=False):
                assert block.y >= previous.y + previous.height + gap - 1e-6
            for block in blocks:
                assert block.y >= chromosome.y - 1e-6
                assert block.y + block.height <= chromosome.y + row_height + 1e-6

    def test_markers_sit_at_their_band_midpoint_in_order(
        self, layout, cytobands, encoding
    ) -> None:
        geometry = encoding["layout"]
        by_chromosome = {c.name: c for c in layout.chromosomes}
        previous_marker_y: dict[str, float] = {}
        for block in layout.blocks:
            shape = by_chromosome[block.chromosome]
            hit = place_gene(block.gene["chromosomalLocation"], cytobands)
            assert hit is not None
            assert block.marker_y == pytest.approx(
                shape.y + hit.midpoint / shape.length * shape.height
            )
            assert shape.y <= block.marker_y <= shape.y + shape.height
            assert (
                block.x == shape.x + geometry["chromosomeWidth"] + geometry["leaderGap"]
            )
            assert block.width == geometry["labelColumn"]
            assert block.marker_y >= previous_marker_y.get(
                block.chromosome, block.marker_y
            )
            previous_marker_y[block.chromosome] = block.marker_y


class TestEncoding:
    def test_pills_and_glyphs_follow_the_evidence_columns(self, encoding) -> None:
        first = encoding["traits"][0]
        family = next(f for f in encoding["families"] if f["key"] == first["family"])
        evidence = _gene(
            encoding,
            gwasTrait=[first["key"]],
            evidenceFromOtherOmicsStudies=["TWAS"],
            linkToMonogenicDisease=["100000"],
            mendelianRandomization="Yes",
        )
        pills = pills_for(evidence, encoding)
        assert [p.label for p in pills] == [first["key"]]
        assert pills[0].family == family["key"]
        assert pills[0].fill == family["tint"]
        assert pills[0].stroke == family["hue"]
        assert [g.key for g in glyphs_for(evidence, encoding)] == [
            "omics",
            "monogenic",
            "mr",
        ]
        bare = _gene(encoding, gwasTrait=["(none found)"])
        assert pills_for(bare, encoding) == ()
        assert glyphs_for(bare, encoding) == ()
        assert block_height(len(pills), encoding["layout"]) > block_height(
            0, encoding["layout"]
        )


class TestResolveCollisions:
    """Same cases as tests/phenogram_layout_test.ts, same expected tops."""

    def test_leaves_separated_blocks_alone(self) -> None:
        assert resolve_collisions([0, 50], [20, 20], 0, 400, 6) == [0, 50]

    def test_pushes_a_following_block_down(self) -> None:
        assert resolve_collisions([0, 10], [20, 20], 0, 400, 6) == [0, 26]

    def test_pulls_a_stack_up_at_the_bottom(self) -> None:
        assert resolve_collisions([380, 390], [20, 20], 0, 400, 6) == [354, 380]

    def test_clamps_a_lone_block(self) -> None:
        assert resolve_collisions([-10], [20], 0, 400, 6) == [0]
        assert resolve_collisions([395], [20], 0, 400, 6) == [380]
        assert resolve_collisions([], [], 0, 400, 6) == []


class TestRender:
    """matplotlib is not a base dependency -- it arrives with the `figure`
    group, through pyCirclize. CI installs that group, but a plain
    `uv sync --group dev` does not, and phenogram_figure.py imports
    matplotlib inside the drawing functions rather than at module scope, so
    only these tests reach it. The layout tests above need none of it.
    """

    @pytest.fixture(autouse=True)
    def _needs_matplotlib(self) -> None:
        pytest.importorskip("matplotlib")

    def test_svg_carries_every_gene_and_both_legends(
        self, encoding, cytobands, tmp_path: Path
    ) -> None:
        rows = [
            _gene(encoding, gene="GENE1", chromosomalLocation="7q31.1"),
            _gene(encoding, gene="GENE2/3", chromosomalLocation="13q34"),
        ]
        fig = draw(rows, encoding, cytobands)
        target = tmp_path / "phenogram.svg"
        fig.savefig(target, format="svg", bbox_inches="tight")
        svg = target.read_text(encoding="utf-8")
        assert svg.count('id="gene-') == 2
        # A gene symbol can carry a "/"; the gid is sanitized so the id stays
        # a valid XML Name.
        assert 'id="gene-GENE2-3"' in svg
        assert "Supporting evidence" in svg
        assert "GWAS phenotypes" in svg
        assert encoding["families"][0]["label"] in svg
        # Text stays text: real <text> elements, not glyph outlines. Comments
        # and gids carry the symbols regardless of svg.fonttype, so look for
        # the symbol as element content.
        assert svg.count("<text") >= 2
        assert re.search(r"<text[^>]*>(?:<tspan[^>]*>)?GENE2/3<", svg)

    def test_main_writes_the_requested_formats(
        self, encoding, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rows = tmp_path / "table1.json"
        unplaced = _gene(encoding, gene="GENE2", chromosomalLocation="(unknown)")
        rows.write_text(json.dumps([_gene(encoding), unplaced]), encoding="utf-8")
        out = tmp_path / "fig"
        argv = ["--genes", str(rows), "--out", str(out), "--format", "svg", "pdf"]
        code = main([*argv, "--dpi", "72"])
        assert code == 0
        assert (out / "phenogram.svg").stat().st_size > 0
        assert (out / "phenogram.pdf").stat().st_size > 0
        assert not (out / "phenogram.png").exists()
        assert "Unplaced (no hg38 band): GENE2 ((unknown))" in capsys.readouterr().err

    def test_main_reports_an_empty_table_and_writes_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A fork starts from `deno task data:empty`; there is nothing to draw
        and the script says so rather than writing an empty karyogram."""
        rows = tmp_path / "table1.json"
        rows.write_text("[]\n", encoding="utf-8")
        out = tmp_path / "fig"
        assert main(["--genes", str(rows), "--out", str(out)]) == 0
        assert not out.exists()
        assert "no gene rows" in capsys.readouterr().err


class TestNormalization:
    """normalize_gene mirrors lib/data/genes.ts's normalizeGene (Important 2): a
    regenerated table1.json carrying stray whitespace or blank list entries
    must render the same pills and glyphs in print as it does on screen.
    """

    @pytest.fixture
    def raw_row(self, encoding) -> dict[str, Any]:
        return {
            "gene": "TESTGENE",
            "chromosomalLocation": " 7q31.1 ",
            "gwasTrait": [f"{encoding['traits'][0]['key']} ", ""],
            "evidenceFromOtherOmicsStudies": [" proteomics"],
            "linkToMonogenicDisease": [],
            "mendelianRandomization": "Yes ",
        }

    def test_normalize_gene_trims_and_falls_back_like_lib_data_ts(
        self, raw_row, encoding
    ) -> None:
        row = normalize_gene(raw_row)
        assert row["chromosomalLocation"] == "7q31.1"
        assert row["gwasTrait"] == [encoding["traits"][0]["key"]]
        assert row["evidenceFromOtherOmicsStudies"] == ["proteomics"]
        assert row["linkToMonogenicDisease"] == ["(none found)"]
        assert row["mendelianRandomization"] == "Yes"

    def test_normalized_row_yields_one_pill_and_two_glyphs(
        self, raw_row, encoding
    ) -> None:
        row = normalize_gene(raw_row)
        first = encoding["traits"][0]
        pills = pills_for(row, encoding)
        assert [p.label for p in pills] == [first["key"]]
        assert pills[0].family == first["family"]
        family = next(f for f in encoding["families"] if f["key"] == first["family"])
        assert pills[0].fill == family["tint"]
        assert [g.key for g in glyphs_for(row, encoding)] == ["omics", "mr"]


class TestGlyphGeometry:
    """The three evidence marks are a symbol set, and it spans two renderers.

    tests/phenogram_encoding_test.ts pins the encoding side; these pin that this
    renderer actually reads it. The star is the case that matters: matplotlib's
    built-in ``*`` hard-codes ``innerCircle=0.381966``, so for as long as each
    renderer owned its own star the print figure carried a different mark from
    the island and no test in either suite could tell.

    matplotlib arrives only with the `figure` group (see TestRender), and
    two of these tests import it, so they skip rather than fail without it.
    """

    @pytest.fixture(autouse=True)
    def _needs_matplotlib(self) -> None:
        pytest.importorskip("matplotlib")

    def test_star_is_built_from_the_encoding_not_matplotlibs_builtin(
        self, encoding
    ) -> None:
        from matplotlib.markers import MarkerStyle
        from matplotlib.path import Path as MarkerPath

        ratio = encoding["glyphs"]["star"]["innerRatio"]
        marker = glyph_marker("star", encoding["glyphs"])
        assert isinstance(marker, MarkerPath), "star fell back to the builtin '*'"

        expected = MarkerPath.unit_regular_star(5, innerCircle=ratio)
        assert marker.vertices == pytest.approx(expected.vertices)

        builtin = MarkerStyle("*").get_path()
        assert marker.vertices != pytest.approx(builtin.vertices), (
            f"innerRatio {ratio} reproduces matplotlib's 0.381966"
        )

    def test_a_custom_star_is_sized_like_the_builtin_one(self, encoding) -> None:
        """Both normalise by 0.5 / max(|vertices|), so markersize means the same."""
        from matplotlib.markers import MarkerStyle
        from matplotlib.transforms import Affine2DBase

        def values(marker: MarkerStyle) -> tuple[float, ...]:
            # A marker's transform is always affine, but `get_transform` is
            # annotated with the base class, which carries no `to_values`.
            transform = marker.get_transform()
            assert isinstance(transform, Affine2DBase)
            return transform.to_values()

        custom = MarkerStyle(glyph_marker("star", encoding["glyphs"]))
        builtin = MarkerStyle("*")
        assert values(custom) == pytest.approx(values(builtin))

    def test_triangle_and_square_keep_matplotlibs_own_marks(self, encoding) -> None:
        assert glyph_marker("triangle", encoding["glyphs"]) == "^"
        assert glyph_marker("square", encoding["glyphs"]) == "s"

    def test_the_three_glyphs_carry_comparable_ink(self, encoding) -> None:
        """Ink is area, not bounding box; the gap this geometry closes was 3.24x."""
        import math

        glyphs = encoding["glyphs"]
        box = 9.0

        def area(shape: str) -> float:
            side = box * glyph_scale(shape, glyphs)
            if shape == "square":
                return side * side
            if shape == "triangle":
                return side * side / 2
            outer = side / 2
            return 5 * outer * (outer * glyphs["star"]["innerRatio"]) * math.sin(
                math.pi / 5
            )

        areas = [area(s) for s in ("square", "triangle", "star")]
        spread = max(areas) / min(areas)
        assert spread <= 1.75, f"glyph ink spread {spread:.2f}x: {areas}"

    def test_only_the_triangle_is_lifted(self, encoding) -> None:
        glyphs = encoding["glyphs"]
        assert glyph_lift("triangle", glyphs) > 0
        assert glyph_lift("square", glyphs) == 0
        assert glyph_lift("star", glyphs) == 0
