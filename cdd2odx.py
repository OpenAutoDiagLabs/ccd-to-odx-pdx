#!/usr/bin/env python3
"""Convert a Vector CANdela .cdd file into an ODX 2.2 .pdx container.

cdd_utils (patched, see patches/) reads the CDD; odxtools builds and writes the ODX.
Usage: python cdd2odx.py INPUT.cdd OUTPUT.pdx [--bitfield-order msb|lsb]
"""
from __future__ import annotations

import argparse
import collections
import dataclasses
import pathlib
import re
import sys
import warnings
import xml.etree.ElementTree as ET

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "third_party" / "cdd_utils"))

import odxtools
from cdd import load_cdd_from_candela
from cdd.codec.codec_utils import parse_textmap_s
from cdd.database import CddDatabase
from cdd.models import candela as cdd_xml
from cdd.models.base import CodedElement
from cdd.models.containers import DidElement, MultiplexedElement, NumIterElement, StructElement
from cdd.models.elements import ConstElement, LinCompElement, PlaceholderElement, TextTableElement
from odxtools.addressing import Addressing
from odxtools.diaglayers.basevariant import BaseVariant
from odxtools.compumethods.compucategory import CompuCategory
from odxtools.compumethods.compuconst import CompuConst
from odxtools.compumethods.compuinternaltophys import CompuInternalToPhys
from odxtools.compumethods.compurationalcoeffs import CompuRationalCoeffs
from odxtools.compumethods.compuscale import CompuScale
from odxtools.compumethods.identicalcompumethod import IdenticalCompuMethod
from odxtools.compumethods.limit import Limit
from odxtools.compumethods.linearcompumethod import LinearCompuMethod
from odxtools.compumethods.texttablecompumethod import TexttableCompuMethod
from odxtools.database import Database
from odxtools.dataobjectproperty import DataObjectProperty
from odxtools.diagdatadictionaryspec import DiagDataDictionarySpec
from odxtools.diaglayercontainer import DiagLayerContainer
from odxtools.diaglayers.basevariantraw import BaseVariantRaw
from odxtools.diaglayers.diaglayertype import DiagLayerType
from odxtools.diagservice import DiagService
from odxtools.endofpdufield import EndOfPduField
from odxtools.functionalclass import FunctionalClass
from odxtools.minmaxlengthtype import MinMaxLengthType
from odxtools.nameditemlist import NamedItemList
from odxtools.odxlink import DocType, OdxDocFragment, OdxLinkId, OdxLinkRef
from odxtools.odxtypes import DataType
from odxtools.parameters.codedconstparameter import CodedConstParameter
from odxtools.parameters.reservedparameter import ReservedParameter
from odxtools.parameters.valueparameter import ValueParameter
from odxtools.physicaltype import PhysicalType
from odxtools.request import Request
from odxtools.response import Response, ResponseType
from odxtools.standardlengthtype import StandardLengthType
from odxtools.structure import Structure
from odxtools.termination import Termination
from xsdata.formats.dataclass.parsers import XmlParser
from xsdata.formats.dataclass.parsers.config import ParserConfig

STRING_TYPES = {"asc": DataType.A_ASCIISTRING, "utf": DataType.A_UTF8STRING}


def _v(x) -> str:
    """cdd_utils mixes enums and plain strings for the same attribute."""
    return str(getattr(x, "value", x))


def _text(name) -> str:
    return name.tuv[0].value or "" if name is not None and name.tuv else ""


def _short_name(s: str) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_]", "_", s)).strip("_") or "unnamed"


def _unique(name: str, used: set) -> str:
    candidate, n = name, 2
    while candidate in used:
        candidate, n = f"{name}_{n}", n + 1
    used.add(candidate)
    return candidate


def _cardinality_losses(path: pathlib.Path) -> collections.Counter:
    """Repeated XML children that land in a single-valued cdd_utils field are silently dropped; count them."""
    single = collections.defaultdict(set)
    for cls in vars(cdd_xml).values():
        if dataclasses.is_dataclass(cls) and hasattr(cls, "Meta"):
            for f in dataclasses.fields(cls):
                if f.metadata.get("type") == "Element" and "list" not in str(f.type).lower():
                    single[cls.Meta.name].add(f.metadata.get("name", f.name.upper()))
    lost = collections.Counter()
    for parent in ET.parse(path).iter():
        for tag, n in collections.Counter(c.tag for c in parent).items():
            if n > 1 and tag in single.get(parent.tag, ()):
                lost[f"{parent.tag}/{tag}"] += n - 1
    return lost


