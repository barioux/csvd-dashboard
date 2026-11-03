"""Draw the phenogram karyogram (genes on hg38 chromosomes) for print.

The dashboard draws the same figure in the browser (islands/Phenogram.tsx from
lib/phenogram.ts). Both renderers read lib/phenogram_encoding.json -- the one
place colours, labels, definitions, band stains and the geometry constants
live -- and data/cytobands_hg38.json, and both apply the same layout rule:
chromosomes that carry a gene, to scale, in two rows split after chromosome
10; one marker per gene at the midpoint of its band; a label block beside the
chromosome (symbol and evidence glyphs, then one pill per GWAS phenotype),
stacked without overlap by resolve_collisions. Coordinates are the SVG user
units of the encoding's viewBox, y down; the matplotlib axes are inverted to
match.

Usage:
    uv run --group figure scripts/phenogram_figure.py
    uv run --group figure scripts/phenogram_figure.py --out figures/ --format svg pdf
    uv run --group figure scripts/phenogram_figure.py --font /path/Arial.ttf --dpi 600
"""

import argparse
import json
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_GENES = _PROJECT_ROOT / "data" / "table1.json"
DEFAULT_CYTOBANDS = _PROJECT_ROOT / "data" / "cytobands_hg38.json"
DEFAULT_ENCODING = _PROJECT_ROOT / "lib" / "phenogram_encoding.json"
DEFAULT_FAMILIES = _PROJECT_ROOT / "disease" / "phenogram.json"
DEFAULT_VOCABULARY = _PROJECT_ROOT / "disease" / "vocabulary.json"
DEFAULT_OUT = _PROJECT_ROOT / "figures"
FORMATS = ("svg", "pdf", "png")

# The sentinel lib/constants.ts matches literally.
NONE_FOUND = "(none found)"
CHROMOSOMES = [str(n) for n in range(1, 23)] + ["X", "Y"]
CHROMOSOME_LABEL_OFFSET = 18.0
FALLBACK_PILL = ("#ffffff", "#888888")
FALLBACK_STAIN = "#cccccc"
LOCATION = re.compile(r"^(\d{1,2}|X|Y)([pq]\d+(?:\.\d+)?)(?:-([pq]\d+(?:\.\d+)?))?$")

# One viewBox unit is 1/100 inch on the page: 12 units of text is 8.64 pt.
UNITS_PER_INCH = 100.0
PT_PER_UNIT = 72.0 / UNITS_PER_INCH
SYMBOL_FONT_UNITS = 12.0
PILL_FONT_UNITS = 9.5
CHROMOSOME_FONT_UNITS = 13.0
GLYPH_UNITS = 9.0
GLYPH_GAP = 4.0
PILL_PADDING_X = 5.0
LEGEND_FONT_PT = 7.5
# Base size of a legend evidence mark, before its per-shape scale.
LEGEND_GLYPH_PT = 6.0
INK = "#14172b"
# The two shapes matplotlib draws exactly as the island does: `^` is
# [[0,1],[-1,-1],[1,-1]] scaled 0.5 -- base = height = markersize -- and `s` is
# a unit rectangle of side markersize. The star is built from the encoding
# instead; see glyph_marker.
_BUILTIN_MARKERS = {"triangle": "^", "square": "s"}


@dataclass(frozen=True)
class BandHit:
    chromosome: str
    band: str
    start: int
    end: int
    midpoint: float


@dataclass(frozen=True)
class Band:
    y: float
    height: float
    stain: str
    fill: str


@dataclass(frozen=True)
class ChromosomeShape:
    name: str
    length: int
    x: float
    y: float
    width: float
    height: float
    bands: tuple[Band, ...]
    p_arm: tuple[float, float]
    centromere: tuple[float, float]
    q_arm: tuple[float, float]
    label_point: tuple[float, float]


@dataclass(frozen=True)
class Pill:
    label: str
    family: str
    fill: str
    stroke: str


@dataclass(frozen=True)
class Glyph:
    key: str
    shape: str


@dataclass(frozen=True)
class GeneBlock:
    gene: dict[str, Any]
    symbol: str
    chromosome: str
    marker_y: float
    x: float
    y: float
    width: float
    height: float
    pills: tuple[Pill, ...]
    glyphs: tuple[Glyph, ...]


