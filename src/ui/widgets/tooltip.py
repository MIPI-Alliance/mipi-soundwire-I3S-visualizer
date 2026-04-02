"""
Tooltip widget for SWI3S Visualizer.

Provides a simple tooltip that follows the mouse cursor and supports
rounded corners with proper macOS transparency.
"""

import tkinter as tk
import customtkinter as ctk


class SimpleToolTip:
    """Simple tooltip using Toplevel for proper macOS transparency."""

    def __init__(self, widget, message, delay=0.2, corner_radius=12,
                 bg_color="#4a4a4a", alpha=0.95, x_offset=20, y_offset=10):
        """Initialize the tooltip.

        Args:
            widget: The widget to attach the tooltip to
            message: The tooltip text to display
            delay: Delay in seconds before showing tooltip
            corner_radius: Radius for rounded corners
            bg_color: Background color of the tooltip
            alpha: Transparency level (0.0 to 1.0)
            x_offset: Horizontal offset from cursor
            y_offset: Vertical offset from cursor
        """
        self.widget = widget
        self.message = message
        self.delay = delay
        self.corner_radius = corner_radius
        self.bg_color = bg_color
        self.alpha = alpha
        self.x_offset = x_offset
        self.y_offset = y_offset

        self.tooltip_window = None
        self.scheduled_id = None
        self.last_x = 0
        self.last_y = 0

        widget.bind("<Enter>", self._on_enter, add="+")
        widget.bind("<Leave>", self._on_leave, add="+")
        widget.bind("<Motion>", self._on_motion, add="+")

    def _on_enter(self, event):
        self.last_x = event.x_root
        self.last_y = event.y_root
        self._schedule_show()

    def _on_motion(self, event):
        self.last_x = event.x_root
        self.last_y = event.y_root
        if self.tooltip_window:
            self._position_tooltip()

    def _on_leave(self, event):
        self._cancel_scheduled()
        self._hide()

    def _schedule_show(self):
        self._cancel_scheduled()
        self.scheduled_id = self.widget.after(int(self.delay * 1000), self._show)

    def _cancel_scheduled(self):
        if self.scheduled_id:
            self.widget.after_cancel(self.scheduled_id)
            self.scheduled_id = None

    def _position_tooltip(self):
        if self.tooltip_window:
            self.tooltip_window.geometry(f"+{self.last_x + self.x_offset}+{self.last_y + self.y_offset}")

    def _show(self):
        if self.tooltip_window or not self.widget.winfo_exists():
            return

        self.tooltip_window = tk.Toplevel(self.widget)
        self.tooltip_window.withdraw()
        self.tooltip_window.overrideredirect(True)
        self.tooltip_window.attributes("-topmost", True)
        self.tooltip_window.attributes("-alpha", self.alpha)

        # Get the app's background color for corner blending (cross-platform approach)
        # This makes corners appear transparent by matching the app background
        app_bg = "#2b2b2b"  # CTk dark mode gray17
        try:
            # Try to get actual appearance mode
            mode = ctk.get_appearance_mode()
            if mode == "Light":
                app_bg = "#dbdbdb"  # CTk light mode gray86
        except Exception:
            pass

        self.tooltip_window.config(bg=app_bg)

        # Measure text size
        temp_label = tk.Label(self.tooltip_window, text=self.message, font=("SF Pro", 13))
        temp_label.update_idletasks()
        text_width = temp_label.winfo_reqwidth()
        text_height = temp_label.winfo_reqheight()
        temp_label.destroy()

        padding_x = 12
        padding_y = 6
        width = text_width + padding_x * 2
        height = text_height + padding_y * 2

        # Create canvas with app background for corner blending
        canvas = tk.Canvas(
            self.tooltip_window,
            width=width,
            height=height,
            highlightthickness=0,
            bg=app_bg
        )
        canvas.pack()

        # Draw rounded rectangle
        self._draw_rounded_rect(canvas, 0, 0, width, height, self.corner_radius, self.bg_color)

        # Draw text
        canvas.create_text(
            width // 2, height // 2,
            text=self.message,
            fill="white",
            font=("SF Pro", 13)
        )

        self._position_tooltip()
        self.tooltip_window.deiconify()

    def _draw_rounded_rect(self, canvas, x1, y1, x2, y2, radius, fill):
        """Draw a rounded rectangle on the canvas using arcs and lines."""
        r = min(radius, (x2 - x1) // 2, (y2 - y1) // 2)  # Ensure radius isn't too large

        # Draw using create_arc for corners and create_rectangle/polygon for fill
        # This creates a proper rounded rectangle
        canvas.create_arc(x1, y1, x1 + 2*r, y1 + 2*r, start=90, extent=90, fill=fill, outline=fill)
        canvas.create_arc(x2 - 2*r, y1, x2, y1 + 2*r, start=0, extent=90, fill=fill, outline=fill)
        canvas.create_arc(x1, y2 - 2*r, x1 + 2*r, y2, start=180, extent=90, fill=fill, outline=fill)
        canvas.create_arc(x2 - 2*r, y2 - 2*r, x2, y2, start=270, extent=90, fill=fill, outline=fill)

        # Fill the center and edges
        canvas.create_rectangle(x1 + r, y1, x2 - r, y2, fill=fill, outline=fill)
        canvas.create_rectangle(x1, y1 + r, x1 + r, y2 - r, fill=fill, outline=fill)
        canvas.create_rectangle(x2 - r, y1 + r, x2, y2 - r, fill=fill, outline=fill)

    def _hide(self):
        if self.tooltip_window:
            self.tooltip_window.destroy()
            self.tooltip_window = None
