# cdd2odx

Converts a Vector CANdela `.cdd` diagnostic description into an ODX 2.2 `.pdx`.
[vector_candela_ccd_parser](https://github.com/OpenAutoDiagLabs/vector_candela_ccd_parser) reads the CDD and
[odxtools](https://github.com/mercedes-benz/odxtools) writes the ODX.

```sh
./setup.sh                                   # clones cdd_utils @ pinned commit, applies patches/, makes .venv
.venv/bin/python cdd2odx.py in.cdd out.pdx   # convert
.venv/bin/python test_cdd2odx.py in.cdd      # round-trip check, exits 1 on any mismatch
```

## What the patch fixes in cdd_utils

- `STRUCTDT` members (`DATAOBJ`/`STRUCT`/`GAPDATAOBJ`) and `NUMITERCOMP` proxies were modelled as single
  values, so every member after the first was silently dropped.
- `EXTSTORAGEITEMS` allowed only one attachment, which made the default parser fail.
- Multi-byte constants stored as byte tuples (`v='(0,0,96,79,25)'`) crashed `encode_default()`.

The converter parses leniently (CDD metadata such as `EMAIL`/`ATTACHPROXY` is not modelled) and prints a
`DATA LOSS` warning if any repeated XML element would still be dropped by the cdd_utils model.

## Mapping

| CDD | ODX |
|---|---|
| ECU / DIAGCLASS | BASE-VARIANT / FUNCT-CLASS |
| instantiated DIAGINST services (`is_used`) | DIAG-SERVICE + REQUEST / POS-RESPONSE / NEG-RESPONSE |
| top-level constants (SID, subfunction, DID) | CODED-CONST parameters |
| TEXTTBL / LINCOMP (`raw*f/div+o`) / IDENT | DOP with TEXTTABLE / LINEAR / IDENTICAL compu method |
| STRUCTDT, DID records | STRUCTURE |
| variable-length fields, repeated records | MIN-MAX-LENGTH-TYPE / END-OF-PDU-FIELD |
| gap / zero reserved bits | RESERVED parameters |

## Known gaps

- **Bit-field order is not verified against CANdela.** cdd_utils packs sub-byte fields MSB-first and the
  default `--bitfield-order msb` matches it. Check one bit-field DID against a CANdela/CANoe trace; if the
  bits come out mirrored, use `--bitfield-order lsb`.
- No communication parameters (CAN IDs, ISO-TP timing): the CDD carries none. Add them in the ODX tool of
  your choice or extend `Converter.convert()`.
- Session/security state charts, units, and EXCL ranges are not exported.
- MUXCOMP / NUMITERCOMP are skipped with a warning (in the BCM file they are DTC snapshot and extended-data
  records whose `envData` cdd_utils cannot resolve).