@dataclass(frozen=True)
class Layout:
    canvas: tuple[float, float]
    rows: tuple[tuple[ChromosomeShape, ...], ...]
    chromosomes: tuple[ChromosomeShape, ...]
    blocks: tuple[GeneBlock, ...]
    unplaced: tuple[dict[str, Any], ...]


def _nullable_text(value: Any) -> str | None:
    """A trimmed string, or None for anything blank or not a string."""
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


def _text(value: Any, fallback: str) -> str:
    return _nullable_text(value) or fallback


def _text_list(value: Any, fallback: str) -> list[str]:
    values = value if isinstance(value, list) else [value]
    normalized = [t for v in values if (t := _nullable_text(v)) is not None]
    return normalized if normalized else [fallback]


def normalize_gene(row: dict[str, Any]) -> dict[str, Any]:
    """Normalize the six fields this script reads from a Table 1 row.

    Mirrors `normalizeGene` in lib/data.ts, which every browser row passes
    through before the island ever draws it: strings are trimmed, a blank
    scalar falls back to "(unknown)", and a list field drops blank entries
    and falls back to `[NONE_FOUND]`. Without this, a regenerated
    table1.json carrying stray whitespace (`"WMH "`, `"Yes "`) would render
    differently in this print twin than on screen. Fields the twin never
    reads (protein, brainCellTypes, ...) pass through unchanged.
    """
    return {
        **row,
        "gene": _text(row.get("gene"), "(unknown)"),
        "chromosomalLocation": _text(row.get("chromosomalLocation"), "(unknown)"),
        "gwasTrait": _text_list(row.get("gwasTrait"), NONE_FOUND),
        "evidenceFromOtherOmicsStudies": _text_list(
            row.get("evidenceFromOtherOmicsStudies"), NONE_FOUND
        ),
        "linkToMonogenicDisease": _text_list(
            row.get("linkToMonogenicDisease"), NONE_FOUND
        ),
        "mendelianRandomization": _text(row.get("mendelianRandomization"), "(unknown)"),
    }


def load_genes(path: Path = DEFAULT_GENES) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [normalize_gene(row) for row in json.load(handle)]


def load_cytobands(path: Path = DEFAULT_CYTOBANDS) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def load_encoding(
    path: Path = DEFAULT_ENCODING,
    families: Path = DEFAULT_FAMILIES,
    vocabulary: Path = DEFAULT_VOCABULARY,
) -> dict[str, Any]:
    """Appearance from the encoding, families and trait identity from disease/.

    Composed so the rest of this module keeps reading one ``encoding``,
    matching ``lib/phenogram.ts``.
    """
    with path.open(encoding="utf-8") as handle:
        encoding = json.load(handle)
    with families.open(encoding="utf-8") as handle:
        encoding["families"] = json.load(handle)["families"]
    with vocabulary.open(encoding="utf-8") as handle:
        encoding["traits"] = json.load(handle)["traits"]
    return encoding


def glyph_marker(shape: str, glyphs: dict[str, Any]) -> Any:
    """The matplotlib marker for a shape, carrying the encoding's star waist.

    matplotlib's built-in ``*`` hard-codes ``innerCircle=0.381966``
    (``markers.py``'s ``_set_star``), so while each renderer owned its own star
    this one drew a spikier, ~9% lighter mark than ``lib/phenogram.ts``'s 0.42
    and nothing could see the difference. A custom ``Path`` is sized
    identically: ``_set_custom_marker`` normalises any path by
    ``0.5 / max(|vertices|)`` and ``unit_regular_star`` has an outer radius of
    1, which is exactly what the built-in gets.
    """
    if shape == "star":
        from matplotlib.path import Path as MarkerPath

        ratio = glyphs.get("star", {}).get("innerRatio", 0.5)
        return MarkerPath.unit_regular_star(5, innerCircle=ratio)
    return _BUILTIN_MARKERS.get(shape, "o")


def glyph_scale(shape: str, glyphs: dict[str, Any]) -> float:
    """Linear scale of a shape inside its advance box, as lib/phenogram.ts."""
    return glyphs.get(shape, {}).get("scale", 1.0)


def glyph_lift(shape: str, glyphs: dict[str, Any]) -> float:
    """How far to raise a glyph off the symbol line, in viewBox units.

    Placement rather than shape, exactly as on the island: an apex-up
    triangle's centroid sits at a third of its height, so box-centring it
    against bold text reads low.
    """
    return glyphs.get(shape, {}).get("lift", 0.0)


