"""Notifications Panel widget for displaying persistent warnings and errors.

This module provides a scrollable panel that shows validation errors,
bus clashes, device clashes, TxP/DRQ mismatches, and read overlaps
in a persistent manner rather than using modal dialogs.
"""

from __future__ import annotations

import tkinter as tk
from typing import TYPE_CHECKING, Optional, List, Tuple, Any

import customtkinter as ctk

from src.ui.constants import APP_FONT, TEXT_SIZE
from src.config import SpecialDevices
from src.drawing.clash_detector import SlotClashCategory

if TYPE_CHECKING:
    from src.models.bus_model import BusModel
    from src.drawing.clash_detector import ClashInfo
    from src.utils.validators import ValidationResult


# =============================================================================
# Error Panel Constants
# =============================================================================

ERROR_PANEL_WIDTH = 250      # Width of the error panel
ERROR_PANEL_MIN_HEIGHT = 100  # Minimum height
ERROR_SECTION_PADX = 5       # Horizontal padding inside sections
ERROR_SECTION_PADY = 2       # Vertical padding inside sections

# Error type colors
COLORS = {
    'bus_clash': '#D32F2F',       # Red - critical
    'validation': '#F57C00',      # Orange - warning (includes truncation)
    'device_clash': '#FBC02D',    # Yellow - warning
    'flow_control': '#7B1FA2',    # Purple - TxP/DRQ issues
    'scrambler': '#00897B',       # Teal - scrambler mismatches
    'test_mode': '#E91E63',       # Pink - test mode mismatches
    'sample_bit': '#00BCD4',      # Cyan - sample/bit mismatches
    'informational': '#1976D2',   # Blue - informational (sink handover, read overlaps, no channels)
    'success': '#388E3C',         # Green - no errors
}


def _device_name(device: int) -> str:
    """Convert device number to display name."""
    if device == SpecialDevices.MANAGER:
        return "Manager"
    elif device == SpecialDevices.UNIVERSAL:
        return "CDS"
    elif device == SpecialDevices.VISUALIZER:
        return "Viz"
    else:
        return f"Device {device}"


