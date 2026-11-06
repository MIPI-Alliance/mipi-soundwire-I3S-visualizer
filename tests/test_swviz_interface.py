"""The visualizer's Interface model (swviz.models.interface): the validated fields a loaded
config is written into, port-to-device assignment, and the PHY3 Row layout.

* An integer or float field outside its range raises ValueError naming the field, a value
  of the wrong type raises TypeError, and the field keeps its previous value either way.
  A boolean field takes a bool, an int, or "true"/"false"/"1"/"0" in any case.
* A row rate that is NaN, infinite or too large for a float raises ValueError.
* The Interface's row-rate limits are the config CSV reader's: a saved file at either limit
  loads without a RowRate warning, and one just outside is reported.
* A port moved to another device leaves its old one, a device is created on first use, and
  the port's `_device` is the device it was moved to.
* In PHY3 mode S1 is Column 0, then S1's tail, then its handover, then the CDS.
"""
import pytest

from swi3s_studio.swviz.config.constants import SpecialDevices
from swi3s_studio.swviz.models.interface import Interface


@pytest.mark.parametrize("field,bad", [
    ("NumColumns_REG", Interface.MAX_COLUMNS + 1),
    ("NumColumns_REG", Interface.MIN_COLUMNS - 1),
    ("row_rate", Interface.MAX_ROW_RATE * 2),
    ("row_rate", 0.0),
    ("SkippingDenominator_REG", Interface.MAX_SKIPPING_DENOMINATOR + 1),
])
def test_a_value_out_of_range_is_refused_and_the_field_keeps_its_value(field, bad):
    iface = Interface()
    before = getattr(iface, field)
    with pytest.raises(ValueError):
        setattr(iface, field, bad)
    assert getattr(iface, field) == before


@pytest.mark.parametrize("field,bad", [
    ("NumColumns_REG", "15"), ("NumColumns_REG", 7.5), ("row_rate", "3072"),
    ("phy3_enabled", "maybe"), ("phy3_enabled", 1.0),
    ("NumColumns_REG", True), ("row_rate", True),
])
def test_a_value_of_the_wrong_type_is_refused(field, bad):
    iface = Interface()
    before = getattr(iface, field)
    with pytest.raises(TypeError):
        setattr(iface, field, bad)
    assert getattr(iface, field) == before


@pytest.mark.parametrize("value,expect", [
    (True, True), (0, False), (2, True), ("TRUE", True), ("false", False), ("1", True),
    ("0", False),
])
def test_a_boolean_field_reads_bools_ints_and_their_spellings(value, expect):
    iface = Interface()
    iface.phy3_enabled = value
    assert iface.phy3_enabled is expect


def test_a_row_rate_given_as_an_int_is_stored_as_a_float():
    iface = Interface()
    iface.row_rate = 6144
    assert iface.row_rate == 6144.0 and isinstance(iface.row_rate, float)


def test_the_row_rate_limits_are_the_config_csv_readers(tmp_path):
    import swi3score

    from swi3s_studio.swviz.io.csv_handler import CSVHandler
    from swi3s_studio.swviz.viz import VizConfig

    def warnings_for(rate):
        path = tmp_path / f"rate_{rate}.csv"
        CSVHandler.save_csv(str(path), Interface(), VizConfig())
        lines = path.read_text(encoding="utf-8").splitlines()
        lines = [f"RowRate,{rate}" if ln.startswith("RowRate,") else ln for ln in lines]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return [w for w in swi3score.csv_warnings(str(path)) if "RowRate" in w]

    low, high = Interface.MIN_ROW_RATE, Interface.MAX_ROW_RATE
    assert warnings_for(low) == [] and warnings_for(high) == []
    assert warnings_for(low / 2) and warnings_for(high * 1.001)
    assert all(f"1..{high:g} kHz" in w for w in warnings_for(high * 1.001))


def test_moving_a_port_to_another_device_moves_it_out_of_the_old_one():
    iface = Interface()
    old = iface.get_dp_device(3)
    iface.set_dp_device(3, 7)
    assert iface.get_dp_device(3) == 7
    assert 3 not in [dp.dp_index for dp in iface.devices[old].data_ports]
    iface.set_dp_device(3, SpecialDevices.MANAGER)
    assert iface.is_dp_in_manager(3)
    iface.set_dp_device(3, SpecialDevices.MANAGER)          # no change
    assert iface.get_dp_device(3) == SpecialDevices.MANAGER


def test_an_unknown_port_index_is_refused():
    iface = Interface()
    with pytest.raises(ValueError):
        iface.get_fcp(Interface.NUM_DATA_PORTS)
    with pytest.raises(ValueError):
        iface.get_dp_device(Interface.NUM_DATA_PORTS)
    with pytest.raises(ValueError):
        iface.set_dp_device(-1, 0)


@pytest.mark.parametrize("tail,handover,start", [(0, True, 2), (2, True, 4), (1, False, 2)])
def test_the_phy3_cds_follows_s1_its_tail_and_its_handover(tail, handover, start):
    iface = Interface()
    assert iface.CDS_HorizontalStart_REG == 0                 # PHY3 off: CDS at Column 0
    iface.phy3_enabled = True
    iface.tail_width = tail
    iface.s1_handover_enabled = handover
    assert iface.CDS_HorizontalStart_REG == start


def test_a_moved_ports_device_is_the_target_device_object():
    """After set_dp_device the port's `_device` is the target device object."""
    iface = Interface()
    iface.set_dp_device(0, SpecialDevices.MANAGER)
    dp = iface.data_ports[0]
    assert dp._device is iface.devices[SpecialDevices.MANAGER]
    assert dp._device.device_num == iface.get_dp_device(0)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_row_rate_is_refused(bad):
    """NaN and both infinities raise ValueError naming the finite requirement."""
    with pytest.raises(ValueError, match="finite"):
        Interface().row_rate = bad


def test_an_integer_too_large_for_a_float_is_a_value_error():
    """An int beyond float range raises ValueError and the row rate keeps its value."""
    iface = Interface()
    before = iface.row_rate
    with pytest.raises(ValueError, match="finite"):
        iface.row_rate = 10 ** 400
    assert iface.row_rate == before


def test_the_viz_config_holds_one_slot_per_interface_port():
    from swi3s_studio.swviz.viz import dataport_viz
    assert dataport_viz.NUM_DATA_PORTS == Interface.NUM_DATA_PORTS
    assert len(dataport_viz.VizConfig().data_ports) == len(Interface().data_ports)