def place_gene(location: str, table: dict[str, Any]) -> BandHit | None:
    """The region a location names, or None. Exact bands only, and a range
    (`10q24.33-q25.1`) spans its two ends, as lib/cytobands.ts."""
    match = LOCATION.fullmatch(location.strip())
    if not match:
        return None
    chromosome, first, last = match.groups()
    bands = {
        band["name"]: band
        for chrom in table["chromosomes"]
        if chrom["name"] == chromosome
        for band in chrom["bands"]
    }
    names = (first,) if last is None else (first, last)
    if any(name not in bands for name in names):
        return None
    start = min(bands[name]["start"] for name in names)
    end = max(bands[name]["end"] for name in names)
    band_name = first if last is None else f"{first}-{last}"
    return BandHit(chromosome, band_name, start, end, (start + end) / 2)


def block_height(pill_count: int, layout: dict[str, Any]) -> float:
    return (
        2 * layout["blockPadding"]
        + layout["symbolLine"]
        + pill_count * layout["pillLine"]
    )


def pills_for(gene: dict[str, Any], encoding: dict[str, Any]) -> tuple[Pill, ...]:
    """One pill per non-sentinel GWAS trait, in the table's order."""
    families = {f["key"]: f for f in encoding["families"]}
    traits = {t["key"]: t for t in encoding["traits"]}
    pills: list[Pill] = []
    for value in gene["gwasTrait"]:
        if value == NONE_FOUND:
            continue
        trait = traits.get(value)
        family = families.get(trait["family"]) if trait else None
        pills.append(
            Pill(
                label=trait["label"] if trait else value,
                family=trait["family"] if trait else "unknown",
                fill=family["tint"] if family else FALLBACK_PILL[0],
                stroke=family["hue"] if family else FALLBACK_PILL[1],
            )
        )
    return tuple(pills)


def glyphs_for(gene: dict[str, Any], encoding: dict[str, Any]) -> tuple[Glyph, ...]:
    """Evidence glyphs in encoding order: omics, monogenic, Mendelian randomization."""

    def has(values: list[str]) -> bool:
        return any(value != NONE_FOUND for value in values)

    present = {
        "omics": has(gene["evidenceFromOtherOmicsStudies"]),
        "monogenic": has(gene["linkToMonogenicDisease"]),
        # Case- and whitespace-insensitive, like `normalize()` in
        # lib/filters.ts and so like lib/phenogram.ts's `glyphsFor`: a raw
        # compare drops the glyph for a value the sidebar filter still
        # matches, and would drop it from print only.
        "mr": gene["mendelianRandomization"].strip().casefold() == "yes",
    }
    return tuple(
        Glyph(entry["key"], entry["shape"])
        for entry in encoding["evidence"]
        if present.get(entry["key"], False)
    )


def resolve_collisions(
    desired: Sequence[float],
    heights: Sequence[float],
    top: float,
    bottom: float,
    gap: float,
) -> list[float]:
    """Top edges for one chromosome's blocks; the twin of lib/phenogram.ts."""
    y = [min(max(d, top), bottom - h) for d, h in zip(desired, heights, strict=True)]
    for i in range(1, len(y)):
        y[i] = max(y[i], y[i - 1] + heights[i - 1] + gap)
    for i in range(len(y) - 1, -1, -1):
        limit = bottom - heights[i] if i == len(y) - 1 else y[i + 1] - gap - heights[i]
        y[i] = min(y[i], limit)
    return y


