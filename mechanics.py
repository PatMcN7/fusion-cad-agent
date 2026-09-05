"""Closed-form mechanics: the designer's-eye layer.

An engineer does not run FEA on every part. They run section properties, beam
formulas and ratios -- good to 10-20%, which is plenty to choose between
options -- and reach for analysis only when intuition runs out.

Everything here is arithmetic, microseconds per part, and it reports
*numbers* rather than verdicts. A boolean only lets you conform; a metric lets
you compare and optimise.

Every rule carries its validity domain. A beam formula applied to a short deep
member gives confident nonsense, which is the vacuous-assertion trap wearing
different clothes.

Units are inch-pound-second throughout: inches, pounds force, psi.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

CM_PER_INCH = 2.54


# ------------------------------------------------------------ materials ----

@dataclass(frozen=True)
class Material:
    name: str
    E: float               # Young's modulus, psi
    density: float         # lb/in^3
    yield_strength: float  # psi
    ultimate: float        # psi
    G: float = 0.0         # shear modulus, psi

    @property
    def specific_stiffness(self) -> float:
        return self.E / self.density

    @property
    def specific_strength(self) -> float:
        return self.yield_strength / self.density


MATERIALS: dict[str, Material] = {
    "6061-T6":   Material("6061-T6",   10.0e6, 0.098,  40_000,  45_000, 3.77e6),
    "7075-T6":   Material("7075-T6",   10.4e6, 0.102,  73_000,  83_000, 3.90e6),
    "2024-T3":   Material("2024-T3",   10.6e6, 0.100,  50_000,  70_000, 4.00e6),
    "1018":      Material("1018",      29.0e6, 0.284,  54_000,  64_000, 11.5e6),
    "4130":      Material("4130",      29.7e6, 0.283,  63_000,  97_000, 11.5e6),
    "Ti-6Al-4V": Material("Ti-6Al-4V", 16.5e6, 0.160, 128_000, 138_000, 6.20e6),
    "PLA":       Material("PLA",        0.50e6, 0.045,  7_000,   8_000, 0.19e6),
}


def material(name: str) -> Material:
    if name not in MATERIALS:
        raise KeyError(f"unknown material '{name}'; have {sorted(MATERIALS)}")
    return MATERIALS[name]


# ------------------------------------------------------ section geometry ----

@dataclass(frozen=True)
class Section:
    """Area properties of a prismatic cross-section, about its centroid."""

    label: str
    area: float        # in^2
    Ix: float          # in^4, about the horizontal centroidal axis
    Iy: float          # in^4, about the vertical centroidal axis
    height: float      # in, extreme fibre to extreme fibre in Y
    width: float       # in

    @property
    def Sx(self) -> float:
        """Section modulus: I / c. Bending stress is M / Sx."""
        return self.Ix / (self.height / 2) if self.height else 0.0

    @property
    def Sy(self) -> float:
        return self.Iy / (self.width / 2) if self.width else 0.0

    @property
    def rx(self) -> float:
        """Radius of gyration -- what slenderness is measured in."""
        return math.sqrt(self.Ix / self.area) if self.area else 0.0

    @property
    def ry(self) -> float:
        return math.sqrt(self.Iy / self.area) if self.area else 0.0

    def mass_per_foot(self, mat: Material) -> float:
        return self.area * 12.0 * mat.density

    def as_dict(self) -> dict[str, float]:
        return {
            "label": self.label, "area_in2": self.area,
            "Ix_in4": self.Ix, "Iy_in4": self.Iy,
            "Sx_in3": self.Sx, "Sy_in3": self.Sy,
            "rx_in": self.rx, "ry_in": self.ry,
            "height_in": self.height, "width_in": self.width,
        }


def rect(width: float, height: float) -> Section:
    return Section("solid rect", width * height,
                   width * height**3 / 12, height * width**3 / 12,
                   height, width)


def rect_tube(width: float, height: float, wall: float) -> Section:
    if wall * 2 >= min(width, height):
        raise ValueError("wall consumes the section")
    iw, ih = width - 2 * wall, height - 2 * wall
    return Section(
        f"rect tube {width}x{height}x{wall}",
        width * height - iw * ih,
        (width * height**3 - iw * ih**3) / 12,
        (height * width**3 - ih * iw**3) / 12,
        height, width,
    )


def round_tube(outer_diameter: float, wall: float) -> Section:
    inner = outer_diameter - 2 * wall
    if inner <= 0:
        raise ValueError("wall consumes the section")
    area = math.pi / 4 * (outer_diameter**2 - inner**2)
    inertia = math.pi / 64 * (outer_diameter**4 - inner**4)
    return Section(f"round tube {outer_diameter}x{wall}", area, inertia, inertia,
                   outer_diameter, outer_diameter)


def plate(width: float, thickness: float) -> Section:
    return rect(width, thickness)


def net_section(section: Section, hole_diameter: float, holes_through: int,
                wall: float) -> float:
    """Fraction of area left at the worst hole station."""
    removed = holes_through * hole_diameter * wall
    return max(0.0, (section.area - removed) / section.area)


def section_from_prismatic(volume_in3: float, length_in: float,
                           mass_moment: float | None = None) -> float:
    """Cross-sectional area of a prismatic body from its volume."""
    return volume_in3 / length_in if length_in else 0.0


# ----------------------------------------------------------- beam cases ----

@dataclass
class BeamResult:
    case: str
    deflection: float      # in
    max_moment: float      # lb-in
    bending_stress: float  # psi
    span: float
    notes: list[str] = field(default_factory=list)

    @property
    def span_over_deflection(self) -> float:
        return self.span / self.deflection if self.deflection else float("inf")

    def margin(self, mat: Material) -> float:
        return mat.yield_strength / self.bending_stress if self.bending_stress else float("inf")

    def as_dict(self, mat: Material | None = None) -> dict[str, Any]:
        out = {
            "case": self.case,
            "deflection_in": self.deflection,
            "span_over_deflection": self.span_over_deflection,
            "max_moment_lb_in": self.max_moment,
            "bending_stress_psi": self.bending_stress,
            "notes": self.notes,
        }
        if mat:
            out["yield_margin"] = self.margin(mat)
        return out


def _beam(case, deflection, moment, section, span) -> BeamResult:
    stress = moment / section.Sx if section.Sx else float("inf")
    notes = []
    # Euler-Bernoulli ignores shear deflection; below roughly 10:1 span-to-depth
    # that assumption starts to matter and the answer reads stiffer than reality.
    if section.height and span / section.height < 10:
        notes.append(
            f"span/depth {span / section.height:.1f} < 10 -- shear deflection "
            "is not captured; treat the number as optimistic"
        )
    return BeamResult(case, deflection, moment, stress, span, notes)


def cantilever_point(load, span, mat: Material, section: Section) -> BeamResult:
    return _beam("cantilever, point at tip",
                 load * span**3 / (3 * mat.E * section.Ix),
                 load * span, section, span)


def cantilever_udl(load_per_inch, span, mat: Material, section: Section) -> BeamResult:
    return _beam("cantilever, uniform load",
                 load_per_inch * span**4 / (8 * mat.E * section.Ix),
                 load_per_inch * span**2 / 2, section, span)


def simply_supported_point(load, span, mat: Material, section: Section) -> BeamResult:
    return _beam("simply supported, point at midspan",
                 load * span**3 / (48 * mat.E * section.Ix),
                 load * span / 4, section, span)


def simply_supported_udl(load_per_inch, span, mat: Material, section: Section) -> BeamResult:
    return _beam("simply supported, uniform load",
                 5 * load_per_inch * span**4 / (384 * mat.E * section.Ix),
                 load_per_inch * span**2 / 8, section, span)


def fixed_fixed_point(load, span, mat: Material, section: Section) -> BeamResult:
    return _beam("fixed both ends, point at midspan",
                 load * span**3 / (192 * mat.E * section.Ix),
                 load * span / 8, section, span)


BEAM_CASES: dict[str, Callable] = {
    "cantilever_point": cantilever_point,
    "cantilever_udl": cantilever_udl,
    "simply_supported_point": simply_supported_point,
    "simply_supported_udl": simply_supported_udl,
    "fixed_fixed_point": fixed_fixed_point,
}


# --------------------------------------------------------- plates, buckling ----

def flat_plate_udl_simply_supported(pressure, a, b, thickness, mat: Material,
                                    poisson: float = 0.33) -> dict[str, Any]:
    """Rectangular plate, uniform pressure, simply supported. Roark table 11.4."""
    ratio = max(a, b) / min(a, b)
    alpha_table = {1.0: 0.0443, 1.2: 0.0616, 1.4: 0.0770, 1.6: 0.0906,
                   1.8: 0.1017, 2.0: 0.1110, 3.0: 0.1335, 100.0: 0.1421}
    keys = sorted(alpha_table)
    alpha = alpha_table[min(keys, key=lambda k: abs(k - ratio))]
    short = min(a, b)
    stiffness = mat.E * thickness**3 / (12 * (1 - poisson**2))
    deflection = alpha * pressure * short**4 / stiffness
    notes = []
    if deflection > thickness / 2:
        notes.append(
            f"deflection {deflection:.4f} in exceeds half the thickness -- "
            "small-deflection plate theory no longer applies, real stiffness "
            "will be higher due to membrane action"
        )
    return {"deflection_in": deflection, "aspect_ratio": ratio,
            "alpha": alpha, "notes": notes}


def euler_buckling(section: Section, length, mat: Material, end_fixity: float = 1.0):
    """Critical compressive load. end_fixity K: 1.0 pinned, 0.5 fixed, 2.0 free."""
    inertia = min(section.Ix, section.Iy)
    effective = end_fixity * length
    critical = math.pi**2 * mat.E * inertia / effective**2
    radius = math.sqrt(inertia / section.area)
    slenderness = effective / radius
    notes = []
    # Below the transition slenderness the column yields before it buckles, so
    # the Euler load is not the governing limit.
    transition = math.sqrt(2 * math.pi**2 * mat.E / mat.yield_strength)
    if slenderness < transition:
        notes.append(
            f"slenderness {slenderness:.0f} < transition {transition:.0f} -- "
            "this is a short column; yield governs, not Euler buckling"
        )
    return {"critical_load_lb": critical, "slenderness": slenderness,
            "transition_slenderness": transition, "notes": notes}


def kt_hole_in_plate(hole_diameter: float, plate_width: float) -> float:
    """Stress concentration for a transverse hole in a plate under tension.

    Heywood's fit to Howland's solution. Approaches 3.0 for a small hole in a
    wide plate, which is the number worth carrying in your head.
    """
    r = hole_diameter / plate_width
    if not 0 < r < 1:
        raise ValueError("hole must be smaller than the plate width")
    return 3.00 - 3.13 * r + 3.66 * r**2 - 1.53 * r**3


# --------------------------------------------------------------- rules ----

@dataclass
class Rule:
    name: str
    value: float
    limit: float
    comparison: str          # ">=" or "<="
    units: str
    source: str
    severity: str = "warning"

    @property
    def ok(self) -> bool:
        return self.value >= self.limit if self.comparison == ">=" else self.value <= self.limit

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "value": self.value, "limit": self.limit,
                "comparison": self.comparison, "units": self.units,
                "source": self.source, "ok": self.ok, "severity": self.severity}


def hole_rules(hole_diameter, edge_distance, pitch, wall) -> list[Rule]:
    """Standard bolted-joint ratios.

    ``edge_distance`` is measured from the hole CENTRE to the edge of the part,
    which is how every reference quotes the 2D rule. It is not the clear
    material beyond the hole, which is smaller by one radius -- confusing the
    two understates the ratio by 0.5D.
    """
    if edge_distance < hole_diameter / 2:
        raise ValueError(
            f"edge_distance {edge_distance:.4f} is less than the hole radius; "
            "this is measured centre-to-edge, not from the hole's edge"
        )
    return [
        Rule("hole_edge_distance", edge_distance / hole_diameter, 2.0, ">=", "D",
             "Machinery's Handbook; 2D minimum, 2.5D preferred"),
        Rule("hole_pitch", pitch / hole_diameter, 3.0, ">=", "D",
             "standard practice for bolted joints"),
        Rule("wall_per_hole_diameter", wall / hole_diameter, 1.0, ">=", "x",
             "thread engagement and bearing area"),
    ]


def thread_rules(diameter, engagement, into_material: str) -> list[Rule]:
    # Aluminium needs more engagement than steel for the same strength.
    minimum = 1.0 if into_material in {"1018", "4130", "Ti-6Al-4V"} else 1.5
    return [
        Rule("thread_engagement", engagement / diameter, minimum, ">=", "D",
             f"engagement into {into_material}; 1D steel, 1.5-2D aluminium"),
    ]


def machining_rules(pocket_depth, wall_thickness, internal_radius) -> list[Rule]:
    rules = [
        Rule("wall_aspect_ratio", pocket_depth / wall_thickness, 8.0, "<=", ":1",
             "milling; beyond this the wall chatters and deflects"),
    ]
    if internal_radius > 0:
        rules.append(
            Rule("corner_tool_reach", pocket_depth / (2 * internal_radius), 10.0, "<=", ":1",
                 "internal corner radius implies a tool diameter and reach"))
    return rules


def structure_rules(span, deflection, section: Section) -> list[Rule]:
    rules = [
        Rule("deflection_limit", span / deflection if deflection else float("inf"),
             360.0, ">=", "L/x", "conventional structural serviceability limit"),
    ]
    if section.height:
        rules.append(
            Rule("beam_slenderness", span / section.height, 10.0, ">=", ":1",
                 "Euler-Bernoulli validity; below this shear matters"))
    return rules


def plate_rules(span, thickness, unsupported_overhang: float = 0.0) -> list[Rule]:
    rules = [
        Rule("plate_span_ratio", span / thickness, 200.0, "<=", ":1",
             "sheet panel practice; beyond this it oil-cans"),
    ]
    if unsupported_overhang > 0:
        rules.append(
            Rule("cantilever_ratio", unsupported_overhang / thickness, 20.0, "<=", ":1",
                 "unsupported edge of a panel"))
    return rules


# --------------------------------------------------------------- report ----

def section_facts(section: Section, mat: Material,
                  rules: Sequence[Rule] = ()) -> dict[str, Any]:
    """Properties that need no assumptions. Safe to compute always.

    Section modulus, second moment, mass per foot, stiffness per unit area --
    all pure geometry. These answer "did that change make it stiffer?" without
    anyone declaring a load, which is most of what comparative design needs.
    """
    flags = [r.as_dict() for r in rules]
    return {
        "material": mat.name,
        "section": section.as_dict(),
        "mass_per_foot_lb": section.mass_per_foot(mat),
        "stiffness_per_area": section.Ix / section.area if section.area else 0.0,
        "rules": flags,
        "violations": [f for f in flags if not f["ok"]],
    }


def load_case(section: Section, mat: Material, *, span: float, load: float,
              case: str = "simply_supported_point") -> dict[str, Any]:
    """A response, not a fact. Only meaningful once someone declares a load.

    Never call this with an invented load. A deflection computed from a guessed
    load reads as analysis and is fabricated precision.
    """
    if case not in BEAM_CASES:
        raise KeyError(f"unknown beam case '{case}'; have {sorted(BEAM_CASES)}")
    result = BEAM_CASES[case](load, span, mat, section)
    out = result.as_dict(mat)
    flags = [r.as_dict() for r in structure_rules(span, result.deflection, section)]
    out["rules"] = flags
    out["violations"] = [f for f in flags if not f["ok"]]
    return out


def report(section: Section, mat: Material, *, span: float | None = None,
           load: float | None = None, case: str = "simply_supported_point",
           rules: Sequence[Rule] = ()) -> dict[str, Any]:
    """Facts always; a load case only when one was actually supplied."""
    out = section_facts(section, mat, rules)
    if span is not None and load is not None:
        out["beam"] = load_case(section, mat, span=span, load=load, case=case)
        out["violations"] = out["violations"] + out["beam"]["violations"]
    else:
        out["beam"] = None
        out["beam_note"] = (
            "no load case declared, so no deflection or stress is reported. "
            "Section properties above are enough to compare designs."
        )
    return out