def load(path: str | pathlib.Path) -> CddDatabase:
    """Parse leniently: CDD metadata elements (EMAIL, ATTACHPROXY, ...) are not modelled by cdd_utils."""
    path = pathlib.Path(path)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        candela = XmlParser(config=ParserConfig(
            fail_on_unknown_properties=False, fail_on_unknown_attributes=False)).from_path(path, cdd_xml.Candela)
    db = load_cdd_from_candela(candela, strict=False)
    db.warnings.extend(f"xsdata: {w.message}" for w in caught)
    db.warnings.extend(f"DATA LOSS: {n} x {k} dropped by cdd_utils model" for k, n in _cardinality_losses(path).items())
    return db


class Converter:
    def __init__(self, db: CddDatabase, bitfield_order: str = "msb"):
        self.db = db
        self.lsb_first = bitfield_order == "lsb"
        self.dlc_name = _short_name(db.ecu.qualifier or _text(db.ecu.name))
        self.frags = (OdxDocFragment(self.dlc_name, DocType.CONTAINER),)
        self.ids: set = set()
        self.dops: dict = {}      # signature -> DataObjectProperty
        self.structures: dict = {}  # id(cdd element) -> (Structure, bit length)
        self.eopfs: dict = {}     # id(cdd element) -> EndOfPduField
        self.structure_names: set = set()
        self.warnings: list[str] = []
        self.service_names: dict[str, str] = {}  # cdd service qualifier -> ODX short name

    def _id(self, kind: str, name: str) -> OdxLinkId:
        return OdxLinkId(_unique(f"{self.dlc_name}.{kind}.{name}", self.ids), self.frags)

    def _position(self, cursor: int, nbits: int) -> tuple[int, int | None]:
        byte, offset = divmod(cursor, 8)
        if offset == 0 and nbits % 8 == 0:
            return byte, None
        if self.lsb_first:
            return byte, offset
        # cdd_utils packs bits MSB-first; ODX BIT-POSITION counts from the LSB of the extracted bytes
        nbytes = -(-(offset + nbits) // 8)
        return byte, nbytes * 8 - offset - nbits

    # --- data types --------------------------------------------------------------------------

    @staticmethod
    def _coded_type(e: CodedElement):
        """Returns (diag coded type, bit length or None if variable length)."""
        enc, is_field = _v(e.encoding), _v(e.quantity) == "field"
        if is_field:
            base = STRING_TYPES.get(enc, DataType.A_BYTEFIELD)
            if e.minsz != e.maxsz:
                return MinMaxLengthType(base_data_type=base, min_length=e.bit_length * e.minsz // 8,
                                        max_length=e.bit_length * e.maxsz // 8,
                                        termination=Termination.END_OF_PDU), None
            bits = e.bit_length * e.minsz
        else:
            bits = e.bit_length
            base = {"sgn": DataType.A_INT32, "flt": DataType.A_FLOAT32, "dbl": DataType.A_FLOAT64}.get(
                enc, DataType.A_UINT32 if bits <= 32 else DataType.A_BYTEFIELD)
        return StandardLengthType(base_data_type=base, bit_length=bits), bits

    def _dop(self, e: CodedElement, coded) -> DataObjectProperty:
        internal = coded.base_data_type
        textmap = [tm for tm in getattr(e, "textmap", ()) if isinstance(tm.s, int)]
        comp = getattr(e, "comp", None)
        if isinstance(e, TextTableElement) and textmap and internal == DataType.A_UINT32:
            ranges = [(tm.s, tm.e if isinstance(tm.e, int) else tm.s, _text(tm.text)) for tm in textmap]
            counts = collections.Counter(text for _, _, text in ranges)
            # ODX needs a unique text per scale to encode; suffix the raw range on empty or repeated texts
            entries = tuple(
                (lo, hi, text if text and counts[text] == 1 else
                 f"{text} ({f'0x{lo:X}' if lo == hi else f'0x{lo:X}-0x{hi:X}'})".lstrip())
                for lo, hi, text in ranges)
            key = ("texttable", repr(coded), entries)
            physical = DataType.A_UNICODE2STRING
            make_compu = lambda: TexttableCompuMethod(
                category=CompuCategory.TEXTTABLE, internal_type=internal, physical_type=physical,
                compu_internal_to_phys=CompuInternalToPhys(compu_scales=[
                    CompuScale(compu_const=CompuConst(vt=text,
                                                      data_type=physical),
                               lower_limit=Limit(value_raw=str(s), value_type=internal),
                               upper_limit=Limit(value_raw=str(hi), value_type=internal),
                               domain_type=internal, range_type=physical)
                    for s, hi, text in entries]))
        elif isinstance(e, LinCompElement) and comp is not None and internal == DataType.A_UINT32:
            # CANdela: phys = raw * f / div + o
            f, div, o = comp.f if comp.f is not None else 1, comp.div or 1, comp.o or 0
            integral = div == 1 and float(f).is_integer() and float(o).is_integer()
            physical = DataType.A_INT32 if integral else DataType.A_FLOAT64
            key = ("linear", repr(coded), f, div, o)
            make_compu = lambda: LinearCompuMethod(
                category=CompuCategory.LINEAR, internal_type=internal, physical_type=physical,
                compu_internal_to_phys=CompuInternalToPhys(compu_scales=[CompuScale(
                    compu_rational_coeffs=CompuRationalCoeffs(
                        value_type=physical, numerators=[o * div, f], denominators=[div]),
                    domain_type=internal, range_type=physical)]))
        else:
            physical = internal
            key = ("identical", repr(coded))
            make_compu = lambda: IdenticalCompuMethod(
                category=CompuCategory.IDENTICAL, internal_type=internal, physical_type=physical)
        if key not in self.dops:
            name = _short_name(e.qualifier or _text(e.name))
            self.dops[key] = DataObjectProperty(
                odx_id=self._id("DOP", name), short_name=_unique(name, {d.short_name for d in self.dops.values()}),
                long_name=_text(e.name) or None, diag_coded_type=coded,
                physical_type=PhysicalType(base_data_type=physical), compu_method=make_compu())
        return self.dops[key]

    # --- parameters ----------------------------------------------------------------------------

    def _params(self, elements, top: bool) -> tuple[list, int]:
        out, used, cursor = [], set(), 0
        for e in elements:
            cursor = self._param(e, cursor, top, out, used)
        return out, cursor

    def _param(self, e, cursor: int, top: bool, out: list, used: set) -> int:
        label = _text(e.name)
        name = _unique(_short_name(e.qualifier or label), used)
        long_name = label or None

        if isinstance(e, (StructElement, DidElement)):
            children = getattr(e, "children", ())
            if not children:
                return cursor
            repeated = isinstance(e, StructElement) and e.max_num_of_items != 1
            if cursor % 8 and not repeated:
                # ODX structures start on a byte boundary; inline the members instead.
                for child in children:
                    cursor = self._param(child, cursor, False, out, used)
                return cursor
            structure, nbits = self._structure(e)
            dop = structure
            if repeated:
                key = id(e)
                if key not in self.eopfs:
                    self.eopfs[key] = EndOfPduField(
                        odx_id=self._id("EOPF", structure.short_name), short_name=structure.short_name,
                        structure_ref=OdxLinkRef.from_id(structure.odx_id),
                        min_number_of_items=e.min_num_of_items, max_number_of_items=e.max_num_of_items)
                dop = self.eopfs[key]
            out.append(ValueParameter(short_name=name, long_name=long_name, byte_position=cursor // 8,
                                      dop_ref=OdxLinkRef.from_id(dop.odx_id)))
            return cursor + nbits

        if isinstance(e, (MultiplexedElement, NumIterElement)):
            # Only empty instances (unresolved envData refs) exist in practice; cdd_utils encodes nothing for them.
            self.warnings.append(f"skipped unsupported {type(e).__name__} '{label}'")
            return cursor

        if not isinstance(e, CodedElement):
            self.warnings.append(f"skipped unknown element {type(e).__name__} '{label}'")
            return cursor

        coded, nbits = self._coded_type(e)
        byte, bit = self._position(cursor, nbits or 8)
        common = dict(short_name=name, long_name=long_name, byte_position=byte, bit_position=bit)
        is_plain = type(e) is CodedElement
        if isinstance(e, PlaceholderElement) or (not top and is_plain and e.constvalue == 0):
            out.append(ReservedParameter(bit_length=nbits, **common))
        elif e.constvalue is not None and nbits and (top or isinstance(e, ConstElement)):
            value = parse_textmap_s(e.constvalue)  # multi-byte constants come as '(0,0,96,79,25)'
            raw = value.to_bytes(nbits // 8, "big").hex() if coded.base_data_type == DataType.A_BYTEFIELD else str(value)
            out.append(CodedConstParameter(coded_value_raw=raw, diag_coded_type=coded,
                                           semantic="SERVICE-ID" if e.spec == "sid" else None, **common))
        else:
            out.append(ValueParameter(dop_ref=OdxLinkRef.from_id(self._dop(e, coded).odx_id), **common))
        return cursor + (nbits or 0)

    def _structure(self, e) -> tuple[Structure, int]:
        key = id(e)  # cdd_utils ids are not unique per record; identical records share one frozen object
        if key not in self.structures:
            params, nbits = self._params(e.children, top=False)
            name = _unique(_short_name(e.qualifier or _text(e.name)), self.structure_names)
            self.structures[key] = (Structure(
                odx_id=self._id("STRUCT", name), short_name=name, long_name=_text(e.name) or None,
                parameters=NamedItemList(params)), nbits)
        return self.structures[key]

    # --- layers --------------------------------------------------------------------------------

    def _used_services(self):
        """Yields (functional class name, service) for every service instantiated by a DIAGINST."""
        for group in self.db.ecu.diag_list:
            for inst in getattr(group, "diagInst", None) or (group,):
                for svc in inst.services:
                    if svc.is_used:
                        yield _short_name(group.qualifier or _text(group.name)), svc

    def convert(self) -> Database:
        requests, pos_responses, neg_responses, services, fclasses = [], [], [], [], {}
        names: set = set()
        for fclass, svc in self._used_services():
            if fclass not in fclasses:
                fclasses[fclass] = FunctionalClass(odx_id=self._id("FC", fclass), short_name=fclass)
            name = _unique(_short_name(svc.qualifier or _text(svc.service_name)), names)
            self.service_names[svc.qualifier] = name

            params, _ = self._params(svc.request.elements, top=True)
            rq = Request(odx_id=self._id("RQ", name), short_name=f"RQ_{name}", parameters=NamedItemList(params))
            requests.append(rq)
            resp_refs = {}
            for kind, msg, bucket, rtype in (("PR", svc.positive_responses, pos_responses, ResponseType.POSITIVE),
                                             ("NR", svc.negative_responses, neg_responses, ResponseType.NEGATIVE)):
                resp_refs[kind] = []
                if msg is None:
                    continue
                params, _ = self._params(msg.elements, top=True)
                resp = Response(odx_id=self._id(kind, name), short_name=f"{kind}_{name}",
                                response_type=rtype, parameters=NamedItemList(params))
                bucket.append(resp)
                resp_refs[kind].append(OdxLinkRef.from_id(resp.odx_id))

            addressing = (Addressing.FUNCTIONAL_OR_PHYSICAL if svc.func and svc.phys
                          else Addressing.FUNCTIONAL if svc.func else Addressing.PHYSICAL)
            services.append(DiagService(
                odx_id=self._id("DC", name), short_name=name, long_name=_text(svc.service_name) or None,
                functional_class_refs=[OdxLinkRef.from_id(fclasses[fclass].odx_id)],
                request_ref=OdxLinkRef.from_id(rq.odx_id), pos_response_refs=resp_refs["PR"],
                neg_response_refs=resp_refs["NR"], addressing_raw=addressing))

        ddds = DiagDataDictionarySpec(
            data_object_props=NamedItemList(self.dops.values()),
            structures=NamedItemList(s for s, _ in self.structures.values()),
            end_of_pdu_fields=NamedItemList(self.eopfs.values()))
        base_variant = BaseVariant(diag_layer_raw=BaseVariantRaw(
            variant_type=DiagLayerType.BASE_VARIANT, odx_id=self._id("BV", self.dlc_name),
            short_name=self.dlc_name, long_name=_text(self.db.ecu.name) or None,
            functional_classes=NamedItemList(fclasses.values()), diag_data_dictionary_spec=ddds,
            diag_comms_raw=services, requests=NamedItemList(requests),
            positive_responses=NamedItemList(pos_responses), negative_responses=NamedItemList(neg_responses)))
        dlc = DiagLayerContainer(odx_id=self._id("DLC", self.dlc_name), short_name=self.dlc_name,
                                 long_name=_text(self.db.ecu.name) or None,
                                 base_variants=NamedItemList([base_variant]))
        database = Database()
        database.short_name = self.dlc_name
        database._diag_layer_containers = NamedItemList([dlc])
        database.refresh()
        return database


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cdd")
    ap.add_argument("pdx")
    ap.add_argument("--bitfield-order", choices=("msb", "lsb"), default="msb",
                    help="how sub-byte fields fill a byte; msb matches cdd_utils (default)")
    args = ap.parse_args()

    db = load(args.cdd)
    conv = Converter(db, args.bitfield_order)
    database = conv.convert()
    odxtools.write_pdx_file(args.pdx, database)
    for w in db.warnings + conv.warnings:
        print("warning:", w, file=sys.stderr)
    print(f"wrote {args.pdx}: {len(conv.service_names)} services, {len(conv.dops)} DOPs, "
          f"{len(conv.structures)} structures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