def chromosome_shapes(
    names: Iterable[str], table: dict[str, Any], encoding: dict[str, Any]
) -> list[list[ChromosomeShape]]:
    """Chromosomes in CHROMOSOMES order, scaled to the longest, in two rows."""
    wanted = set(names)
    layout = encoding["layout"]
    longest = max(c["length"] for c in table["chromosomes"])
    scale = layout["rowHeight"] / longest
    pitch = layout["chromosomeWidth"] + layout["leaderGap"] + layout["labelColumn"]
    split_index = CHROMOSOMES.index(layout["rowSplitAfter"])
    by_name = {c["name"]: c for c in table["chromosomes"]}
    rows: list[list[ChromosomeShape]] = [[], []]
    for order, name in enumerate(CHROMOSOMES):
        if name not in wanted or name not in by_name:
            continue
        chromosome = by_name[name]
        row = 0 if order <= split_index else 1
        x = layout["margin"] + len(rows[row]) * pitch
        y = layout["margin"] + row * (layout["rowHeight"] + layout["rowGap"])
        height = chromosome["length"] * scale
        acen = [b for b in chromosome["bands"] if b["stain"] == "acen"]
        cen_start = acen[0]["start"] if acen else chromosome["length"] / 2
        cen_end = acen[-1]["end"] if acen else cen_start
        bands = tuple(
            Band(
                y=y + b["start"] * scale,
                height=(b["end"] - b["start"]) * scale,
                stain=b["stain"],
                fill=encoding["stains"].get(b["stain"], FALLBACK_STAIN),
            )
            for b in chromosome["bands"]
        )
        rows[row].append(
            ChromosomeShape(
                name=name,
                length=chromosome["length"],
                x=x,
                y=y,
                width=layout["chromosomeWidth"],
                height=height,
                bands=bands,
                p_arm=(y, cen_start * scale),
                centromere=(y + cen_start * scale, (cen_end - cen_start) * scale),
                q_arm=(y + cen_end * scale, (chromosome["length"] - cen_end) * scale),
                label_point=(
                    x + layout["chromosomeWidth"] / 2,
                    y + height + CHROMOSOME_LABEL_OFFSET,
                ),
            )
        )
    return rows


def compute_layout(
    genes: Sequence[dict[str, Any]], encoding: dict[str, Any], table: dict[str, Any]
) -> Layout:
    layout = encoding["layout"]
    hits: list[tuple[dict[str, Any], BandHit]] = []
    unplaced: list[dict[str, Any]] = []
    for gene in genes:
        hit = place_gene(gene["chromosomalLocation"], table)
        if hit is None:
            unplaced.append(gene)
        else:
            hits.append((gene, hit))

    rows = chromosome_shapes({hit.chromosome for _, hit in hits}, table, encoding)
    chromosomes = [shape for row in rows for shape in row]
    blocks: list[GeneBlock] = []
    for shape in chromosomes:
        here = [
            (gene, hit, shape.y + hit.midpoint / shape.length * shape.height)
            for gene, hit in hits
            if hit.chromosome == shape.name
        ]
        # Same order as lib/phenogram.ts: by marker, then symbol
        # (`localeCompare(b, "en")` there, case-folded here; identical on the
        # committed symbols).
        here.sort(key=lambda item: (item[2], item[0]["gene"].casefold()))
        pills = [pills_for(gene, encoding) for gene, _, _ in here]
        heights = [block_height(len(p), layout) for p in pills]
        desired = [
            marker_y - height / 2
            for (_, _, marker_y), height in zip(here, heights, strict=True)
        ]
        tops = resolve_collisions(
            desired,
            heights,
            shape.y,
            shape.y + layout["rowHeight"],
            layout["blockGap"],
        )
        x = shape.x + layout["chromosomeWidth"] + layout["leaderGap"]
        for (gene, _hit, marker_y), pill_list, height, top in zip(
            here, pills, heights, tops, strict=True
        ):
            blocks.append(
                GeneBlock(
                    gene=gene,
                    symbol=gene["gene"],
                    chromosome=shape.name,
                    marker_y=marker_y,
                    x=x,
                    y=top,
                    width=layout["labelColumn"],
                    height=height,
                    pills=pill_list,
                    glyphs=glyphs_for(gene, encoding),
                )
            )

    return Layout(
        canvas=(layout["viewBox"][0], layout["viewBox"][1]),
        rows=tuple(tuple(row) for row in rows),
        chromosomes=tuple(chromosomes),
        blocks=tuple(blocks),
        unplaced=tuple(unplaced),
    )


def configure_output() -> None:
    """Text stays text in the SVG; the PDF/PS embed the face as TrueType."""
    from matplotlib import rcParams

    rcParams["svg.fonttype"] = "none"
    rcParams["pdf.fonttype"] = 42
    rcParams["ps.fonttype"] = 42


def configure_fonts(font: Path | None) -> None:
    from matplotlib import font_manager, rcParams

    if font is not None:
        font_manager.fontManager.addfont(str(font))
        properties = font_manager.FontProperties(fname=str(font))
        rcParams["font.family"] = properties.get_name()


