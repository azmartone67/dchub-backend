"""gridstatus.io is budgeted for PJM only (owner, 2026-09-24).

ISOService.get_fuel_mix asked gridstatus first for EVERY ISO, so the paid
/api/grid/fuel-mix and /api/grid/all-isos routes spent the 200/mo budget that
keeps PJM-DOM live on ERCOT/CAISO/MISO/SPP/NYISO/ISONE. Behavioural: the
gridstatus client is replaced with a recorder, no network.
"""
from enhancements.iso_integrations import ISOService


class _Recorder:
    def __init__(self):
        self.isos = []

    def get_fuel_mix(self, iso):
        self.isos.append(iso)
        return {"error": "stub"}


class _NoNet:
    def get_fuel_mix(self):
        return {"error": "stub"}


def _svc():
    s = ISOService()
    s.gridstatus = _Recorder()
    s.ercot = _NoNet()
    s.pjm = _NoNet()
    return s


def test_all_isos_spends_gridstatus_on_pjm_only():
    s = _svc()
    out = s.get_all_isos()
    assert s.gridstatus.isos == ["PJM"]
    assert set(out) == set(ISOService.SUPPORTED_ISOS)   # every ISO still answered


def test_non_pjm_iso_never_reaches_gridstatus():
    for iso in ("ERCOT", "caiso", "MISO", "SPP", "NYISO", "ISONE"):
        s = _svc()
        s.get_fuel_mix(iso)
        assert s.gridstatus.isos == [], iso