class CollapsibleSection(ctk.CTkFrame):
    """A collapsible section with header and content area."""

    def __init__(
        self,
        master: Any,  # CTkFrame or CTkScrollableFrame
        title: str,
        color: str,
        **kwargs: Any
    ):
        super().__init__(master, **kwargs)
        self.title = title
        self.header_color = color
        self.expanded = False  # Start collapsed
        self.item_count = 0

        self._create_widgets()

    def _create_widgets(self) -> None:
        """Create header and content widgets."""
        # Header frame (clickable)
        self.header_frame = ctk.CTkFrame(self, fg_color=self.header_color, corner_radius=4)
        self.header_frame.pack(fill=tk.X, padx=2, pady=(2, 0))
        self.header_frame.bind("<Button-1>", self._toggle)

        # Header label (centered)
        self.header_label = ctk.CTkLabel(
            self.header_frame,
            text=f"▶ {self.title} (0)",  # Start with collapsed arrow
            font=(APP_FONT, TEXT_SIZE + 2, 'bold'),
            text_color='white',
            anchor='center'
        )
        self.header_label.pack(fill=tk.X, padx=ERROR_SECTION_PADX, pady=ERROR_SECTION_PADY)
        self.header_label.bind("<Button-1>", self._toggle)

        # Content frame (don't pack initially - start collapsed)
        self.content_frame = ctk.CTkFrame(self, fg_color='transparent')

    def _toggle(self, event: Any = None) -> None:
        """Toggle expanded/collapsed state."""
        self.expanded = not self.expanded
        if self.expanded:
            self.content_frame.pack(fill=tk.X, padx=4, pady=(0, 2))
            self._update_header_text("▼")
        else:
            self.content_frame.pack_forget()
            self._update_header_text("▶")

    def _update_header_text(self, arrow: str) -> None:
        """Update header with arrow and count."""
        self.header_label.configure(text=f"{arrow} {self.title} ({self.item_count})")

    def clear(self) -> None:
        """Clear all items from content."""
        for widget in self.content_frame.winfo_children():
            widget.destroy()
        self.item_count = 0
        self._update_header_text("▼" if self.expanded else "▶")

    def add_item(self, text: str, increment_count: bool = True) -> None:
        """Add an item to the section.

        Args:
            text: The text to display
            increment_count: If True, increment the header count. Set False for
                           summary items like "... and X more" that shouldn't
                           be counted as separate issues.
        """
        # Use CTkTextbox for selectable text (read-only)
        # Estimate line count based on text length and available width
        chars_per_line = max(1, (ERROR_PANEL_WIDTH - 30) // 7)  # ~7 pixels per char
        num_lines = max(1, (len(text) + chars_per_line - 1) // chars_per_line)
        height = num_lines * 22 + 0  # ~18 pixels per line + minimal padding

        textbox = ctk.CTkTextbox(
            self.content_frame,
            font=(APP_FONT, TEXT_SIZE + 1),
            width=ERROR_PANEL_WIDTH - 30,
            height=height,
            wrap='word',
            fg_color='transparent',
            border_width=0,
            activate_scrollbars=False
        )
        textbox.insert('1.0', text)
        textbox.configure(state='disabled')  # Read-only but selectable
        textbox.pack(fill=tk.X, padx=ERROR_SECTION_PADX, pady=0)
        if increment_count:
            self.item_count += 1
            self._update_header_text("▼" if self.expanded else "▶")

    def add_to_count(self, amount: int) -> None:
        """Add to the item count without adding visible items.

        Use this to reflect the true count when only showing a subset of items.
        For example, if showing 10 of 32 items, call add_to_count(22) after
        adding the 10 visible items.
        """
        self.item_count += amount
        self._update_header_text("▼" if self.expanded else "▶")

    def add_note(self, text: str) -> None:
        """Add a note (description) to the section with italic styling."""
        # Use CTkTextbox for selectable text (read-only)
        # Estimate line count based on text length and available width
        chars_per_line = max(1, (ERROR_PANEL_WIDTH - 30) // 7)  # ~7 pixels per char
        num_lines = max(1, (len(text) + chars_per_line - 1) // chars_per_line)
        height = num_lines * 22 + 0  # ~16 pixels per line (smaller font) + minimal padding

        textbox = ctk.CTkTextbox(
            self.content_frame,
            font=(APP_FONT, TEXT_SIZE, 'italic'),
            width=ERROR_PANEL_WIDTH - 30,
            height=height,
            wrap='word',
            fg_color='transparent',
            border_width=0,
            text_color='gray',
            activate_scrollbars=False
        )
        textbox.insert('1.0', text)
        textbox.configure(state='disabled')  # Read-only but selectable
        textbox.pack(fill=tk.X, padx=ERROR_SECTION_PADX, pady=(0, 2))

    def set_visible(self, visible: bool) -> None:
        """Show or hide the entire section."""
        if visible:
            self.pack(fill=tk.X, pady=2)
        else:
            self.pack_forget()


class ErrorPanel(ctk.CTkFrame):
    """Persistent notifications panel showing validation warnings and clash information.

    Displays:
    - Bus clashes (red) - physical bus collisions with device/DP info
    - Validation errors (orange) - configuration issues
    - Device clashes (yellow) - same-device conflicts
    - TxP/DRQ mismatches (purple) - flow control issues
    - Read overlaps (blue) - visualizer-only warnings
    """

    def __init__(self, master: Any, **kwargs: Any):
        super().__init__(master, width=ERROR_PANEL_WIDTH, fg_color='transparent', **kwargs)

        self._create_widgets()
        self._show_no_errors()
        self.pack(side=tk.TOP, expand=tk.NO, fill=tk.X, pady=(0, 5))

    def _create_widgets(self) -> None:
        """Create the panel widgets."""
        # Notifications label (above the bordered section)
        self.title_label = ctk.CTkLabel(
            self,
            text="Notifications",
            font=(APP_FONT, TEXT_SIZE + 6),  # Match Description panel title style
            anchor='center'
        )
        self.title_label.pack(fill=tk.X, pady=(0, 2))

        # Notifications section with gray border (narrower with padding)
        self.notifications_frame = ctk.CTkFrame(
            self,
            border_width=1,
            border_color='#979DA2',  # Match entry box border color
            corner_radius=6,
            fg_color='transparent'
        )
        self.notifications_frame.pack(fill=tk.X, expand=False, pady=(0, 5), padx=(28, 40))  # 25% narrower, reduced left gap

        # Scrollable container for sections (height set to align with Skipping Numerator row)
        self.scroll_frame = ctk.CTkScrollableFrame(
            self.notifications_frame,
            width=ERROR_PANEL_WIDTH - 20,
            height=370,  # Height increased 33% from 278
            fg_color='transparent'
        )
        self.scroll_frame.pack(fill=tk.BOTH, expand=True, padx=2, pady=2)

        # Create sections for each error type
        self.bus_clash_section = CollapsibleSection(
            self.scroll_frame,
            "Bus Clashes",
            COLORS['bus_clash']
        )

        self.validation_section = CollapsibleSection(
            self.scroll_frame,
            "Validation",
            COLORS['validation']
        )

        self.device_clash_section = CollapsibleSection(
            self.scroll_frame,
            "Device Clashes",
            COLORS['device_clash']
        )

        self.flow_control_section = CollapsibleSection(
            self.scroll_frame,
            "Flow Control",
            COLORS['flow_control']
        )

        self.scrambler_section = CollapsibleSection(
            self.scroll_frame,
            "Scrambler",
            COLORS['scrambler']
        )

        self.test_mode_section = CollapsibleSection(
            self.scroll_frame,
            "Test Mode",
            COLORS['test_mode']
        )

        self.sample_bit_section = CollapsibleSection(
            self.scroll_frame,
            "Sample/Bit Mismatch",
            COLORS['sample_bit']
        )

        self.informational_section = CollapsibleSection(
            self.scroll_frame,
            "Informational",
            COLORS['informational']
        )

        # Success message (shown when no errors)
        self.success_label = ctk.CTkLabel(
            self.scroll_frame,
            text="No issues detected",
            font=(APP_FONT, TEXT_SIZE + 2),
            text_color=COLORS['success'],
            anchor='center'
        )

    def _show_no_errors(self) -> None:
        """Show the 'no errors' success message."""
        self.bus_clash_section.set_visible(False)
        self.validation_section.set_visible(False)
        self.device_clash_section.set_visible(False)
        self.flow_control_section.set_visible(False)
        self.scrambler_section.set_visible(False)
        self.test_mode_section.set_visible(False)
        self.sample_bit_section.set_visible(False)
        self.informational_section.set_visible(False)
        self.success_label.pack(fill=tk.X, pady=20)

    def _hide_no_errors(self) -> None:
        """Hide the success message."""
        self.success_label.pack_forget()

    def _format_clash_detail(self, clash: 'ClashInfo') -> str:
        """Format a clash detail for display.

        Args:
            clash: ClashInfo object with full clash details

        Returns:
            Formatted string like "Row 0 Col 5: Device 0 vs. Device 1"
        """
        dev_a = _device_name(clash.device_a)
        dev_b = _device_name(clash.device_b)
        return f"Row {clash.row} Col {clash.column}: {dev_a} vs. {dev_b}"

    def _port_mode_name(self, mode: int) -> str:
        """Convert port mode number to display name.

        Args:
            mode: Port mode (0=Normal, 1=Reserved, 2=Test Ones, 3=Test Zeros)

        Returns:
            Human-readable mode name
        """
        mode_names = {
            0: "Normal",
            1: "Reserved",
            2: "Test 1s",
            3: "Test 0s",
        }
        return mode_names.get(mode, f"Mode {mode}")

    def refresh_errors(self, bus_model: Optional['BusModel']) -> None:
        """Update the panel with errors from the bus model.

        Args:
            bus_model: The current bus model with clash/validation info
        """
        # Clear all sections
        self.bus_clash_section.clear()
        self.validation_section.clear()
        self.device_clash_section.clear()
        self.flow_control_section.clear()
        self.scrambler_section.clear()
        self.test_mode_section.clear()
        self.sample_bit_section.clear()
        self.informational_section.clear()

        if bus_model is None:
            self._show_no_errors()
            return

        has_errors = False

        # Add bus clashes with device details (WRITE clashes only)
        if bus_model.clash_details:
            # Filter for different-device WRITE clashes (physical bus collisions)
            bus_clashes = [c for c in bus_model.clash_details
                         if c.device_a != c.device_b and c.category == SlotClashCategory.WRITE_CLASH]
            if bus_clashes:
                has_errors = True
                self._hide_no_errors()
                self.bus_clash_section.set_visible(True)
                for clash in bus_clashes[:100]:  # Limit display
                    self.bus_clash_section.add_item(self._format_clash_detail(clash))
                if len(bus_clashes) > 100:
                    self.bus_clash_section.add_item(f"... and {len(bus_clashes) - 100} more", increment_count=False)
                    self.bus_clash_section.add_to_count(len(bus_clashes) - 100)
            else:
                self.bus_clash_section.set_visible(False)

            # Filter for same-device WRITE clashes (internal conflicts)
            device_clashes = [c for c in bus_model.clash_details
                            if c.device_a == c.device_b and c.category == SlotClashCategory.WRITE_CLASH]
            if device_clashes:
                has_errors = True
                self._hide_no_errors()
                self.device_clash_section.set_visible(True)
                self.device_clash_section.add_note(
                    "Note: Clashes internal to a device are not clashes on the bus "
                    "but do likely indicate an error in device data port settings."
                )
                for clash in device_clashes[:100]:
                    dev = _device_name(clash.device_a)
                    self.device_clash_section.add_item(
                        f"Row {clash.row} Col {clash.column}: {dev} internal"
                    )
                if len(device_clashes) > 100:
                    self.device_clash_section.add_item(f"... and {len(device_clashes) - 100} more", increment_count=False)
                    self.device_clash_section.add_to_count(len(device_clashes) - 100)
            else:
                self.device_clash_section.set_visible(False)
        else:
            self.bus_clash_section.set_visible(False)
            self.device_clash_section.set_visible(False)

        # Add validation errors
        if bus_model.validation_issues:
            has_errors = True
            self._hide_no_errors()
            self.validation_section.set_visible(True)
            for name, result in bus_model.validation_issues:
                # ValidationResult always has .errors attribute
                for error in result.errors:
                    self.validation_section.add_item(f"{name}: {error.message}")
        else:
            self.validation_section.set_visible(False)

        # Add TxP/DRQ mismatches
        flow_issues = []
        flow_hidden = 0
        if bus_model.txp_mismatches:
            for bit_index in bus_model.txp_mismatches[:100]:
                row, col = bus_model.position(bit_index)
                flow_issues.append(f"TxP Source Row {row} Col {col}, No Sink TxP")
            if len(bus_model.txp_mismatches) > 100:
                flow_hidden += len(bus_model.txp_mismatches) - 100
        if bus_model.txp_orphan_sinks:
            for bit_index in bus_model.txp_orphan_sinks[:100]:
                row, col = bus_model.position(bit_index)
                flow_issues.append(f"TxP Sink Row {row} Col {col}, No Source TxP")
            if len(bus_model.txp_orphan_sinks) > 100:
                flow_hidden += len(bus_model.txp_orphan_sinks) - 100
        if bus_model.drq_mismatches:
            for bit_index in bus_model.drq_mismatches[:100]:
                row, col = bus_model.position(bit_index)
                flow_issues.append(f"DRQ Source Row {row} Col {col}, No Sink DRQ")
            if len(bus_model.drq_mismatches) > 100:
                flow_hidden += len(bus_model.drq_mismatches) - 100
        if bus_model.drq_orphan_sinks:
            for bit_index in bus_model.drq_orphan_sinks[:100]:
                row, col = bus_model.position(bit_index)
                flow_issues.append(f"DRQ Sink Row {row} Col {col}, No Source DRQ")
            if len(bus_model.drq_orphan_sinks) > 100:
                flow_hidden += len(bus_model.drq_orphan_sinks) - 100

        if flow_issues:
            has_errors = True
            self._hide_no_errors()
            self.flow_control_section.set_visible(True)
            for issue in flow_issues:
                self.flow_control_section.add_item(issue)
            if flow_hidden > 0:
                self.flow_control_section.add_item(f"... and {flow_hidden} more", increment_count=False)
                self.flow_control_section.add_to_count(flow_hidden)
        else:
            self.flow_control_section.set_visible(False)

        # Add scrambler mismatches
        if bus_model.scrambler_mismatches:
            has_errors = True
            self._hide_no_errors()
            self.scrambler_section.set_visible(True)
            self.scrambler_section.add_note(
                "Note: Source and sink have different scrambler settings at the same bit position."
            )
            for bit_index, source_dp, sink_dp in bus_model.scrambler_mismatches[:100]:
                row, col = bus_model.position(bit_index)
                self.scrambler_section.add_item(f"Row {row} Col {col}: DP{source_dp} → DP{sink_dp}")
            if len(bus_model.scrambler_mismatches) > 100:
                self.scrambler_section.add_item(f"... and {len(bus_model.scrambler_mismatches) - 100} more", increment_count=False)
                self.scrambler_section.add_to_count(len(bus_model.scrambler_mismatches) - 100)
        else:
            self.scrambler_section.set_visible(False)

        # Add test mode mismatches
        if bus_model.test_mode_mismatches:
            has_errors = True
            self._hide_no_errors()
            self.test_mode_section.set_visible(True)
            self.test_mode_section.add_note(
                "Note: Data ports at the same position have different port test modes."
            )
            for mismatch in bus_model.test_mode_mismatches[:100]:
                bit_index, (dp1, mode1), (dp2, mode2) = mismatch
                row, col = bus_model.position(bit_index)
                mode1_str = self._port_mode_name(mode1)
                mode2_str = self._port_mode_name(mode2)
                self.test_mode_section.add_item(
                    f"Row {row} Col {col}: DP{dp1} ({mode1_str}) vs DP{dp2} ({mode2_str})"
                )
            if len(bus_model.test_mode_mismatches) > 100:
                self.test_mode_section.add_item(f"... and {len(bus_model.test_mode_mismatches) - 100} more", increment_count=False)
                self.test_mode_section.add_to_count(len(bus_model.test_mode_mismatches) - 100)
        else:
            self.test_mode_section.set_visible(False)

        # Add truncation warnings (interval overflow) to validation section
        if bus_model.interval_overflow_warnings:
            has_errors = True
            self._hide_no_errors()
            self.validation_section.set_visible(True)
            for dp_name, bits_needed, bits_available in bus_model.interval_overflow_warnings[:100]:
                self.validation_section.add_item(
                    f"{dp_name}: Interval overflow, needed {bits_needed} bits, only {bits_available} fit"
                )
            if len(bus_model.interval_overflow_warnings) > 100:
                self.validation_section.add_item(f"... and {len(bus_model.interval_overflow_warnings) - 100} more", increment_count=False)
                self.validation_section.add_to_count(len(bus_model.interval_overflow_warnings) - 100)

        # Add sample/bit mismatches
        if bus_model.sample_bit_mismatches:
            has_errors = True
            self._hide_no_errors()
            self.sample_bit_section.set_visible(True)
            self.sample_bit_section.add_note(
                "Note: Source and sink at same position have different sample or bit numbers."
            )
            for mismatch in bus_model.sample_bit_mismatches[:100]:
                bit_index, src_dp, src_sample, src_bit, sink_dp, sink_sample, sink_bit = mismatch
                row, col = bus_model.position(bit_index)
                self.sample_bit_section.add_item(
                    f"Row {row} Col {col}: DP{src_dp}(s{src_sample}b{src_bit}) vs DP{sink_dp}(s{sink_sample}b{sink_bit})"
                )
            if len(bus_model.sample_bit_mismatches) > 100:
                self.sample_bit_section.add_item(f"... and {len(bus_model.sample_bit_mismatches) - 100} more", increment_count=False)
                self.sample_bit_section.add_to_count(len(bus_model.sample_bit_mismatches) - 100)
        else:
            self.sample_bit_section.set_visible(False)

        # Add informational items (sink handover, no channels, read overlaps, display truncation)
        has_informational = False

        # Display truncation warnings (not errors - user can increase RowsToDraw)
        if bus_model.display_truncation_warnings:
            has_informational = True
            for dp_name, interval_rows, displayed_rows in bus_model.display_truncation_warnings[:100]:
                self.informational_section.add_item(
                    f"{dp_name}: Interval is {interval_rows} rows, only {displayed_rows} displayed"
                )
            if len(bus_model.display_truncation_warnings) > 100:
                self.informational_section.add_item(f"... and {len(bus_model.display_truncation_warnings) - 100} more", increment_count=False)
                self.informational_section.add_to_count(len(bus_model.display_truncation_warnings) - 100)

        # Sink handover warnings
        if bus_model.sink_handover_warnings:
            has_informational = True
            for dp_name, dp_number in bus_model.sink_handover_warnings[:100]:
                self.informational_section.add_item(f"{dp_name}: Sink data ports don't need handovers")
            if len(bus_model.sink_handover_warnings) > 100:
                self.informational_section.add_item(f"... and {len(bus_model.sink_handover_warnings) - 100} more", increment_count=False)
                self.informational_section.add_to_count(len(bus_model.sink_handover_warnings) - 100)

        # Enabled but no channels warnings
        if bus_model.enabled_no_channels_warnings:
            has_informational = True
            for dp_name, dp_number in bus_model.enabled_no_channels_warnings[:100]:
                self.informational_section.add_item(f"{dp_name}: No channel enabled")
            if len(bus_model.enabled_no_channels_warnings) > 100:
                self.informational_section.add_item(f"... and {len(bus_model.enabled_no_channels_warnings) - 100} more", increment_count=False)
                self.informational_section.add_to_count(len(bus_model.enabled_no_channels_warnings) - 100)

        # Read overlaps
        if bus_model.read_overlaps:
            has_informational = True
            for bit_index in bus_model.read_overlaps[:100]:
                row, col = bus_model.position(bit_index)
                self.informational_section.add_item(f"Read overlap: Row {row} Col {col}")
            if len(bus_model.read_overlaps) > 100:
                self.informational_section.add_item(f"... and {len(bus_model.read_overlaps) - 100} more", increment_count=False)
                self.informational_section.add_to_count(len(bus_model.read_overlaps) - 100)

        if has_informational:
            has_errors = True
            self._hide_no_errors()
            self.informational_section.set_visible(True)
        else:
            self.informational_section.set_visible(False)

        # Show success if no errors
        if not has_errors:
            self._show_no_errors()

    def update_theme_colors(self, mode: str) -> None:
        """Update colors when theme changes.

        Args:
            mode: Appearance mode ('Light' or 'Dark')
        """
        # The section colors are fixed (not theme-dependent)
        # But we could adjust text colors if needed
        pass

    def clear(self) -> None:
        """Clear all error displays."""
        self.bus_clash_section.clear()
        self.validation_section.clear()
        self.device_clash_section.clear()
        self.flow_control_section.clear()
        self.scrambler_section.clear()
        self.test_mode_section.clear()
        self.sample_bit_section.clear()
        self.informational_section.clear()
        self._show_no_errors()