def _rounded(x: float, y: float, width: float, height: float, **style: Any) -> Any:
    from matplotlib.patches import FancyBboxPatch

    return FancyBboxPatch(
        (x, y),
        width,
        height,
        boxstyle=f"round,pad=0,rounding_size={width / 2}",
        mutation_aspect=1,
        **style,
    )


def _draw_chromosome(ax: Any, shape: ChromosomeShape, encoding: dict[str, Any]) -> None:
    from matplotlib.patches import Rectangle

    outline = encoding["stains"]["gpos100"]
    for arm_y, arm_height in (shape.p_arm, shape.q_arm):
        if arm_height <= 0:
            continue
        clip = _rounded(shape.x, arm_y, shape.width, arm_height, facecolor="white")
        clip.set_edgecolor("none")
        ax.add_patch(clip)
        for band in shape.bands:
            if band.y + band.height <= arm_y or band.y >= arm_y + arm_height:
                continue
            rect = Rectangle(
                (shape.x, band.y),
                shape.width,
                band.height,
                facecolor=band.fill,
                edgecolor="none",
            )
            ax.add_patch(rect)
            rect.set_clip_path(clip)
        ax.add_patch(
            _rounded(
                shape.x,
                arm_y,
                shape.width,
                arm_height,
                facecolor="none",
                edgecolor=outline,
                linewidth=0.6,
            )
        )
    cen_y, cen_height = shape.centromere
    ax.add_patch(
        Rectangle(
            (shape.x + shape.width * 0.2, cen_y),
            shape.width * 0.6,
            cen_height,
            facecolor=encoding["stains"]["acen"],
            edgecolor="none",
        )
    )
    ax.text(
        shape.label_point[0],
        shape.label_point[1],
        shape.name,
        ha="center",
        va="baseline",
        fontsize=CHROMOSOME_FONT_UNITS * PT_PER_UNIT,
        fontweight="bold",
        color=INK,
    )


def _draw_block(ax: Any, block: GeneBlock, encoding: dict[str, Any]) -> None:
    layout = encoding["layout"]
    glyphs = encoding.get("glyphs", {})
    left = block.x - layout["leaderGap"] - layout["chromosomeWidth"]
    edge = block.x - layout["leaderGap"]
    ax.plot([left, edge], [block.marker_y, block.marker_y], color=INK, linewidth=1.2)
    ax.plot(
        [edge, block.x],
        [block.marker_y, block.y + block.height / 2],
        color=INK,
        linewidth=0.6,
    )
    pad = layout["blockPadding"]
    symbol_line = layout["symbolLine"]
    text = ax.text(
        block.x,
        block.y + pad + symbol_line * 0.78,
        block.symbol,
        ha="left",
        va="baseline",
        fontsize=SYMBOL_FONT_UNITS * PT_PER_UNIT,
        fontweight="bold",
        color=INK,
    )
    # `/` and friends are valid in a gene symbol (a curated pair label, say)
    # but not in an XML Name, which `id` must be.
    gid_symbol = re.sub(r"[^A-Za-z0-9._-]", "-", block.symbol)
    text.set_gid(f"gene-{gid_symbol}")
    glyph_y = block.y + pad + symbol_line / 2
    # Glyphs follow the symbol at its rendered width, as the island measures it.
    extent = text.get_window_extent()
    inverse = ax.transData.inverted()
    symbol_width = (
        inverse.transform((extent.x1, 0))[0] - inverse.transform((extent.x0, 0))[0]
    )
    for i, glyph in enumerate(block.glyphs):
        x = block.x + symbol_width + 2 * GLYPH_GAP + i * (GLYPH_UNITS + GLYPH_GAP)
        # The advance box stays GLYPH_UNITS wide whatever the shape does inside
        # it, so glyph positions are the island's unchanged. markersize carries
        # the per-shape scale instead of the uniform 1.1 this used to apply,
        # which inflated all three marks 10% over the island's for no stated
        # reason and could not close the ink gap it was masking.
        ax.plot(
            [x + GLYPH_UNITS / 2],
            [glyph_y - glyph_lift(glyph.shape, glyphs)],
            marker=glyph_marker(glyph.shape, glyphs),
            markersize=GLYPH_UNITS * PT_PER_UNIT * glyph_scale(glyph.shape, glyphs),
            color=INK,
            linestyle="none",
        )
    for i, pill in enumerate(block.pills):
        top = block.y + pad + symbol_line + i * layout["pillLine"]
        ax.text(
            block.x + PILL_PADDING_X,
            top + layout["pillLine"] / 2,
            pill.label,
            ha="left",
            va="center",
            fontsize=PILL_FONT_UNITS * PT_PER_UNIT,
            color=INK,
            bbox={
                "boxstyle": "round,pad=0.3",
                "facecolor": pill.fill,
                "edgecolor": pill.stroke,
                "linewidth": 0.5,
            },
        )


def _add_legends(fig: Any, encoding: dict[str, Any]) -> None:
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    evidence = [
        Line2D(
            [],
            [],
            marker=glyph_marker(entry["shape"], encoding.get("glyphs", {})),
            linestyle="none",
            color=INK,
            markersize=LEGEND_GLYPH_PT * glyph_scale(
                entry["shape"], encoding.get("glyphs", {})
            ),
            label=entry["label"],
        )
        for entry in encoding["evidence"]
    ]
    families = []
    for family in encoding["families"]:
        traits = [
            t["label"] for t in encoding["traits"] if t["family"] == family["key"]
        ]
        families.append(
            Patch(
                facecolor=family["tint"],
                edgecolor=family["hue"],
                label=f"{family['label']}: {', '.join(traits)}",
            )
        )
    fig.legend(
        handles=evidence,
        title="Supporting evidence",
        loc="lower left",
        bbox_to_anchor=(0.0, 1.0),
        fontsize=LEGEND_FONT_PT,
        title_fontsize=LEGEND_FONT_PT,
        frameon=False,
    )
    fig.legend(
        handles=families,
        title="GWAS phenotypes",
        loc="lower left",
        bbox_to_anchor=(0.22, 1.0),
        ncols=2,
        fontsize=LEGEND_FONT_PT,
        title_fontsize=LEGEND_FONT_PT,
        frameon=False,
    )


def draw(
    genes: Sequence[dict[str, Any]], encoding: dict[str, Any], table: dict[str, Any]
) -> Any:
    """The figure, ready for `savefig(..., bbox_inches="tight")`."""
    import matplotlib

    matplotlib.use("Agg")
    configure_output()
    from matplotlib import pyplot as plt

    layout = compute_layout(genes, encoding, table)
    width, height = layout.canvas
    fig = plt.figure(figsize=(width / UNITS_PER_INCH, height / UNITS_PER_INCH))
    ax = fig.add_axes((0.0, 0.0, 1.0, 1.0))
    ax.set_xlim(0, width)
    ax.set_ylim(height, 0)
    ax.set_aspect("equal")
    ax.axis("off")
    for shape in layout.chromosomes:
        _draw_chromosome(ax, shape, encoding)
    for block in layout.blocks:
        _draw_block(ax, block, encoding)
    _add_legends(fig, encoding)
    return fig


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--genes", type=Path, default=DEFAULT_GENES)
    parser.add_argument("--cytobands", type=Path, default=DEFAULT_CYTOBANDS)
    parser.add_argument("--encoding", type=Path, default=DEFAULT_ENCODING)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--format", nargs="+", choices=FORMATS, default=list(FORMATS))
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument(
        "--font",
        type=Path,
        default=None,
        help="a .ttf/.otf to render with (journals usually want Arial/Helvetica)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_fonts(args.font)
    genes = load_genes(args.genes)
    if not genes:
        # A repository generated for another disease starts from
        # `deno task data:empty`; an empty karyogram would only look like a bug.
        print(f"{args.genes}: no gene rows; nothing to draw.", file=sys.stderr)
        return 0
    encoding = load_encoding(args.encoding)
    table = load_cytobands(args.cytobands)

    layout = compute_layout(genes, encoding, table)
    if layout.unplaced:
        unplaced = ", ".join(
            f"{gene['gene']} ({gene['chromosomalLocation']})"
            for gene in layout.unplaced
        )
        print(f"Unplaced (no hg38 band): {unplaced}", file=sys.stderr)

    fig = draw(genes, encoding, table)
    args.out.mkdir(parents=True, exist_ok=True)
    for fmt in args.format:
        target = args.out / f"phenogram.{fmt}"
        fig.savefig(target, format=fmt, dpi=args.dpi, bbox_inches="tight")
        print(f"Wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
